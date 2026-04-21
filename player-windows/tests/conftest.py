"""Pytest configuration for the Windows player tests.

Both ``player-linux/`` and ``player-windows/`` expose top-level modules
with the same names (``config``, ``hardware``, ``worker_network`` …).
When pytest collects tests from both trees in the same invocation, the
first tree to import its ``config`` wins and subsequent imports are
served from the module cache — causing cross-contamination that looks
like spurious ``AttributeError`` failures.

To avoid that, we:
  * Prepend the player-windows source dir to ``sys.path`` at collection
    time (so ``import config`` resolves here).
  * Evict any previously-imported namesakes from ``sys.modules`` so the
    next import re-reads the file from disk.
"""
from __future__ import annotations

import sys
from pathlib import Path


_PKG_ROOT = Path(__file__).resolve().parent.parent

# Prepend (not append) — beat any leftover entry from sibling test suites.
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))
else:
    sys.path.remove(str(_PKG_ROOT))
    sys.path.insert(0, str(_PKG_ROOT))


_LOCAL_MODULES = (
    "config",
    "hardware",
    "power",
    "single_instance",
    "libmpv_fetch",
    "worker_network",
    "player_ui",
)


def _evict_sibling_modules() -> None:
    """Drop any ``config``/``worker_network``/etc. that come from the
    sibling ``player-linux`` tree so our imports re-resolve against
    player-windows.
    """
    for name in _LOCAL_MODULES:
        existing = sys.modules.get(name)
        if existing is None:
            continue
        file_attr = getattr(existing, "__file__", "") or ""
        if str(_PKG_ROOT) not in str(file_attr):
            del sys.modules[name]


_evict_sibling_modules()
