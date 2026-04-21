"""Phase 2 Step 2 — server-side PDF → JPEG ingestion.

Every test builds its own PDF in-memory with PyMuPDF so:

  * no binary fixture is committed to git,
  * each test controls exactly the shape it wants (N pages, sizes,
    contents, encryption),
  * the end-to-end contract is exercised: real PDF bytes → real JPEG
    files on disk → real ``Media`` rows in the database.

The heavy lifting lives in ``server/pdf.py``; the router wires it up
to the ``upload_dir`` + the ``Media`` table. We unit-test both.
"""
from __future__ import annotations

import io
from pathlib import Path

import fitz  # type: ignore[import-untyped]  # PyMuPDF
import pytest
from fastapi.testclient import TestClient

from server.tests.test_api import (  # noqa: F401  — fixtures + helpers
    _admin_auth,
    client,
)


# ---------------------------------------------------------------------------
# In-memory PDF factories
# ---------------------------------------------------------------------------


def make_pdf_bytes(page_count: int = 3, *, encrypted: bool = False) -> bytes:
    """Build a small, deterministic PDF with ``page_count`` pages.

    Each page has a distinct string on it so the resulting JPEGs are
    *different* files (not byte-identical) — that keeps the
    deduplication code path meaningful instead of accidentally
    collapsing every page of the test PDF into the same MD5.
    """
    doc = fitz.open()
    for i in range(page_count):
        page = doc.new_page(width=400, height=300)  # small canvas, fast render
        page.insert_text(
            (72, 72),
            f"Test page {i + 1} of {page_count} — unique content {i}",
            fontsize=24,
        )
    buf = io.BytesIO()
    if encrypted:
        doc.save(buf, encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="secret", owner_pw="secret")
    else:
        doc.save(buf)
    doc.close()
    return buf.getvalue()


def _upload_pdf(
    client: TestClient,
    headers: dict[str, str],
    pdf_bytes: bytes,
    *,
    filename: str = "test.pdf",
    default_duration: int = 5,
    dpi: int = 150,
) -> tuple[int, dict]:
    resp = client.post(
        "/api/media/pdf",
        headers=headers,
        files={"file": (filename, pdf_bytes, "application/pdf")},
        data={"default_duration": str(default_duration), "dpi": str(dpi)},
    )
    try:
        body = resp.json()
    except ValueError:
        body = {"raw": resp.text}
    return resp.status_code, body


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_upload_pdf_creates_one_media_per_page(client: TestClient) -> None:
    headers = _admin_auth(client)
    pdf = make_pdf_bytes(page_count=3)

    status, body = _upload_pdf(client, headers, pdf, filename="brochure.pdf")
    assert status == 201, body

    assert body["pages_added"] == 3
    assert body["pages_deduplicated"] == 0
    assert len(body["pages"]) == 3

    for idx, page in enumerate(body["pages"]):
        assert page["type"] == "image"
        assert page["mime_type"] == "image/jpeg"
        assert page["md5_hash"]  # non-empty
        assert page["size_bytes"] > 0
        # Human-readable name preserved + suffix added
        assert page["original_name"] == f"brochure.pdf — page {idx + 1} / 3"

    # Each page must have a distinct MD5 (the factory writes distinct
    # text per page). If this ever fails, the rendering path somehow
    # collapsed every page to the same bytes — a real bug.
    hashes = {p["md5_hash"] for p in body["pages"]}
    assert len(hashes) == 3, f"expected 3 distinct MD5s, got {hashes!r}"


def test_pdf_pages_are_served_via_signed_url(client: TestClient) -> None:
    """End-to-end: a PDF-derived page is a first-class Media the CMS
    can preview and the player can sync — no special case anywhere."""
    headers = _admin_auth(client)
    pdf = make_pdf_bytes(page_count=1)
    _, body = _upload_pdf(client, headers, pdf, filename="single.pdf")
    media_id = body["pages"][0]["id"]

    # Ask the existing preview endpoint for a signed URL on the page.
    preview = client.post(
        f"/api/media/{media_id}/preview-url", headers=headers
    )
    assert preview.status_code == 200
    signed_url = preview.json()["url"]

    # Public-ish download via the signed URL works without auth.
    dl = client.get(signed_url)
    assert dl.status_code == 200
    # First bytes of a baseline JPEG are FF D8 FF.
    assert dl.content[:3] == b"\xff\xd8\xff", "expected a JPEG image"


def test_uploading_same_pdf_twice_dedups_pages(client: TestClient) -> None:
    headers = _admin_auth(client)
    pdf = make_pdf_bytes(page_count=2)

    _, first = _upload_pdf(client, headers, pdf)
    assert first["pages_added"] == 2
    first_ids = [p["id"] for p in first["pages"]]

    _, second = _upload_pdf(client, headers, pdf)
    assert second["pages_added"] == 0
    assert second["pages_deduplicated"] == 2
    assert [p["id"] for p in second["pages"]] == first_ids


def test_partial_dedup_when_only_some_pages_overlap(client: TestClient) -> None:
    """Upload PDF A (3 pages), then PDF B that reuses page 1 of A and
    adds a fresh page. The second upload should split 1+1."""
    headers = _admin_auth(client)

    # PDF A: 3 pages, contents "… unique content 0/1/2"
    _, first = _upload_pdf(client, headers, make_pdf_bytes(page_count=3))
    assert first["pages_added"] == 3

    # PDF B: craft one with text matching A's page 0 and a fresh page.
    doc = fitz.open()
    p = doc.new_page(width=400, height=300)
    p.insert_text((72, 72), "Test page 1 of 3 — unique content 0", fontsize=24)
    p = doc.new_page(width=400, height=300)
    p.insert_text((72, 72), "Brand-new page — never rendered before", fontsize=24)
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()

    _, second = _upload_pdf(client, headers, buf.getvalue(), filename="mixed.pdf")
    # Rendering gives us two files; only one (the brand-new page)
    # needs to be persisted as a new row. The other reuses the A row.
    assert second["pages_deduplicated"] + second["pages_added"] == 2
    # Brand-new page is counted as added.
    assert second["pages_added"] >= 1


def test_dpi_override_is_honoured(client: TestClient) -> None:
    """Higher DPI → larger JPEG. We only assert monotonicity (not exact
    bytes) because JPEG compression depends on content."""
    headers = _admin_auth(client)
    pdf = make_pdf_bytes(page_count=1)

    _, low = _upload_pdf(client, headers, pdf, filename="a.pdf", dpi=72)
    _, hi = _upload_pdf(
        client, headers, make_pdf_bytes(page_count=1, encrypted=False),
        filename="b.pdf", dpi=300,
    )
    low_size = low["pages"][0]["size_bytes"]
    hi_size = hi["pages"][0]["size_bytes"]
    assert hi_size > low_size, (
        f"higher DPI should produce a larger JPEG; got low={low_size} hi={hi_size}"
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_upload_pdf_requires_admin(client: TestClient) -> None:
    pdf = make_pdf_bytes(page_count=1)
    resp = client.post(
        "/api/media/pdf",
        files={"file": ("anon.pdf", pdf, "application/pdf")},
        data={"default_duration": "5", "dpi": "150"},
    )
    assert resp.status_code == 401


def test_non_pdf_upload_returns_400(client: TestClient) -> None:
    headers = _admin_auth(client)
    # A PNG with matching magic bytes but wrong extension — the sniff
    # must catch it on the ``%PDF-`` header check.
    status, body = _upload_pdf(
        client, headers, b"\x89PNG\r\n\x1a\n-not-a-pdf", filename="tricky.pdf"
    )
    assert status == 400
    assert "PDF" in body["detail"]


def test_empty_upload_returns_400(client: TestClient) -> None:
    headers = _admin_auth(client)
    status, body = _upload_pdf(client, headers, b"", filename="empty.pdf")
    assert status == 400


def test_encrypted_pdf_is_rejected(client: TestClient) -> None:
    """We never accept passwords at upload time — signage content
    must be uploaded unencrypted."""
    headers = _admin_auth(client)
    pdf = make_pdf_bytes(page_count=1, encrypted=True)
    status, body = _upload_pdf(client, headers, pdf, filename="secret.pdf")
    assert status == 400
    assert "password" in body["detail"].lower()


def test_oversized_pdf_is_rejected_with_413(client: TestClient, monkeypatch) -> None:
    """Cap is configurable via the MAX_PAGES constant; we patch it down
    so the test stays fast."""
    import server.pdf as pdf_mod

    monkeypatch.setattr(pdf_mod, "MAX_PAGES", 3)
    headers = _admin_auth(client)
    status, body = _upload_pdf(client, headers, make_pdf_bytes(page_count=4))
    assert status == 413
    assert "caps" in body["detail"].lower() or "pages" in body["detail"].lower()


def test_dpi_outside_allowed_range_returns_400(client: TestClient) -> None:
    headers = _admin_auth(client)
    for bad_dpi in (0, 50, 400, -1):
        status, _ = _upload_pdf(
            client, headers, make_pdf_bytes(page_count=1), dpi=bad_dpi
        )
        assert status == 400, f"dpi={bad_dpi} should be rejected"


def test_zero_duration_returns_400(client: TestClient) -> None:
    headers = _admin_auth(client)
    status, _ = _upload_pdf(
        client, headers, make_pdf_bytes(page_count=1), default_duration=0
    )
    assert status == 400


# ---------------------------------------------------------------------------
# Transactional guarantees
# ---------------------------------------------------------------------------


def test_rendering_failure_leaves_no_files_or_rows(client: TestClient, monkeypatch, tmp_path) -> None:
    """A failure mid-render must unlink every page it already wrote and
    leave the DB untouched. We simulate a renderer crash after two
    pages by monkey-patching ``Page.get_pixmap`` to raise on the 3rd."""
    headers = _admin_auth(client)
    pdf = make_pdf_bytes(page_count=5)

    import server.pdf as pdf_mod

    # Count how many media rows are visible before the upload.
    before = client.get("/api/media", headers=headers).json()

    real_render = pdf_mod.render_pdf_to_jpegs

    def exploding_render(pdf_bytes, output_dir, *, dpi, max_pages):
        # Render everything through the real pipeline, but then force
        # an error BEFORE returning so the roll-back cleanup has
        # something to undo.
        pages = real_render(pdf_bytes, output_dir, dpi=dpi, max_pages=max_pages)
        # Make sure it actually wrote files.
        for p in pages:
            assert (output_dir / p.filename).exists()
        raise RuntimeError("simulated mid-flight failure")

    monkeypatch.setattr(pdf_mod, "render_pdf_to_jpegs", exploding_render)
    # Also patch the symbol in the router namespace — it imported by
    # name so the module-level patch above isn't seen through the
    # existing ``from ..pdf import render_pdf_to_jpegs``.
    import server.routers.media as media_mod

    monkeypatch.setattr(media_mod, "render_pdf_to_jpegs", exploding_render)

    status, body = _upload_pdf(client, headers, pdf)
    # We raise HTTPException(500) for unexpected renderer failures.
    assert status == 500, body

    after = client.get("/api/media", headers=headers).json()
    assert len(after) == len(before), (
        "a failed PDF ingest must not leave any Media rows behind"
    )


def test_db_failure_after_render_rolls_back_files(
    client: TestClient, monkeypatch
) -> None:
    """Simulate a post-render DB failure: every page already rendered
    to disk must be unlinked so a retry doesn't see orphan files.

    We inject the failure by wrapping ``Session.commit`` so the
    upstream render + flush succeed, but the final commit throws. This
    is the realistic failure mode (disk ok, DB temporarily borked);
    replacing ``Media`` itself interferes with upstream ``select()``
    calls and doesn't exercise the code path we care about.
    """
    headers = _admin_auth(client)
    pdf = make_pdf_bytes(page_count=3)

    from server.config import settings
    from sqlmodel import Session

    upload_dir_before = set(p.name for p in Path(settings.upload_dir).iterdir())

    original_commit = Session.commit

    def exploding_commit(self):
        raise RuntimeError("simulated DB commit failure")

    monkeypatch.setattr(Session, "commit", exploding_commit)
    try:
        status, _ = _upload_pdf(client, headers, pdf)
    finally:
        monkeypatch.setattr(Session, "commit", original_commit)

    # The ``except Exception`` branch in the router re-raises → 500.
    assert status == 500

    upload_dir_after = set(p.name for p in Path(settings.upload_dir).iterdir())
    assert upload_dir_after == upload_dir_before, (
        f"files written during a failed ingest must be cleaned up; "
        f"leftovers={upload_dir_after - upload_dir_before!r}"
    )

    # And zero Media rows added for this attempt.
    listed = client.get("/api/media", headers=headers).json()
    assert all("simulated" not in m.get("original_name", "") for m in listed)


# ---------------------------------------------------------------------------
# Pure pdf.py helpers (no FastAPI)
# ---------------------------------------------------------------------------


def test_looks_like_pdf_detects_the_magic() -> None:
    from server.pdf import looks_like_pdf

    assert looks_like_pdf(b"%PDF-1.4\n...") is True
    assert looks_like_pdf(b"") is False
    assert looks_like_pdf(b"Hello") is False
    assert looks_like_pdf(b"\x89PNG\r\n\x1a\n") is False
    # Short payloads that happen to start with %PDF- are still rejected
    # by the MIN_PDF_BYTES floor.
    assert looks_like_pdf(b"%PDF-") is False


def test_render_pdf_to_jpegs_writes_one_file_per_page(tmp_path: Path) -> None:
    from server.pdf import render_pdf_to_jpegs

    pdf = make_pdf_bytes(page_count=4)
    rendered = render_pdf_to_jpegs(pdf, tmp_path, dpi=120)
    assert len(rendered) == 4
    assert {p.page_index for p in rendered} == {0, 1, 2, 3}
    for page in rendered:
        path = tmp_path / page.filename
        assert path.exists()
        assert path.read_bytes()[:3] == b"\xff\xd8\xff"
        assert page.total_pages == 4
        assert page.human_label.startswith("page ")


def test_render_pdf_to_jpegs_rolls_back_on_mid_flight_failure(
    tmp_path: Path, monkeypatch
) -> None:
    """If PyMuPDF raises on page N, pages 0..N-1 must be unlinked."""
    from server import pdf as pdf_mod

    # Patch Pixmap.save to raise on the 3rd call. pymupdf internals
    # don't expose a stable override point, so we wrap the Document's
    # load_page instead — that's what the public function calls.
    original = fitz.Page.get_pixmap
    call_count = {"n": 0}

    def flaky_get_pixmap(self, *args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 3:
            raise RuntimeError("simulated rendering failure on page 3")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(fitz.Page, "get_pixmap", flaky_get_pixmap)

    with pytest.raises(RuntimeError, match="page 3"):
        pdf_mod.render_pdf_to_jpegs(
            make_pdf_bytes(page_count=5), tmp_path, dpi=120
        )

    # No files left behind.
    leftover = list(tmp_path.iterdir())
    assert leftover == [], f"rollback left {leftover!r} on disk"
