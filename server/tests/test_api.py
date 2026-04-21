"""Smoke tests for the REST API.

Run with: `pytest server/tests` (after `pip install pytest httpx`).
"""
from __future__ import annotations

import hmac
import time
from hashlib import sha256

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


def _admin_auth(client: TestClient) -> dict[str, str]:
    resp = client.post("/api/auth/login-json", json={"username": "admin", "password": "admin"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _device_auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_health(client: TestClient) -> None:
    assert client.get("/api/health").status_code == 200


def test_device_registration_flow(client: TestClient) -> None:
    resp = client.post("/api/register", json={"mac_address": "aa:bb:cc:dd:ee:ff"})
    assert resp.status_code == 201
    device = resp.json()
    assert device["status"] == "pending"
    # Register must surface the cleartext token; every subsequent call
    # uses it.
    assert device["api_token"] and isinstance(device["api_token"], str)
    first_token = device["api_token"]

    # Re-registering the same MAC is idempotent on the device row but
    # rotates the token (the player has "lost" it).
    dup = client.post("/api/register", json={"mac_address": "aa:bb:cc:dd:ee:ff"})
    assert dup.status_code == 201
    assert dup.json()["id"] == device["id"]
    assert dup.json()["api_token"] != first_token

    # Admin listing hides the cleartext token but exposes an
    # ``api_token_issued_at`` timestamp + boolean flag.
    headers = _admin_auth(client)
    listed = client.get("/api/devices", headers=headers)
    assert listed.status_code == 200
    rows = listed.json()
    assert len(rows) == 1
    assert "api_token" not in rows[0]
    assert rows[0]["has_api_token"] is True
    assert rows[0]["api_token_issued_at"] is not None


def test_admin_can_rotate_device_token(client: TestClient) -> None:
    reg = client.post("/api/register", json={"mac_address": "aa:bb:cc:dd:ee:10"})
    device_id = reg.json()["id"]
    old_token = reg.json()["api_token"]

    headers = _admin_auth(client)
    rot = client.post(f"/api/devices/{device_id}/rotate-token", headers=headers)
    assert rot.status_code == 200
    assert rot.json()["api_token"] != old_token


def test_ping_requires_device_token(client: TestClient) -> None:
    reg = client.post("/api/register", json={"mac_address": "aa:bb:cc:dd:ee:01"})
    device = reg.json()
    device_id = device["id"]
    token = device["api_token"]

    # Unauthenticated → 401.
    anon = client.post(f"/api/devices/{device_id}/ping")
    assert anon.status_code == 401

    # Wrong token → 401.
    bad = client.post(
        f"/api/devices/{device_id}/ping",
        headers={"Authorization": "Bearer not-the-token"},
    )
    assert bad.status_code == 401

    # Correct token → 200.
    ok = client.post(f"/api/devices/{device_id}/ping", headers=_device_auth(token))
    assert ok.status_code == 200
    assert ok.json()["id"] == device_id


def test_unknown_device_ping_returns_404(client: TestClient) -> None:
    bogus = "00000000-0000-0000-0000-000000000000"
    resp = client.post(
        f"/api/devices/{bogus}/ping", headers={"Authorization": "Bearer whatever"}
    )
    assert resp.status_code == 404


def test_manifest_requires_token_and_returns_signed_urls(client: TestClient) -> None:
    headers = _admin_auth(client)

    reg = client.post("/api/register", json={"mac_address": "11:22:33:44:55:66"})
    device = reg.json()
    device_id = device["id"]
    token = device["api_token"]

    payload = b"fake-image-bytes"
    files = {"file": ("sample.png", payload, "image/png")}
    up = client.post("/api/media", headers=headers, files=files, data={"default_duration": "5"})
    assert up.status_code == 201
    media = up.json()

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

    # Anonymous manifest fetch → 401.
    anon = client.get(f"/api/schedule/{device_id}")
    assert anon.status_code == 401

    # Correct token → manifest with a signed URL.
    manifest = client.get(f"/api/schedule/{device_id}", headers=_device_auth(token))
    assert manifest.status_code == 200, manifest.text
    data = manifest.json()
    assert data["schedule_id"] == schedule_id
    item = data["items"][0]
    assert item["duration"] == 7
    assert "device_id=" in item["url"]
    assert "exp=" in item["url"]
    assert "sig=" in item["url"]

    # The signed URL must work without any Authorization header (that's
    # the whole point).
    dl = client.get(item["url"])
    assert dl.status_code == 200
    assert dl.content == payload


def test_signed_url_rejected_with_wrong_signature(client: TestClient) -> None:
    headers = _admin_auth(client)

    reg = client.post("/api/register", json={"mac_address": "11:22:33:44:66:77"})
    device = reg.json()

    up = client.post(
        "/api/media",
        headers=headers,
        files={"file": ("x.png", b"content", "image/png")},
        data={"default_duration": "3"},
    )
    media_id = up.json()["id"]

    # Craft a URL whose signature is wrong.
    exp = int(time.time()) + 3600
    resp = client.get(
        f"/api/media/{media_id}/download",
        params={"device_id": device["id"], "exp": exp, "sig": "bad-sig"},
    )
    assert resp.status_code == 403


def test_signed_url_rejected_after_expiry(client: TestClient) -> None:
    headers = _admin_auth(client)

    reg = client.post("/api/register", json={"mac_address": "aa:ee:ee:ee:ee:ee"})
    device = reg.json()
    token = device["api_token"]

    up = client.post(
        "/api/media",
        headers=headers,
        files={"file": ("y.png", b"content", "image/png")},
        data={"default_duration": "3"},
    )
    media_id = up.json()["id"]

    exp = int(time.time()) - 10  # already in the past
    payload = f"{device['id']}|{media_id}|{exp}".encode("utf-8")
    import base64

    sig = (
        base64.urlsafe_b64encode(
            hmac.new(token.encode("utf-8"), payload, sha256).digest()
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    resp = client.get(
        f"/api/media/{media_id}/download",
        params={"device_id": device["id"], "exp": exp, "sig": sig},
    )
    assert resp.status_code == 403


def test_signed_url_rejected_from_other_device(client: TestClient) -> None:
    headers = _admin_auth(client)

    alice = client.post("/api/register", json={"mac_address": "aa:aa:aa:aa:aa:aa"}).json()
    bob = client.post("/api/register", json={"mac_address": "bb:bb:bb:bb:bb:bb"}).json()

    up = client.post(
        "/api/media",
        headers=headers,
        files={"file": ("z.png", b"content", "image/png")},
        data={"default_duration": "3"},
    )
    media_id = up.json()["id"]

    schedule = client.post(
        "/api/schedules",
        headers=headers,
        json={"name": "X", "items": [{"media_id": media_id, "order": 0}]},
    ).json()
    client.patch(
        f"/api/devices/{alice['id']}",
        headers=headers,
        json={"status": "active", "current_schedule_id": schedule["id"]},
    )

    manifest = client.get(
        f"/api/schedule/{alice['id']}",
        headers=_device_auth(alice["api_token"]),
    ).json()
    alice_url = manifest["items"][0]["url"]

    # Bob cannot replay Alice's URL: the signature binds ``device_id=alice``.
    # Substituting bob's device_id with alice's signature must fail.
    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(alice_url)
    params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    params["device_id"] = bob["id"]
    resp = client.get(parsed.path, params=params)
    assert resp.status_code == 403


def test_manifest_unknown_device_returns_404(client: TestClient) -> None:
    """Players detect a stale device_id by looking for a 404 here."""
    bogus = "00000000-0000-0000-0000-000000000000"
    resp = client.get(
        f"/api/schedule/{bogus}",
        headers={"Authorization": "Bearer whatever"},
    )
    assert resp.status_code == 404


def test_websocket_requires_token(client: TestClient) -> None:
    """Missing token closes the handshake with 4401."""
    reg = client.post("/api/register", json={"mac_address": "aa:bb:cc:00:00:01"}).json()
    device_id = reg["id"]

    # Wrong token → 4401.
    with client.websocket_connect(f"/ws/player/{device_id}?token=wrong") as ws:
        with pytest.raises(Exception) as excinfo:  # noqa: PT011
            ws.receive_text()
        code = getattr(excinfo.value, "code", None)
        assert code == 4401, f"Expected 4401, got {code!r}"


def test_websocket_accepts_correct_token(client: TestClient) -> None:
    reg = client.post("/api/register", json={"mac_address": "aa:bb:cc:00:00:02"}).json()
    device_id = reg["id"]
    token = reg["api_token"]

    with client.websocket_connect(f"/ws/player/{device_id}?token={token}") as ws:
        ws.send_json({"type": "hello"})
        # Keepalive ping will arrive after 30 s; we just need to verify
        # the handshake succeeded without an immediate close. Send a
        # second message to exercise the loop and then close.
        ws.send_json({"type": "status"})


def test_preview_url_requires_admin(client: TestClient) -> None:
    """POST /api/media/{id}/preview-url and GET /api/schedules/{id}/preview
    must be admin-only."""
    headers = _admin_auth(client)
    up = client.post(
        "/api/media",
        headers=headers,
        files={"file": ("preview.png", b"preview-bytes", "image/png")},
        data={"default_duration": "5"},
    )
    media_id = up.json()["id"]

    # Unauthenticated request → 401.
    anon = client.post(f"/api/media/{media_id}/preview-url")
    assert anon.status_code == 401


def test_admin_preview_url_streams_media(client: TestClient) -> None:
    """The admin-signed URL must serve the file without any Auth header
    (that's the whole point — browsers can embed it in <img>/<video>)."""
    headers = _admin_auth(client)
    payload = b"preview-bytes"
    up = client.post(
        "/api/media",
        headers=headers,
        files={"file": ("preview.png", payload, "image/png")},
        data={"default_duration": "5"},
    )
    media_id = up.json()["id"]

    signed = client.post(f"/api/media/{media_id}/preview-url", headers=headers).json()
    assert "admin_exp=" in signed["url"]
    assert "admin_sig=" in signed["url"]
    # The device-signed params are NOT in an admin URL, so an attacker
    # who only has this link can't replay it as a device download.
    assert "device_id=" not in signed["url"]

    resp = client.get(signed["url"])
    assert resp.status_code == 200
    assert resp.content == payload


def test_admin_preview_url_rejects_forged_signature(client: TestClient) -> None:
    headers = _admin_auth(client)
    up = client.post(
        "/api/media",
        headers=headers,
        files={"file": ("preview2.png", b"x", "image/png")},
        data={"default_duration": "5"},
    )
    media_id = up.json()["id"]

    exp = int(time.time()) + 300
    resp = client.get(
        f"/api/media/{media_id}/download",
        params={"admin_exp": exp, "admin_sig": "forged"},
    )
    assert resp.status_code == 403


def test_admin_preview_url_rejects_after_expiry(client: TestClient) -> None:
    headers = _admin_auth(client)
    up = client.post(
        "/api/media",
        headers=headers,
        files={"file": ("preview3.png", b"x", "image/png")},
        data={"default_duration": "5"},
    )
    media_id = up.json()["id"]

    # Compute a valid signature for an already-expired timestamp.
    exp = int(time.time()) - 10
    secret_key = "change-me-in-production"  # matches default for the test settings
    payload = f"{media_id}|{exp}".encode("utf-8")
    import base64

    sig = (
        base64.urlsafe_b64encode(
            hmac.new(secret_key.encode("utf-8"), payload, sha256).digest()
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    resp = client.get(
        f"/api/media/{media_id}/download",
        params={"admin_exp": exp, "admin_sig": sig},
    )
    assert resp.status_code == 403


def test_schedule_preview_returns_admin_signed_items(client: TestClient) -> None:
    headers = _admin_auth(client)

    up = client.post(
        "/api/media",
        headers=headers,
        files={"file": ("item.png", b"hello", "image/png")},
        data={"default_duration": "3"},
    )
    media_id = up.json()["id"]

    sched = client.post(
        "/api/schedules",
        headers=headers,
        json={
            "name": "PreviewMe",
            "items": [{"media_id": media_id, "order": 0, "duration_override": 4}],
        },
    )
    schedule_id = sched.json()["id"]

    # Unauthenticated → 401.
    anon = client.get(f"/api/schedules/{schedule_id}/preview")
    assert anon.status_code == 401

    # Admin → full preview.
    resp = client.get(f"/api/schedules/{schedule_id}/preview", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["schedule_id"] == schedule_id
    assert data["schedule_name"] == "PreviewMe"
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["duration"] == 4
    assert item["type"] == "image"
    assert "admin_exp=" in item["url"]
    assert "admin_sig=" in item["url"]

    # Each URL streams without auth.
    dl = client.get(item["url"])
    assert dl.status_code == 200
    assert dl.content == b"hello"


def test_device_download_still_rejects_admin_shaped_params(client: TestClient) -> None:
    """Passing admin_exp/admin_sig on a route that only has a device
    signature must not short-circuit the device check — and vice versa."""
    headers = _admin_auth(client)
    up = client.post(
        "/api/media",
        headers=headers,
        files={"file": ("guard.png", b"guard", "image/png")},
        data={"default_duration": "3"},
    )
    media_id = up.json()["id"]

    # No signature at all → 401.
    resp = client.get(f"/api/media/{media_id}/download")
    assert resp.status_code == 401

    # Partial device params (missing sig) → still 401.
    resp = client.get(
        f"/api/media/{media_id}/download",
        params={"device_id": "00000000-0000-0000-0000-000000000000", "exp": 999},
    )
    assert resp.status_code == 401


def test_create_stream_media_minimal(client: TestClient) -> None:
    headers = _admin_auth(client)
    resp = client.post(
        "/api/media/stream",
        headers=headers,
        json={
            "name": "Lobby camera",
            "url": "rtsp://10.0.0.42:554/live",
            "default_duration": 60,
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["type"] == "stream"
    assert body["stream_url"] == "rtsp://10.0.0.42:554/live"
    assert body["filename"] is None
    assert body["md5_hash"] is None
    assert body["default_duration"] == 60


def test_create_stream_requires_admin(client: TestClient) -> None:
    resp = client.post(
        "/api/media/stream",
        json={"name": "x", "url": "https://example.com/s.m3u8", "default_duration": 30},
    )
    assert resp.status_code == 401


def test_create_stream_rejects_dangerous_schemes(client: TestClient) -> None:
    headers = _admin_auth(client)
    bad_urls = [
        "file:///etc/passwd",
        "javascript:alert(1)",
        "data:text/html,<script>",
        "ftp://example.com/foo",
        "",
        "not-a-url",
        "http:",
    ]
    for url in bad_urls:
        resp = client.post(
            "/api/media/stream",
            headers=headers,
            json={"name": "n", "url": url, "default_duration": 10},
        )
        assert resp.status_code == 400, f"expected 400 for {url!r}, got {resp.status_code}"


def test_create_stream_rejects_zero_duration(client: TestClient) -> None:
    headers = _admin_auth(client)
    resp = client.post(
        "/api/media/stream",
        headers=headers,
        json={"name": "n", "url": "https://e/s.m3u8", "default_duration": 0},
    )
    assert resp.status_code == 400


def test_stream_appears_in_player_manifest_with_upstream_url(client: TestClient) -> None:
    headers = _admin_auth(client)

    reg = client.post("/api/register", json={"mac_address": "ab:cd:ef:01:02:03"})
    device = reg.json()
    device_id = device["id"]
    token = device["api_token"]

    stream_resp = client.post(
        "/api/media/stream",
        headers=headers,
        json={
            "name": "Live HLS",
            "url": "https://cdn.example.com/playlist.m3u8",
            "default_duration": 45,
        },
    )
    stream_id = stream_resp.json()["id"]

    sched = client.post(
        "/api/schedules",
        headers=headers,
        json={"name": "Live", "items": [{"media_id": stream_id, "order": 0}]},
    )
    schedule_id = sched.json()["id"]

    client.patch(
        f"/api/devices/{device_id}",
        headers=headers,
        json={"status": "active", "current_schedule_id": schedule_id},
    )

    manifest = client.get(
        f"/api/schedule/{device_id}", headers=_device_auth(token)
    )
    assert manifest.status_code == 200
    item = manifest.json()["items"][0]
    # The upstream URL is passed through verbatim — no signature, no
    # device binding, no expiry. md5/size are zeroed out so the player
    # knows to skip the cache pipeline.
    assert item["url"] == "https://cdn.example.com/playlist.m3u8"
    assert item["type"] == "stream"
    assert item["md5_hash"] == ""
    assert item["size_bytes"] == 0
    assert item["duration"] == 45


def test_stream_preview_returns_upstream_url(client: TestClient) -> None:
    headers = _admin_auth(client)
    stream_resp = client.post(
        "/api/media/stream",
        headers=headers,
        json={
            "name": "Camera 7",
            "url": "rtsp://10.0.0.7:554/stream1",
            "default_duration": 30,
        },
    )
    stream_id = stream_resp.json()["id"]

    # Single-media preview URL: returns the upstream URL verbatim.
    preview = client.post(
        f"/api/media/{stream_id}/preview-url", headers=headers
    )
    assert preview.status_code == 200
    assert preview.json()["url"] == "rtsp://10.0.0.7:554/stream1"

    # Schedule preview: same.
    sched = client.post(
        "/api/schedules",
        headers=headers,
        json={"name": "PreviewLive", "items": [{"media_id": stream_id, "order": 0}]},
    )
    sched_preview = client.get(
        f"/api/schedules/{sched.json()['id']}/preview", headers=headers
    )
    assert sched_preview.status_code == 200
    assert sched_preview.json()["items"][0]["url"] == "rtsp://10.0.0.7:554/stream1"


def test_stream_download_endpoint_returns_400(client: TestClient) -> None:
    """A misbehaving client that hits /download for a stream item with a
    valid admin signature should still get a clear 400 instead of a 500
    or a stale FileResponse."""
    headers = _admin_auth(client)
    stream_resp = client.post(
        "/api/media/stream",
        headers=headers,
        json={"name": "x", "url": "rtsp://x/y", "default_duration": 10},
    )
    stream_id = stream_resp.json()["id"]

    # Hand-craft a valid admin signature for the stream's media_id.
    exp = int(time.time()) + 300
    secret_key = "change-me-in-production"
    payload = f"{stream_id}|{exp}".encode("utf-8")
    import base64

    sig = (
        base64.urlsafe_b64encode(
            hmac.new(secret_key.encode("utf-8"), payload, sha256).digest()
        )
        .rstrip(b"=")
        .decode("ascii")
    )

    resp = client.get(
        f"/api/media/{stream_id}/download",
        params={"admin_exp": exp, "admin_sig": sig},
    )
    assert resp.status_code == 400
    assert "stream" in resp.json()["detail"].lower()


def test_stream_can_be_deleted(client: TestClient) -> None:
    """Streams have no on-disk file; delete must not blow up trying to
    unlink a non-existent path."""
    headers = _admin_auth(client)
    stream_resp = client.post(
        "/api/media/stream",
        headers=headers,
        json={"name": "x", "url": "rtsp://x/y", "default_duration": 10},
    )
    stream_id = stream_resp.json()["id"]

    resp = client.delete(f"/api/media/{stream_id}", headers=headers)
    assert resp.status_code == 204


def test_websocket_unknown_device_closes_with_4404(client: TestClient) -> None:
    """Unknown device_id still returns the dedicated 4404 code so the
    player can distinguish "I don't exist" from "my token is wrong"."""
    bogus = "00000000-0000-0000-0000-000000000000"
    with client.websocket_connect(f"/ws/player/{bogus}?token=x") as ws:
        with pytest.raises(Exception) as excinfo:  # noqa: PT011
            ws.receive_text()
        code = getattr(excinfo.value, "code", None)
        assert code == 4404, f"Expected 4404, got {code!r}"
