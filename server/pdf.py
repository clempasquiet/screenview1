"""Server-side PDF → JPEG rendering.

Rationale
=========

The player has a hard rule: **it never handles PDF**. Asking every
kiosk to render PDF would mean shipping a PDF engine (fonts, forms,
JS scripting, annotations, encryption…) — a huge surface area for a
signage display whose only job is to show pre-validated frames. So
the server flattens every uploaded PDF into a sequence of plain
``.jpg`` pages at ingest time and stores them as ordinary ``Media``
rows (``type=image``). Players see pages as normal images; the rest
of the store-and-forward pipeline (MD5 validation, signed URLs,
cache eviction) works unchanged.

Rendering backend
=================

We use **PyMuPDF** (``import fitz``) because:

  * it ships as a single wheel with no native dependencies beyond the
    tiny MuPDF runtime already embedded,
  * its ``Pixmap.save(..., jpg_quality=N)`` path produces a reasonable
    file size without a round-trip through Pillow,
  * it handles encrypted PDFs gracefully (see :class:`EncryptedPdfError`).

This module is intentionally **pure**: it does not touch the database,
FastAPI, or the settings object. It takes bytes in, drops files in a
caller-supplied directory, and returns metadata. That makes unit
tests fast and keeps the surface small.
"""
from __future__ import annotations

import io
import logging
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import fitz  # type: ignore[import-untyped]  # PyMuPDF

logger = logging.getLogger(__name__)


# Public tuning knobs — kept as module constants rather than settings
# entries because they're operational choices, not per-deployment
# config. Operators who need to change them can override at the
# endpoint level (``POST /api/media/pdf`` accepts a ``dpi`` form field).
DEFAULT_DPI = 150
MIN_DPI = 72
MAX_DPI = 300

# Safety ceiling: render more than this many pages in one upload and
# the server refuses. Exists to prevent a malicious or accidental
# upload of a 10 000-page PDF from saturating disk I/O and locking up
# the worker thread. Operators legitimately needing more pages can
# chunk the document up-stream.
MAX_PAGES = 100

# JPEG quality used for each rendered page. 85 is the industry sweet
# spot — visually indistinguishable from 95 on typical signage
# content, ≈35 % smaller on disk. Not configurable per upload because
# the trade-off is the same across every source PDF.
JPEG_QUALITY = 85

# Minimum size for a candidate PDF. Anything smaller than the 4-byte
# ``%PDF`` header is obviously not a real document; catch it early
# with a clean error before we hand it to MuPDF.
MIN_PDF_BYTES = 8


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class PdfError(ValueError):
    """Base class for everything this module raises.

    Subclassed from ``ValueError`` so callers that already catch
    ``ValueError`` for bad input keep working.
    """


class InvalidPdfError(PdfError):
    """The payload does not parse as a PDF (wrong magic bytes, corrupt)."""


class EncryptedPdfError(PdfError):
    """The PDF is encrypted and no password was supplied.

    We intentionally do **not** try to crack or accept passwords here —
    signage content should be uploaded in the clear; otherwise there's
    no benefit to the player-side rendering freedom we're buying.
    """


class PdfTooLargeError(PdfError):
    """The PDF has more pages than :data:`MAX_PAGES`."""


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RenderedPage:
    """One ``.jpg`` rendered from a PDF page.

    ``filename`` is the on-disk basename (a hex UUID the caller can
    treat opaquely). ``page_index`` is zero-based. ``size_bytes`` is
    the JPEG file size — convenient for the ``Media`` row the caller
    will insert, and saves a round-trip to ``os.stat()`` at ingest.
    ``total_pages`` is carried on every entry so the caller can build
    a human name like "— page 2 of 7" without having to pass the full
    list around.
    """

    page_index: int
    total_pages: int
    filename: str
    size_bytes: int

    @property
    def human_label(self) -> str:
        """Admin-friendly ``"… page 2 / 5"`` suffix."""
        return f"page {self.page_index + 1} / {self.total_pages}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def looks_like_pdf(blob: bytes) -> bool:
    """Cheap sniff: the PDF spec requires files to start with ``%PDF-``.

    Not authoritative — we rely on PyMuPDF to actually parse. But it
    lets the endpoint return a clean 400 before doing any expensive
    work on something that can't possibly be a PDF (e.g. an HTML
    error page a proxy substituted, or a leftover ``.docx``).
    """
    if not blob or len(blob) < MIN_PDF_BYTES:
        return False
    return blob[:5] == b"%PDF-"


def render_pdf_to_jpegs(
    pdf_bytes: bytes,
    output_dir: Path,
    *,
    dpi: int = DEFAULT_DPI,
    max_pages: int = MAX_PAGES,
) -> list[RenderedPage]:
    """Render every page of *pdf_bytes* into *output_dir* as JPEG files.

    Returns one :class:`RenderedPage` per page, in page-order. Writes
    files with a uuid-based basename so the existing ``upload_dir``
    layout (everything in a flat directory keyed by uuid) keeps
    working — the PDF handler doesn't need to know anything special
    about ``Media.filename`` conventions.

    Behavioural contract:

      * The input is validated (``looks_like_pdf`` + PyMuPDF parse)
        **before** any file is written. A malformed PDF results in
        zero side effects.
      * The page count is checked **before** rendering starts. An
        over-long PDF fails with :exc:`PdfTooLargeError` and zero
        pages on disk.
      * Individual page rendering is wrapped so a failure on page N
        rolls back pages 0..N-1 (we unlink them before re-raising).

    The caller owns the transactional story with the database: the
    normal usage is "render to a temp dir, insert DB rows in one
    commit, move files into upload_dir". We don't do that here
    because ``output_dir`` abstraction stays minimal.
    """
    if not looks_like_pdf(pdf_bytes):
        raise InvalidPdfError(
            "File does not start with %PDF- and cannot be parsed as PDF."
        )
    dpi = _clamp_dpi(dpi)

    doc = _open_pdf(pdf_bytes)
    total_pages = doc.page_count
    if total_pages == 0:
        doc.close()
        raise InvalidPdfError("PDF contains zero pages.")
    if total_pages > max_pages:
        doc.close()
        raise PdfTooLargeError(
            f"PDF has {total_pages} pages; the server caps single uploads "
            f"at {max_pages}. Split the document or raise MAX_PAGES."
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    zoom = dpi / 72.0  # PDF user-space is 72 DPI by definition
    matrix = fitz.Matrix(zoom, zoom)

    rendered: list[RenderedPage] = []
    try:
        for page_index in range(total_pages):
            page = doc.load_page(page_index)
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            filename = f"{secrets.token_hex(16)}.jpg"
            dest = output_dir / filename
            pix.save(str(dest), jpg_quality=JPEG_QUALITY)
            size_bytes = dest.stat().st_size
            rendered.append(
                RenderedPage(
                    page_index=page_index,
                    total_pages=total_pages,
                    filename=filename,
                    size_bytes=size_bytes,
                )
            )
        return rendered
    except Exception:
        # Roll back any page we already wrote so the caller doesn't
        # see a half-rendered PDF on disk.
        _cleanup(output_dir, rendered)
        raise
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _clamp_dpi(dpi: int) -> int:
    """Bring *dpi* into ``[MIN_DPI, MAX_DPI]``.

    We deliberately clamp rather than reject because the endpoint
    already validates the user-supplied value. This function guards
    against callers (including internal ones) passing a raw integer
    from somewhere else later on.
    """
    if dpi < MIN_DPI:
        logger.warning("DPI %s below MIN_DPI=%s; clamping.", dpi, MIN_DPI)
        return MIN_DPI
    if dpi > MAX_DPI:
        logger.warning("DPI %s above MAX_DPI=%s; clamping.", dpi, MAX_DPI)
        return MAX_DPI
    return dpi


def _open_pdf(pdf_bytes: bytes):  # -> fitz.Document
    """Wrapper around :func:`fitz.open` that translates errors."""
    try:
        doc = fitz.open(stream=io.BytesIO(pdf_bytes), filetype="pdf")
    except Exception as exc:  # PyMuPDF raises RuntimeError / FileDataError
        raise InvalidPdfError(f"PyMuPDF could not parse the payload: {exc}") from exc
    if doc.needs_pass:
        doc.close()
        raise EncryptedPdfError(
            "PDF is password-protected. Upload an unlocked version."
        )
    return doc


def _cleanup(directory: Path, pages: Iterable[RenderedPage]) -> None:
    """Best-effort unlink of already-rendered pages after a failure."""
    for page in pages:
        path = directory / page.filename
        try:
            path.unlink()
        except OSError as exc:
            logger.debug("Rollback: could not unlink %s: %s", path, exc)
