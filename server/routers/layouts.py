"""Admin CRUD for Phase 2 multi-zone Layouts.

Surface
=======

  * ``GET    /api/layouts``                — list layouts (admin).
  * ``POST   /api/layouts``                — create a Layout with an
                                             optional initial Zone list.
  * ``GET    /api/layouts/{id}``           — fetch a Layout with its
                                             zones + zone items nested.
  * ``PATCH  /api/layouts/{id}``           — metadata-only patch, or
                                             full zone-list replacement
                                             when ``zones`` is provided.
  * ``DELETE /api/layouts/{id}``           — rejects with ``409`` if the
                                             Layout is still referenced
                                             by at least one
                                             ``ScheduleItem``; callers
                                             must detach first.

Zone mutations go through the parent Layout's ``PATCH`` so the CMS can
stay transactional (swap the whole Zone set atomically). Adding a
per-Zone endpoint later is a layer the current design can grow into
without breaking this one.

The endpoints are intentionally admin-only; the player never needs to
see Layouts through their own REST surface. The manifest endpoint
(``GET /api/schedule/{device_id}``) will render the Zone tree inline
for the device in Step 4 of this sprint — that change does NOT land
in this PR.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlmodel import Session, select

from ..database import get_session
from ..models import Layout, Media, ScheduleItem, Zone, ZoneItem
from ..schemas import (
    LayoutCreate,
    LayoutRead,
    LayoutUpdate,
    ZoneIn,
)
from ..security import require_admin

router = APIRouter(prefix="/api/layouts", tags=["layouts"])


def _validate_zone(zone: ZoneIn, layout: Layout) -> None:
    """Reject Zones whose bounding box escapes the Layout's canvas.

    This is a sanity check, not a security control — the player clamps
    to screen size anyway. Catching bad values here gives the CMS a
    clean 400 with an actionable message instead of a visually broken
    composition at play time.
    """
    if zone.width <= 0 or zone.height <= 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"Zone '{zone.name}' must have positive width and height.",
        )
    if zone.position_x < 0 or zone.position_y < 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"Zone '{zone.name}' cannot have negative offsets.",
        )
    if (
        zone.position_x + zone.width > layout.resolution_w
        or zone.position_y + zone.height > layout.resolution_h
    ):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Zone '{zone.name}' "
                f"({zone.position_x},{zone.position_y} {zone.width}×{zone.height}) "
                f"extends past the Layout canvas "
                f"({layout.resolution_w}×{layout.resolution_h})."
            ),
        )


def _apply_zones(session: Session, layout: Layout, zones: list[ZoneIn]) -> None:
    """Replace a Layout's zone set wholesale.

    Uses the SQLAlchemy relationship (with ``cascade='all, delete-orphan'``)
    to wipe existing rows so we don't hit the "Instance has been deleted"
    cascade error that bit us in PR #9 for ``ScheduleItem``. Validates
    every incoming Zone up-front so a bad payload returns 400 with the
    existing Zones still in place, not a half-wiped Layout.
    """
    # Up-front validation. Also check every referenced media_id exists
    # before we mutate anything.
    for zone_in in zones:
        _validate_zone(zone_in, layout)
        for zi in zone_in.items:
            if not session.get(Media, zi.media_id):
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    detail=f"Media {zi.media_id} not found",
                )

    # Clear via the relationship; cascade handles the DELETEs at flush.
    layout.zones.clear()
    session.flush()

    for zone_in in zones:
        zone = Zone(
            name=zone_in.name or "Zone",
            position_x=zone_in.position_x,
            position_y=zone_in.position_y,
            width=zone_in.width,
            height=zone_in.height,
            z_index=zone_in.z_index,
        )
        for zi in zone_in.items:
            zone.items.append(
                ZoneItem(
                    media_id=zi.media_id,
                    order=zi.order,
                    duration_override=zi.duration_override,
                )
            )
        layout.zones.append(zone)


@router.get("", response_model=list[LayoutRead])
def list_layouts(
    session: Annotated[Session, Depends(get_session)],
    _admin: Annotated[str, Depends(require_admin)],
) -> list[Layout]:
    return list(session.exec(select(Layout).order_by(Layout.updated_at.desc())))


@router.post("", response_model=LayoutRead, status_code=status.HTTP_201_CREATED)
def create_layout(
    payload: LayoutCreate,
    session: Annotated[Session, Depends(get_session)],
    _admin: Annotated[str, Depends(require_admin)],
) -> Layout:
    if payload.resolution_w <= 0 or payload.resolution_h <= 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="Layout resolution must be positive.",
        )

    layout = Layout(
        name=payload.name,
        description=payload.description,
        resolution_w=payload.resolution_w,
        resolution_h=payload.resolution_h,
    )
    session.add(layout)
    # Flush so the Layout gets a PK before we attach zones.
    session.flush()
    _apply_zones(session, layout, payload.zones)
    session.commit()
    session.refresh(layout)
    return layout


@router.get("/{layout_id}", response_model=LayoutRead)
def get_layout(
    layout_id: int,
    session: Annotated[Session, Depends(get_session)],
    _admin: Annotated[str, Depends(require_admin)],
) -> Layout:
    layout = session.get(Layout, layout_id)
    if not layout:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Layout not found")
    return layout


@router.patch("/{layout_id}", response_model=LayoutRead)
def update_layout(
    layout_id: int,
    payload: LayoutUpdate,
    session: Annotated[Session, Depends(get_session)],
    _admin: Annotated[str, Depends(require_admin)],
) -> Layout:
    layout = session.get(Layout, layout_id)
    if not layout:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Layout not found")

    data = payload.model_dump(exclude_unset=True)
    if data.get("resolution_w") is not None and data["resolution_w"] <= 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="resolution_w must be positive"
        )
    if data.get("resolution_h") is not None and data["resolution_h"] <= 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="resolution_h must be positive"
        )

    # Apply metadata first so zone validation runs against the new
    # canvas size if the caller resized AND re-zoned in one PATCH.
    for key in ("name", "description", "resolution_w", "resolution_h"):
        if key in data:
            setattr(layout, key, data[key])

    if payload.zones is not None:
        _apply_zones(session, layout, payload.zones)

    layout.updated_at = datetime.utcnow()
    session.add(layout)
    session.commit()
    session.refresh(layout)
    return layout


@router.delete(
    "/{layout_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def delete_layout(
    layout_id: int,
    session: Annotated[Session, Depends(get_session)],
    _admin: Annotated[str, Depends(require_admin)],
) -> Response:
    layout = session.get(Layout, layout_id)
    if not layout:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Layout not found")

    in_use = session.exec(
        select(ScheduleItem).where(ScheduleItem.layout_id == layout_id)
    ).first()
    if in_use:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=(
                "Layout is used by at least one schedule item; "
                "remove it from schedules before deleting."
            ),
        )

    session.delete(layout)
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
