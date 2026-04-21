"""Network worker thread for the ScreenView Windows player.

Responsibilities (all strictly off the UI thread):

  * Register the device on first launch.
  * Maintain a persistent WebSocket connection for real-time signalling
    (ping/pong + `sync_required` triggers).
  * On request, pull the manifest from `GET /api/schedule/{device_id}`, diff
    it against the local cache, download missing media, verify MD5 hashes,
    and finally emit `playlist_ready` to the UI thread.
  * Self-heal when the server has forgotten this device (e.g. SQLite DB
    reset, or device deleted in the CMS). Both the manifest endpoint
    (`404`) and the WebSocket (`403`/close code `4404`) are treated as
    "stale credentials": the worker clears ``device_id`` from its local
    config and re-registers from scratch.

Communication with the UI thread goes through PyQt signals only; no direct
attribute access, no shared mutable state.

This is a Windows-adapted port of ``player-linux/worker_network.py``. The
core protocol and thread contract are identical; only the cache path and
config class differ.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
import websocket  # type: ignore[import-untyped]
from PyQt6.QtCore import QObject, QThread, pyqtSignal

from config import PlayerConfig
from hardware import get_hardware_id, get_mac_address

logger = logging.getLogger(__name__)

# Close codes that the server emits when a device is unknown. See
# server/routers/websocket.py (``close(code=4404)``) and the
# ``REDIRECT_STATUS_CODE`` that Starlette returns when a WS is closed
# before ``accept()`` (HTTP 403 on the handshake).
# Server-emitted WebSocket close codes that mean "your credentials are
# stale; re-register". The handler in ``server/routers/websocket.py``
# emits 4404 for an unknown device_id and 4401 for a rejected token.
WS_UNKNOWN_DEVICE_CLOSE_CODES = (4404, 4401)

# Upper bound for the WebSocket reconnect backoff, in seconds. Prevents
# the player from spamming the server (and the log file) when the
# network is unreachable or the server keeps closing the handshake.
WS_BACKOFF_MAX = 300

# Minimum backoff. Starts here and doubles on every failure until
# WS_BACKOFF_MAX, reset on every successful connection.
WS_BACKOFF_MIN = 5


@dataclass
class PlaylistEntry:
    """Resolved playlist item pointing at a locally cached file."""

    media_id: int
    kind: str  # 'video' | 'image' | 'widget'
    path: Path
    duration: int
    original_name: str


class _StaleDeviceError(RuntimeError):
    """Raised when the server returns 404/403 for a known device_id.

    Triggers a full re-registration cycle in the worker.
    """


class NetworkWorker(QObject):
    """Runs inside a QThread; orchestrates all I/O."""

    registered = pyqtSignal(str)  # device_id
    status_changed = pyqtSignal(str, str)  # level, message
    playlist_ready = pyqtSignal(list)  # list[PlaylistEntry]
    sync_progress = pyqtSignal(int, int)  # done, total

    def __init__(self, config: PlayerConfig) -> None:
        super().__init__()
        self._config = config
        self._session = requests.Session()
        self._ws: websocket.WebSocketApp | None = None
        self._running = True
        self._cache_dir = config.cache_path
        # Track the last WS error message so we don't spam identical lines
        # at the UI every few seconds during a long outage.
        self._last_ws_error: str | None = None
        # Flag set from WS callbacks; the outer reconnect loop consumes it
        # to decide whether to wipe the local device_id and re-register.
        self._ws_requires_reregister = False
        # Tame the websocket-client library's own logging, which dumps the
        # full HTTP response headers on every handshake failure.
        logging.getLogger("websocket").setLevel(logging.CRITICAL)

    # ----- lifecycle -----------------------------------------------------

    def start(self) -> None:
        """Entry point — runs on the worker thread.

        The top-level loop allows us to restart the whole registration +
        sync + WS cycle after a forced re-registration without unwinding
        back to Qt's thread machinery.
        """
        while self._running:
            try:
                self._ensure_registered()
                self._sync()
                self._run_ws_loop()
            except _StaleDeviceError:
                self._forget_device("server no longer recognises this device")
                continue
            except Exception as exc:  # noqa: BLE001
                logger.exception("Worker crashed: %s", exc)
                self.status_changed.emit("error", f"Worker crashed: {exc}")
                # Don't tight-loop on programming errors.
                time.sleep(5)
            # Normal exit path (stop requested) — leave the loop.
            if not self._running:
                break

    def stop(self) -> None:
        self._running = False
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:  # noqa: BLE001
                pass

    # ----- registration --------------------------------------------------

    def _forget_device(self, reason: str) -> None:
        logger.warning("Forgetting stored device_id: %s", reason)
        self.status_changed.emit(
            "warn", f"Re-registering device: {reason}"
        )
        self._config.device_id = None
        self._config.device_name = None
        self._config.api_token = None
        try:
            self._config.save()
        except OSError as exc:
            logger.warning("Could not persist cleared config: %s", exc)

    def _auth_headers(self) -> dict[str, str]:
        """Return the Authorization header for the current device token.

        Kept tiny so we can include it on every outgoing REST call without
        worrying about ``requests.Session.auth`` semantics interacting
        badly with the signed download URLs (which carry their own
        ``?device_id&exp&sig`` credentials).
        """
        if not self._config.api_token:
            return {}
        return {"Authorization": f"Bearer {self._config.api_token}"}

    def _ensure_registered(self) -> None:
        if self._config.device_id and self._config.api_token:
            self.status_changed.emit("info", f"Device ID: {self._config.device_id}")
            self.registered.emit(self._config.device_id)
            return

        # Missing either id or token ⇒ re-register from scratch. Registration
        # on a known MAC is idempotent and rotates the token.
        if self._config.device_id and not self._config.api_token:
            logger.info(
                "Have device_id but no api_token; re-registering to acquire one."
            )
            self._config.device_id = None
            self._config.device_name = None

        mac = get_mac_address()
        hw_id = get_hardware_id()
        payload = {"mac_address": mac, "hardware_id": hw_id}
        self.status_changed.emit("info", "Registering device…")
        while self._running:
            try:
                resp = self._session.post(
                    f"{self._config.server_url}/api/register", json=payload, timeout=10
                )
                resp.raise_for_status()
                data = resp.json()
                self._config.device_id = data["id"]
                self._config.device_name = data.get("name")
                token = data.get("api_token")
                if not token:
                    raise RuntimeError(
                        "Server did not return an api_token. Upgrade the "
                        "CMS to a build that supports per-device tokens."
                    )
                self._config.api_token = token
                self._config.save()
                self.status_changed.emit("info", f"Registered as {data['name']}")
                self.registered.emit(self._config.device_id)
                return
            except Exception as exc:  # noqa: BLE001
                self.status_changed.emit(
                    "warn", f"Registration failed ({exc}); retrying in 5s"
                )
                time.sleep(self._config.reconnect_delay_seconds)

    # ----- synchronisation ----------------------------------------------

    def _sync(self) -> None:
        if not self._config.device_id:
            return
        try:
            resp = self._session.get(
                f"{self._config.server_url}/api/schedule/{self._config.device_id}",
                headers=self._auth_headers(),
                timeout=15,
            )
        except requests.RequestException as exc:
            self.status_changed.emit("warn", f"Manifest fetch failed: {exc}")
            return

        if resp.status_code in (401, 403, 404):
            # 401 ⇒ token rejected (probably rotated in the CMS).
            # 403 ⇒ legacy server, still means we shouldn't be here.
            # 404 ⇒ device row deleted.
            # Every case implies "drop credentials and re-register".
            raise _StaleDeviceError(
                f"manifest endpoint returned {resp.status_code} for "
                f"device {self._config.device_id}"
            )

        try:
            resp.raise_for_status()
            manifest: dict[str, Any] = resp.json()
        except (requests.RequestException, ValueError) as exc:
            self.status_changed.emit("warn", f"Manifest fetch failed: {exc}")
            return

        items = manifest.get("items") or []
        if not items:
            self.status_changed.emit("info", "No schedule assigned yet.")
            self.playlist_ready.emit([])
            return

        self.status_changed.emit("info", f"Syncing {len(items)} item(s)…")
        resolved: list[PlaylistEntry] = []
        total = len(items)
        for idx, item in enumerate(items):
            try:
                local_path = self._ensure_cached(item)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Failed to cache %s", item)
                self.status_changed.emit(
                    "warn", f"Failed to cache {item.get('original_name')}: {exc}"
                )
                # Abort the whole sync; a partial playlist would be worse than
                # keeping the last known-good one cached on the UI side.
                return
            resolved.append(
                PlaylistEntry(
                    media_id=item["media_id"],
                    kind=item["type"],
                    path=local_path,
                    duration=int(item.get("duration") or 10),
                    original_name=item.get("original_name") or local_path.name,
                )
            )
            self.sync_progress.emit(idx + 1, total)

        self._cleanup_cache(keep={e.path.name for e in resolved})
        self.status_changed.emit("info", "Sync complete — swapping playlist.")
        self.playlist_ready.emit(resolved)

    def _ensure_cached(self, item: dict[str, Any]) -> Path:
        md5 = item["md5_hash"]
        filename = f"{md5}{Path(item.get('original_name', '')).suffix or ''}"
        dest = self._cache_dir / filename
        if dest.exists() and _md5(dest) == md5:
            return dest

        url = item["url"]
        self.status_changed.emit("info", f"Downloading {item.get('original_name')}")
        tmp = dest.with_suffix(dest.suffix + ".part")
        with self._session.get(url, stream=True, timeout=60) as resp:
            resp.raise_for_status()
            with tmp.open("wb") as fh:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        fh.write(chunk)
        actual = _md5(tmp)
        if actual != md5:
            tmp.unlink(missing_ok=True)
            raise ValueError(f"Checksum mismatch for {url}: {actual} != {md5}")
        # os.replace is atomic on the same volume (NTFS on Windows, ext* on Linux).
        tmp.replace(dest)
        return dest

    def _cleanup_cache(self, keep: set[str]) -> None:
        for entry in self._cache_dir.iterdir():
            if entry.is_file() and entry.name not in keep and not entry.name.endswith(".part"):
                try:
                    entry.unlink()
                except OSError:
                    pass

    # ----- WebSocket -----------------------------------------------------

    def _run_ws_loop(self) -> None:
        if not self._config.device_id or not self._config.api_token:
            return
        # Token passed on the query string because the WebSocket handshake
        # cannot carry custom headers from many clients. Safe over TLS.
        from urllib.parse import quote

        ws_url = (
            f"{self._config.ws_url}/ws/player/{self._config.device_id}"
            f"?token={quote(self._config.api_token, safe='')}"
        )
        backoff = WS_BACKOFF_MIN
        self._ws_requires_reregister = False

        def on_open(ws: websocket.WebSocket) -> None:
            nonlocal backoff
            backoff = WS_BACKOFF_MIN
            self._last_ws_error = None
            ws.send(json.dumps({"type": "hello"}))
            self.status_changed.emit("info", "Connected to server.")

        def on_message(_ws: websocket.WebSocket, raw: str) -> None:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                return
            if msg.get("type") == "ping":
                _ws.send(json.dumps({"type": "pong"}))
                return
            if msg.get("action") == "sync_required":
                try:
                    self._sync()
                except _StaleDeviceError:
                    # Signal the outer loop to restart; closing the WS
                    # unblocks ``run_forever``.
                    self._ws_requires_reregister = True
                    _ws.close()

        def on_error(_ws: websocket.WebSocket, err: Exception) -> None:
            msg = _compact_ws_error(err)
            if msg != self._last_ws_error:
                self._last_ws_error = msg
                self.status_changed.emit("warn", f"Server link: {msg}")
            if _is_unknown_device_error(err):
                self._ws_requires_reregister = True

        def on_close(ws: websocket.WebSocket, close_code: int | None, reason: str | None) -> None:
            if close_code in WS_UNKNOWN_DEVICE_CLOSE_CODES:
                self._ws_requires_reregister = True

        while self._running:
            self._ws_requires_reregister = False
            try:
                self._ws = websocket.WebSocketApp(
                    ws_url,
                    on_open=on_open,
                    on_message=on_message,
                    on_error=on_error,
                    on_close=on_close,
                )
                self._ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as exc:  # noqa: BLE001
                msg = _compact_ws_error(exc)
                if msg != self._last_ws_error:
                    self._last_ws_error = msg
                    self.status_changed.emit("warn", f"WS loop exception: {msg}")
                if _is_unknown_device_error(exc):
                    self._ws_requires_reregister = True

            if self._ws_requires_reregister:
                raise _StaleDeviceError(
                    "WebSocket handshake rejected; server does not know this device"
                )

            if not self._running:
                return
            # Exponential backoff, capped. Reset to WS_BACKOFF_MIN on successful
            # open (see on_open).
            time.sleep(min(backoff, WS_BACKOFF_MAX))
            backoff = min(backoff * 2, WS_BACKOFF_MAX)


def start_in_thread(config: PlayerConfig) -> tuple[QThread, NetworkWorker]:
    """Convenience helper: wire up the worker on a dedicated QThread."""
    thread = QThread()
    worker = NetworkWorker(config)
    worker.moveToThread(thread)
    thread.started.connect(worker.start)
    return thread, worker


def _md5(path: Path) -> str:
    md5 = hashlib.md5()  # noqa: S324  # integrity only, not security
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            md5.update(chunk)
    return md5.hexdigest()


def _compact_ws_error(err: Exception | str) -> str:
    """Shorten websocket-client error messages for end-user logs.

    The library's handshake error looks like
    ``Handshake status 403 Forbidden -+-+- {headers...} -+-+- b''``.
    Trim everything after the first ``-+-+-`` so the status code is
    still visible but the headers don't flood the log.
    """
    text = str(err)
    marker = "-+-+-"
    if marker in text:
        text = text.split(marker, 1)[0].strip()
    return text or err.__class__.__name__


def _is_unknown_device_error(err: Exception | str) -> bool:
    """Best-effort detector for "server won't talk to us with these credentials".

    Covers:
      * HTTP 403 on the handshake — old server before PR #5 used to close
        the socket before ``accept()``, which Starlette served as plain 403.
      * Close code 4404 — server says the device_id is unknown.
      * Close code 4401 — server says the token is wrong (rotated from CMS).
    All three call for a re-registration cycle.
    """
    text = str(err)
    if "403" in text and "Handshake" in text:
        return True
    if "4404" in text or "4401" in text:
        return True
    return False
