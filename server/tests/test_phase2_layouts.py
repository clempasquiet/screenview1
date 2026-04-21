"""Phase 2 Step 1 — Layouts / Zones / ZoneItems + ScheduleItem XOR.

Covers:

  * Admin CRUD on ``/api/layouts``.
  * Nested Zone + ZoneItem creation / replacement / deletion.
  * Validation: canvas bounds, non-positive resolutions, bad media_id,
    409 when deleting a Layout still referenced by a ScheduleItem.
  * XOR invariant on ``ScheduleItem``: exactly one of ``media_id`` /
    ``layout_id`` per row.
  * Legacy manifest + preview endpoints silently skip Layout slots
    (they become a dedicated render path in Step 4).
  * PR #9 regression (PATCH a non-empty schedule) still works with
    mixed media + layout slots.
  * Idempotent migration of a legacy SQLite database lacking the
    ``layout_id`` column / having ``media_id`` declared NOT NULL.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


# The ``client`` fixture is provided by ``conftest.py`` / imported below
# from the existing test module. We import ``pytest`` fixtures via star
# so we don't duplicate the settings / monkeypatch / reload dance.
from server.tests.test_api import (  # noqa: F401  -- fixtures + helpers
    _admin_auth,
    _device_auth,
    _upload_image,
    client,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_layout(client: TestClient, headers: dict[str, str], **overrides) -> dict:
    payload = {
        "name": overrides.pop("name", "Default"),
        "description": overrides.pop("description", None),
        "resolution_w": overrides.pop("resolution_w", 1920),
        "resolution_h": overrides.pop("resolution_h", 1080),
        "zones": overrides.pop("zones", []),
    }
    payload.update(overrides)
    resp = client.post("/api/layouts", headers=headers, json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Layout CRUD happy path
# ---------------------------------------------------------------------------


def test_create_and_fetch_empty_layout(client: TestClient) -> None:
    headers = _admin_auth(client)
    created = _create_layout(client, headers, name="Blank canvas")
    assert created["zones"] == []
    assert created["resolution_w"] == 1920
    assert created["resolution_h"] == 1080

    fetched = client.get(f"/api/layouts/{created['id']}", headers=headers).json()
    assert fetched["name"] == "Blank canvas"


def test_list_layouts_newest_first(client: TestClient) -> None:
    headers = _admin_auth(client)
    _create_layout(client, headers, name="Old")
    _create_layout(client, headers, name="New")
    resp = client.get("/api/layouts", headers=headers)
    assert resp.status_code == 200
    names = [lay["name"] for lay in resp.json()]
    assert names[0] == "New"  # updated_at desc


def test_create_layout_with_zones_and_items(client: TestClient) -> None:
    headers = _admin_auth(client)
    m1 = _upload_image(client, "bg.png", b"bg")
    m2 = _upload_image(client, "logo.png", b"logo")

    created = _create_layout(
        client,
        headers,
        name="News",
        zones=[
            {
                "name": "Main",
                "position_x": 0,
                "position_y": 0,
                "width": 1920,
                "height": 720,
                "z_index": 0,
                "items": [
                    {"media_id": m1, "order": 0, "duration_override": 8},
                    {"media_id": m1, "order": 1, "duration_override": None},
                ],
            },
            {
                "name": "Logo",
                "position_x": 1700,
                "position_y": 20,
                "width": 200,
                "height": 80,
                "z_index": 10,
                "items": [{"media_id": m2, "order": 0}],
            },
        ],
    )
    assert len(created["zones"]) == 2
    # Zones sort by z_index ascending in the response — "Main" (0) first.
    assert created["zones"][0]["name"] == "Main"
    assert created["zones"][1]["name"] == "Logo"
    assert created["zones"][0]["items"][0]["duration_override"] == 8


def test_update_layout_metadata_only(client: TestClient) -> None:
    headers = _admin_auth(client)
    layout = _create_layout(client, headers, name="Old name")

    resp = client.patch(
        f"/api/layouts/{layout['id']}",
        headers=headers,
        json={"name": "New name", "description": "updated"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "New name"
    assert body["description"] == "updated"
    # Resolution untouched.
    assert body["resolution_w"] == layout["resolution_w"]


def test_update_layout_replaces_zones_wholesale(client: TestClient) -> None:
    headers = _admin_auth(client)
    m = _upload_image(client, "a.png", b"a")

    layout = _create_layout(
        client,
        headers,
        name="Changing",
        zones=[
            {
                "name": "A",
                "position_x": 0,
                "position_y": 0,
                "width": 100,
                "height": 100,
                "z_index": 0,
                "items": [{"media_id": m, "order": 0}],
            }
        ],
    )

    resp = client.patch(
        f"/api/layouts/{layout['id']}",
        headers=headers,
        json={
            "zones": [
                {
                    "name": "B",
                    "position_x": 10,
                    "position_y": 10,
                    "width": 200,
                    "height": 200,
                    "z_index": 1,
                    "items": [],
                }
            ],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["zones"]) == 1
    assert body["zones"][0]["name"] == "B"
    # The old "A" zone + its ZoneItem are gone via the delete-orphan cascade.


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_layout_rejects_non_positive_resolution(client: TestClient) -> None:
    """The handler's own guard returns a human-readable 400 rather than
    Pydantic's 422 so the CMS can show an actionable message.

    We also cover the PATCH path where the same check fires."""
    headers = _admin_auth(client)
    resp = client.post(
        "/api/layouts",
        headers=headers,
        json={"name": "bad", "resolution_w": 0, "resolution_h": 1080},
    )
    assert resp.status_code == 400
    assert "positive" in resp.json()["detail"].lower()

    resp = client.post(
        "/api/layouts",
        headers=headers,
        json={"name": "bad", "resolution_w": 1920, "resolution_h": -1},
    )
    assert resp.status_code == 400

    # PATCH path: the check must fire on update too.
    layout = _create_layout(client, headers)
    resp = client.patch(
        f"/api/layouts/{layout['id']}",
        headers=headers,
        json={"resolution_w": 0},
    )
    assert resp.status_code == 400


def test_zone_bounding_box_must_fit_canvas(client: TestClient) -> None:
    headers = _admin_auth(client)
    resp = client.post(
        "/api/layouts",
        headers=headers,
        json={
            "name": "Too big",
            "resolution_w": 100,
            "resolution_h": 100,
            "zones": [
                {
                    "name": "Overflow",
                    "position_x": 50,
                    "position_y": 50,
                    "width": 100,  # 50+100 = 150 > 100
                    "height": 10,
                    "z_index": 0,
                    "items": [],
                }
            ],
        },
    )
    assert resp.status_code == 400
    assert "extends past" in resp.json()["detail"]


def test_zone_item_with_unknown_media_is_rejected(client: TestClient) -> None:
    headers = _admin_auth(client)
    resp = client.post(
        "/api/layouts",
        headers=headers,
        json={
            "name": "Broken",
            "zones": [
                {
                    "name": "Z",
                    "position_x": 0,
                    "position_y": 0,
                    "width": 10,
                    "height": 10,
                    "z_index": 0,
                    "items": [{"media_id": 999_999, "order": 0}],
                }
            ],
        },
    )
    assert resp.status_code == 400
    assert "Media 999999 not found" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


def test_delete_layout_succeeds_when_unused(client: TestClient) -> None:
    headers = _admin_auth(client)
    layout = _create_layout(client, headers)

    resp = client.delete(f"/api/layouts/{layout['id']}", headers=headers)
    assert resp.status_code == 204

    # Gone.
    resp = client.get(f"/api/layouts/{layout['id']}", headers=headers)
    assert resp.status_code == 404


def test_delete_layout_blocked_when_referenced_by_schedule(client: TestClient) -> None:
    headers = _admin_auth(client)
    layout = _create_layout(client, headers, name="Pinned")

    # Attach via a schedule item.
    sched = client.post(
        "/api/schedules",
        headers=headers,
        json={
            "name": "Uses the layout",
            "items": [{"layout_id": layout["id"], "order": 0, "duration_override": 30}],
        },
    )
    assert sched.status_code == 201, sched.text

    resp = client.delete(f"/api/layouts/{layout['id']}", headers=headers)
    assert resp.status_code == 409
    assert "schedule" in resp.json()["detail"].lower()

    # Detach and retry → success.
    client.patch(
        f"/api/schedules/{sched.json()['id']}",
        headers=headers,
        json={"items": []},
    )
    resp = client.delete(f"/api/layouts/{layout['id']}", headers=headers)
    assert resp.status_code == 204


# ---------------------------------------------------------------------------
# ScheduleItem XOR
# ---------------------------------------------------------------------------


def test_schedule_item_accepts_layout_id(client: TestClient) -> None:
    headers = _admin_auth(client)
    layout = _create_layout(client, headers)

    resp = client.post(
        "/api/schedules",
        headers=headers,
        json={
            "name": "Layout-only playlist",
            "items": [{"layout_id": layout["id"], "order": 0, "duration_override": 20}],
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["layout_id"] == layout["id"]
    assert item["media_id"] is None


def test_schedule_item_rejects_both_media_and_layout(client: TestClient) -> None:
    headers = _admin_auth(client)
    layout = _create_layout(client, headers)
    m = _upload_image(client, "both.png", b"both")

    resp = client.post(
        "/api/schedules",
        headers=headers,
        json={
            "name": "Confused",
            "items": [
                {"media_id": m, "layout_id": layout["id"], "order": 0},
            ],
        },
    )
    assert resp.status_code == 400
    assert "both media_id and layout_id" in resp.json()["detail"]


def test_schedule_item_rejects_neither_media_nor_layout(client: TestClient) -> None:
    headers = _admin_auth(client)

    resp = client.post(
        "/api/schedules",
        headers=headers,
        json={
            "name": "Empty",
            "items": [{"order": 0, "duration_override": 5}],
        },
    )
    assert resp.status_code == 400
    assert "media_id OR layout_id" in resp.json()["detail"]


def test_schedule_item_rejects_unknown_layout_id(client: TestClient) -> None:
    headers = _admin_auth(client)

    resp = client.post(
        "/api/schedules",
        headers=headers,
        json={
            "name": "Dangling",
            "items": [{"layout_id": 999_999, "order": 0}],
        },
    )
    assert resp.status_code == 400
    assert "Layout 999999 not found" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Legacy endpoints: Layout slots skipped (until Step 4)
# ---------------------------------------------------------------------------


def test_layout_slots_are_skipped_in_manifest_and_preview(client: TestClient) -> None:
    headers = _admin_auth(client)
    layout = _create_layout(client, headers, name="Phase2Only")
    media_id = _upload_image(client, "legacy.png", b"legacy")

    # Register a device and give it a schedule mixing a legacy media
    # slot and a layout slot.
    reg = client.post("/api/register", json={"mac_address": "de:ad:be:ef:00:01"}).json()
    token = reg["api_token"]

    sched = client.post(
        "/api/schedules",
        headers=headers,
        json={
            "name": "Mixed",
            "items": [
                {"media_id": media_id, "order": 0, "duration_override": 4},
                {"layout_id": layout["id"], "order": 1, "duration_override": 10},
            ],
        },
    )
    assert sched.status_code == 201, sched.text
    schedule_id = sched.json()["id"]

    client.patch(
        f"/api/devices/{reg['id']}",
        headers=headers,
        json={"status": "active", "current_schedule_id": schedule_id},
    )

    manifest = client.get(
        f"/api/schedule/{reg['id']}", headers=_device_auth(token)
    ).json()
    # Only the legacy media slot made it into the manifest. The Layout
    # slot is silently skipped because Step 4 has not landed yet.
    assert len(manifest["items"]) == 1
    assert manifest["items"][0]["media_id"] == media_id

    preview = client.get(
        f"/api/schedules/{schedule_id}/preview", headers=headers
    ).json()
    assert len(preview["items"]) == 1
    assert preview["items"][0]["media_id"] == media_id


# ---------------------------------------------------------------------------
# PR #9 regression still works with mixed items
# ---------------------------------------------------------------------------


def test_patch_schedule_with_mixed_items_is_safe(client: TestClient) -> None:
    """The cascade-fix from PR #9 has to cope with ScheduleItem rows
    that carry a layout_id instead of a media_id. Repeatedly PATCHing
    such a schedule must not 500."""
    headers = _admin_auth(client)
    layout = _create_layout(client, headers)
    m = _upload_image(client, "mix.png", b"mix")

    sched = client.post(
        "/api/schedules",
        headers=headers,
        json={
            "name": "Mix",
            "items": [{"media_id": m, "order": 0}, {"layout_id": layout["id"], "order": 1}],
        },
    )
    schedule_id = sched.json()["id"]

    for _ in range(3):
        resp = client.patch(
            f"/api/schedules/{schedule_id}",
            headers=headers,
            json={
                "items": [
                    {"layout_id": layout["id"], "order": 0, "duration_override": 15},
                    {"media_id": m, "order": 1, "duration_override": 3},
                ],
            },
        )
        assert resp.status_code == 200, resp.text
        items = resp.json()["items"]
        assert len(items) == 2
        assert items[0]["layout_id"] == layout["id"]
        assert items[0]["media_id"] is None
        assert items[1]["media_id"] == m
        assert items[1]["layout_id"] is None


# ---------------------------------------------------------------------------
# Legacy SQLite migration
# ---------------------------------------------------------------------------


def test_migration_upgrades_legacy_sqlite_schema(tmp_path, monkeypatch) -> None:
    """Simulate a pre-Phase-2 database and check the boot-time migration
    (a) adds the ``layout_id`` column and (b) drops the NOT NULL on
    ``media_id``.

    We hand-craft the legacy ``scheduleitem`` table (no layout_id,
    media_id NOT NULL) then import + reload the server modules so
    ``init_db`` runs against it.
    """
    import importlib

    from sqlalchemy import create_engine, inspect, text

    db_file = tmp_path / "legacy.db"

    # 1. Build a legacy schema. We only need scheduleitem with the old
    #    shape — every other table can be empty / absent.
    legacy = create_engine(f"sqlite:///{db_file}", future=True)
    with legacy.begin() as conn:
        conn.execute(
            text(
                'CREATE TABLE "scheduleitem" ('
                '  "id" INTEGER PRIMARY KEY AUTOINCREMENT,'
                '  "schedule_id" INTEGER NOT NULL,'
                '  "media_id" INTEGER NOT NULL,'
                '  "order" INTEGER NOT NULL DEFAULT 0,'
                '  "duration_override" INTEGER'
                ')'
            )
        )
    legacy.dispose()

    # 2. Point the server at that DB and force a fresh import so
    #    ``init_db`` is what we test.
    monkeypatch.setenv("SCREENVIEW_DATABASE_URL", f"sqlite:///{db_file}")
    monkeypatch.setenv("SCREENVIEW_UPLOAD_DIR", str(tmp_path / "uploads"))
    (tmp_path / "uploads").mkdir()

    import server.config as config_mod

    importlib.reload(config_mod)
    import server.database as db_mod

    importlib.reload(db_mod)

    db_mod.init_db()

    # 3. Inspect: layout_id column exists + media_id is nullable.
    insp = inspect(db_mod.engine)
    cols = {c["name"]: c for c in insp.get_columns("scheduleitem")}
    assert "layout_id" in cols, "migration did not add layout_id"
    assert cols["media_id"].get("nullable", True), (
        "migration did not relax media_id NOT NULL — inserting a Layout "
        "slot (media_id NULL) would fail on legacy DBs."
    )
    # Idempotence: running init_db a second time is a no-op.
    db_mod.init_db()
