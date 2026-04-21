"""Media upload / listing / deletion routes."""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Annotated

from uuid import UUID

import aiofiles
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile, status
from fastapi.responses import FileResponse
from sqlmodel import Session, select

from ..config import settings
from ..database import get_session
from ..device_auth import verify_media_signature
from ..models import Device, Media, MediaType, ScheduleItem
from ..schemas import MediaRead, MediaUpdate
from ..security import require_admin
from ..utils import guess_media_type, md5_of_file

router = APIRouter(prefix="/api/media", tags=["media"])


@router.get("", response_model=list[MediaRead])
def list_media(
    session: Annotated[Session, Depends(get_session)],
    _admin: Annotated[str, Depends(require_admin)],
) -> list[Media]:
    return list(session.exec(select(Media).order_by(Media.created_at.desc())))


@router.post("", response_model=MediaRead, status_code=status.HTTP_201_CREATED)
async def upload_media(
    session: Annotated[Session, Depends(get_session)],
    _admin: Annotated[str, Depends(require_admin)],
    file: UploadFile = File(...),
    default_duration: int = Form(10),
) -> Media:
    if not file.filename:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Missing filename")

    suffix = Path(file.filename).suffix
    stored_name = f"{uuid.uuid4().hex}{suffix}"
    dest: Path = settings.upload_dir / stored_name

    size = 0
    async with aiofiles.open(dest, "wb") as out:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            await out.write(chunk)

    media_type, mime = guess_media_type(file.filename)
    md5 = md5_of_file(dest)

    # Deduplicate by hash: if an identical file already exists, drop this copy.
    existing = session.exec(select(Media).where(Media.md5_hash == md5)).first()
    if existing:
        dest.unlink(missing_ok=True)
        return existing

    media = Media(
        filename=stored_name,
        original_name=file.filename,
        type=media_type,
        md5_hash=md5,
        size_bytes=size,
        default_duration=default_duration,
        mime_type=mime,
    )
    session.add(media)
    session.commit()
    session.refresh(media)
    return media


@router.get("/{media_id}", response_model=MediaRead)
def get_media(
    media_id: int,
    session: Annotated[Session, Depends(get_session)],
    _admin: Annotated[str, Depends(require_admin)],
) -> Media:
    media = session.get(Media, media_id)
    if not media:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Media not found")
    return media


@router.patch("/{media_id}", response_model=MediaRead)
def update_media(
    media_id: int,
    payload: MediaUpdate,
    session: Annotated[Session, Depends(get_session)],
    _admin: Annotated[str, Depends(require_admin)],
) -> Media:
    media = session.get(Media, media_id)
    if not media:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Media not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(media, key, value)
    session.add(media)
    session.commit()
    session.refresh(media)
    return media


@router.delete(
    "/{media_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def delete_media(
    media_id: int,
    session: Annotated[Session, Depends(get_session)],
    _admin: Annotated[str, Depends(require_admin)],
) -> Response:
    media = session.get(Media, media_id)
    if not media:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Media not found")

    in_use = session.exec(
        select(ScheduleItem).where(ScheduleItem.media_id == media_id)
    ).first()
    if in_use:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="Media is used by at least one schedule; remove it from schedules first.",
        )

    path = settings.upload_dir / media.filename
    path.unlink(missing_ok=True)
    session.delete(media)
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{media_id}/download", include_in_schema=True)
def download_media(
    media_id: int,
    session: Annotated[Session, Depends(get_session)],
    device_id: Annotated[UUID, Query(description="Device that the link was signed for")],
    exp: Annotated[int, Query(description="Expiry (Unix timestamp, seconds)")],
    sig: Annotated[str, Query(description="HMAC signature")],
) -> FileResponse:
    """Download endpoint used by players.

    URLs are pre-signed by ``GET /api/schedule/{device_id}`` using the
    device's ``api_token`` as the HMAC key. Each URL binds a specific
    device to a specific media item until a specific expiry timestamp.
    A leaked URL is therefore:

      * useless after ``exp`` passes,
      * useless from any other ``device_id``,
      * invalidated globally if the device's token is rotated.
    """
    media = session.get(Media, media_id)
    if not media:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Media not found")

    device = session.get(Device, device_id)
    if not device:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Device not registered")

    verify_media_signature(device=device, media_id=media_id, exp=exp, sig=sig)

    path = settings.upload_dir / media.filename
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="File missing on disk")
    return FileResponse(
        path,
        media_type=media.mime_type or "application/octet-stream",
        filename=media.original_name,
    )
