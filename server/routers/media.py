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
from .. import pdf as pdf_mod  # exposes MAX_PAGES etc as module attrs
from ..pdf import (
    DEFAULT_DPI,
    MAX_DPI,
    MIN_DPI,
    EncryptedPdfError,
    InvalidPdfError,
    PdfTooLargeError,
    looks_like_pdf,
    render_pdf_to_jpegs,
)
from ..schemas import MediaPreviewUrl, MediaRead, MediaUpdate, PdfIngestResult, StreamCreate
from ..security import require_admin
from ..utils import guess_media_type, md5_of_file, validate_stream_url

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


@router.post(
    "/stream",
    response_model=MediaRead,
    status_code=status.HTTP_201_CREATED,
    summary="Register a live stream as a Media item (no upload)",
)
def create_stream_media(
    payload: StreamCreate,
    session: Annotated[Session, Depends(get_session)],
    _admin: Annotated[str, Depends(require_admin)],
) -> Media:
    """Live streams are a thin wrapper around a user-supplied URL.

    They break the offline-first guarantee for themselves only — the
    player skips the local-cache + MD5 pipeline and hands the URL
    straight to libmpv. The rest of the playlist is unaffected.

    Stream URLs are validated against an allow-list of schemes
    (``http``/``https``/``rtsp``/``rtsps``/``rtmp``/``rtmps``/``srt``/
    ``udp``/``rtp``) so the CMS can't accidentally instruct players to
    open ``file://`` URLs that would resolve on the kiosk's own
    filesystem.
    """
    try:
        stream_url = validate_stream_url(payload.url)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    if payload.default_duration <= 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="default_duration must be a positive integer (seconds).",
        )

    name = payload.name.strip() or "Live stream"

    media = Media(
        filename=None,
        original_name=name,
        type=MediaType.stream,
        md5_hash=None,
        size_bytes=0,
        default_duration=payload.default_duration,
        mime_type=payload.mime_type,
        stream_url=stream_url,
    )
    session.add(media)
    session.commit()
    session.refresh(media)
    return media


@router.post(
    "/pdf",
    response_model=PdfIngestResult,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a PDF; server flattens it to one image Media per page",
)
async def upload_pdf(
    session: Annotated[Session, Depends(get_session)],
    _admin: Annotated[str, Depends(require_admin)],
    file: UploadFile = File(...),
    default_duration: int = Form(10),
    dpi: int = Form(DEFAULT_DPI),
) -> PdfIngestResult:
    """Render a PDF server-side into one JPEG per page.

    Each page becomes an ordinary ``MediaType.image`` row with the
    normal MD5 hash, signed download URLs, and dedup — so the rest of
    the Store-and-Forward pipeline (player cache, schedules, preview)
    treats them as any other image. **Players never parse PDF**; this
    endpoint is what upholds that rule.

    Rendering

      * Uses PyMuPDF at ``dpi`` (default 150, clamped to 72–300).
      * Output is baseline JPEG at quality 85 (see ``server/pdf.py``).
      * Caps single uploads at ``pdf.MAX_PAGES`` (100 by default) to
        prevent a runaway upload from saturating disk + the worker
        thread.

    Transactional guarantees

      * If anything in the rendering step fails (malformed PDF, one
        bad page), **nothing** is persisted — no files on disk, no
        rows in the DB, and the caller gets a 400 with a specific
        error.
      * Individual pages are deduplicated by MD5. Re-uploading the
        same PDF returns the existing Media rows instead of
        duplicating the library. The response separates
        ``pages_added`` from ``pages_deduplicated`` so the CMS can
        show an accurate summary.
    """
    if dpi < MIN_DPI or dpi > MAX_DPI:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"dpi must be between {MIN_DPI} and {MAX_DPI} (got {dpi}).",
        )
    if default_duration <= 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="default_duration must be a positive integer (seconds).",
        )
    if not file.filename:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Missing filename.")

    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Empty upload.")
    if not looks_like_pdf(pdf_bytes):
        # Cheap sniff before we spin up a MuPDF parser. The player
        # also catches this but the friendly 400 here is what the
        # CMS shows the operator.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="File is not a PDF (missing %PDF- header).",
        )

    # Render first, commit to the DB second. The renderer already
    # rolls back partial pages on its own failure, and we roll the
    # directory back ourselves below if the DB insert throws.
    try:
        # Dereference ``MAX_PAGES`` via the module each call so tests
        # can monkey-patch ``server.pdf.MAX_PAGES`` and hit the
        # oversized-PDF branch without a server restart.
        rendered = render_pdf_to_jpegs(
            pdf_bytes,
            settings.upload_dir,
            dpi=dpi,
            max_pages=pdf_mod.MAX_PAGES,
        )
    except (InvalidPdfError, EncryptedPdfError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except PdfTooLargeError as exc:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=str(exc)
        ) from exc
    except Exception as exc:  # noqa: BLE001 — genuine catch-all for the renderer
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"PDF rendering failed: {exc}",
        ) from exc

    ordered_media: list[Media] = []
    added = 0
    deduplicated = 0

    try:
        for page in rendered:
            abs_path = settings.upload_dir / page.filename
            page_md5 = md5_of_file(abs_path)

            # Dedup: same bytes already in the library → drop this
            # copy and reuse the existing row. Re-uploading an
            # identical brochure converges to the same Media ids.
            existing = session.exec(
                select(Media).where(Media.md5_hash == page_md5)
            ).first()
            if existing:
                abs_path.unlink(missing_ok=True)
                ordered_media.append(existing)
                deduplicated += 1
                continue

            media = Media(
                filename=page.filename,
                original_name=f"{file.filename} — {page.human_label}",
                type=MediaType.image,
                md5_hash=page_md5,
                size_bytes=page.size_bytes,
                default_duration=default_duration,
                mime_type="image/jpeg",
            )
            session.add(media)
            session.flush()  # get the PK before we move on
            ordered_media.append(media)
            added += 1

        session.commit()
    except Exception as exc:
        # Something blew up mid-ingest. Unlink every page we wrote
        # for THIS upload (the ones we reused from the library stay
        # put — they pre-existed) so a retry doesn't see orphan
        # files. ``session.rollback`` clears any pending INSERTs from
        # the transaction. We convert to HTTPException(500) rather
        # than letting the original exception propagate so clients
        # (and the Starlette TestClient) see a proper JSON error
        # response instead of a raw traceback.
        for page in rendered:
            (settings.upload_dir / page.filename).unlink(missing_ok=True)
        try:
            session.rollback()
        except Exception:  # noqa: BLE001  — best-effort; we're already unwinding
            pass
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"PDF ingest failed while persisting Media rows: {exc}",
        ) from exc

    # Refresh in one pass so ``MediaRead`` serialisation picks up
    # autogenerated ids + timestamps.
    for m in ordered_media:
        session.refresh(m)

    return PdfIngestResult(
        pages_added=added,
        pages_deduplicated=deduplicated,
        pages=[MediaRead.model_validate(m, from_attributes=True) for m in ordered_media],
    )


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

    data = payload.model_dump(exclude_unset=True)
    if "stream_url" in data and data["stream_url"]:
        try:
            data["stream_url"] = validate_stream_url(data["stream_url"])
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    for key, value in data.items():
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

    # Streams have no on-disk artifact; only file-backed media need an unlink.
    if media.filename:
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

    if media.type == MediaType.stream or not media.filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This media has no downloadable file (live stream).",
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

    # Streams don't go through our download endpoint at all — the
    # CMS preview hands the upstream URL straight to <video>/<iframe>
    # like the player would. No signing needed because the URL is
    # already public-by-construction.
    if media.type == MediaType.stream:
        if not media.stream_url:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, detail="Stream has no URL set."
            )
        return MediaPreviewUrl(
            media_id=media_id,
            url=media.stream_url,
            mime_type=media.mime_type,
            type=media.type,
            original_name=media.original_name,
            default_duration=media.default_duration,
        )

    base = extract_request_base_url(request)
    return MediaPreviewUrl(
        media_id=media_id,
        url=sign_admin_preview_url(base, media_id),
        mime_type=media.mime_type,
        type=media.type,
        original_name=media.original_name,
        default_duration=media.default_duration,
    )
