"""Kiosk-mode UI for the ScreenView Windows player.

Phase 2 architecture: **layered** rendering instead of a stack switcher.

Two child surfaces are instantiated once and remain visible together
for the entire process lifetime:

  * **Layer 0** — a native ``QWidget`` that hosts libmpv's hardware
    video output. Plays at most one video or live stream at a time.
  * **Layer 1** — a ``QWebEngineView`` with a **transparent** page +
    ``WA_TranslucentBackground`` on the widget. Renders the current
    frame's HTML composition (images, HTML widgets, text, placeholder).
    Transparent areas of the HTML let the video on Layer 0 show
    through.

The previous design used a ``QStackedWidget`` that flipped between
children. On Windows that caused the "audio plays but picture is
black" bug that spooked PR #10: mutating a *hidden* ``QWebEngineView``
woke up Chromium's compositor which then raised its native HWND above
libmpv's in the OS z-order. With the layered design the web view is
**always** on top by construction, so redrawing it is a no-op for z-
order.

Runtime contract
================
The external interface is unchanged:

  * ``PlayerWindow(fullscreen=..., show_cursor=..., …)``
  * ``.set_playlist(entries: list[PlaylistEntry])`` — queue the next
    playlist; swaps in at end-of-media for gapless transitions.
  * ``.show_status(level, message)`` — log sink the worker signals to.

``PlaylistEntry.kind`` still takes values ``image`` / ``video`` /
``stream`` / ``widget``; this PR does not change the manifest format.
Layout-aware entries (Phase 2 Step 4) will arrive as a new kind
(e.g. ``layout``) without churn to the routing below.

Failure handling
================
  * Render errors never cascade into an infinite retry loop. Each
    entry tracks a failure count; after ``MAX_ITEM_FAILURES`` failures
    the entry is skipped for the rest of that playlist. If every entry
    fails we fall back to the placeholder until a new manifest
    arrives.

Memory hygiene (from Phase 1 Fix 3, preserved here)
===================================================
  * The WebEngine profile is configured at startup with
    ``MemoryHttpCache`` + ``NoPersistentCookies`` + a 50 MiB ceiling
    so a 24/7 kiosk loading many widget pages cannot grow the cache
    on disk.
  * Before each widget load we wipe the profile's in-memory cache +
    cookies.
  * ``closeEvent`` performs a final teardown (about:blank, deleteLater,
    gc.collect) to free Chromium's persistent state at process exit.
"""
from __future__ import annotations

import gc
import logging
import os
import sys
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QSize, Qt, QTimer, QUrl
from PyQt6.QtGui import QColor, QGuiApplication, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QApplication,
    QVBoxLayout,
    QWidget,
)

try:
    from PyQt6.QtWebEngineWidgets import QWebEngineView  # type: ignore[import-not-found]
    from PyQt6.QtWebEngineCore import QWebEngineProfile, QWebEnginePage  # type: ignore[import-not-found]

    HAS_WEBENGINE = True
except Exception:  # noqa: BLE001
    HAS_WEBENGINE = False
    QWebEngineProfile = None  # type: ignore[assignment]
    QWebEnginePage = None  # type: ignore[assignment]

from layout_html import (
    ensure_absolute_url,
    render_layout_html,
    render_placeholder_layout,
    render_single_image_layout,
)
from libmpv_fetch import ensure_libmpv
from worker_network import PlaylistEntry

logger = logging.getLogger(__name__)


# Maximum number of consecutive render failures we accept for a single entry
# before permanently skipping it within the current playlist.
MAX_ITEM_FAILURES = 2

# Retry/backoff used after any render failure. Keeps the UI thread sane and
# the logs readable.
RENDER_ERROR_RETRY_MS = 2000
MIN_DURATION_MS = 1000


# ---------------------------------------------------------------------------
# libmpv bootstrap helpers (unchanged from Phase 1 — kept here for locality)
# ---------------------------------------------------------------------------


def _prepend_to_path(directory: Path) -> None:
    """Add *directory* to ``PATH`` and the Win32 DLL search list."""
    if not directory or not directory.is_dir():
        return
    entry = str(directory)
    if sys.platform == "win32" and hasattr(os, "add_dll_directory"):
        try:
            os.add_dll_directory(entry)
        except (OSError, FileNotFoundError):
            pass
    current = os.environ.get("PATH", "")
    if entry not in current.split(os.pathsep):
        os.environ["PATH"] = entry + os.pathsep + current


def _resolve_libmpv(
    libmpv_dir: str | None,
    app_data_dir: Path | None,
    allow_download: bool,
) -> Path | None:
    """Locate libmpv-2.dll, attempting an auto-download if allowed.

    Must never raise — the player UI has to boot even if this helper
    explodes in a way ``libmpv_fetch`` didn't anticipate.
    """
    bundled = Path(__file__).resolve().parent
    try:
        found = ensure_libmpv(
            bundled_dir=bundled,
            app_data_dir=app_data_dir,
            libmpv_dir=libmpv_dir,
            allow_download=allow_download,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "libmpv resolution failed (%s: %s); continuing without video support.",
            type(exc).__name__,
            exc,
        )
        logger.debug("libmpv resolution traceback:", exc_info=True)
        return None
    if found is None:
        return None
    try:
        _prepend_to_path(found)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not add %s to PATH: %s", found, exc)
    return found


def _try_load_mpv(
    libmpv_dir: str | None,
    app_data_dir: Path | None,
    allow_download: bool,
):
    """Import python-mpv lazily so the rest of the UI can boot without it."""
    resolved = _resolve_libmpv(libmpv_dir, app_data_dir, allow_download)
    try:
        import mpv  # type: ignore[import-untyped]
    except (ImportError, OSError) as exc:
        if resolved is None:
            logger.warning(
                "libmpv not available (no DLL found and auto-download failed). "
                "Video items will show the placeholder. Details: %s",
                exc,
            )
        else:
            logger.warning("libmpv present at %s but mpv import failed: %s", resolved, exc)
        return None
    return mpv


# ---------------------------------------------------------------------------
# PlayerWindow
# ---------------------------------------------------------------------------


class PlayerWindow(QWidget):
    """Fullscreen, borderless, always-on-top kiosk window.

    Children (always both alive, both visible):

      * ``_video_container``  — Layer 0, hosts libmpv.
      * ``_web_view``         — Layer 1, transparent HTML overlay.

    The web view is raised above the video container at construction
    time and we **never** reshuffle the z-order afterwards. That is
    how we avoid the Phase-1 "black video" regression.
    """

    def __init__(
        self,
        fullscreen: bool = True,
        show_cursor: bool = False,
        libmpv_dir: str | None = None,
        libmpv_app_data_dir: Path | None = None,
        libmpv_auto_download: bool = True,
    ) -> None:
        super().__init__()
        self.setWindowTitle("ScreenView Player")
        self.setStyleSheet("background-color: #000; color: #e6e8ef;")
        flags = Qt.WindowType.FramelessWindowHint
        if fullscreen:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        if not show_cursor:
            self.setCursor(Qt.CursorShape.BlankCursor)

        # No QVBoxLayout / QStackedWidget: we position the two children
        # manually in ``resizeEvent`` so they both occupy the whole
        # window and the overlay stays on top regardless of what the
        # layout manager would otherwise do on stack changes.
        self._video_container = QWidget(self)
        self._video_container.setStyleSheet("background-color: #000;")
        self._video_container.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors)
        self._video_container.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)

        self._web_view: QWebEngineView | None = None
        if HAS_WEBENGINE:
            self._web_view = QWebEngineView(self)
            # Make the web surface itself transparent at every layer
            # the compositor cares about: the widget attribute, the
            # page background colour (Chromium), and the underlying
            # palette (Qt).
            self._web_view.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            self._web_view.setStyleSheet("background: transparent;")
            page = self._web_view.page()
            try:
                page.setBackgroundColor(QColor(0, 0, 0, 0))
            except Exception as exc:  # noqa: BLE001
                # Non-fatal: the HTML's own background still renders.
                logger.debug("setBackgroundColor failed: %s", exc)

            # Cap the WebEngine disk cache so a 24/7 kiosk loading many
            # widget pages cannot grow it unbounded. Mirrors the Phase
            # 1 Fix 3 tuning.
            try:
                profile = page.profile()
                profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.MemoryHttpCache)
                profile.setPersistentCookiesPolicy(
                    QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies
                )
                profile.setHttpCacheMaximumSize(50 * 1024 * 1024)  # 50 MiB
            except Exception as exc:  # noqa: BLE001
                logger.debug("Could not tune QWebEngineProfile: %s", exc)

            # Final, and most important: keep Layer 1 above Layer 0.
            # ``raise_`` is called once here and never again — touching
            # the z-order at runtime is what caused the black-video
            # bug during Phase 1.
            self._web_view.raise_()

        # libmpv attaches to the video container's native HWND. Must
        # happen after ``_video_container.winId()`` has been realised
        # (Qt creates the native window lazily on the first winId()
        # call, which our attribute flags above guarantee).
        self._mpv = None
        mpv = _try_load_mpv(libmpv_dir, libmpv_app_data_dir, libmpv_auto_download)
        if mpv is not None:
            try:
                self._mpv = mpv.MPV(
                    wid=str(int(self._video_container.winId())),
                    vo="gpu",
                    keep_open="yes",
                    hwdec="auto-safe",
                    loop_file="no",
                    osc=False,
                )
                self._mpv.observe_property("eof-reached", self._on_mpv_eof)
            except Exception as exc:  # noqa: BLE001
                logger.warning("mpv initialisation failed: %s", exc)
                self._mpv = None

        self._playlist: list[PlaylistEntry] = []
        self._pending_playlist: Optional[list[PlaylistEntry]] = None
        self._index = 0
        # Per-entry failure counters (reset on each playlist swap).
        self._failures: dict[int, int] = {}
        self._playlist_broken = False

        self._advance_timer = QTimer(self)
        self._advance_timer.setSingleShot(True)
        self._advance_timer.timeout.connect(self._advance)

        # Track what the overlay is currently showing so the "clear the
        # overlay when moving from widget → video" path can do the
        # minimum work. ``None`` = untouched since startup; any other
        # value is the source that's currently loaded.
        self._overlay_signature: Optional[str] = None

        self._disable_close_shortcut()

        if fullscreen:
            self.showFullScreen()
        else:
            self.resize(1280, 720)
            self.show()

        # First overlay paint: the branded placeholder. Done after
        # ``show`` so the web view has a surface to draw on.
        self._show_placeholder("Waiting for schedule…")

    # ----- resize handling (no layout manager for the overlay pair) -----

    def resizeEvent(self, event) -> None:  # noqa: N802
        """Keep both surfaces filling the window on every resize."""
        self._video_container.setGeometry(0, 0, self.width(), self.height())
        if self._web_view is not None:
            self._web_view.setGeometry(0, 0, self.width(), self.height())
            # ``raise_`` is idempotent; calling it after a resize is
            # defensive in case a window manager on an exotic platform
            # treats resize as a z-order reset.
            self._web_view.raise_()
        super().resizeEvent(event)

    # ----- public slots --------------------------------------------------

    def set_playlist(self, entries: list[PlaylistEntry]) -> None:
        """Queue a new playlist. It swaps in at the end of the current item.

        Any previous "this playlist is broken" state is cleared because
        a fresh manifest may include different media types that *can*
        play.
        """
        if not entries:
            self._playlist = []
            self._pending_playlist = None
            self._failures.clear()
            self._playlist_broken = False
            self._stop_video_layer()
            self._show_placeholder("Waiting for schedule…")
            return

        if not self._playlist or self._playlist_broken:
            self._playlist = entries
            self._index = 0
            self._failures.clear()
            self._playlist_broken = False
            self._play_current()
            return

        # Defer the swap until the current media ends.
        self._pending_playlist = entries

    def show_status(self, level: str, message: str) -> None:
        logger.log(
            {"info": logging.INFO, "warn": logging.WARNING, "error": logging.ERROR}.get(
                level, logging.INFO
            ),
            "status: %s",
            message,
        )

    # ----- internals -----------------------------------------------------

    def _disable_close_shortcut(self) -> None:
        def _swallow() -> None:
            pass

        for seq in (QKeySequence("Alt+F4"), QKeySequence("Ctrl+W")):
            shortcut = QShortcut(seq, self)
            shortcut.activated.connect(_swallow)

    # ---- Layer 0 (mpv) operations — NEVER touch the web view ----------

    def _play_video_on_layer0(self, file_or_url: str) -> None:
        """Send a video / stream to libmpv.

        Intentionally does **not** mutate ``self._web_view``: the
        overlay is whatever the previous ``_render`` call left there
        (typically the placeholder or a transparent layout). Touching
        the overlay on the video path is what caused the Phase-1
        regression.
        """
        if self._mpv is None:
            raise RuntimeError(
                "libmpv not available — install libmpv-2.dll or run "
                "scripts\\fetch-libmpv.ps1"
            )
        self._mpv.loadfile(file_or_url, mode="replace")
        self._mpv.pause = False

    def _stop_video_layer(self) -> None:
        """Stop libmpv playback, leaving the video container blank.

        Called when the playlist empties so we don't keep the last
        frame of the previous item on screen behind a transparent
        overlay.
        """
        if self._mpv is None:
            return
        try:
            self._mpv.command("stop")
        except Exception as exc:  # noqa: BLE001
            logger.debug("mpv stop failed: %s", exc)

    # ---- Layer 1 (overlay) operations — NEVER talk to libmpv ----------

    def _paint_overlay(self, html_document: str, *, signature: str) -> None:
        """Replace the overlay with *html_document*.

        ``signature`` is an opaque string used to short-circuit
        identical repaints (e.g. the placeholder is loaded once and
        reused for every empty-playlist transition). Before every
        load we wipe the WebEngine profile's in-memory cache + cookies
        so long-running widget rotation can't grow the RSS unbounded.
        """
        if self._web_view is None:
            return
        if self._overlay_signature == signature:
            return

        try:
            profile = self._web_view.page().profile()
            profile.clearHttpCache()
            profile.cookieStore().deleteAllCookies()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Pre-load WebEngine cleanup failed: %s", exc)

        # ``setHtml`` renders directly from the string; no temp file
        # needed, no base URL needed for data-less placeholders.
        self._web_view.setHtml(html_document)
        self._overlay_signature = signature

    def _show_placeholder(self, message: str = "ScreenView") -> None:
        """Paint the branded fallback frame on the overlay.

        Rendered as an opaque HTML panel so the video behind it
        (whatever mpv happens to be holding) is hidden. The
        placeholder is the only overlay state that's intentionally
        opaque; every other state is transparent and lets Layer 0
        show through.
        """
        doc = render_placeholder_layout(
            message,
            resolution=(self.width() or 1920, self.height() or 1080),
        )
        self._paint_overlay(doc, signature=f"placeholder:{message}")

    def _show_image(self, path: Path) -> None:
        """Paint a single full-canvas image on the overlay."""
        src = ensure_absolute_url(str(path))
        doc = render_single_image_layout(
            src,
            resolution=(self.width() or 1920, self.height() or 1080),
        )
        # ``path`` is a stable cache-dir filename (md5-based), so it
        # makes a perfect signature for the short-circuit.
        self._paint_overlay(doc, signature=f"image:{src}")

    def _show_widget(self, path: Path) -> None:
        """Paint a full-canvas HTML widget on the overlay.

        Routed through the same layout pipeline as images/placeholder
        so the whole pipeline has exactly one paint code path —
        simpler, and easier to reason about for the no-more-black-
        video invariant.
        """
        src = ensure_absolute_url(str(path))
        doc = render_layout_html(
            {
                "resolution_w": self.width() or 1920,
                "resolution_h": self.height() or 1080,
                "zones": [
                    {
                        "id": "widget-fullscreen",
                        "kind": "widget",
                        "position_x": 0,
                        "position_y": 0,
                        "width": self.width() or 1920,
                        "height": self.height() or 1080,
                        "z_index": 0,
                        "src": src,
                    }
                ],
            }
        )
        self._paint_overlay(doc, signature=f"widget:{src}")

    def _clear_overlay_for_video(self) -> None:
        """Make the overlay transparent so the video is visible.

        Called once when switching **to** a video/stream entry. We
        paint a no-zones layout: everything behind it (libmpv) is
        visible because the HTML body has ``background: transparent``.
        """
        doc = render_layout_html(
            {
                "resolution_w": self.width() or 1920,
                "resolution_h": self.height() or 1080,
                "zones": [],
            }
        )
        self._paint_overlay(doc, signature="video-transparent")

    # ---- playlist cursor / advance logic ------------------------------

    def _play_current(self) -> None:
        if not self._playlist:
            self._show_placeholder()
            return

        # Skip entries that have already exceeded their failure budget.
        start = self._index
        n = len(self._playlist)
        attempts = 0
        while attempts < n:
            entry = self._playlist[self._index]
            if self._failures.get(id(entry), 0) < MAX_ITEM_FAILURES:
                break
            self._index = (self._index + 1) % n
            attempts += 1
            if self._index == start:
                break

        entry = self._playlist[self._index]
        if self._failures.get(id(entry), 0) >= MAX_ITEM_FAILURES:
            self._mark_playlist_broken()
            return

        self._advance_timer.stop()
        try:
            self._render(entry)
        except Exception as exc:  # noqa: BLE001
            fails = self._failures.get(id(entry), 0) + 1
            self._failures[id(entry)] = fails
            logger.warning(
                "Render failed for %s (%s/%s): %s",
                entry.original_name,
                fails,
                MAX_ITEM_FAILURES,
                exc,
            )
            self.show_status(
                "warn",
                f"Render failed for {entry.original_name}: {exc}",
            )
            if fails >= MAX_ITEM_FAILURES and self._all_entries_broken():
                self._mark_playlist_broken()
                return
            self._index = (self._index + 1) % len(self._playlist)
            QTimer.singleShot(RENDER_ERROR_RETRY_MS, self._play_current)
            return

        # Auto-advance policy:
        #   * Recorded videos: advance on libmpv's eof-reached event.
        #   * Live streams: no natural EOF, so we *also* arm the
        #     duration timer. Whichever fires first wins.
        #   * Images / widgets: timer-driven.
        needs_timer = entry.kind != "video" or self._mpv is None
        if needs_timer:
            duration_ms = max(MIN_DURATION_MS, entry.duration * 1000)
            self._advance_timer.start(duration_ms)

    def _all_entries_broken(self) -> bool:
        return all(
            self._failures.get(id(e), 0) >= MAX_ITEM_FAILURES for e in self._playlist
        )

    def _mark_playlist_broken(self) -> None:
        if not self._playlist_broken:
            logger.error(
                "Every item in the current playlist failed to render; "
                "holding on the placeholder until a new manifest arrives."
            )
        self._playlist_broken = True
        self._advance_timer.stop()
        # Swap in any queued playlist immediately — it may fix the situation.
        if self._pending_playlist is not None:
            self._playlist = self._pending_playlist
            self._pending_playlist = None
            self._index = 0
            self._failures.clear()
            self._playlist_broken = False
            self._play_current()
            return
        self._stop_video_layer()
        self._show_placeholder("Content unavailable")

    # ---- Render dispatch -----------------------------------------------

    def _render(self, entry: PlaylistEntry) -> None:
        """Dispatch a PlaylistEntry to Layer 0 (video) or Layer 1 (HTML).

        Layering rule
        -------------
        * Every branch talks to **exactly one** of
          ``_play_video_on_layer0`` / ``_paint_overlay`` (via the
          ``_show_*`` helpers). Mixing the two layers from a single
          branch is what would re-enable the Phase-1 black-video
          regression on Windows.
        * The ``video`` and ``stream`` branches additionally clear the
          overlay to transparent so whatever was there (a previous
          image, the placeholder) doesn't occlude libmpv.
        * The static check in tests/test_render_video_isolation.py
          pins these rules so a future refactor can't silently break
          them.
        """
        path = entry.path

        if entry.kind == "image":
            # Layer 1 only. Layer 0 keeps playing its last frame
            # *underneath* but is fully occluded by the opaque-backed
            # layout (we'd use a transparent canvas if we wanted to
            # show the video through the image; that's Phase-4
            # territory).
            if path is None:
                raise ValueError("Image entry missing path")
            self._show_image(path)
            return

        if entry.kind == "video":
            # Layer 0 only (for the loadfile call). Layer 1 is painted
            # to transparent so the video is visible end-to-end.
            self._play_video_on_layer0(str(path))
            self._clear_overlay_for_video()
            return

        if entry.kind == "stream":
            if not entry.stream_url:
                raise ValueError("Stream item missing upstream URL")
            self._play_video_on_layer0(entry.stream_url)
            self._clear_overlay_for_video()
            return

        if entry.kind == "widget":
            if self._web_view is None:
                raise RuntimeError("QWebEngineView not available")
            if path is None:
                raise ValueError("Widget entry missing path")
            self._show_widget(path)
            return

        raise ValueError(f"Unknown media kind: {entry.kind}")

    def _advance(self) -> None:
        if not self._playlist:
            self._show_placeholder()
            return
        self._index += 1
        if self._index >= len(self._playlist):
            if self._pending_playlist is not None:
                self._playlist = self._pending_playlist
                self._pending_playlist = None
                self._index = 0
                self._failures.clear()
                self._playlist_broken = False
            else:
                self._index = 0
        self._play_current()

    def _on_mpv_eof(self, _name: str, value: bool) -> None:  # noqa: FBT001
        if value:
            QTimer.singleShot(0, self._advance)

    def closeEvent(self, event) -> None:  # noqa: N802
        # Tear down libmpv first — blocks a few hundred ms on decoder
        # shutdown, which must happen before we release the WebEngine
        # or Qt's display server resources.
        if self._mpv is not None:
            try:
                self._mpv.terminate()
            except Exception:  # noqa: BLE001
                pass
            self._mpv = None

        if self._web_view is not None:
            try:
                profile = self._web_view.page().profile()
                profile.clearHttpCache()
                profile.clearAllVisitedLinks()
                profile.cookieStore().deleteAllCookies()
            except Exception as exc:  # noqa: BLE001
                logger.debug("WebEngine final cleanup failed: %s", exc)
            try:
                self._web_view.setUrl(QUrl("about:blank"))
                self._web_view.deleteLater()
            except Exception:  # noqa: BLE001
                pass
            self._web_view = None
            gc.collect()

        super().closeEvent(event)


def screen_size() -> QSize:
    screen = QGuiApplication.primaryScreen()
    if screen is None:
        return QSize(1920, 1080)
    return screen.size()


__all__ = ["PlayerWindow", "QApplication"]
