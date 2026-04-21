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


def _default_app_data_dir() -> Path:
    """Return `%LOCALAPPDATA%\\ScreenView` on Windows, a home-dir fallback elsewhere."""
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / APP_NAME
    return Path.home() / f".{APP_NAME.lower()}"


APP_DATA_DIR = _default_app_data_dir()
DEFAULT_CONFIG_PATH = APP_DATA_DIR / "config.json"
BUNDLED_CONFIG_PATH = Path(__file__).resolve().parent / "config.json"


@dataclass
class PlayerConfig:
    server_url: str = "http://localhost:8000"
    device_id: Optional[str] = None
    device_name: Optional[str] = None
    reconnect_delay_seconds: int = 5
    sync_poll_interval_seconds: int = 60
    cache_dir: Optional[str] = None  # None -> APP_DATA_DIR / 'cache'
    fullscreen: bool = True
    show_cursor: bool = False
    prevent_display_sleep: bool = True
    libmpv_dir: Optional[str] = None

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
        if self.cache_dir:
            base = Path(self.cache_dir)
        else:
            base = APP_DATA_DIR / "cache"
        base.mkdir(parents=True, exist_ok=True)
        return base

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
