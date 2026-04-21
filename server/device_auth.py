"""Per-device authentication and signed-URL helpers.

Each registered device gets an opaque random ``api_token`` at registration
time. The player persists it in its local config and sends it back on
every subsequent request:

  * REST: ``Authorization: Bearer <token>`` (preferred) or the
    ``X-Device-Token`` header (useful when the Authorization header is
    already used by an upstream proxy).
  * WebSocket: ``?token=<token>`` query string parameter (WebSocket
    handshakes don't cleanly support custom headers from browsers).

The same token doubles as the HMAC key for the per-device signed media
download URLs embedded in the manifest:

  sig = urlsafe_base64( HMAC-SHA256( token, "<device_id>|<media_id>|<exp>" ) )

This gives us two properties for free:

  1. A leaked URL is useless **after** the expiry timestamp.
  2. A leaked URL is useless **from a different device** because the
     signature binds the download to the device's token.

Tokens can be rotated from the CMS (``POST /api/devices/{id}/rotate-token``)
which invalidates all outstanding manifests — the player transparently
re-registers after the next 401 thanks to the stale-device flow added in
an earlier PR.
"""
from __future__ import annotations

import base64
import hmac
import logging
import secrets
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Annotated, Optional
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Query, Request, status
from sqlmodel import Session, select

from .database import get_session
from .models import Device

logger = logging.getLogger(__name__)


# How long a signed URL remains valid. Longer than the typical sync
# cycle (once per day is enough) but short enough to bound exposure on
# leaked manifests.
DEFAULT_SIGNATURE_TTL = timedelta(hours=6)


# ---------------------------------------------------------------------------
# Token generation + constant-time comparison
# ---------------------------------------------------------------------------


def generate_api_token() -> str:
    """Return a fresh opaque device token (URL-safe, ~43 chars)."""
    return secrets.token_urlsafe(32)


def tokens_match(a: Optional[str], b: Optional[str]) -> bool:
    if not a or not b:
        return False
    return hmac.compare_digest(a, b)


# ---------------------------------------------------------------------------
# FastAPI dependency: resolve + authenticate a device
# ---------------------------------------------------------------------------


def _extract_token(
    authorization: Optional[str],
    x_device_token: Optional[str],
    query_token: Optional[str],
) -> Optional[str]:
    """Pull the device token out of whichever transport supplied it.

    Order of preference: ``Authorization: Bearer …`` → ``X-Device-Token``
    header → ``?token=…`` query string. The query-string form exists for
    WebSocket clients that cannot set custom headers.
    """
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value:
            return value.strip()
    if x_device_token:
        return x_device_token.strip()
    if query_token:
        return query_token.strip()
    return None


def authenticate_device(
    device_id: UUID,
    session: Session,
    authorization: Optional[str],
    x_device_token: Optional[str],
    query_token: Optional[str],
) -> Device:
    """Resolve ``device_id`` and verify the supplied token.

    Raises 401 if the device exists but the token is wrong; 404 if the
    device is unknown. The 404 lets the player distinguish "I'm stale,
    re-register" from "my token was rotated, refresh it".
    """
    device = session.get(Device, device_id)
    if device is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Device not registered"
        )

    supplied = _extract_token(authorization, x_device_token, query_token)
    if not tokens_match(supplied, device.api_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid device token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return device


def device_auth_dependency(
    device_id: UUID,
    session: Annotated[Session, Depends(get_session)],
    authorization: Annotated[Optional[str], Header()] = None,
    x_device_token: Annotated[Optional[str], Header(alias="X-Device-Token")] = None,
    token: Annotated[Optional[str], Query()] = None,
) -> Device:
    """FastAPI dependency for endpoints that take ``device_id`` in the path."""
    return authenticate_device(
        device_id=device_id,
        session=session,
        authorization=authorization,
        x_device_token=x_device_token,
        query_token=token,
    )


# ---------------------------------------------------------------------------
# Signed URLs
# ---------------------------------------------------------------------------


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unix_ts(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def compute_signature(token: str, device_id: UUID, media_id: int, exp: int) -> str:
    """Deterministic HMAC-SHA256 over the canonical triple."""
    payload = f"{device_id}|{media_id}|{exp}".encode("utf-8")
    digest = hmac.new(token.encode("utf-8"), payload, sha256).digest()
    return _b64(digest)


def sign_media_url(
    base_url: str,
    device: Device,
    media_id: int,
    ttl: timedelta = DEFAULT_SIGNATURE_TTL,
) -> str:
    """Return an absolute URL that proves *device* is allowed to fetch
    *media_id* until ``now + ttl``."""
    exp = _unix_ts(datetime.now(tz=timezone.utc) + ttl)
    if not device.api_token:
        # Should never happen post-migration, but be explicit.
        raise RuntimeError(f"Device {device.id} has no api_token; refusing to sign.")
    sig = compute_signature(device.api_token, device.id, media_id, exp)
    base = base_url.rstrip("/")
    return (
        f"{base}/api/media/{media_id}/download"
        f"?device_id={device.id}&exp={exp}&sig={sig}"
    )


def verify_media_signature(
    device: Device, media_id: int, exp: int, sig: str, now: Optional[datetime] = None
) -> None:
    """Raise 401/403 if the signature is wrong or expired."""
    if exp < _unix_ts(now or datetime.now(tz=timezone.utc)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Signature expired"
        )
    if not device.api_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Device has no token"
        )
    expected = compute_signature(device.api_token, device.id, media_id, exp)
    if not hmac.compare_digest(expected, sig):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Invalid signature"
        )


def extract_request_base_url(request: Request) -> str:
    """Get a stable base URL ('http://host:port') from the current request."""
    return str(request.base_url).rstrip("/")
