"""Smoke tests for the REST API.

Run with: `pytest server/tests` (after `pip install pytest httpx`).
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    uploads = tmp_path / "uploads"
    uploads.mkdir()

    monkeypatch.setenv("SCREENVIEW_DATABASE_URL", f"sqlite:///{db_file}")
    monkeypatch.setenv("SCREENVIEW_UPLOAD_DIR", str(uploads))
    monkeypatch.setenv("SCREENVIEW_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("SCREENVIEW_ADMIN_PASSWORD", "admin")

    # Force a fresh import of settings/db for this test.
    import importlib

    import server.config as config_mod

    importlib.reload(config_mod)

    import server.database as db_mod

    importlib.reload(db_mod)

    import server.main as main_mod

    importlib.reload(main_mod)

    with TestClient(main_mod.app) as c:
        yield c


def _auth(client: TestClient) -> dict[str, str]:
    resp = client.post("/api/auth/login-json", json={"username": "admin", "password": "admin"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def test_health(client: TestClient) -> None:
    assert client.get("/api/health").status_code == 200


def test_device_registration_flow(client: TestClient) -> None:
    resp = client.post("/api/register", json={"mac_address": "aa:bb:cc:dd:ee:ff"})
    assert resp.status_code == 201
    device = resp.json()
    assert device["status"] == "pending"

    dup = client.post("/api/register", json={"mac_address": "aa:bb:cc:dd:ee:ff"})
    assert dup.status_code == 201
    assert dup.json()["id"] == device["id"]

    headers = _auth(client)
    listed = client.get("/api/devices", headers=headers)
    assert listed.status_code == 200
    assert len(listed.json()) == 1


def test_media_and_schedule_manifest(client: TestClient) -> None:
    headers = _auth(client)

    reg = client.post("/api/register", json={"mac_address": "11:22:33:44:55:66"})
    device_id = reg.json()["id"]

    payload = b"fake-image-bytes"
    files = {"file": ("sample.png", payload, "image/png")}
    up = client.post("/api/media", headers=headers, files=files, data={"default_duration": "5"})
    assert up.status_code == 201
    media = up.json()
    assert media["size_bytes"] == len(payload)

    sched = client.post(
        "/api/schedules",
        headers=headers,
        json={
            "name": "Demo",
            "items": [{"media_id": media["id"], "order": 0, "duration_override": 7}],
        },
    )
    assert sched.status_code == 201, sched.text
    schedule_id = sched.json()["id"]

    assign = client.patch(
        f"/api/devices/{device_id}",
        headers=headers,
        json={"status": "active", "current_schedule_id": schedule_id},
    )
    assert assign.status_code == 200

    manifest = client.get(f"/api/schedule/{device_id}")
    assert manifest.status_code == 200
    data = manifest.json()
    assert data["schedule_id"] == schedule_id
    assert len(data["items"]) == 1
    assert data["items"][0]["duration"] == 7
    assert data["items"][0]["md5_hash"] == media["md5_hash"]


def test_manifest_unknown_device_returns_404(client: TestClient) -> None:
    """Players detect a stale device_id by looking for a 404 here."""
    bogus = "00000000-0000-0000-0000-000000000000"
    resp = client.get(f"/api/schedule/{bogus}")
    assert resp.status_code == 404


def test_websocket_unknown_device_closes_with_4404(client: TestClient) -> None:
    """The WS endpoint must accept the handshake then close with 4404
    (rather than rejecting the handshake with HTTP 403), so clients can
    reliably distinguish 'unknown device' from a generic auth failure
    behind a reverse proxy.
    """
    bogus = "00000000-0000-0000-0000-000000000000"
    with client.websocket_connect(f"/ws/player/{bogus}") as ws:
        with pytest.raises(Exception) as excinfo:  # noqa: PT011
            ws.receive_text()
        # starlette.websockets.WebSocketDisconnect surfaces the code here.
        code = getattr(excinfo.value, "code", None)
        assert code == 4404, f"Expected close code 4404, got {code!r}"
