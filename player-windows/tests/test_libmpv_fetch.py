"""Tests for the libmpv auto-fetch helper.

We only exercise the pure-Python discovery / extractor-selection logic. The
actual download path is gated behind a network call and a working archive
extractor on PATH, so it's intentionally skipped in CI.
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


@pytest.fixture()
def fetch_mod() -> ModuleType:
    return _load_local("libmpv_fetch")


def test_find_existing_dll_returns_first_match(fetch_mod, tmp_path):
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()

    (dir_b / "libmpv-2.dll").write_bytes(b"stub")

    assert fetch_mod.find_existing_dll([dir_a, dir_b]).parent == dir_b


def test_find_existing_dll_ignores_missing_dirs(fetch_mod, tmp_path):
    missing = tmp_path / "does-not-exist"
    present = tmp_path / "present"
    present.mkdir()
    (present / "mpv-2.dll").write_bytes(b"stub")

    result = fetch_mod.find_existing_dll([missing, present])
    assert result is not None
    assert result.name in fetch_mod.DLL_NAMES


def test_find_existing_dll_none(fetch_mod, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert fetch_mod.find_existing_dll([empty]) is None


def test_default_search_dirs_deduplicates(fetch_mod, tmp_path):
    dirs = fetch_mod.default_search_dirs(
        bundled_dir=tmp_path,
        app_data_dir=tmp_path,
        libmpv_dir=str(tmp_path),
    )
    assert dirs.count(tmp_path) == 1


def test_default_search_dirs_respects_order(fetch_mod, tmp_path):
    user = tmp_path / "user"
    bundle = tmp_path / "bundle"
    appdata = tmp_path / "appdata"
    for d in (user, bundle, appdata):
        d.mkdir()

    dirs = fetch_mod.default_search_dirs(
        bundled_dir=bundle,
        app_data_dir=appdata,
        libmpv_dir=str(user),
    )
    assert dirs[0] == user


def test_ensure_libmpv_skips_download_when_not_allowed(fetch_mod, tmp_path, monkeypatch):
    monkeypatch.delenv("PATH", raising=False)

    empty = tmp_path / "nothing"
    empty.mkdir()

    result = fetch_mod.ensure_libmpv(
        bundled_dir=empty,
        app_data_dir=tmp_path,
        libmpv_dir=str(empty),
        allow_download=False,
    )
    assert result is None


def test_ensure_libmpv_returns_dir_when_dll_present(fetch_mod, tmp_path):
    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "libmpv-2.dll").write_bytes(b"stub")

    result = fetch_mod.ensure_libmpv(
        bundled_dir=existing,
        app_data_dir=tmp_path,
        libmpv_dir=None,
        allow_download=False,
    )
    assert result == existing


def test_ensure_libmpv_no_download_off_windows(fetch_mod, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")

    empty = tmp_path / "nothing"
    empty.mkdir()

    result = fetch_mod.ensure_libmpv(
        bundled_dir=empty,
        app_data_dir=tmp_path,
        libmpv_dir=None,
        allow_download=True,
    )
    assert result is None


def test_extract_with_py7zr_used_when_available(fetch_mod, tmp_path, monkeypatch):
    """If py7zr is installed, we should invoke it before any external
    extractor. Uses a real .7z archive generated on the fly."""
    py7zr = pytest.importorskip("py7zr")

    # Build a tiny archive containing a single file.
    src = tmp_path / "src"
    src.mkdir()
    (src / "libmpv-2.dll").write_bytes(b"stub-dll")
    archive = tmp_path / "sample.7z"
    with py7zr.SevenZipFile(str(archive), "w") as zf:
        zf.writeall(str(src), arcname="mpv")

    dest = tmp_path / "out"
    dest.mkdir()
    assert fetch_mod._extract_archive(archive, dest) is True
    dll = fetch_mod._find_dll_in_tree(dest)
    assert dll is not None and dll.read_bytes() == b"stub-dll"
