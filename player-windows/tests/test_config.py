"""Tests for the Windows player's config + hardware helpers.

These run on any platform; Windows-only code paths (registry lookups, DPI
awareness, SetThreadExecutionState) are covered by no-op assertions when
``sys.platform != "win32"``.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import PlayerConfig  # noqa: E402
from hardware import get_hardware_id, get_mac_address  # noqa: E402
from power import prevent_display_sleep, restore_power_state  # noqa: E402


def test_config_seeds_from_bundled_file_on_first_run(tmp_path, monkeypatch):
    monkeypatch.delenv("SCREENVIEW_CONFIG", raising=False)
    target = tmp_path / "config.json"
    cfg = PlayerConfig.load(target)
    assert target.exists()
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["server_url"].startswith("http")
    assert cfg.fullscreen is True


def test_config_roundtrip(tmp_path):
    path = tmp_path / "conf.json"
    cfg = PlayerConfig.load(path)
    cfg.device_id = "deadbeef"
    cfg.server_url = "http://example.com:9000"
    cfg.save(path)

    reloaded = PlayerConfig.load(path)
    assert reloaded.device_id == "deadbeef"
    assert reloaded.server_url == "http://example.com:9000"


def test_cache_path_created(tmp_path):
    cfg = PlayerConfig(cache_dir=str(tmp_path / "cache"))
    cache_dir = cfg.cache_path
    assert cache_dir.is_dir()
    assert cache_dir == tmp_path / "cache"


def test_log_path_under_app_data(monkeypatch, tmp_path):
    # Force APP_DATA_DIR via LOCALAPPDATA so the test doesn't pollute the
    # developer's real profile directory.
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    import importlib

    import config as config_mod

    importlib.reload(config_mod)
    cfg = config_mod.PlayerConfig()
    log_path = cfg.log_path
    assert log_path.parent.is_dir()
    assert log_path.parent.name == "logs"


def test_ws_url_conversion():
    assert PlayerConfig(server_url="http://a:8000").ws_url == "ws://a:8000"
    assert PlayerConfig(server_url="https://secure.example").ws_url == "wss://secure.example"


def test_mac_address_format():
    mac = get_mac_address()
    parts = mac.split(":")
    assert len(parts) == 6
    for p in parts:
        assert len(p) == 2
        int(p, 16)


def test_hardware_id_is_stable_string():
    hw = get_hardware_id()
    assert isinstance(hw, str)
    assert hw
    # On non-Windows we fall back to a sanitised MAC.
    if sys.platform != "win32":
        assert ":" not in hw


def test_power_helpers_are_noops_off_windows():
    # Should neither raise nor require the Win32 API on other platforms.
    result = prevent_display_sleep()
    restore_power_state()
    if sys.platform != "win32":
        assert result is False


def test_md5_helper(tmp_path):
    pytest.importorskip("PyQt6")
    from worker_network import _md5

    target = tmp_path / "sample.bin"
    data = b"hello screen view windows"
    target.write_bytes(data)
    assert _md5(target) == hashlib.md5(data).hexdigest()  # noqa: S324
