"""Schedule/playlist CRUD and per-device manifest generation."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlmodel import Session, select

logger = logging.getLogger(__name__)

from ..database import get_session
from ..device_auth import (
    device_auth_dependency,
    extract_request_base_url,
    sign_admin_preview_url,
    sign_media_url,
)
from ..models import Device, Layout, Media, MediaType, Schedule, ScheduleItem
from ..schemas import (
    PlaylistManifest,
    PlaylistManifestItem,
    ScheduleCreate,
    ScheduleItemIn,
    SchedulePreview,
    SchedulePreviewItem,
    ScheduleRead,
    ScheduleUpdate,
)
from ..security import require_admin
from ..ws_manager import manager

router = APIRouter(prefix="/api", tags=["schedules"])


def _validate_item_xor(item: ScheduleItemIn) -> None:
    """Enforce the ScheduleItem shape: exactly one of media_id / layout_id.

    See the class docstring on ``models.ScheduleItem`` for why the
    invariant is validated here rather than via a DB CHECK constraint:
    SQLite can't add one retroactively without a full table rebuild,
    and doing that on every boot for a legacy field is overkill.

    Raised errors are 400s with a specific message so the CMS can show
    the operator exactly what's wrong with their payload.
    """
    has_media = item.media_id is not None
    has_layout = item.layout_id is not None
    if has_media and has_layout:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="ScheduleItem cannot specify both media_id and layout_id.",
        )
    if not has_media and not has_layout:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="ScheduleItem must specify media_id OR layout_id.",
        )


def _apply_items(session: Session, schedule: Schedule, items: list[ScheduleItemIn]) -> None:
    """Replace a schedule's items in place, driven by the ORM relationship.

    Each incoming item is either a **media slot** (legacy, single file
    on screen) or a **layout slot** (Phase 2, multi-zone canvas). The
    XOR is enforced by :func:`_validate_item_xor`.

    We mutate ``schedule.items`` **through the relationship** rather
    than issuing explicit ``session.delete()`` calls on the existing
    rows. The relationship is declared with
    ``cascade='all, delete-orphan'`` so clearing the list and appending
    fresh ``ScheduleItem`` rows causes SQLAlchemy to emit the
    necessary DELETEs for the orphaned items at flush time.

    An older implementation deleted items manually then flushed, which
    left ``schedule.items`` populated with deleted-but-still-in-memory
    objects. The next ``session.add(schedule)`` (e.g. from
    ``update_schedule`` when it refreshes ``updated_at``) then walked
    the relationship via the cascade and raised
    ``InvalidRequestError: Instance has been deleted``. PR #9 fixed
    that by going through the relationship; we preserve the fix here.
    """
    # Up-front validation: XOR shape, then FK existence. A bad payload
    # must return 400 with the existing playlist untouched — never a
    # 400 on top of a half-wiped schedule.
    for item in items:
        _validate_item_xor(item)
        if item.media_id is not None:
            if not session.get(Media, item.media_id):
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    detail=f"Media {item.media_id} not found",
                )
        else:  # layout_id is not None (XOR guaranteed above)
            if not session.get(Layout, item.layout_id):
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    detail=f"Layout {item.layout_id} not found",
                )

    # Drop every existing ScheduleItem via the relationship. The
    # delete-orphan cascade takes care of the DELETEs at flush time.
    schedule.items.clear()
    session.flush()

    # Attach the new items via the relationship. SQLAlchemy fills in
    # ``schedule_id`` automatically from the relationship back-ref.
    for item in items:
        schedule.items.append(
            ScheduleItem(
                media_id=item.media_id,
                layout_id=item.layout_id,
                order=item.order,
                duration_override=item.duration_override,
            )
        )


@router.get("/schedules", response_model=list[ScheduleRead])
def list_schedules(
    session: Annotated[Session, Depends(get_session)],
    _admin: Annotated[str, Depends(require_admin)],
) -> list[Schedule]:
    return list(session.exec(select(Schedule).order_by(Schedule.updated_at.desc())))


@router.post("/schedules", response_model=ScheduleRead, status_code=status.HTTP_201_CREATED)
def create_schedule(
    payload: ScheduleCreate,
    session: Annotated[Session, Depends(get_session)],
    _admin: Annotated[str, Depends(require_admin)],
) -> Schedule:
    schedule = Schedule(name=payload.name, description=payload.description)
    session.add(schedule)
    session.flush()
    _apply_items(session, schedule, payload.items)
    session.commit()
    session.refresh(schedule)
    return schedule


@router.get("/schedules/{schedule_id}", response_model=ScheduleRead)
def get_schedule(
    schedule_id: int,
    session: Annotated[Session, Depends(get_session)],
    _admin: Annotated[str, Depends(require_admin)],
) -> Schedule:
    schedule = session.get(Schedule, schedule_id)
    if not schedule:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Schedule not found")
    return schedule


@router.patch("/schedules/{schedule_id}", response_model=ScheduleRead)
def update_schedule(
    schedule_id: int,
    payload: ScheduleUpdate,
    session: Annotated[Session, Depends(get_session)],
    _admin: Annotated[str, Depends(require_admin)],
) -> Schedule:
    schedule = session.get(Schedule, schedule_id)
    if not schedule:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Schedule not found")

    data = payload.model_dump(exclude_unset=True)
    if "name" in data and data["name"] is not None:
        schedule.name = data["name"]
    if "description" in data:
        schedule.description = data["description"]
    if payload.items is not None:
        _apply_items(session, schedule, payload.items)
    schedule.updated_at = datetime.utcnow()

    session.add(schedule)
    session.commit()
    session.refresh(schedule)
    return schedule


@router.delete(
    "/schedules/{schedule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def delete_schedule(
    schedule_id: int,
    session: Annotated[Session, Depends(get_session)],
    _admin: Annotated[str, Depends(require_admin)],
) -> Response:
    schedule = session.get(Schedule, schedule_id)
    if not schedule:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Schedule not found")

    attached = session.exec(
        select(Device).where(Device.current_schedule_id == schedule_id)
    ).first()
    if attached:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="Schedule is assigned to at least one device; unassign first.",
        )
    session.delete(schedule)
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/schedules/{schedule_id}/preview",
    response_model=SchedulePreview,
    summary="Admin preview of a schedule (signed URLs, short TTL)",
)
def get_schedule_preview(
    schedule_id: int,
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    _admin: Annotated[str, Depends(require_admin)],
) -> SchedulePreview:
    """Return the same playlist the players receive, but with
    admin-signed preview URLs instead of per-device ones. The CMS uses
    this to render a live preview of the schedule before publishing."""
    schedule = session.get(Schedule, schedule_id)
    if not schedule:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Schedule not found")

    base = extract_request_base_url(request)
    items: list[SchedulePreviewItem] = []
    for item in sorted(schedule.items, key=lambda i: i.order):
        # Phase 2 multi-zone slots (layout_id set, media_id NULL) are
        # intentionally skipped at this layer for now. The preview
        # endpoint gains a dedicated Layout render path in Step 4 of
        # the sprint; keeping the change surface tight for Step 1.
        if item.media_id is None:
            logger.debug(
                "Skipping schedule item %s (layout_id=%s) in legacy preview",
                item.id,
                item.layout_id,
            )
            continue
        media = session.get(Media, item.media_id)
        if not media:
            continue
        # Streams skip the signed-URL pipeline entirely — the player
        # (and the CMS preview) hands the upstream URL straight to mpv.
        if media.type == MediaType.stream:
            if not media.stream_url:
                continue
            url = media.stream_url
        else:
            url = sign_admin_preview_url(base, media.id)  # type: ignore[arg-type]
        items.append(
            SchedulePreviewItem(
                media_id=media.id,  # type: ignore[arg-type]
                order=item.order,
                type=media.type,
                original_name=media.original_name,
                mime_type=media.mime_type,
                url=url,
                duration=item.duration_override or media.default_duration,
            )
        )

    return SchedulePreview(
        schedule_id=schedule.id,  # type: ignore[arg-type]
        schedule_name=schedule.name,
        generated_at=datetime.utcnow(),
        items=items,
    )


@router.post("/schedules/{schedule_id}/publish")
async def publish_schedule(
    schedule_id: int,
    session: Annotated[Session, Depends(get_session)],
    _admin: Annotated[str, Depends(require_admin)],
) -> dict[str, int]:
    """Notify every device currently assigned to this schedule to re-sync."""
    schedule = session.get(Schedule, schedule_id)
    if not schedule:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Schedule not found")

    devices = list(
        session.exec(select(Device).where(Device.current_schedule_id == schedule_id))
    )
    notified = 0
    for device in devices:
        if await manager.send(device.id, {"action": "sync_required", "schedule_id": schedule_id}):
            notified += 1
    return {"devices": len(devices), "notified": notified}


@router.get("/schedule/{device_id}", response_model=PlaylistManifest)
def get_device_manifest(
    device_id: UUID,  # noqa: ARG001  # used by the auth dependency
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    device: Annotated[Device, Depends(device_auth_dependency)],
) -> PlaylistManifest:
    """Return the JSON manifest a player should cache + download.

    Requires the device's ``api_token`` (Bearer or ``X-Device-Token``).
    Every download URL in the returned manifest is pre-signed with an
    HMAC that binds ``device_id`` + ``media_id`` + ``exp`` to the
    device's token; leaked URLs expire automatically and cannot be
    replayed from a different device.
    """
    schedule: Schedule | None = None
    items: list[PlaylistManifestItem] = []
    if device.current_schedule_id:
        schedule = session.get(Schedule, device.current_schedule_id)
        if schedule:
            base = extract_request_base_url(request)
            for item in sorted(schedule.items, key=lambda i: i.order):
                # Phase 2 multi-zone slots are not yet emitted in the
                # player manifest — that's Step 4 of the sprint. For
                # now we silently skip them so a Schedule mixing legacy
                # media slots and Layout slots still plays the media
                # slots correctly on players running the current code.
                if item.media_id is None:
                    logger.debug(
                        "Skipping schedule item %s (layout_id=%s) in legacy manifest",
                        item.id,
                        item.layout_id,
                    )
                    continue
                media = session.get(Media, item.media_id)
                if not media:
                    continue
                # Streams: hand the upstream URL straight to the player.
                # No signature, no MD5, no size — they are by definition
                # not cacheable. The player skips the cache pipeline for
                # stream items only; the rest of the playlist is unaffected.
                if media.type == MediaType.stream:
                    if not media.stream_url:
                        continue
                    items.append(
                        PlaylistManifestItem(
                            media_id=media.id,  # type: ignore[arg-type]
                            order=item.order,
                            type=media.type,
                            original_name=media.original_name,
                            url=media.stream_url,
                            md5_hash="",
                            size_bytes=0,
                            duration=item.duration_override or media.default_duration,
                        )
                    )
                    continue
                items.append(
                    PlaylistManifestItem(
                        media_id=media.id,  # type: ignore[arg-type]
                        order=item.order,
                        type=media.type,
                        original_name=media.original_name,
                        url=sign_media_url(base, device, media.id),  # type: ignore[arg-type]
                        md5_hash=media.md5_hash or "",
                        size_bytes=media.size_bytes,
                        duration=item.duration_override or media.default_duration,
                    )
                )

    return PlaylistManifest(
        device_id=device.id,
        schedule_id=schedule.id if schedule else None,
        schedule_name=schedule.name if schedule else None,
        generated_at=datetime.utcnow(),
        items=items,
    )
