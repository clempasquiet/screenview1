"""Phase 2 Step 4 — worker parses the Layout-tree manifest.

These tests exercise the pure parsing + caching logic of
``NetworkWorker._resolve_slide`` / ``_resolve_zone`` /
``_resolve_zone_item`` without spinning up the full Qt event loop,
a live server, or real network I/O. Caching calls (``_ensure_cached``)
are stubbed so we can assert on what the resolver would do with a
healthy cache dir.

The goal is to pin the contract :

  * Every media in every zone of every slide is resolved.
  * Streams bypass the cache pipeline.
  * The progress counter ticks once per item across the whole tree.
  * A cache failure anywhere in the tree raises ``_CacheError``, not
    a silent half-resolved slide.
  * ``Slide.has_video`` + ``Slide.first_video_item`` expose the
    right dispatch primitives for the UI.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pytest


_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))


def _load(name: str) -> ModuleType:
    """Load a player-windows module by path. Mirrors the helper used
    in the other test files here — protects against sys.modules
    collisions with the Linux player during cross-suite runs."""
    path = _PKG_ROOT / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"screenview_windows_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


pytest.importorskip("PyQt6")


@pytest.fixture()
def worker_module() -> ModuleType:
    # Ensure config + libmpv_fetch imports resolve to the Windows copies.
    _load("config")
    _load("libmpv_fetch")
    return _load("worker_network")


def _make_worker(worker_module, tmp_path):
    """Build a NetworkWorker without running ``__init__`` (to avoid Qt
    session requirements). We just stitch the attributes it reads in
    the resolver paths."""
    worker = worker_module.NetworkWorker.__new__(worker_module.NetworkWorker)
    worker._cache_dir = tmp_path / "cache"
    worker._cache_dir.mkdir(parents=True, exist_ok=True)
    worker._session = MagicMock()
    # Stub the status signal — we only care about the counter in these
    # tests; log messages are not asserted here.
    worker.status_changed = MagicMock()
    worker.status_changed.emit = MagicMock()
    return worker


def _stub_cache(monkeypatch, worker_module, successful: bool = True) -> MagicMock:
    """Replace ``NetworkWorker._ensure_cached`` with a stub.

    When ``successful`` is True, the stub writes an empty file named
    after the item's md5 in the cache dir and returns that path. This
    mirrors the real function's contract (path exists on success).
    When False, the stub raises ``RuntimeError`` — the resolver must
    surface that as a ``_CacheError``.
    """
    call_log: list[dict] = []

    def fake(self, item):
        call_log.append(item)
        if not successful:
            raise RuntimeError(f"simulated download failure for {item.get('original_name')}")
        dest = self._cache_dir / f"{item['md5_hash']}.bin"
        dest.write_bytes(b"")
        return dest

    monkeypatch.setattr(worker_module.NetworkWorker, "_ensure_cached", fake)
    return call_log  # type: ignore[return-value]


def _make_progress(worker_module, total: int) -> tuple:
    tracker = MagicMock()
    progress = worker_module._Progress(done=0, total=total, emit=tracker)
    return progress, tracker


# ---------------------------------------------------------------------------
# _resolve_zone_item
# ---------------------------------------------------------------------------


def test_resolve_image_item_caches_and_returns_path(worker_module, tmp_path, monkeypatch):
    worker = _make_worker(worker_module, tmp_path)
    call_log = _stub_cache(monkeypatch, worker_module)
    progress, ticker = _make_progress(worker_module, 1)

    raw = {
        "media_id": 7,
        "order": 0,
        "type": "image",
        "original_name": "a.png",
        "url": "http://srv/api/media/7/download?sig=…",
        "md5_hash": "abc123",
        "size_bytes": 1234,
        "duration": 5,
        "mime_type": "image/png",
    }
    item = worker._resolve_zone_item(raw, progress)
    assert item is not None
    assert item.media_id == 7
    assert item.kind == "image"
    assert item.path is not None and item.path.exists()
    assert item.duration == 5
    assert item.mime_type == "image/png"
    assert item.stream_url is None
    # Cache was called.
    assert len(call_log) == 1
    # Progress ticked once.
    assert progress.done == 1
    ticker.assert_called_with(1, 1)


def test_resolve_stream_item_bypasses_cache(worker_module, tmp_path, monkeypatch):
    worker = _make_worker(worker_module, tmp_path)
    call_log = _stub_cache(monkeypatch, worker_module)
    progress, _ = _make_progress(worker_module, 1)

    raw = {
        "media_id": 42,
        "order": 0,
        "type": "stream",
        "original_name": "Lobby cam",
        "url": "rtsp://10.0.0.42:554/live",
        "md5_hash": "",
        "size_bytes": 0,
        "duration": 60,
    }
    item = worker._resolve_zone_item(raw, progress)
    assert item is not None
    assert item.kind == "stream"
    assert item.path is None
    assert item.stream_url == "rtsp://10.0.0.42:554/live"
    # Cache must NOT have been called for a stream.
    assert call_log == []
    # Progress still ticks so the bar reflects all items.
    assert progress.done == 1


def test_resolve_stream_without_url_is_skipped(worker_module, tmp_path, monkeypatch):
    worker = _make_worker(worker_module, tmp_path)
    _stub_cache(monkeypatch, worker_module)
    progress, _ = _make_progress(worker_module, 1)

    raw = {
        "media_id": 42,
        "order": 0,
        "type": "stream",
        "original_name": "Borked stream",
        "url": "",
        "md5_hash": "",
        "size_bytes": 0,
        "duration": 60,
    }
    assert worker._resolve_zone_item(raw, progress) is None
    # Still ticks progress so the bar completes.
    assert progress.done == 1


def test_resolve_item_raises_cache_error_on_download_failure(worker_module, tmp_path, monkeypatch):
    worker = _make_worker(worker_module, tmp_path)
    _stub_cache(monkeypatch, worker_module, successful=False)
    progress, _ = _make_progress(worker_module, 1)

    raw = {
        "media_id": 1,
        "order": 0,
        "type": "image",
        "original_name": "broken.png",
        "url": "http://srv/x",
        "md5_hash": "bad",
        "duration": 5,
    }
    with pytest.raises(worker_module._CacheError) as excinfo:
        worker._resolve_zone_item(raw, progress)
    assert "broken.png" in str(excinfo.value) or "media_id=1" in str(excinfo.value)


# ---------------------------------------------------------------------------
# _resolve_zone
# ---------------------------------------------------------------------------


def test_resolve_zone_materialises_geometry_and_items(worker_module, tmp_path, monkeypatch):
    worker = _make_worker(worker_module, tmp_path)
    _stub_cache(monkeypatch, worker_module)
    progress, _ = _make_progress(worker_module, 2)

    raw = {
        "zone_id": 3,
        "name": "Main",
        "position_x": 100,
        "position_y": 50,
        "width": 1600,
        "height": 900,
        "z_index": 2,
        "items": [
            {
                "media_id": 1, "order": 0, "type": "image",
                "original_name": "a.png", "url": "u1",
                "md5_hash": "h1", "duration": 5,
            },
            {
                "media_id": 2, "order": 1, "type": "video",
                "original_name": "b.mp4", "url": "u2",
                "md5_hash": "h2", "duration": 30,
            },
        ],
    }
    zone = worker._resolve_zone(raw, progress)
    assert zone.zone_id == 3
    assert zone.name == "Main"
    assert (zone.position_x, zone.position_y) == (100, 50)
    assert (zone.width, zone.height) == (1600, 900)
    assert zone.z_index == 2
    assert [i.media_id for i in zone.items] == [1, 2]
    assert progress.done == 2


def test_resolve_zone_with_missing_fields_uses_defaults(worker_module, tmp_path, monkeypatch):
    worker = _make_worker(worker_module, tmp_path)
    _stub_cache(monkeypatch, worker_module)
    progress, _ = _make_progress(worker_module, 0)

    zone = worker._resolve_zone({"items": []}, progress)
    assert zone.zone_id is None
    assert zone.name == "Zone"
    assert (zone.position_x, zone.position_y) == (0, 0)
    assert (zone.width, zone.height) == (1920, 1080)
    assert zone.z_index == 0
    assert zone.items == []


# ---------------------------------------------------------------------------
# _resolve_slide
# ---------------------------------------------------------------------------


def test_resolve_slide_walks_every_zone(worker_module, tmp_path, monkeypatch):
    """A slide with 2 zones × 2 items each must produce 4 cache calls."""
    worker = _make_worker(worker_module, tmp_path)
    call_log = _stub_cache(monkeypatch, worker_module)
    progress, _ = _make_progress(worker_module, 4)

    raw = {
        "slide_id": "schedule_item:1",
        "order": 0,
        "duration": 30,
        "layout": {
            "layout_id": 9,
            "name": "NewsDesk",
            "resolution_w": 1920,
            "resolution_h": 1080,
            "zones": [
                {
                    "zone_id": 1, "name": "A",
                    "position_x": 0, "position_y": 0,
                    "width": 960, "height": 1080, "z_index": 0,
                    "items": [
                        {"media_id": 11, "type": "image", "order": 0,
                         "url": "u", "md5_hash": "h11", "duration": 5},
                        {"media_id": 12, "type": "image", "order": 1,
                         "url": "u", "md5_hash": "h12", "duration": 5},
                    ],
                },
                {
                    "zone_id": 2, "name": "B",
                    "position_x": 960, "position_y": 0,
                    "width": 960, "height": 1080, "z_index": 1,
                    "items": [
                        {"media_id": 21, "type": "image", "order": 0,
                         "url": "u", "md5_hash": "h21", "duration": 5},
                        {"media_id": 22, "type": "image", "order": 1,
                         "url": "u", "md5_hash": "h22", "duration": 5},
                    ],
                },
            ],
        },
    }
    slide = worker._resolve_slide(raw, progress)
    assert slide.slide_id == "schedule_item:1"
    assert slide.duration == 30
    assert slide.layout_id == 9
    assert slide.layout_name == "NewsDesk"
    assert len(slide.zones) == 2
    assert sum(len(z.items) for z in slide.zones) == 4
    assert len(call_log) == 4
    assert progress.done == 4


def test_resolve_slide_surfaces_cache_error_from_nested_item(worker_module, tmp_path, monkeypatch):
    worker = _make_worker(worker_module, tmp_path)
    _stub_cache(monkeypatch, worker_module, successful=False)
    progress, _ = _make_progress(worker_module, 1)

    raw = {
        "slide_id": "x", "order": 0, "duration": 10,
        "layout": {
            "layout_id": 1, "name": "y",
            "resolution_w": 1920, "resolution_h": 1080,
            "zones": [{
                "zone_id": 1, "name": "z",
                "position_x": 0, "position_y": 0,
                "width": 10, "height": 10, "z_index": 0,
                "items": [{
                    "media_id": 1, "type": "image", "order": 0,
                    "url": "u", "md5_hash": "h", "duration": 5,
                }],
            }],
        },
    }
    with pytest.raises(worker_module._CacheError):
        worker._resolve_slide(raw, progress)


def test_resolve_slide_legacy_shape_has_null_ids(worker_module, tmp_path, monkeypatch):
    """Synthetic legacy slides (layout_id=None, zone_id=None) must
    resolve just like real layouts — the worker does not care about
    the distinction."""
    worker = _make_worker(worker_module, tmp_path)
    _stub_cache(monkeypatch, worker_module)
    progress, _ = _make_progress(worker_module, 1)

    raw = {
        "slide_id": "schedule_item:42", "order": 3, "duration": 8,
        "layout": {
            "layout_id": None,
            "name": "Synthetic: x.png",
            "resolution_w": 1920, "resolution_h": 1080,
            "zones": [{
                "zone_id": None, "name": "Fullscreen",
                "position_x": 0, "position_y": 0,
                "width": 1920, "height": 1080, "z_index": 0,
                "items": [{
                    "media_id": 7, "type": "image", "order": 0,
                    "url": "u", "md5_hash": "h7", "duration": 8,
                }],
            }],
        },
    }
    slide = worker._resolve_slide(raw, progress)
    assert slide.layout_id is None
    assert slide.zones[0].zone_id is None
    assert slide.zones[0].items[0].media_id == 7


# ---------------------------------------------------------------------------
# Slide convenience predicates
# ---------------------------------------------------------------------------


def _image_zone(worker_module, tmp_path):
    return worker_module.Zone(
        zone_id=None, name="z", position_x=0, position_y=0, width=10, height=10,
        z_index=0,
        items=[worker_module.ZoneItem(
            media_id=1, kind="image", path=tmp_path / "x.png",
            duration=5, original_name="x.png",
        )],
    )


def _video_zone(worker_module, tmp_path):
    return worker_module.Zone(
        zone_id=None, name="z", position_x=0, position_y=0, width=10, height=10,
        z_index=0,
        items=[worker_module.ZoneItem(
            media_id=1, kind="video", path=tmp_path / "x.mp4",
            duration=30, original_name="x.mp4",
        )],
    )


def test_slide_has_video_true_when_any_zone_has_video(worker_module, tmp_path):
    slide = worker_module.Slide(
        slide_id="s", order=0, duration=30, layout_id=1, layout_name="x",
        resolution_w=1920, resolution_h=1080,
        zones=[_image_zone(worker_module, tmp_path), _video_zone(worker_module, tmp_path)],
    )
    assert slide.has_video is True
    video_item = slide.first_video_item()
    assert video_item is not None
    assert video_item.kind == "video"


def test_slide_has_video_false_when_only_images(worker_module, tmp_path):
    slide = worker_module.Slide(
        slide_id="s", order=0, duration=30, layout_id=1, layout_name="x",
        resolution_w=1920, resolution_h=1080,
        zones=[_image_zone(worker_module, tmp_path)],
    )
    assert slide.has_video is False
    assert slide.first_video_item() is None


def test_slide_first_video_item_walks_zones_in_order(worker_module, tmp_path):
    img = _image_zone(worker_module, tmp_path)
    vid = _video_zone(worker_module, tmp_path)
    slide = worker_module.Slide(
        slide_id="s", order=0, duration=30, layout_id=1, layout_name="x",
        resolution_w=1920, resolution_h=1080,
        zones=[img, vid, img],
    )
    # The second zone is the first one with video; that's what we should get.
    assert slide.first_video_item() is vid.items[0]


def test_slide_first_video_item_returns_stream_when_present(worker_module, tmp_path):
    zone = worker_module.Zone(
        zone_id=None, name="z", position_x=0, position_y=0, width=10, height=10,
        z_index=0,
        items=[worker_module.ZoneItem(
            media_id=1, kind="stream", path=None,
            duration=30, original_name="cam",
            stream_url="rtsp://cam.local/stream1",
        )],
    )
    slide = worker_module.Slide(
        slide_id="s", order=0, duration=30, layout_id=1, layout_name="x",
        resolution_w=1920, resolution_h=1080,
        zones=[zone],
    )
    assert slide.has_video is True
    v = slide.first_video_item()
    assert v is not None
    assert v.kind == "stream"
    assert v.stream_url == "rtsp://cam.local/stream1"
