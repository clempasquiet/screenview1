"""Pytest configuration for the Linux player tests.

See ``player-windows/tests/conftest.py`` for the rationale — this is the
mirrored version that ensures ``import config`` resolves to
``player-linux/config.py`` even when the Windows test suite runs first.
"""
from __future__ import annotations

import sys
from pathlib import Path


_PKG_ROOT = Path(__file__).resolve().parent.parent

if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))
else:
    sys.path.remove(str(_PKG_ROOT))
    sys.path.insert(0, str(_PKG_ROOT))


_LOCAL_MODULES = ("config", "hardware", "worker_network", "player_ui")


def _evict_sibling_modules() -> None:
    for name in _LOCAL_MODULES:
        existing = sys.modules.get(name)
        if existing is None:
            continue
        file_attr = getattr(existing, "__file__", "") or ""
        if str(_PKG_ROOT) not in str(file_attr):
            del sys.modules[name]


_evict_sibling_modules()
