"""Local configuration for the player.

Stores the persistent state (device ID, server URL, cache location). The
`config.json` file lives alongside the application and can be bootstrapped by
an installer or provisioned via an env var `SCREENVIEW_CONFIG`.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config.json"


@dataclass
class PlayerConfig:
    server_url: str = "http://localhost:8000"
    device_id: Optional[str] = None
    device_name: Optional[str] = None
    reconnect_delay_seconds: int = 5
    sync_poll_interval_seconds: int = 60
    cache_dir: str = "cache"
    fullscreen: bool = True
    show_cursor: bool = False

    @classmethod
    def load(cls, path: Path | None = None) -> "PlayerConfig":
        path = Path(os.environ.get("SCREENVIEW_CONFIG", path or DEFAULT_CONFIG_PATH))
        if not path.exists():
            cfg = cls()
            cfg.save(path)
            return cfg
        data = json.loads(path.read_text())
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def save(self, path: Path | None = None) -> None:
        path = Path(path or DEFAULT_CONFIG_PATH)
        path.write_text(json.dumps(asdict(self), indent=2))

    @property
    def cache_path(self) -> Path:
        base = Path(self.cache_dir)
        if not base.is_absolute():
            base = Path(__file__).resolve().parent / base
        base.mkdir(parents=True, exist_ok=True)
        return base

    @property
    def ws_url(self) -> str:
        url = self.server_url.rstrip("/")
        if url.startswith("https://"):
            return "wss://" + url[len("https://"):]
        if url.startswith("http://"):
            return "ws://" + url[len("http://"):]
        return url
