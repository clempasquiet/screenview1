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
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import requests
import websocket  # type: ignore[import-untyped]
from PyQt6.QtCore import QObject, QThread, pyqtSignal

from config import APP_DIR, PlayerConfig
from hardware import get_hardware_id, get_mac_address

logger = logging.getLogger(__name__)

# Exported so downstream helpers (e.g. future ``mpv.conf`` or script
# lookups) can reach the application's install directory without
# relying on ``os.getcwd()`` — Task Scheduler sets CWD to System32.
_ = APP_DIR  # re-export for modules that import it from here

# Close codes that the server emits when a device is unknown. See
# server/routers/websocket.py (``close(code=4404)``) and the
# ``REDIRECT_STATUS_CODE`` that Starlette returns when a WS is closed
# before ``accept()`` (HTTP 403 on the handshake).
# Server-emitted WebSocket close codes that mean "your credentials are
# stale; re-register". The handler in ``server/routers/websocket.py``
# emits 4404 for an unknown device_id and 4401 for a rejected token.
WS_UNKNOWN_DEVICE_CLOSE_CODES = (4404, 4401)

# Exponential backoff for the WebSocket reconnect loop, in seconds.
# Schedule: 2 → 4 → 8 → 16 → 30 (capped). Each failure doubles the
# previous delay up to ``WS_BACKOFF_MAX``. A successful ``on_open``
# resets the backoff back to ``WS_BACKOFF_MIN``.
#
# A cheap ±20% jitter (``WS_BACKOFF_JITTER``) is layered on top so a
# fleet of players coming back online at the same time (after a power
# cut, for example) doesn't all reconnect on the exact same beat and
# hammer the server's accept loop.
WS_BACKOFF_MIN = 2
WS_BACKOFF_MAX = 30
WS_BACKOFF_JITTER = 0.2  # fraction of the nominal delay


@dataclass
class ZoneItem:
    """One media item attached to a Zone after cache resolution.

    For ``kind in {'video', 'image', 'widget'}`` ``path`` points at a
    locally cached, MD5-validated file. For ``kind == 'stream'``
    there is no local copy: ``path`` is ``None`` and ``stream_url``
    carries the upstream URL that libmpv opens directly.
    """

    media_id: int
    kind: str  # 'video' | 'image' | 'widget' | 'stream'
    path: Optional[Path]
    duration: int
    original_name: str
    order: int = 0
    mime_type: Optional[str] = None
    stream_url: Optional[str] = None

    @property
    def is_stream(self) -> bool:
        return self.kind == "stream"


@dataclass
class Zone:
    """A rectangular region inside a Slide's layout.

    Geometry is in the layout's authoring coordinate space (pixels).
    The player scales the whole layout to its actual screen size
    with ``object-fit: contain`` semantics, so the same layout
    renders identically on 1080p and 4K displays (with letter-
    or pillar-boxing where aspect ratios differ).
    """

    zone_id: Optional[int]
    name: str
    position_x: int
    position_y: int
    width: int
    height: int
    z_index: int
    items: list[ZoneItem]


@dataclass
class Slide:
    """One slot in the device's schedule: a layout + its total
    on-screen duration.

    ``slide_id`` is a stable (across manifest refreshes) string the
    worker + UI can use for logging / diffing. ``duration`` is the
    TOTAL time the slide stays on screen, which replaces the
    per-entry duration of the pre-Phase-2 flat manifest.

    Legacy single-media slots show up here as a synthetic layout
    with ``layout_id=None`` and a single full-canvas zone. The UI
    does not need to special-case them.
    """

    slide_id: str
    order: int
    duration: int
    layout_id: Optional[int]
    layout_name: str
    resolution_w: int
    resolution_h: int
    zones: list[Zone]

    # ---- convenience predicates used by the UI dispatcher ----

    @property
    def has_video(self) -> bool:
        """True if any zone has a video or stream item in its playlist."""
        return any(
            any(i.kind in ("video", "stream") for i in z.items) for z in self.zones
        )

    def first_video_item(self) -> Optional[ZoneItem]:
        """Return the first video/stream ZoneItem encountered walking
        zones in layout (z_index) order. Used by the UI when a mixed
        layout has to fall back to fullscreen-video mode."""
        for zone in self.zones:
            for item in zone.items:
                if item.kind in ("video", "stream"):
                    return item
        return None


class _StaleDeviceError(RuntimeError):
    """Raised when the server returns 404/403 for a known device_id.

    Triggers a full re-registration cycle in the worker.
    """


class _CacheError(RuntimeError):
    """Raised when caching (download + MD5 verify) of a single item
    fails and the sync should be aborted.

    We abort the whole sync rather than emitting a partial playlist
    so the UI keeps playing its last known-good tree rather than
    flickering through half-resolved slides.
    """


@dataclass
class _Progress:
    """Tiny mutable counter + emitter used by ``_sync`` to report
    per-item progress without passing awkward ref-containers around.
    """

    done: int
    total: int
    emit: Any  # ``pyqtBoundSignal.emit`` — duck-typed for testability

    def tick(self) -> None:
        self.done += 1
        self.emit(self.done, self.total)


class NetworkWorker(QObject):
    """Runs inside a QThread; orchestrates all I/O."""

    registered = pyqtSignal(str)  # device_id
    status_changed = pyqtSignal(str, str)  # level, message
    playlist_ready = pyqtSignal(list)  # list[Slide]
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

        slides_raw = manifest.get("slides") or []
        if not slides_raw:
            self.status_changed.emit("info", "No schedule assigned yet.")
            self.playlist_ready.emit([])
            return

        # Count every media in every zone of every slide so the
        # progress bar reflects the real cache work, not just the
        # slide count.
        total_items = sum(
            len(zone.get("items") or [])
            for slide in slides_raw
            for zone in (slide.get("layout", {}).get("zones") or [])
        )
        self.status_changed.emit(
            "info",
            f"Syncing {len(slides_raw)} slide(s), {total_items} media item(s)…",
        )

        resolved_slides: list[Slide] = []
        cached_names: set[str] = set()
        progress = _Progress(done=0, total=total_items, emit=self.sync_progress.emit)

        try:
            for slide_raw in slides_raw:
                slide = self._resolve_slide(slide_raw, progress=progress)
                resolved_slides.append(slide)
                for zone in slide.zones:
                    for item in zone.items:
                        if item.path is not None:
                            cached_names.add(item.path.name)
        except _CacheError as exc:
            # _resolve_slide raises this on any cache failure. Abort
            # the whole sync so we never emit a partial playlist.
            logger.exception("Caching aborted: %s", exc)
            self.status_changed.emit(
                "warn",
                f"Sync aborted: {exc}. Keeping the previous playlist.",
            )
            return

        # Every file-backed ZoneItem across the whole tree must be kept.
        # Stream items have no on-disk file.
        self._cleanup_cache(keep=cached_names)
        self.status_changed.emit("info", "Sync complete — swapping playlist.")
        self.playlist_ready.emit(resolved_slides)

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

    def _resolve_zone_item(
        self,
        raw: dict[str, Any],
        progress: _Progress,
    ) -> ZoneItem | None:
        """Download + MD5-verify a single zone item, return it resolved.

        Streams bypass the cache pipeline (the upstream URL is carried
        through verbatim). Any failure raises :class:`_CacheError` so
        the caller can abort the whole sync cleanly.

        Returns ``None`` and logs a warning for items whose manifest
        entry is malformed (e.g. ``type="stream"`` with no URL); the
        slide keeps going without that one item.
        """
        kind = raw.get("type")
        media_id = raw.get("media_id")
        if media_id is None:
            logger.warning("Zone item missing media_id; skipping: %s", raw)
            return None

        if kind == "stream":
            stream_url = raw.get("url")
            if not stream_url:
                self.status_changed.emit(
                    "warn", f"Stream item missing URL: {raw.get('original_name')}"
                )
                progress.tick()
                return None
            progress.tick()
            return ZoneItem(
                media_id=int(media_id),
                kind="stream",
                path=None,
                duration=int(raw.get("duration") or 30),
                original_name=raw.get("original_name") or "Live stream",
                order=int(raw.get("order") or 0),
                mime_type=raw.get("mime_type"),
                stream_url=stream_url,
            )

        try:
            local_path = self._ensure_cached(raw)
        except Exception as exc:  # noqa: BLE001
            raise _CacheError(
                f"failed to cache {raw.get('original_name')!r} (media_id={media_id}): {exc}"
            ) from exc

        progress.tick()
        return ZoneItem(
            media_id=int(media_id),
            kind=str(kind or "image"),
            path=local_path,
            duration=int(raw.get("duration") or 10),
            original_name=raw.get("original_name") or local_path.name,
            order=int(raw.get("order") or 0),
            mime_type=raw.get("mime_type"),
        )

    def _resolve_zone(self, raw: dict[str, Any], progress: _Progress) -> Zone:
        """Materialise one zone: geometry + resolved items."""
        items_raw = raw.get("items") or []
        resolved_items: list[ZoneItem] = []
        for item_raw in items_raw:
            item = self._resolve_zone_item(item_raw, progress)
            if item is not None:
                resolved_items.append(item)
        return Zone(
            zone_id=raw.get("zone_id"),
            name=str(raw.get("name") or "Zone"),
            position_x=int(raw.get("position_x") or 0),
            position_y=int(raw.get("position_y") or 0),
            width=int(raw.get("width") or 1920),
            height=int(raw.get("height") or 1080),
            z_index=int(raw.get("z_index") or 0),
            items=resolved_items,
        )

    def _resolve_slide(self, raw: dict[str, Any], progress: _Progress) -> Slide:
        """Materialise one slide (layout + zones + items).

        Every zone item is downloaded and MD5-verified **before** the
        slide is returned. On any cache failure :class:`_CacheError`
        propagates up and the caller aborts the whole sync.
        """
        layout = raw.get("layout") or {}
        zones_raw = layout.get("zones") or []
        resolved_zones = [self._resolve_zone(zr, progress) for zr in zones_raw]

        return Slide(
            slide_id=str(raw.get("slide_id") or f"slide:{raw.get('order', 0)}"),
            order=int(raw.get("order") or 0),
            duration=int(raw.get("duration") or 10),
            layout_id=layout.get("layout_id"),
            layout_name=str(layout.get("name") or "Layout"),
            resolution_w=int(layout.get("resolution_w") or 1920),
            resolution_h=int(layout.get("resolution_h") or 1080),
            zones=resolved_zones,
        )

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
            # Capped exponential backoff with ±20% jitter. The nominal
            # schedule is 2 → 4 → 8 → 16 → 30 seconds. ``on_open``
            # resets ``backoff`` back to WS_BACKOFF_MIN so a momentary
            # blip doesn't promote the delay to its cap permanently.
            nominal = min(backoff, WS_BACKOFF_MAX)
            jittered = _jittered_delay(nominal, WS_BACKOFF_JITTER)
            logger.debug(
                "WS reconnect in %.1fs (nominal %ds, next nominal %ds)",
                jittered,
                nominal,
                min(backoff * 2, WS_BACKOFF_MAX),
            )
            time.sleep(jittered)
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


def _jittered_delay(nominal_seconds: float, jitter_fraction: float) -> float:
    """Return *nominal_seconds* perturbed by ±*jitter_fraction*.

    Uses ``random.uniform`` so a fleet of players reconnecting after a
    common outage doesn't synchronise on the exact same instants.
    Clamped to a minimum of 0.1 s so ``time.sleep()`` never returns
    immediately with a negative input. Non-random-critical use;
    ``random`` is fine here.
    """
    jitter = nominal_seconds * jitter_fraction
    delay = nominal_seconds + random.uniform(-jitter, jitter)  # noqa: S311
    return max(0.1, delay)


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
