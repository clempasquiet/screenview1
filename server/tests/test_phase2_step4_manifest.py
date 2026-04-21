"""Phase 2 Step 4 — Layout-tree manifest emission.

The player manifest (GET /api/schedule/{device_id}) is now a tree:

    LayoutManifest
    └── slides: list[ManifestSlide]
         ├── slide metadata (slide_id, order, duration)
         └── layout: ManifestLayout
               ├── layout metadata (name, resolution_w, resolution_h)
               └── zones: list[ManifestZone]
                     ├── geometry (x, y, w, h, z_index)
                     └── items: list[ManifestZoneItem]

These tests pin:
  * Empty schedule → empty manifest.
  * Legacy single-media slot → synthetic layout (layout_id None,
    zone_id None, full-canvas zone).
  * Real layout slot with one image zone → emitted as-is.
  * Real layout with multiple zones → ordering + geometry preserved.
  * Stream medias → upstream URL passed through, md5 empty.
  * duration_override on ScheduleItem overrides the slide duration.
  * A layout with items of mixed durations rolls up to the max zone-
    item duration when no slot-level override is present.
  * Dangling layout_id / media_id → slide silently omitted.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from server.tests.test_api import (  # noqa: F401  — fixtures + helpers
    _admin_auth,
    _device_auth,
    _upload_image,
    client,
)
from server.tests.test_phase2_layouts import _create_layout  # noqa: F401


def _register_and_attach(
    client: TestClient, headers: dict[str, str], schedule_id: int, mac: str
) -> tuple[dict, str]:
    """Helper: register a fresh device and assign the schedule."""
    reg = client.post("/api/register", json={"mac_address": mac}).json()
    client.patch(
        f"/api/devices/{reg['id']}",
        headers=headers,
        json={"status": "active", "current_schedule_id": schedule_id},
    )
    return reg, reg["api_token"]


# ---------------------------------------------------------------------------
# Shape: empty / missing schedule
# ---------------------------------------------------------------------------


def test_manifest_empty_when_no_schedule_assigned(client: TestClient) -> None:
    reg = client.post(
        "/api/register", json={"mac_address": "00:11:22:33:44:55"}
    ).json()

    resp = client.get(
        f"/api/schedule/{reg['id']}", headers=_device_auth(reg["api_token"])
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["schedule_id"] is None
    assert data["schedule_name"] is None
    assert data["slides"] == []


def test_manifest_empty_when_schedule_has_no_items(client: TestClient) -> None:
    headers = _admin_auth(client)
    sched = client.post(
        "/api/schedules", headers=headers, json={"name": "Nothing", "items": []}
    ).json()
    reg, token = _register_and_attach(client, headers, sched["id"], "00:11:22:33:44:56")
    data = client.get(
        f"/api/schedule/{reg['id']}", headers=_device_auth(token)
    ).json()
    assert data["schedule_id"] == sched["id"]
    assert data["slides"] == []


# ---------------------------------------------------------------------------
# Synthetic layouts for legacy single-media slots
# ---------------------------------------------------------------------------


def test_single_media_slot_wraps_in_synthetic_fullscreen_layout(client: TestClient) -> None:
    headers = _admin_auth(client)
    media_id = _upload_image(client, "lone.png", b"lone")
    sched = client.post(
        "/api/schedules",
        headers=headers,
        json={
            "name": "Solo",
            "items": [{"media_id": media_id, "order": 0, "duration_override": 12}],
        },
    ).json()
    reg, token = _register_and_attach(client, headers, sched["id"], "aa:00:11:22:33:44")

    data = client.get(
        f"/api/schedule/{reg['id']}", headers=_device_auth(token)
    ).json()
    assert len(data["slides"]) == 1
    slide = data["slides"][0]

    assert slide["slide_id"].startswith("schedule_item:")
    assert slide["order"] == 0
    assert slide["duration"] == 12

    layout = slide["layout"]
    assert layout["layout_id"] is None, "synthetic layout must have layout_id=None"
    assert layout["resolution_w"] == 1920
    assert layout["resolution_h"] == 1080

    assert len(layout["zones"]) == 1
    zone = layout["zones"][0]
    assert zone["zone_id"] is None, "synthetic zone must have zone_id=None"
    assert zone["position_x"] == 0
    assert zone["position_y"] == 0
    assert zone["width"] == 1920
    assert zone["height"] == 1080

    assert len(zone["items"]) == 1
    item = zone["items"][0]
    assert item["media_id"] == media_id
    assert item["type"] == "image"
    # Signed URL binds device + media + expiry.
    assert "device_id=" in item["url"]
    assert "sig=" in item["url"]
    assert item["md5_hash"]
    assert item["size_bytes"] > 0


def test_duration_override_absent_uses_media_default(client: TestClient) -> None:
    """When ScheduleItem.duration_override is None, the synthetic
    layout's slide duration falls back to the max zone-item duration
    (which is the media's default_duration in the single-media case)."""
    headers = _admin_auth(client)
    up = client.post(
        "/api/media",
        headers=headers,
        files={"file": ("default-dur.png", b"x", "image/png")},
        data={"default_duration": "25"},
    )
    media_id = up.json()["id"]
    sched = client.post(
        "/api/schedules",
        headers=headers,
        json={"name": "Default", "items": [{"media_id": media_id, "order": 0}]},
    ).json()
    reg, token = _register_and_attach(client, headers, sched["id"], "aa:00:11:22:33:45")

    data = client.get(
        f"/api/schedule/{reg['id']}", headers=_device_auth(token)
    ).json()
    assert data["slides"][0]["duration"] == 25


# ---------------------------------------------------------------------------
# Real layouts
# ---------------------------------------------------------------------------


def test_layout_slot_emits_real_zones_and_geometry(client: TestClient) -> None:
    headers = _admin_auth(client)
    bg = _upload_image(client, "bg.png", b"bg")
    logo = _upload_image(client, "logo.png", b"logo")

    layout = _create_layout(
        client,
        headers,
        name="NewsDesk",
        resolution_w=1920,
        resolution_h=1080,
        zones=[
            {
                "name": "Main",
                "position_x": 0,
                "position_y": 0,
                "width": 1920,
                "height": 720,
                "z_index": 0,
                "items": [
                    {"media_id": bg, "order": 0, "duration_override": 8},
                    {"media_id": bg, "order": 1, "duration_override": None},
                ],
            },
            {
                "name": "Logo",
                "position_x": 1700,
                "position_y": 20,
                "width": 200,
                "height": 80,
                "z_index": 10,
                "items": [{"media_id": logo, "order": 0}],
            },
        ],
    )
    sched = client.post(
        "/api/schedules",
        headers=headers,
        json={
            "name": "WithLayout",
            "items": [{"layout_id": layout["id"], "order": 0, "duration_override": 30}],
        },
    ).json()
    reg, token = _register_and_attach(client, headers, sched["id"], "aa:00:11:22:33:46")

    data = client.get(
        f"/api/schedule/{reg['id']}", headers=_device_auth(token)
    ).json()
    slide = data["slides"][0]
    assert slide["duration"] == 30

    layout_out = slide["layout"]
    assert layout_out["layout_id"] == layout["id"]
    assert layout_out["name"] == "NewsDesk"
    assert layout_out["resolution_w"] == 1920
    assert layout_out["resolution_h"] == 1080

    # Zones sorted by z_index: Main (0) then Logo (10).
    assert [z["name"] for z in layout_out["zones"]] == ["Main", "Logo"]
    main = layout_out["zones"][0]
    assert (main["position_x"], main["position_y"]) == (0, 0)
    assert (main["width"], main["height"]) == (1920, 720)
    assert main["z_index"] == 0
    assert len(main["items"]) == 2
    assert main["items"][0]["duration"] == 8
    # zone_id should be the real DB row id (non-null for real layouts).
    assert main["zone_id"] is not None

    logo_out = layout_out["zones"][1]
    assert (logo_out["position_x"], logo_out["position_y"]) == (1700, 20)
    assert logo_out["z_index"] == 10
    assert logo_out["items"][0]["media_id"] == logo


def test_layout_slot_without_override_rolls_up_max_item_duration(client: TestClient) -> None:
    """A layout with no slot-level duration_override takes the max
    zone-item duration so every zone's playlist gets at least one
    full cycle on screen."""
    headers = _admin_auth(client)
    fast = client.post(
        "/api/media",
        headers=headers,
        files={"file": ("fast.png", b"fast-bytes", "image/png")},
        data={"default_duration": "3"},
    ).json()["id"]
    slow = client.post(
        "/api/media",
        headers=headers,
        files={"file": ("slow.png", b"slow-bytes", "image/png")},
        data={"default_duration": "30"},
    ).json()["id"]
    assert fast != slow, "MD5 dedup must not collapse these two images"

    layout = _create_layout(
        client,
        headers,
        name="Mixed",
        zones=[
            {
                "name": "A",
                "position_x": 0, "position_y": 0,
                "width": 100, "height": 100, "z_index": 0,
                "items": [{"media_id": fast, "order": 0}],
            },
            {
                "name": "B",
                "position_x": 100, "position_y": 0,
                "width": 100, "height": 100, "z_index": 1,
                "items": [{"media_id": slow, "order": 0}],
            },
        ],
    )
    sched = client.post(
        "/api/schedules",
        headers=headers,
        json={"name": "Rollup", "items": [{"layout_id": layout["id"], "order": 0}]},
    ).json()
    reg, token = _register_and_attach(client, headers, sched["id"], "aa:00:11:22:33:47")

    data = client.get(
        f"/api/schedule/{reg['id']}", headers=_device_auth(token)
    ).json()
    assert data["slides"][0]["duration"] == 30


# ---------------------------------------------------------------------------
# Streams inside the tree
# ---------------------------------------------------------------------------


def test_stream_zone_item_passes_upstream_url_through(client: TestClient) -> None:
    headers = _admin_auth(client)
    stream = client.post(
        "/api/media/stream",
        headers=headers,
        json={
            "name": "Lobby cam",
            "url": "rtsp://10.0.0.42:554/live",
            "default_duration": 60,
        },
    ).json()
    layout = _create_layout(
        client,
        headers,
        name="WithStream",
        zones=[
            {
                "name": "Full",
                "position_x": 0, "position_y": 0,
                "width": 1920, "height": 1080, "z_index": 0,
                "items": [{"media_id": stream["id"], "order": 0}],
            },
        ],
    )
    sched = client.post(
        "/api/schedules",
        headers=headers,
        json={"name": "Live", "items": [{"layout_id": layout["id"], "order": 0}]},
    ).json()
    reg, token = _register_and_attach(client, headers, sched["id"], "aa:00:11:22:33:48")

    data = client.get(
        f"/api/schedule/{reg['id']}", headers=_device_auth(token)
    ).json()
    item = data["slides"][0]["layout"]["zones"][0]["items"][0]
    assert item["type"] == "stream"
    assert item["url"] == "rtsp://10.0.0.42:554/live"
    assert item["md5_hash"] == ""
    assert item["size_bytes"] == 0


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


def test_dangling_media_id_is_skipped_not_500(client: TestClient) -> None:
    """If a media row is deleted while a ScheduleItem still references
    it via media_id (should not happen in normal flow — PATCH protects
    against it), the manifest must skip the slide rather than crash."""
    headers = _admin_auth(client)
    media_id = _upload_image(client, "orphan.png", b"bye")
    sched = client.post(
        "/api/schedules",
        headers=headers,
        json={"name": "About to lose", "items": [{"media_id": media_id, "order": 0}]},
    ).json()
    reg, token = _register_and_attach(client, headers, sched["id"], "aa:00:11:22:33:49")

    # Force the media deletion by removing it from the library (we
    # cannot use the normal /api/media/{id} DELETE because it rejects
    # in-use rows — we bypass with a direct DB manipulation).
    import importlib

    import server.database as db_mod

    importlib.reload(db_mod)
    from sqlalchemy import text as _text

    with db_mod.engine.begin() as conn:
        # Break the FK by nulling the ScheduleItem.media_id while
        # deleting the media. This mirrors a "lost media" invariant
        # that can happen in disaster-recovery restores.
        conn.execute(
            _text("UPDATE scheduleitem SET media_id = 99999 WHERE media_id = :mid"),
            {"mid": media_id},
        )

    data = client.get(
        f"/api/schedule/{reg['id']}", headers=_device_auth(token)
    ).json()
    # The dangling slide is silently skipped; manifest is empty but
    # valid. The schedule itself is still reported so the player
    # knows WHICH schedule it thinks it's playing.
    assert data["schedule_id"] == sched["id"]
    assert data["slides"] == []


def test_slide_id_is_stable_across_refreshes(client: TestClient) -> None:
    """Players may use slide_id for per-slide caching or logging, so
    it must not change between two consecutive manifest fetches for
    the same schedule."""
    headers = _admin_auth(client)
    media_id = _upload_image(client, "stable.png", b"x")
    sched = client.post(
        "/api/schedules",
        headers=headers,
        json={"name": "Stable", "items": [{"media_id": media_id, "order": 0}]},
    ).json()
    reg, token = _register_and_attach(client, headers, sched["id"], "aa:00:11:22:33:50")

    a = client.get(
        f"/api/schedule/{reg['id']}", headers=_device_auth(token)
    ).json()
    b = client.get(
        f"/api/schedule/{reg['id']}", headers=_device_auth(token)
    ).json()
    assert a["slides"][0]["slide_id"] == b["slides"][0]["slide_id"]
