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

Extraction is the tricky part. The mpv release archives are ``.7z`` files
that use the **BCJ2** filter for binary optimisation, which ``py7zr``
(pure-Python) does not implement. We therefore bootstrap the official
standalone ``7zr.exe`` (~600 KB) from 7-zip.org on first need, cache it
next to the DLL under ``%LOCALAPPDATA%\\ScreenView\\libmpv\\``, and reuse
it forever after. Extraction order:

  1. ``7zr.exe`` (bootstrapped, or from a pre-existing install)
  2. Any ``7z.exe`` / ``7zr.exe`` already on ``PATH``
  3. ``py7zr`` — only works on archives without BCJ2; kept as a last
     resort so offline deployments with py7zr pre-installed can still
     succeed on non-BCJ2 archives.
  4. ``tar.exe`` — some Windows builds ship libarchive with 7z support.

The whole pipeline is best-effort: **any failure is swallowed and logged
at WARNING level**. The caller (``player_ui.py``) treats a ``None``
return as "no video playback; show placeholder for video items" — the
player UI itself never crashes because the DLL could not be fetched.
"""
from __future__ import annotations

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

# Official standalone 7-Zip console extractor. Signed, ~600 KB, supports every
# 7z filter including BCJ2 that ``py7zr`` cannot handle.
SEVENZR_URL = "https://www.7-zip.org/a/7zr.exe"
SEVENZR_MIN_SIZE = 100 * 1024   # sanity bound; the real file is ~600 KB
SEVENZR_MAX_SIZE = 10 * 1024 * 1024


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

    This helper **never raises**. Any unexpected error is caught and logged
    at WARNING level; the function simply returns ``None`` in that case.
    The caller (``player_ui.py``) treats ``None`` as "no video playback;
    the placeholder frame will render for video items".
    """
    try:
        return _ensure_libmpv_inner(
            bundled_dir=bundled_dir,
            app_data_dir=app_data_dir,
            libmpv_dir=libmpv_dir,
            allow_download=allow_download,
            variant=variant,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "libmpv auto-provisioning aborted unexpectedly: %s: %s",
            type(exc).__name__,
            exc,
        )
        logger.debug("libmpv auto-provisioning traceback:", exc_info=True)
        return None


def _ensure_libmpv_inner(
    bundled_dir: Path | None,
    app_data_dir: Path | None,
    libmpv_dir: str | None,
    allow_download: bool,
    variant: str,
) -> Optional[Path]:
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

    logger.info(
        "Downloading %s (~%d MB)…",
        asset_name,
        int(asset.get("size", 0) // (1024 * 1024)),
    )

    tmp_dir = Path(tempfile.mkdtemp(prefix="screenview-libmpv-"))
    try:
        archive = tmp_dir / asset_name
        try:
            data = _http_get(asset_url, timeout=300)
            archive.write_bytes(data)
        except (URLError, OSError) as exc:
            logger.warning("Download failed: %s", exc)
            return None

        extract_dir = tmp_dir / "extracted"
        extract_dir.mkdir()

        if not _extract_archive(archive, extract_dir, persistent_tools_dir=target_dir):
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
    finally:
        _rmtree_best_effort(tmp_dir)


def _rmtree_best_effort(path: Path) -> None:
    """Like ``shutil.rmtree`` but swallows everything.

    Windows can hold a file lock briefly after a subprocess exits (and even
    longer when an external AV scans the freshly-written bytes). We care
    about the happy path, not the cleanup, so missing / locked files
    should never escape this function.
    """
    def _onerror(func, fn, exc_info):  # noqa: ANN001
        logger.debug("rmtree could not remove %s: %s", fn, exc_info[1])

    try:
        shutil.rmtree(path, onerror=_onerror)
    except Exception as exc:  # noqa: BLE001
        logger.debug("rmtree(%s) raised %s: %s", path, type(exc).__name__, exc)


# ---------------------------------------------------------------------------
# Archive extraction
# ---------------------------------------------------------------------------


def _extract_archive(
    archive: Path, dest: Path, persistent_tools_dir: Path | None = None
) -> bool:
    """Extract *archive* into *dest*. Returns True on success.

    Extractor preference:
      1. A cached or newly-bootstrapped ``7zr.exe`` (official standalone
         console extractor from 7-zip.org). This is the only option that
         reliably handles the BCJ2 filter used by the mpv release archives.
      2. An existing ``7z.exe`` / ``7zr.exe`` on ``PATH``.
      3. ``py7zr`` — works on archives that don't use BCJ2.
      4. ``tar.exe`` as a true last resort.

    ``persistent_tools_dir`` is where we store the bootstrapped
    ``7zr.exe`` between runs. Pass ``None`` to disable bootstrapping.
    """
    commands: list[list[str]] = []

    seven_zr = _ensure_7zr(persistent_tools_dir) if persistent_tools_dir else None
    if seven_zr is not None:
        commands.append([str(seven_zr), "x", "-y", f"-o{dest}", str(archive)])

    commands.extend(_external_extractor_commands(archive, dest))

    for cmd in commands:
        if _run_extractor(cmd):
            return True

    if _extract_with_py7zr(archive, dest):
        return True

    commands_tar = _tar_extractor_command(archive, dest)
    if commands_tar and _run_extractor(commands_tar):
        return True

    logger.warning(
        "Could not extract %s with any available 7z tool. Install 7-Zip "
        "(https://www.7-zip.org/) or ensure internet access so 7zr.exe can "
        "be bootstrapped automatically.",
        archive.name,
    )
    return False


def _run_extractor(cmd: list[str]) -> bool:
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
        return False
    if result.returncode == 0:
        return True
    logger.debug(
        "Extractor %s exited %s: %s",
        cmd[0],
        result.returncode,
        (result.stderr or b"").decode("utf-8", errors="replace").strip(),
    )
    return False


def _external_extractor_commands(archive: Path, dest: Path) -> list[list[str]]:
    candidates: list[list[str]] = []
    for exe in ("7z.exe", "7zr.exe", "7z"):
        full = shutil.which(exe)
        if full:
            candidates.append([full, "x", "-y", f"-o{dest}", str(archive)])
    return candidates


def _tar_extractor_command(archive: Path, dest: Path) -> list[str] | None:
    tar = shutil.which("tar.exe") or shutil.which("tar")
    if not tar:
        return None
    return [tar, "-xf", str(archive), "-C", str(dest)]


def _extract_with_py7zr(archive: Path, dest: Path) -> bool:
    try:
        import py7zr  # type: ignore[import-not-found]
    except ImportError:
        logger.debug("py7zr not available; falling back to other extractors.")
        return False
    try:
        with py7zr.SevenZipFile(str(archive), mode="r") as zf:
            zf.extractall(path=str(dest))
    except Exception as exc:  # noqa: BLE001
        # The mpv dev archives use the BCJ2 filter which py7zr does not
        # support; this is expected and handled by falling through to
        # the external ``7zr.exe`` path.
        logger.debug("py7zr extraction failed (expected for BCJ2 archives): %s", exc)
        return False
    return True


# ---------------------------------------------------------------------------
# 7zr.exe bootstrap
# ---------------------------------------------------------------------------


def _ensure_7zr(tools_dir: Path) -> Path | None:
    """Return a path to a working ``7zr.exe``.

    Preference order:
      1. A copy we already bootstrapped into ``tools_dir``.
      2. An existing ``7zr.exe`` / ``7z.exe`` on ``PATH``.
      3. Downloaded from the official 7-zip.org URL and cached in
         ``tools_dir`` for future runs.

    Returns ``None`` if every avenue fails.
    """
    cached = tools_dir / "7zr.exe"
    if cached.is_file() and cached.stat().st_size >= SEVENZR_MIN_SIZE:
        return cached

    for exe in ("7zr.exe", "7z.exe"):
        found = shutil.which(exe)
        if found:
            return Path(found)

    if sys.platform != "win32":
        return None

    try:
        tools_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("Cannot create tools dir %s: %s", tools_dir, exc)
        return None

    logger.info("Bootstrapping 7zr.exe from %s…", SEVENZR_URL)
    try:
        data = _http_get(SEVENZR_URL, timeout=60)
    except (URLError, OSError) as exc:
        logger.warning("Could not download 7zr.exe: %s", exc)
        return None

    if not (SEVENZR_MIN_SIZE <= len(data) <= SEVENZR_MAX_SIZE) or data[:2] != b"MZ":
        logger.warning(
            "Downloaded 7zr.exe looks wrong (size=%d, magic=%r); ignoring.",
            len(data),
            data[:2],
        )
        return None

    # Atomic write: temp name on same volume → rename. Avoids anti-virus
    # races where a partial file is being scanned.
    tmp = cached.with_suffix(".part")
    try:
        tmp.write_bytes(data)
        if cached.exists():
            try:
                cached.unlink()
            except OSError:
                pass
        tmp.replace(cached)
    except OSError as exc:
        logger.warning("Could not persist 7zr.exe at %s: %s", cached, exc)
        try:
            tmp.unlink()
        except OSError:
            pass
        return None

    logger.info("Cached 7zr.exe at %s (%d bytes)", cached, cached.stat().st_size)
    return cached


def _find_dll_in_tree(root: Path) -> Optional[Path]:
    for name in DLL_NAMES:
        matches = list(root.rglob(name))
        if matches:
            return matches[0]
    return None


def _no_window_flag() -> int:
    if sys.platform == "win32":
        return 0x08000000  # CREATE_NO_WINDOW
    return 0
