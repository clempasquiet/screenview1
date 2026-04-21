"""Phase 1 hardening regressions.

Covers three focused invariants:

  1. Paths: relative ``cache_dir`` / ``libmpv_dir`` config values resolve
     against the application directory (``APP_DIR``), never against
     ``os.getcwd()`` — the key hardening that keeps Task Scheduler runs
     working when Windows sets CWD=``C:\\Windows\\System32``.

  2. WebSocket reconnect backoff obeys the spec'd schedule
     (2 → 4 → 8 → 16 → 30 s, capped) with ±20 % jitter.

  3. ``_jittered_delay`` never returns a zero / negative value so
     ``time.sleep`` cannot busy-loop.

No Qt / PyQt6 imports here so the module runs in headless CI.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

import pytest


_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))


def _load_local(name: str) -> ModuleType:
    """Load ``<player-windows>/<name>.py`` under a unique module name.

    Same pattern as the other test files in this package: prevents
    ``sys.modules`` collisions with the Linux player when both suites
    run in the same pytest invocation.
    """
    path = _PKG_ROOT / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"screenview_windows_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Fix 1 — path hardening
# ---------------------------------------------------------------------------


@pytest.fixture()
def config_mod() -> ModuleType:
    return _load_local("config")


def test_app_dir_is_script_directory(config_mod):
    """APP_DIR must always point at the player-windows source directory,
    regardless of what CWD the process was launched from."""
    assert config_mod.APP_DIR == _PKG_ROOT


def test_resolve_app_path_anchors_relative_paths(config_mod, monkeypatch, tmp_path):
    """The helper that every path-using code site routes through must
    anchor relative inputs to APP_DIR, NOT to os.getcwd()."""
    # Simulate a scheduled task CWD somewhere irrelevant.
    monkeypatch.chdir(tmp_path)
    resolved = config_mod.resolve_app_path("cache")
    assert resolved is not None
    assert resolved == (config_mod.APP_DIR / "cache").resolve()


def test_resolve_app_path_passes_absolute_through(config_mod, tmp_path):
    abs_target = tmp_path / "elsewhere"
    resolved = config_mod.resolve_app_path(str(abs_target))
    assert resolved == abs_target


def test_resolve_app_path_returns_none_for_empty(config_mod):
    assert config_mod.resolve_app_path(None) is None
    assert config_mod.resolve_app_path("") is None


def test_cache_path_relative_resolves_against_app_dir(config_mod, monkeypatch, tmp_path):
    """The headline Task-Scheduler bug: ``{"cache_dir": "cache"}`` in
    the config file previously resolved against ``os.getcwd()``. It
    must now land inside APP_DIR even when CWD is elsewhere."""
    monkeypatch.chdir(tmp_path)
    cfg = config_mod.PlayerConfig(cache_dir="cache")
    assert cfg.cache_path == (config_mod.APP_DIR / "cache").resolve()


def test_cache_path_absolute_is_honoured(config_mod, tmp_path):
    cfg = config_mod.PlayerConfig(cache_dir=str(tmp_path / "custom"))
    assert cfg.cache_path == tmp_path / "custom"


def test_cache_path_default_uses_app_data_dir(config_mod, monkeypatch, tmp_path):
    """With ``cache_dir=None`` we still fall back to the per-user
    APP_DATA_DIR (respected via ``LOCALAPPDATA`` on Windows)."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    # Reload the module so APP_DATA_DIR picks up the patched env.
    reloaded = _load_local("config")
    cfg = reloaded.PlayerConfig(cache_dir=None)
    assert cfg.cache_path.parent == tmp_path / reloaded.APP_NAME
    assert cfg.cache_path.name == "cache"


def test_libmpv_search_dir_is_app_relative(config_mod, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    cfg = config_mod.PlayerConfig(libmpv_dir="mpv-libs")
    resolved = cfg.libmpv_search_dir
    assert resolved is not None
    assert resolved == (config_mod.APP_DIR / "mpv-libs").resolve()


def test_libmpv_search_dir_none_when_unset(config_mod):
    cfg = config_mod.PlayerConfig(libmpv_dir=None)
    assert cfg.libmpv_search_dir is None


# ---------------------------------------------------------------------------
# Fix 2 — WebSocket reconnect backoff
# ---------------------------------------------------------------------------


@pytest.fixture()
def worker_mod() -> ModuleType:
    pytest.importorskip("PyQt6")
    # Also load config first so worker_network's ``from config import ...``
    # resolves to the player-windows copy (not the linux one).
    _load_local("config")
    return _load_local("worker_network")


def test_ws_backoff_constants_match_spec(worker_mod):
    """The backoff schedule is documented as 2 → 4 → 8 → 16 → 30 s
    (capped) with ±20 % jitter. This pins the constants so a future
    refactor can't silently change the spec."""
    assert worker_mod.WS_BACKOFF_MIN == 2
    assert worker_mod.WS_BACKOFF_MAX == 30
    assert 0 < worker_mod.WS_BACKOFF_JITTER < 1


def test_ws_backoff_series_doubles_and_caps(worker_mod):
    """Walk the schedule the way the worker loop does."""
    backoff = worker_mod.WS_BACKOFF_MIN
    observed = []
    for _ in range(8):
        observed.append(min(backoff, worker_mod.WS_BACKOFF_MAX))
        backoff = min(backoff * 2, worker_mod.WS_BACKOFF_MAX)
    # 2, 4, 8, 16, 30, 30, 30, 30
    assert observed == [2, 4, 8, 16, 30, 30, 30, 30]


def test_jittered_delay_stays_near_nominal(worker_mod):
    """±20 % jitter means the sampled delay must fall in
    [0.8 * nominal, 1.2 * nominal]. Run it 200× to cover the random space."""
    nominal = 8.0
    lo = nominal * (1 - worker_mod.WS_BACKOFF_JITTER) - 1e-6
    hi = nominal * (1 + worker_mod.WS_BACKOFF_JITTER) + 1e-6
    for _ in range(200):
        delay = worker_mod._jittered_delay(nominal, worker_mod.WS_BACKOFF_JITTER)
        assert lo <= delay <= hi, f"{delay!r} not in [{lo}, {hi}]"


def test_jittered_delay_never_negative(worker_mod):
    """A jitter larger than the nominal must still produce a sleepable
    positive value so the loop never busy-spins on a misconfigured
    constant."""
    for nominal in (0.0, 0.001, 0.1):
        for fraction in (0.2, 1.5, 5.0):
            delay = worker_mod._jittered_delay(nominal, fraction)
            assert delay >= 0.1


def test_jittered_delay_deterministic_with_seeded_random(worker_mod):
    """Sanity: the helper is a thin wrapper around ``random.uniform``
    so seeding the global RNG makes it fully reproducible."""
    import random

    random.seed(42)
    first = [worker_mod._jittered_delay(4, 0.2) for _ in range(5)]
    random.seed(42)
    second = [worker_mod._jittered_delay(4, 0.2) for _ in range(5)]
    assert first == second
