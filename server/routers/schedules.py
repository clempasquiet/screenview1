"""Schedule/playlist CRUD and per-device manifest generation."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlmodel import Session, select

from ..database import get_session
from ..device_auth import (
    device_auth_dependency,
    extract_request_base_url,
    sign_media_url,
)
from ..models import Device, Media, Schedule, ScheduleItem
from ..schemas import (
    PlaylistManifest,
    PlaylistManifestItem,
    ScheduleCreate,
    ScheduleItemIn,
    ScheduleRead,
    ScheduleUpdate,
)
from ..security import require_admin
from ..ws_manager import manager

router = APIRouter(prefix="/api", tags=["schedules"])


def _apply_items(session: Session, schedule: Schedule, items: list[ScheduleItemIn]) -> None:
    for existing in list(schedule.items):
        session.delete(existing)
    session.flush()
    for item in items:
        media = session.get(Media, item.media_id)
        if not media:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, detail=f"Media {item.media_id} not found"
            )
        session.add(
            ScheduleItem(
                schedule_id=schedule.id,  # type: ignore[arg-type]
                media_id=item.media_id,
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
                media = session.get(Media, item.media_id)
                if not media:
                    continue
                items.append(
                    PlaylistManifestItem(
                        media_id=media.id,  # type: ignore[arg-type]
                        order=item.order,
                        type=media.type,
                        original_name=media.original_name,
                        url=sign_media_url(base, device, media.id),  # type: ignore[arg-type]
                        md5_hash=media.md5_hash,
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
