"""Media upload / listing / deletion routes."""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Annotated

from typing import Optional
from uuid import UUID

import aiofiles
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.responses import FileResponse
from sqlmodel import Session, select

from ..config import settings
from ..database import get_session
from ..device_auth import (
    extract_request_base_url,
    sign_admin_preview_url,
    verify_admin_signature,
    verify_media_signature,
)
from ..models import Device, Media, MediaType, ScheduleItem
from ..schemas import MediaPreviewUrl, MediaRead, MediaUpdate
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
    device_id: Annotated[Optional[UUID], Query(description="Device that the link was signed for")] = None,
    exp: Annotated[Optional[int], Query(description="Expiry for a device-signed URL")] = None,
    sig: Annotated[Optional[str], Query(description="HMAC for a device-signed URL")] = None,
    admin_exp: Annotated[Optional[int], Query(description="Expiry for an admin preview URL")] = None,
    admin_sig: Annotated[Optional[str], Query(description="HMAC for an admin preview URL")] = None,
) -> FileResponse:
    """Download endpoint used by players and by the CMS preview modal.

    Two flavours of signed URL are accepted, distinguished by which
    query-string parameters are present:

    * **Device-signed** (``device_id``, ``exp``, ``sig``): pre-signed
      by ``GET /api/schedule/{device_id}`` using the device's
      ``api_token``. Default TTL: 6 hours.
    * **Admin-signed** (``admin_exp``, ``admin_sig``): pre-signed by
      ``POST /api/media/{id}/preview-url`` with the server's
      ``secret_key``. Default TTL: 15 minutes. Used by the live preview
      in the CMS.

    At least one of the two flavours must be present and valid. Neither
    flavour relies on Authorization headers, so browsers can point
    ``<img src>`` / ``<video src>`` directly at these URLs.
    """
    media = session.get(Media, media_id)
    if not media:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Media not found")

    if admin_sig is not None and admin_exp is not None:
        verify_admin_signature(media_id=media_id, exp=admin_exp, sig=admin_sig)
    elif device_id is not None and exp is not None and sig is not None:
        device = session.get(Device, device_id)
        if not device:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Device not registered")
        verify_media_signature(device=device, media_id=media_id, exp=exp, sig=sig)
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing signed URL parameters",
        )

    path = settings.upload_dir / media.filename
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="File missing on disk")
    return FileResponse(
        path,
        media_type=media.mime_type or "application/octet-stream",
        filename=media.original_name,
    )


@router.post(
    "/{media_id}/preview-url",
    response_model=MediaPreviewUrl,
    summary="Generate a short-lived admin preview URL",
)
def make_preview_url(
    media_id: int,
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    _admin: Annotated[str, Depends(require_admin)],
) -> MediaPreviewUrl:
    """Return an admin-signed URL the browser can embed directly in
    ``<img>`` / ``<video>`` tags without attaching the admin JWT on
    each request. The URL expires automatically; re-open the preview
    to get a fresh one.
    """
    media = session.get(Media, media_id)
    if not media:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Media not found")
    base = extract_request_base_url(request)
    return MediaPreviewUrl(
        media_id=media_id,
        url=sign_admin_preview_url(base, media_id),
        mime_type=media.mime_type,
        type=media.type,
        original_name=media.original_name,
        default_duration=media.default_duration,
    )
