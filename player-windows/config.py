"""Local configuration for the Windows player.

Persisted at `%LOCALAPPDATA%\\ScreenView\\config.json` by default so the app
can run under an unprivileged kiosk user without needing write access to
`Program Files`. Override the location with `SCREENVIEW_CONFIG`.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional


APP_NAME = "ScreenView"


# Directory containing this source file. Anchoring every relative path to
# this location (instead of ``os.getcwd()``) is what keeps the player
# working under Task Scheduler — Windows launches scheduled tasks with
# CWD=``C:\Windows\System32`` by default, which would otherwise send
# every ``Path("cache")`` / ``Path("mpv")`` lookup into the system
# directory.
APP_DIR = Path(__file__).resolve().parent


def _default_app_data_dir() -> Path:
    """Return `%LOCALAPPDATA%\\ScreenView` on Windows, a home-dir fallback elsewhere."""
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / APP_NAME
    return Path.home() / f".{APP_NAME.lower()}"


APP_DATA_DIR = _default_app_data_dir()
DEFAULT_CONFIG_PATH = APP_DATA_DIR / "config.json"
BUNDLED_CONFIG_PATH = APP_DIR / "config.json"


def resolve_app_path(value: str | os.PathLike[str] | None) -> Path | None:
    """Resolve a user-supplied path relative to the *application* directory.

    Absolute paths pass through unchanged. Relative paths are anchored to
    :data:`APP_DIR` so the player behaves identically whether it was
    launched from its install directory, from a scheduled task with a
    different CWD, or via ``python -m player-windows``.
    Returns ``None`` if the input is falsy.
    """
    if not value:
        return None
    p = Path(value)
    if p.is_absolute():
        return p
    return (APP_DIR / p).resolve()


@dataclass
class PlayerConfig:
    server_url: str = "http://localhost:8000"
    device_id: Optional[str] = None
    device_name: Optional[str] = None
    # Opaque per-device API token, returned by POST /api/register and
    # persisted here. Sent back on every subsequent request as
    # ``Authorization: Bearer <token>``. Rotated transparently by
    # re-registering after a 401.
    api_token: Optional[str] = None
    reconnect_delay_seconds: int = 5
    sync_poll_interval_seconds: int = 60
    cache_dir: Optional[str] = None  # None -> APP_DATA_DIR / 'cache'
    fullscreen: bool = True
    show_cursor: bool = False
    prevent_display_sleep: bool = True
    libmpv_dir: Optional[str] = None
    libmpv_auto_download: bool = True

    @classmethod
    def load(cls, path: Path | None = None) -> "PlayerConfig":
        """Load config. Seeds from the bundled `config.json` on first launch."""
        path = Path(os.environ.get("SCREENVIEW_CONFIG", path or DEFAULT_CONFIG_PATH))
        path.parent.mkdir(parents=True, exist_ok=True)

        if not path.exists():
            # Seed from the file bundled next to the executable if present.
            if BUNDLED_CONFIG_PATH.exists() and BUNDLED_CONFIG_PATH != path:
                data = json.loads(BUNDLED_CONFIG_PATH.read_text(encoding="utf-8"))
                cfg = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
            else:
                cfg = cls()
            cfg.save(path)
            return cfg

        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def save(self, path: Path | None = None) -> None:
        target = Path(path or os.environ.get("SCREENVIEW_CONFIG", DEFAULT_CONFIG_PATH))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    @property
    def cache_path(self) -> Path:
        # Relative ``cache_dir`` values (e.g. "cache", "./media") are
        # anchored to APP_DIR rather than os.getcwd() so a scheduled
        # task launched from C:\Windows\System32 still finds its cache.
        resolved = resolve_app_path(self.cache_dir)
        base = resolved if resolved is not None else APP_DATA_DIR / "cache"
        base.mkdir(parents=True, exist_ok=True)
        return base

    @property
    def libmpv_cache_dir(self) -> Path:
        """Writable directory for auto-downloaded ``libmpv-2.dll`` copies."""
        base = APP_DATA_DIR / "libmpv"
        base.mkdir(parents=True, exist_ok=True)
        return base

    @property
    def libmpv_search_dir(self) -> Path | None:
        """Resolved :data:`libmpv_dir` or ``None``. Relative values are
        resolved against the application directory so operators can set
        ``"libmpv_dir": "mpv"`` without tying the install to a CWD."""
        return resolve_app_path(self.libmpv_dir)

    @property
    def app_data_dir(self) -> Path:
        APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
        return APP_DATA_DIR

    @property
    def app_dir(self) -> Path:
        """Directory of the running application (co-located with main.py /
        the packaged exe). Never changes at runtime; safe for DLL / config
        lookups independent of the current CWD."""
        return APP_DIR

    @property
    def log_path(self) -> Path:
        logs = APP_DATA_DIR / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        return logs / "player.log"

    @property
    def ws_url(self) -> str:
        url = self.server_url.rstrip("/")
        if url.startswith("https://"):
            return "wss://" + url[len("https://"):]
        if url.startswith("http://"):
            return "ws://" + url[len("http://"):]
        return url
