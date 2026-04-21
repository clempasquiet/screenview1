"""Kiosk-mode UI for the ScreenView Windows player.

Phase 2 architecture (second attempt, post user-directed fix)
=============================================================

Two full-window children composed by a ``QStackedLayout`` in
``StackingMode.StackAll`` mode — Qt's documented idiom for
overlay-on-video compositing on Windows:

    ┌────────────────────────────────────────────┐
    │  self (QWidget, frameless, opaque black)   │
    │  ┌──────────────────────────────────────┐  │
    │  │ QStackedLayout(StackAll)             │  │
    │  │                                      │  │
    │  │   _video_container  (QWidget)  ← 0   │  Layer 0: libmpv
    │  │   _web_view  (QWebEngineView)  ← 1   │  Layer 1: HTML overlay
    │  │                                      │  (transparent CSS +
    │  │                                      │   WA_TranslucentBackground)
    │  └──────────────────────────────────────┘  │
    └────────────────────────────────────────────┘

Why ``QStackedLayout(StackAll)``
-------------------------------
Previous attempts free-floated the children and raised the overlay
with ``raise_()``. On Windows DWM that broke in two ways:

  * (Step 3 v1) The Chromium HWND opaquely clipped libmpv despite
    WA_TranslucentBackground. The overlay was *always* occluding the
    video.
  * (Step 3 v2) Hiding the overlay during video fixed the occlusion
    but disabled every Phase 2 use case that needs composition over
    video (text, logos, weather, clock).

``QStackedLayout.StackingMode.StackAll`` is the layout manager that
Qt provides precisely for this case: both children remain laid out
and visible together, the layout owns their geometry, and the order
children are ``addWidget``'d to the layout establishes a stable
z-order. With the WebEngine's page background set to
``Qt.GlobalColor.transparent`` and ``WA_TranslucentBackground`` on the
widget, Chromium composites with the mpv surface behind it correctly.

The top-level window (``self``) MUST stay opaque black — making
*it* translucent disables the HWND-level composition that makes the
overlay work on Windows.

Runtime contract
================
External interface is unchanged from Phase 1:

  * ``PlayerWindow(fullscreen=..., show_cursor=..., …)``
  * ``.set_playlist(entries: list[PlaylistEntry])``
  * ``.show_status(level, message)``

``PlaylistEntry.kind`` still takes values ``image`` / ``video`` /
``stream`` / ``widget``.

Overlay content policy
----------------------
  * **video / stream**: mpv plays on Layer 0; the overlay carries a
    zero-zone transparent document so the video shows through.
  * **image**: mpv is stopped (no ghost frame behind the letterbox
    gutters); the overlay renders a full-canvas ``<img>`` zone
    (``object-fit: contain``).
  * **widget**: mpv is stopped; the overlay renders an ``<iframe>``
    zone with the widget's HTML file.
  * **placeholder** (empty / broken playlist): mpv is stopped; the
    overlay renders the branded ``screenview-placeholder`` panel.

Failure handling (from Phase 1, preserved)
==========================================
Each entry tracks a failure count; after ``MAX_ITEM_FAILURES``
failures the entry is skipped for the rest of that playlist. If
every entry fails we fall back to the placeholder until a new
manifest arrives.

Memory hygiene (from Phase 1 Fix 3, preserved)
==============================================
  * WebEngine profile configured at startup with ``MemoryHttpCache``
    + ``NoPersistentCookies`` + a 50 MiB ceiling.
  * We wipe the profile's in-memory cache + cookies before every
    widget / placeholder / image load.
  * ``closeEvent`` performs a final teardown.
"""
from __future__ import annotations

import gc
import logging
import os
import sys
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QSize, Qt, QTimer, QUrl
from PyQt6.QtGui import QGuiApplication, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QApplication,
    QStackedLayout,
    QWidget,
)

try:
    from PyQt6.QtWebEngineWidgets import QWebEngineView  # type: ignore[import-not-found]
    from PyQt6.QtWebEngineCore import QWebEngineProfile  # type: ignore[import-not-found]

    HAS_WEBENGINE = True
except Exception:  # noqa: BLE001
    HAS_WEBENGINE = False
    QWebEngineProfile = None  # type: ignore[assignment]

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

    Uses ``QStackedLayout(StackingMode.StackAll)`` so both children are
    visible together and the overlay's CSS transparency lets the video
    on Layer 0 show through. See the module docstring for the
    architectural rationale.
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
        flags = Qt.WindowType.FramelessWindowHint
        if fullscreen:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        if not show_cursor:
            self.setCursor(Qt.CursorShape.BlankCursor)

        # The top-level window MUST stay opaque (black) on Windows.
        # Making ``self`` translucent breaks the HWND-level composition
        # path that the overlay relies on to blend with libmpv.
        self.setStyleSheet("background-color: black;")

        # ``QStackedLayout(StackAll)`` is Qt's documented idiom for
        # overlay-on-video compositing: both children are laid out to
        # the same rect, both remain visible, and the order they were
        # added fixes the z-order (first ``addWidget`` = bottom).
        self._layout = QStackedLayout(self)
        self._layout.setStackingMode(QStackedLayout.StackingMode.StackAll)
        self._layout.setContentsMargins(0, 0, 0, 0)

        # --- Layer 0: native container for libmpv (background) ---
        self._video_container = QWidget()
        # These attributes force Qt to create a native HWND for the
        # container up-front so libmpv can attach via ``winId()``.
        self._video_container.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors)
        self._video_container.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self._video_container.setStyleSheet("background-color: black;")
        self._layout.addWidget(self._video_container)

        # --- Layer 1: transparent WebEngine overlay (foreground) ---
        self._web_view: QWebEngineView | None = None
        if HAS_WEBENGINE:
            self._web_view = QWebEngineView()
            # Both the Qt attribute and the Chromium page background
            # must agree that the overlay is transparent. The CSS on
            # the rendered document (``background: transparent`` on
            # html+body) completes the chain.
            self._web_view.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            page = self._web_view.page()
            try:
                page.setBackgroundColor(Qt.GlobalColor.transparent)
            except Exception as exc:  # noqa: BLE001
                logger.debug("setBackgroundColor failed: %s", exc)
            self._web_view.setStyleSheet("background: transparent; border: none;")

            # The overlay never handles input — clicks would do
            # nothing in a kiosk anyway. Passing them through lets
            # libmpv (or the underlying OS) receive them cleanly if
            # the kiosk ever regains a pointing device for debugging.
            self._web_view.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

            try:
                profile = page.profile()
                profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.MemoryHttpCache)
                profile.setPersistentCookiesPolicy(
                    QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies
                )
                profile.setHttpCacheMaximumSize(50 * 1024 * 1024)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Could not tune QWebEngineProfile: %s", exc)

            # Added *after* the video container → drawn on top.
            self._layout.addWidget(self._web_view)

        # --- libmpv attachment (must happen after the HWND exists) ---
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

        # --- playlist / scheduling state ---
        self._playlist: list[PlaylistEntry] = []
        self._pending_playlist: Optional[list[PlaylistEntry]] = None
        self._index = 0
        self._failures: dict[int, int] = {}
        self._playlist_broken = False

        self._advance_timer = QTimer(self)
        self._advance_timer.setSingleShot(True)
        self._advance_timer.timeout.connect(self._advance)

        # Short-circuit identical repaints (e.g. placeholder loaded
        # once and reused across every idle transition).
        self._overlay_signature: Optional[str] = None

        self._disable_close_shortcut()

        if fullscreen:
            self.showFullScreen()
        else:
            self.resize(1280, 720)
            self.show()

        # First frame: branded placeholder.
        self._show_placeholder("Waiting for schedule…")

    # ----- public slots ------------------------------------------------

    def set_playlist(self, entries: list[PlaylistEntry]) -> None:
        """Queue a new playlist; swaps in at end-of-media."""
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

        self._pending_playlist = entries

    def show_status(self, level: str, message: str) -> None:
        logger.log(
            {"info": logging.INFO, "warn": logging.WARNING, "error": logging.ERROR}.get(
                level, logging.INFO
            ),
            "status: %s",
            message,
        )

    # ----- Layer 0: libmpv controls ----------------------------------

    def _play_video_on_mpv(self, file_or_url: str) -> None:
        """Send a video / stream to libmpv."""
        if self._mpv is None:
            raise RuntimeError(
                "libmpv not available — install libmpv-2.dll or run "
                "scripts\\fetch-libmpv.ps1"
            )
        self._mpv.loadfile(file_or_url, mode="replace")
        self._mpv.pause = False

    def _stop_video_layer(self) -> None:
        """Stop libmpv playback so no ghost frame shows through the
        transparent overlay when the current item is not a video.

        Critical for image / widget / placeholder paths: the overlay
        is designed to compose over whatever mpv is holding; when we
        don't want that compositing, we clear Layer 0 first.
        """
        if self._mpv is None:
            return
        try:
            self._mpv.command("stop")
        except Exception as exc:  # noqa: BLE001
            logger.debug("mpv stop failed: %s", exc)

    # ----- Layer 1: WebEngine overlay ---------------------------------

    def _paint_overlay(self, html_document: str, *, signature: str) -> None:
        """Replace the overlay's document with *html_document*.

        Skips no-op repaints (same signature ⇒ nothing to do) and
        flushes Chromium's in-memory cache + cookies before every
        load so a long-running kiosk can't balloon its RSS.
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
        self._web_view.setHtml(html_document)
        self._overlay_signature = signature

    def _clear_overlay_for_video(self) -> None:
        """Make the overlay a zero-zone transparent document.

        Called when we switch to a video / stream item: libmpv's
        frames should show through the full window, so we paint
        nothing (the HTML body has ``background: transparent``).
        """
        doc = render_layout_html(
            {
                "resolution_w": self.width() or 1920,
                "resolution_h": self.height() or 1080,
                "zones": [],
            }
        )
        self._paint_overlay(doc, signature="video-transparent")

    def _show_placeholder(self, message: str = "ScreenView") -> None:
        """Show the branded fallback frame.

        The placeholder renders opaque — it hides whatever libmpv
        had on Layer 0 behind it. We also call ``_stop_video_layer``
        to free mpv so letterboxed gutters don't leak the previous
        frame if the overlay size changes mid-transition.
        """
        self._stop_video_layer()
        doc = render_placeholder_layout(
            message,
            resolution=(self.width() or 1920, self.height() or 1080),
        )
        self._paint_overlay(doc, signature=f"placeholder:{message}")

    def _show_image(self, path: Path) -> None:
        """Display a full-canvas image on the overlay.

        libmpv is stopped first so the image's own background (set by
        the generated HTML document body) is not composited with
        whatever clip was playing before.
        """
        self._stop_video_layer()
        src = ensure_absolute_url(str(path))
        doc = render_single_image_layout(
            src,
            resolution=(self.width() or 1920, self.height() or 1080),
        )
        self._paint_overlay(doc, signature=f"image:{src}")

    def _show_widget(self, path: Path) -> None:
        """Display a full-canvas HTML widget on the overlay."""
        self._stop_video_layer()
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

    # ---- playlist cursor / advance logic ----------------------------

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
        #   * Live streams: no natural EOF, arm duration timer.
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
        if self._pending_playlist is not None:
            self._playlist = self._pending_playlist
            self._pending_playlist = None
            self._index = 0
            self._failures.clear()
            self._playlist_broken = False
            self._play_current()
            return
        self._show_placeholder("Content unavailable")

    # ---- Render dispatch --------------------------------------------

    def _render(self, entry: PlaylistEntry) -> None:
        """Dispatch to Layer 0 (video) or Layer 1 (HTML overlay).

        Video / stream branches:
          * Hand the file / URL to libmpv on Layer 0.
          * Paint the overlay to a zero-zone transparent document so
            mpv's frames show through end-to-end.

        Image / widget / placeholder branches:
          * Stop libmpv first so no ghost frame composites behind
            ``object-fit: contain`` letterbox gutters.
          * Paint the overlay with the appropriate HTML document.
        """
        path = entry.path

        if entry.kind == "image":
            if path is None:
                raise ValueError("Image entry missing path")
            self._show_image(path)
            return

        if entry.kind == "video":
            self._play_video_on_mpv(str(path))
            self._clear_overlay_for_video()
            return

        if entry.kind == "stream":
            if not entry.stream_url:
                raise ValueError("Stream item missing upstream URL")
            self._play_video_on_mpv(entry.stream_url)
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

    def _disable_close_shortcut(self) -> None:
        def _swallow() -> None:
            pass

        for seq in (QKeySequence("Alt+F4"), QKeySequence("Ctrl+W")):
            shortcut = QShortcut(seq, self)
            shortcut.activated.connect(_swallow)

    def closeEvent(self, event) -> None:  # noqa: N802
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
