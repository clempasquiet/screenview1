"""Runtime helper that fetches ``libmpv-2.dll`` when it is missing.

python-mpv needs ``libmpv-2.dll`` (or ``mpv-2.dll``) to be loadable before
``import mpv``. On Windows the DLL is not shipped with the Python package —
users normally download it separately from a pre-built release. That manual
step is a frequent footgun when deploying kiosks, so this module:

  * Looks for the DLL in a set of candidate locations (next to the exe,
    next to ``main.py``, inside the user's ``%LOCALAPPDATA%\\ScreenView``).
  * If missing, downloads the latest release asset from
    ``zhongfly/mpv-winbuild`` on GitHub (a community CI build of mpv for
    Windows) and extracts ``libmpv-2.dll`` into the ScreenView app-data dir.
  * Returns the directory holding the DLL so ``player_ui.py`` can prepend
    it to ``PATH``.

The download is best-effort: if it fails (no internet, firewall, rate
limit, no 7z extractor available), the function simply returns ``None``
and the player boots without video playback — images and widgets still
work, and the placeholder frame renders on video items.
"""
from __future__ import annotations

import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Iterable, Optional
from urllib.error import URLError

logger = logging.getLogger(__name__)


DLL_NAMES = ("libmpv-2.dll", "mpv-2.dll", "mpv-1.dll")
RELEASE_API_URL = "https://api.github.com/repos/zhongfly/mpv-winbuild/releases/latest"
USER_AGENT = "screenview-player/1.0 (+https://github.com/clempasquiet/screenview1)"


def find_existing_dll(search_dirs: Iterable[Path]) -> Optional[Path]:
    for directory in search_dirs:
        if not directory or not directory.is_dir():
            continue
        for name in DLL_NAMES:
            candidate = directory / name
            if candidate.is_file():
                return candidate
    return None


def default_search_dirs(
    bundled_dir: Path | None = None,
    app_data_dir: Path | None = None,
    libmpv_dir: str | None = None,
) -> list[Path]:
    """Compose the list of directories where we look for ``libmpv-*.dll``.

    Order matters: user-configured > exe dir > bundled data > app-data.
    """
    dirs: list[Path] = []
    if libmpv_dir:
        dirs.append(Path(libmpv_dir))
    if getattr(sys, "frozen", False):
        dirs.append(Path(sys.executable).resolve().parent)
    if bundled_dir is not None:
        dirs.append(bundled_dir)
    pyinstaller_tmp = getattr(sys, "_MEIPASS", None)
    if pyinstaller_tmp:
        dirs.append(Path(pyinstaller_tmp))
    dirs.append(Path(__file__).resolve().parent)
    if app_data_dir is not None:
        dirs.append(app_data_dir)
    # De-duplicate while preserving order.
    seen: set[Path] = set()
    ordered: list[Path] = []
    for d in dirs:
        d = Path(d)
        if d in seen:
            continue
        seen.add(d)
        ordered.append(d)
    return ordered


def ensure_libmpv(
    bundled_dir: Path | None = None,
    app_data_dir: Path | None = None,
    libmpv_dir: str | None = None,
    allow_download: bool = True,
    variant: str = "x86_64",
) -> Optional[Path]:
    """Return the directory holding ``libmpv-2.dll``, downloading if needed.

    Returns ``None`` if the DLL is absent and cannot be fetched — the
    caller must tolerate this (``player_ui.py`` degrades gracefully to
    a placeholder on video items).
    """
    search = default_search_dirs(
        bundled_dir=bundled_dir, app_data_dir=app_data_dir, libmpv_dir=libmpv_dir
    )

    existing = find_existing_dll(search)
    if existing is not None:
        logger.info("Found libmpv DLL at %s", existing)
        return existing.parent

    if sys.platform != "win32":
        return None
    if not allow_download:
        return None
    if app_data_dir is None:
        return None

    target_dir = app_data_dir / "libmpv"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("Cannot create %s: %s", target_dir, exc)
        return None

    dll_path = _download_libmpv(target_dir, variant=variant)
    if dll_path is None:
        return None
    logger.info("Fetched libmpv DLL to %s", dll_path)
    return dll_path.parent


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _http_get(url: str, timeout: float = 30.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310  # HTTPS
        return resp.read()


def _download_libmpv(target_dir: Path, variant: str = "x86_64") -> Optional[Path]:
    """Fetch the latest libmpv-2.dll and place it in ``target_dir``.

    Returns the full path of the extracted DLL on success, or ``None`` on
    any failure (network, extractor, archive layout).
    """
    logger.info("libmpv DLL missing; attempting automatic download.")

    # 1. Find the latest release asset.
    try:
        payload = _http_get(RELEASE_API_URL, timeout=30)
        release = json.loads(payload.decode("utf-8", errors="replace"))
    except (URLError, OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not query %s: %s", RELEASE_API_URL, exc)
        return None

    prefix = f"mpv-dev-{variant}-"
    asset = None
    for item in release.get("assets", []):
        name = item.get("name", "")
        if name.startswith(prefix) and name.endswith(".7z"):
            asset = item
            break
    if asset is None:
        logger.warning("No mpv-dev asset matching %s*.7z in latest release.", prefix)
        return None

    asset_url = asset.get("browser_download_url")
    asset_name = asset.get("name")
    if not asset_url or not asset_name:
        logger.warning("Release asset missing url/name: %s", asset)
        return None

    logger.info("Downloading %s (~%d MB)…", asset_name, int(asset.get("size", 0) // (1024 * 1024)))

    with tempfile.TemporaryDirectory(prefix="screenview-libmpv-") as tmp:
        tmp_path = Path(tmp)
        archive = tmp_path / asset_name
        try:
            data = _http_get(asset_url, timeout=300)
            archive.write_bytes(data)
        except (URLError, OSError) as exc:
            logger.warning("Download failed: %s", exc)
            return None

        extract_dir = tmp_path / "extracted"
        extract_dir.mkdir()

        if not _extract_archive(archive, extract_dir):
            return None

        extracted_dll = _find_dll_in_tree(extract_dir)
        if extracted_dll is None:
            logger.warning("Archive extracted but no libmpv-*.dll found inside.")
            return None

        destination = target_dir / extracted_dll.name
        try:
            shutil.copy2(extracted_dll, destination)
        except OSError as exc:
            logger.warning("Could not copy DLL to %s: %s", destination, exc)
            return None

    return destination


def _extract_archive(archive: Path, dest: Path) -> bool:
    """Try known Windows extractors in order. Returns True on success."""
    commands = _extractor_commands(archive, dest)
    for cmd in commands:
        try:
            result = subprocess.run(  # noqa: S603  # args are a trusted list
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=False,
                creationflags=_no_window_flag(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("Extractor %s unavailable: %s", cmd[0], exc)
            continue
        if result.returncode == 0:
            return True
        logger.debug(
            "Extractor %s exited %s: %s",
            cmd[0],
            result.returncode,
            (result.stderr or b"").decode("utf-8", errors="replace").strip(),
        )
    logger.warning(
        "No working archive extractor found. Install 7-Zip or update Windows to 10 1803+."
    )
    return False


def _extractor_commands(archive: Path, dest: Path) -> list[list[str]]:
    candidates: list[list[str]] = []
    for exe in ("7z.exe", "7zr.exe", "7z"):
        full = shutil.which(exe)
        if full:
            candidates.append([full, "x", "-y", f"-o{dest}", str(archive)])

    tar = shutil.which("tar.exe") or shutil.which("tar")
    if tar:
        candidates.append([tar, "-xf", str(archive), "-C", str(dest)])
    return candidates


def _find_dll_in_tree(root: Path) -> Optional[Path]:
    for name in DLL_NAMES:
        matches = list(root.rglob(name))
        if matches:
            # Prefer x86_64 builds over any nested arch subdirs just in case.
            return matches[0]
    return None


def _no_window_flag() -> int:
    if sys.platform == "win32":
        return 0x08000000  # CREATE_NO_WINDOW
    return 0
