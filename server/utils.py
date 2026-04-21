"""Helper utilities for media handling."""
from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path

from .models import MediaType


CHUNK = 1024 * 1024  # 1 MiB


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
