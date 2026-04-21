"""Tests for the stale-device re-registration flow and log-noise helpers.

The worker itself requires PyQt6 (for QObject/QThread/pyqtSignal), so the
whole module is skipped when PyQt6 is unavailable (e.g. headless CI
without Qt platform plugins).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


_PKG_ROOT = Path(__file__).resolve().parent.parent

if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))


def _load_local(name: str) -> ModuleType:
    path = _PKG_ROOT / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"screenview_windows_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


pytest.importorskip("PyQt6")


@pytest.fixture()
def worker_module() -> ModuleType:
    # Make sure ``from config import PlayerConfig`` inside worker_network
    # resolves to the player-windows copy even when the linux suite ran first.
    _load_local("config")
    return _load_local("worker_network")


def test_compact_ws_error_trims_header_dump(worker_module):
    raw = (
        "Handshake status 403 Forbidden -+-+- {'date': 'Tue, 21 Apr 2026 "
        "10:50:27 GMT', 'content-length': '0', 'content-type': 'text/plain', "
        "'connection': 'close'} -+-+- b''"
    )
    assert worker_module._compact_ws_error(raw) == "Handshake status 403 Forbidden"


def test_compact_ws_error_passes_short_messages_through(worker_module):
    assert worker_module._compact_ws_error("Connection refused") == "Connection refused"


def test_compact_ws_error_handles_exceptions(worker_module):
    exc = ConnectionResetError("peer closed")
    assert worker_module._compact_ws_error(exc) == "peer closed"


def test_is_unknown_device_error_detects_handshake_403(worker_module):
    err = "Handshake status 403 Forbidden -+-+- {} -+-+- b''"
    assert worker_module._is_unknown_device_error(err) is True


def test_is_unknown_device_error_detects_close_4404(worker_module):
    err = "ConnectionClosed (4404, 'unknown device')"
    assert worker_module._is_unknown_device_error(err) is True


def test_is_unknown_device_error_ignores_generic_network_errors(worker_module):
    assert worker_module._is_unknown_device_error("Connection refused") is False
    assert worker_module._is_unknown_device_error("Handshake status 502 Bad Gateway") is False


def test_forget_device_clears_config_and_persists(worker_module, tmp_path, monkeypatch):
    config_mod = _load_local("config")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    cfg = config_mod.PlayerConfig()
    cfg.server_url = "http://example.org"
    cfg.device_id = "abc-123"
    cfg.device_name = "Test"
    config_path = tmp_path / "config.json"
    cfg.save(config_path)
    monkeypatch.setenv("SCREENVIEW_CONFIG", str(config_path))

    worker = worker_module.NetworkWorker.__new__(worker_module.NetworkWorker)
    worker._config = cfg

    captured: list[tuple[str, str]] = []
    worker.status_changed = type(
        "StubSignal", (), {"emit": lambda self, lvl, msg: captured.append((lvl, msg))}
    )()

    worker_module.NetworkWorker._forget_device(worker, "test reason")

    assert cfg.device_id is None
    assert cfg.device_name is None

    reloaded = config_mod.PlayerConfig.load(config_path)
    assert reloaded.device_id is None
    assert reloaded.device_name is None

    assert any("Re-registering device" in msg for lvl, msg in captured)


def test_worker_surfaces_stale_device_error(worker_module):
    assert issubclass(worker_module._StaleDeviceError, RuntimeError)


def test_constants_are_sensible(worker_module):
    assert worker_module.WS_BACKOFF_MIN >= 1
    assert worker_module.WS_BACKOFF_MAX >= worker_module.WS_BACKOFF_MIN
    assert 4404 in worker_module.WS_UNKNOWN_DEVICE_CLOSE_CODES
