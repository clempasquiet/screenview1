"""Tests for the libmpv auto-fetch helper.

We only exercise the pure-Python discovery / extractor-selection logic. The
actual download path is gated behind a network call and a working archive
extractor on PATH, so it's intentionally skipped in CI.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from libmpv_fetch import (  # noqa: E402
    DLL_NAMES,
    default_search_dirs,
    ensure_libmpv,
    find_existing_dll,
)


def test_find_existing_dll_returns_first_match(tmp_path):
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()

    (dir_b / "libmpv-2.dll").write_bytes(b"stub")

    assert find_existing_dll([dir_a, dir_b]).parent == dir_b


def test_find_existing_dll_ignores_missing_dirs(tmp_path):
    missing = tmp_path / "does-not-exist"
    present = tmp_path / "present"
    present.mkdir()
    (present / "mpv-2.dll").write_bytes(b"stub")

    result = find_existing_dll([missing, present])
    assert result is not None
    assert result.name in DLL_NAMES


def test_find_existing_dll_none(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert find_existing_dll([empty]) is None


def test_default_search_dirs_deduplicates(tmp_path):
    dirs = default_search_dirs(
        bundled_dir=tmp_path,
        app_data_dir=tmp_path,
        libmpv_dir=str(tmp_path),
    )
    # Same path was given three times; should only appear once.
    assert dirs.count(tmp_path) == 1


def test_default_search_dirs_respects_order(tmp_path):
    user = tmp_path / "user"
    bundle = tmp_path / "bundle"
    appdata = tmp_path / "appdata"
    for d in (user, bundle, appdata):
        d.mkdir()

    dirs = default_search_dirs(
        bundled_dir=bundle,
        app_data_dir=appdata,
        libmpv_dir=str(user),
    )
    # User override is checked first.
    assert dirs[0] == user


def test_ensure_libmpv_skips_download_when_not_allowed(tmp_path, monkeypatch):
    # No DLL present, download disabled: must return None without touching
    # the network.
    monkeypatch.delenv("PATH", raising=False)

    # Make sure no existing DLL is ever found.
    empty = tmp_path / "nothing"
    empty.mkdir()

    result = ensure_libmpv(
        bundled_dir=empty,
        app_data_dir=tmp_path,
        libmpv_dir=str(empty),
        allow_download=False,
    )
    assert result is None


def test_ensure_libmpv_returns_dir_when_dll_present(tmp_path):
    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "libmpv-2.dll").write_bytes(b"stub")

    result = ensure_libmpv(
        bundled_dir=existing,
        app_data_dir=tmp_path,
        libmpv_dir=None,
        allow_download=False,
    )
    assert result == existing


def test_ensure_libmpv_no_download_off_windows(tmp_path, monkeypatch):
    # Even with allow_download=True, we must not attempt a fetch on
    # non-Windows platforms.
    monkeypatch.setattr(sys, "platform", "linux")

    empty = tmp_path / "nothing"
    empty.mkdir()

    result = ensure_libmpv(
        bundled_dir=empty,
        app_data_dir=tmp_path,
        libmpv_dir=None,
        allow_download=True,
    )
    assert result is None
