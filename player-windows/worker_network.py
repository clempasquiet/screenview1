"""Network worker thread for the ScreenView Windows player.

Responsibilities (all strictly off the UI thread):

  * Register the device on first launch.
  * Maintain a persistent WebSocket connection for real-time signalling
    (ping/pong + `sync_required` triggers).
  * On request, pull the manifest from `GET /api/schedule/{device_id}`, diff
    it against the local cache, download missing media, verify MD5 hashes,
    and finally emit `playlist_ready` to the UI thread.

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


@dataclass
class PlaylistEntry:
    """Resolved playlist item pointing at a locally cached file."""

    media_id: int
    kind: str  # 'video' | 'image' | 'widget'
    path: Path
    duration: int
    original_name: str


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

    # ----- lifecycle -----------------------------------------------------

    def start(self) -> None:
        """Entry point — runs on the worker thread."""
        try:
            self._ensure_registered()
            self._sync()
            self._run_ws_loop()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Worker crashed: %s", exc)
            self.status_changed.emit("error", f"Worker crashed: {exc}")

    def stop(self) -> None:
        self._running = False
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:  # noqa: BLE001
                pass

    # ----- registration --------------------------------------------------

    def _ensure_registered(self) -> None:
        if self._config.device_id:
            self.status_changed.emit("info", f"Device ID: {self._config.device_id}")
            self.registered.emit(self._config.device_id)
            return

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
                timeout=15,
            )
            resp.raise_for_status()
            manifest: dict[str, Any] = resp.json()
        except Exception as exc:  # noqa: BLE001
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
        if not self._config.device_id:
            return
        ws_url = f"{self._config.ws_url}/ws/player/{self._config.device_id}"

        def on_open(ws: websocket.WebSocket) -> None:
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
                self._sync()

        def on_error(_ws: websocket.WebSocket, err: Exception) -> None:
            self.status_changed.emit("warn", f"WS error: {err}")

        def on_close(*_a: Any) -> None:
            self.status_changed.emit("warn", "Server connection lost.")

        while self._running:
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
                self.status_changed.emit("warn", f"WS loop exception: {exc}")
            if self._running:
                time.sleep(self._config.reconnect_delay_seconds)


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
