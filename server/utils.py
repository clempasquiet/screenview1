"""Helper utilities for media handling."""
from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path
from urllib.parse import urlparse

from .models import MediaType


CHUNK = 1024 * 1024  # 1 MiB

# Schemes that libmpv handles natively and that we explicitly allow for
# ``MediaType.stream`` items. Notable exclusions: ``file://`` (would let
# the CMS instruct players to read local files) and any non-network
# scheme that could be abused for SSRF-by-proxy.
ALLOWED_STREAM_SCHEMES = frozenset(
    {"http", "https", "rtsp", "rtsps", "rtmp", "rtmps", "srt", "udp", "rtp"}
)


def md5_of_file(path: Path) -> str:
    """Compute the MD5 hash of a file on disk, streaming in 1 MiB chunks."""
    md5 = hashlib.md5()  # noqa: S324  # MD5 is used for integrity, not security
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(CHUNK)
            if not chunk:
                break
            md5.update(chunk)
    return md5.hexdigest()


def guess_media_type(filename: str) -> tuple[MediaType, str | None]:
    """Infer a MediaType + MIME type from a filename."""
    mime, _ = mimetypes.guess_type(filename)
    if mime is None:
        return MediaType.widget, None
    if mime.startswith("video/"):
        return MediaType.video, mime
    if mime.startswith("image/"):
        return MediaType.image, mime
    if mime == "text/html":
        return MediaType.widget, mime
    return MediaType.widget, mime


def validate_stream_url(url: str) -> str:
    """Sanity-check a user-supplied live-stream URL.

    Returns the trimmed URL on success. Raises ``ValueError`` with a
    human-readable message otherwise. Rules:

      * Must parse as a URL with a non-empty scheme + netloc.
      * Scheme must be in :data:`ALLOWED_STREAM_SCHEMES` (no ``file://``,
        no ``data:``, etc).
      * Netloc cannot be empty (rejects ``http:foo`` style accidents).
    """
    if not url:
        raise ValueError("Stream URL is required.")
    candidate = url.strip()
    if not candidate:
        raise ValueError("Stream URL is required.")
    parsed = urlparse(candidate)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ALLOWED_STREAM_SCHEMES:
        allowed = ", ".join(sorted(ALLOWED_STREAM_SCHEMES))
        raise ValueError(
            f"Unsupported scheme {scheme!r}. Allowed: {allowed}."
        )
    if not parsed.netloc:
        raise ValueError("Stream URL must include a host (e.g. rtsp://camera.local/stream).")
    return candidate
