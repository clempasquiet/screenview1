"""Unit tests for the player's sync helpers that do NOT require a display.

Running the full worker requires PyQt6 + a Qt platform plugin, so these tests
focus on the pure helpers (md5 checksums, cache directory management via the
config object, hardware ID fallbacks).
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import PlayerConfig  # noqa: E402
from hardware import get_mac_address  # noqa: E402


def test_config_roundtrip(tmp_path):
    path = tmp_path / "conf.json"
    cfg = PlayerConfig.load(path)
    assert path.exists()
    cfg.device_id = "abc"
    cfg.save(path)

    reloaded = PlayerConfig.load(path)
    assert reloaded.device_id == "abc"


def test_cache_path_created(tmp_path):
    cfg = PlayerConfig(cache_dir=str(tmp_path / "cache"))
    p = cfg.cache_path
    assert p.is_dir()


def test_ws_url_conversion():
    cfg = PlayerConfig(server_url="http://example.org:8000")
    assert cfg.ws_url == "ws://example.org:8000"

    cfg2 = PlayerConfig(server_url="https://secure.example.org")
    assert cfg2.ws_url == "wss://secure.example.org"


def test_mac_address_format():
    mac = get_mac_address()
    parts = mac.split(":")
    assert len(parts) == 6
    for p in parts:
        assert len(p) == 2
        int(p, 16)


def test_md5_helper(tmp_path):
    # Import here so the PyQt import in worker_network is optional at load time.
    pytest.importorskip("PyQt6")
    from worker_network import _md5

    target = tmp_path / "sample.bin"
    data = b"hello screen view"
    target.write_bytes(data)
    expected = hashlib.md5(data).hexdigest()  # noqa: S324
    assert _md5(target) == expected
