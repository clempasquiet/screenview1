"""Tests for the Windows player's config + hardware helpers.

These run on any platform; Windows-only code paths (registry lookups, DPI
awareness, SetThreadExecutionState) are covered by no-op assertions when
``sys.platform != "win32"``.

The module-level ``config`` name clashes with ``player-linux/config.py``,
so we load the player-windows modules by explicit file path inside a
fixture that also forces a fresh read from disk. This insulates us from
cross-test ``sys.modules`` pollution when both player test suites run
in the same pytest invocation.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest


_PKG_ROOT = Path(__file__).resolve().parent.parent

if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))


def _load_local(name: str) -> ModuleType:
    """Load ``<player-windows>/<name>.py`` regardless of what else may
    already live under the same top-level name in ``sys.modules``."""
    path = _PKG_ROOT / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"screenview_windows_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def config_mod() -> ModuleType:
    return _load_local("config")


@pytest.fixture()
def hardware_mod() -> ModuleType:
    return _load_local("hardware")


@pytest.fixture()
def power_mod() -> ModuleType:
    return _load_local("power")


def test_config_seeds_from_bundled_file_on_first_run(config_mod, tmp_path, monkeypatch):
    monkeypatch.delenv("SCREENVIEW_CONFIG", raising=False)
    target = tmp_path / "config.json"
    cfg = config_mod.PlayerConfig.load(target)
    assert target.exists()
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["server_url"].startswith("http")
    assert cfg.fullscreen is True
    assert cfg.libmpv_auto_download is True


def test_config_ignores_unknown_fields(config_mod, tmp_path):
    target = tmp_path / "config.json"
    target.write_text(
        json.dumps(
            {
                "server_url": "http://example.org",
                "some_future_flag": True,
                "libmpv_auto_download": False,
            }
        ),
        encoding="utf-8",
    )
    cfg = config_mod.PlayerConfig.load(target)
    assert cfg.server_url == "http://example.org"
    assert cfg.libmpv_auto_download is False


def test_config_roundtrip(config_mod, tmp_path):
    path = tmp_path / "conf.json"
    cfg = config_mod.PlayerConfig.load(path)
    cfg.device_id = "deadbeef"
    cfg.server_url = "http://example.com:9000"
    cfg.save(path)

    reloaded = config_mod.PlayerConfig.load(path)
    assert reloaded.device_id == "deadbeef"
    assert reloaded.server_url == "http://example.com:9000"


def test_cache_path_created(config_mod, tmp_path):
    cfg = config_mod.PlayerConfig(cache_dir=str(tmp_path / "cache"))
    cache_dir = cfg.cache_path
    assert cache_dir.is_dir()
    assert cache_dir == tmp_path / "cache"


def test_log_path_under_app_data(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    # Force reload so ``APP_DATA_DIR`` picks up the patched env.
    config_mod = _load_local("config")
    cfg = config_mod.PlayerConfig()
    log_path = cfg.log_path
    assert log_path.parent.is_dir()
    assert log_path.parent.name == "logs"


def test_ws_url_conversion(config_mod):
    assert config_mod.PlayerConfig(server_url="http://a:8000").ws_url == "ws://a:8000"
    assert config_mod.PlayerConfig(server_url="https://secure.example").ws_url == "wss://secure.example"


def test_mac_address_format(hardware_mod):
    mac = hardware_mod.get_mac_address()
    parts = mac.split(":")
    assert len(parts) == 6
    for p in parts:
        assert len(p) == 2
        int(p, 16)


def test_hardware_id_is_stable_string(hardware_mod):
    hw = hardware_mod.get_hardware_id()
    assert isinstance(hw, str)
    assert hw
    if sys.platform != "win32":
        assert ":" not in hw


def test_power_helpers_are_noops_off_windows(power_mod):
    result = power_mod.prevent_display_sleep()
    power_mod.restore_power_state()
    if sys.platform != "win32":
        assert result is False


def test_md5_helper(tmp_path):
    pytest.importorskip("PyQt6")
    worker_mod = _load_local("worker_network")

    target = tmp_path / "sample.bin"
    data = b"hello screen view windows"
    target.write_bytes(data)
    assert worker_mod._md5(target) == hashlib.md5(data).hexdigest()  # noqa: S324
