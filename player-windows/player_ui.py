"""Kiosk-mode UI for the ScreenView Windows player.

Phase 2 Step 3 originally proposed a truly layered design (libmpv on
the bottom, a *transparent* ``QWebEngineView`` permanently on top). On
Linux/macOS that works because compositing is done by the window
manager with per-pixel alpha between sibling HWNDs. On Windows it
does NOT work: DWM does not alpha-blend a Chromium HWND over a sibling
HWND in the same top-level window, so the web view ends up opaquely
clipping mpv behind it — producing audio-only playback. We hit the
symptom on a real kiosk.

Revised architecture (this file)
================================
Three surfaces, **exactly one visible at any given moment**, and a
strict discipline on who owns which transitions:

    ┌────────────────────────────────────────────┐
    │  self (QWidget, frameless, always-on-top)  │
    │                                            │
    │   _video_container   (QWidget)   —  mpv    │  Layer A
    │   _image_label       (QLabel)    —  images │  Layer B
    │   _web_view          (QWebEngineView)      │  Layer C
    │                                            │
    └────────────────────────────────────────────┘

All three children share the same geometry (full window). At any
point exactly one is visible; the other two are hidden. Transitions
go through three private helpers:

  * ``_switch_to_video()``    — hide web view + label; mpv shows.
  * ``_switch_to_image()``    — show label (opaque), hide web view;
                                 mpv keeps running behind but is
                                 occluded by the label.
  * ``_switch_to_overlay()``  — show web view (opaque or transparent
                                 depending on page), hide label.

The web view's stacking is set **once** at startup via ``raise_()``
so the previous black-video regression (reshuffling z-order at
runtime) cannot return. The child visibility toggles are the only
runtime z-order mutation we tolerate — and they never touch the
contents of a hidden view.

Layered rendering for true overlay-on-video (text over a playing
clip) is left for a future PR that migrates mpv to
``QOpenGLWidget + mpv.render_api`` so compositing happens within a
single GL surface. That is a larger change that needs a real Windows
kiosk in the loop, so it's out of scope here.

Runtime contract
================
External interface is unchanged:

  * ``PlayerWindow(fullscreen=..., show_cursor=..., …)``
  * ``.set_playlist(entries: list[PlaylistEntry])`` — queue the next
    playlist; swaps in at end-of-media for gapless transitions.
  * ``.show_status(level, message)`` — log sink the worker signals to.

``PlaylistEntry.kind`` takes values ``image`` / ``video`` /
``stream`` / ``widget``; this PR does not change the manifest format.

Failure handling (from Phase 1, preserved)
==========================================
  * Each entry tracks a failure count; after ``MAX_ITEM_FAILURES``
    failures the entry is skipped for the rest of that playlist.
  * If every entry fails we fall back to the placeholder until a new
    manifest arrives.

Memory hygiene (from Phase 1 Fix 3, preserved here)
===================================================
  * WebEngine profile configured at startup with ``MemoryHttpCache``
    + ``NoPersistentCookies`` + a 50 MiB ceiling.
  * We wipe the profile's in-memory cache + cookies before every
    widget / placeholder load.
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
from PyQt6.QtGui import QGuiApplication, QKeySequence, QPixmap, QShortcut
from PyQt6.QtWidgets import (
    QApplication,
    QLabel,
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

    Children (all full-window, exactly one visible at any moment):

      * ``_video_container`` — native QWidget hosting libmpv.
      * ``_image_label``     — QLabel that renders the current image.
      * ``_web_view``        — QWebEngineView for widgets + placeholder.

    The ``_switch_to_*`` helpers are the only code paths allowed to
    mutate child visibility. ``_render`` calls exactly one of them per
    entry, immediately before handing content to the surface.
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

        # --- Layer A: native container for libmpv ---
        # WA_NativeWindow guarantees Qt creates the HWND up-front so
        # libmpv can attach via winId(). WA_DontCreateNativeAncestors
        # keeps the parent chain logical (non-native) — that's how the
        # video container stays cleanly addressable by mpv without
        # forcing the whole tree into native windows.
        self._video_container = QWidget(self)
        self._video_container.setStyleSheet("background-color: #000;")
        self._video_container.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors)
        self._video_container.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)

        # --- Layer B: QLabel for images ---
        # Kept separate from the WebEngine on purpose. Putting images
        # through Chromium is fine visually but has a side-effect we
        # care about on Windows: a *visible* QWebEngineView sibling
        # will clip a libmpv HWND even with WA_TranslucentBackground
        # set, because DWM doesn't alpha-blend sibling HWNDs. So we
        # use the label for images (cheap, opaque by design) and only
        # wake the WebEngine for widgets + placeholder.
        self._image_label = QLabel(self)
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_label.setStyleSheet("background-color: #000;")
        self._image_label.hide()

        # --- Layer C: QWebEngineView for widgets + placeholder ---
        self._web_view: QWebEngineView | None = None
        if HAS_WEBENGINE:
            self._web_view = QWebEngineView(self)
            # Still set background attributes for cleanliness; they
            # don't help with the DWM issue but keep the rendered
            # page consistent when it IS on screen.
            self._web_view.setStyleSheet("background-color: #000;")
            try:
                profile = self._web_view.page().profile()
                profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.MemoryHttpCache)
                profile.setPersistentCookiesPolicy(
                    QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies
                )
                profile.setHttpCacheMaximumSize(50 * 1024 * 1024)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Could not tune QWebEngineProfile: %s", exc)
            # Raise once so when we DO show the overlay, it sits on
            # top of everything — this is z-order discipline, not
            # alpha-blending.
            self._web_view.raise_()
            # Start hidden; the first real content (placeholder)
            # shows it via ``_switch_to_overlay``.
            self._web_view.hide()

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

        # Track what the overlay currently shows so identical
        # repaints are skipped (e.g. placeholder loaded once).
        self._overlay_signature: Optional[str] = None

        self._disable_close_shortcut()

        if fullscreen:
            self.showFullScreen()
        else:
            self.resize(1280, 720)
            self.show()

        # First frame: branded placeholder.
        self._show_placeholder("Waiting for schedule…")

    # ----- resize handling ---------------------------------------------

    def resizeEvent(self, event) -> None:  # noqa: N802
        """Keep every child filling the window on resize."""
        w, h = self.width(), self.height()
        self._video_container.setGeometry(0, 0, w, h)
        self._image_label.setGeometry(0, 0, w, h)
        if self._web_view is not None:
            self._web_view.setGeometry(0, 0, w, h)
            # Defensive: if the WM reset the z-order on resize (exotic
            # on Windows but harmless to re-affirm) put the overlay
            # back on top of its siblings. Only matters when it's
            # currently visible — hidden views don't participate in
            # z-order.
            if self._web_view.isVisible():
                self._web_view.raise_()
        super().resizeEvent(event)

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

    # ----- layer switching (the ONLY visibility mutation points) -----

    def _switch_to_video(self) -> None:
        """Make libmpv the visible surface. Hides the web view (whose
        opaque HWND would otherwise clip mpv on Windows) and the
        image label.
        """
        if self._web_view is not None and self._web_view.isVisible():
            self._web_view.hide()
        if self._image_label.isVisible():
            self._image_label.hide()
        if not self._video_container.isVisible():
            self._video_container.show()

    def _switch_to_image(self) -> None:
        """Make the image label the visible surface."""
        if self._web_view is not None and self._web_view.isVisible():
            self._web_view.hide()
        if not self._image_label.isVisible():
            self._image_label.show()
            self._image_label.raise_()

    def _switch_to_overlay(self) -> None:
        """Make the WebEngine view the visible surface.

        Used for widgets and the branded placeholder. We re-raise on
        every show so even if Windows reshuffled the z-order while
        the view was hidden, it comes back on top.
        """
        if self._web_view is None:
            # No WebEngine → fall back to image label showing nothing;
            # the caller should treat this as a render error.
            return
        if self._image_label.isVisible():
            self._image_label.hide()
        if not self._web_view.isVisible():
            self._web_view.show()
        self._web_view.raise_()

    # ----- content loaders (one per destination surface) -------------

    def _play_video_on_mpv(self, file_or_url: str) -> None:
        """Send a video / stream to libmpv. Caller must have already
        called :meth:`_switch_to_video`."""
        if self._mpv is None:
            raise RuntimeError(
                "libmpv not available — install libmpv-2.dll or run "
                "scripts\\fetch-libmpv.ps1"
            )
        self._mpv.loadfile(file_or_url, mode="replace")
        self._mpv.pause = False

    def _stop_video_layer(self) -> None:
        """Stop libmpv playback, leaving the container blank."""
        if self._mpv is None:
            return
        try:
            self._mpv.command("stop")
        except Exception as exc:  # noqa: BLE001
            logger.debug("mpv stop failed: %s", exc)

    def _load_image_on_label(self, path: Path) -> None:
        """Paint a scaled QPixmap on the image label. Caller must have
        already called :meth:`_switch_to_image`."""
        pix = QPixmap(str(path))
        if pix.isNull():
            raise ValueError("Unable to load image")
        scaled = pix.scaled(
            self.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._image_label.setPixmap(scaled)

    def _paint_overlay(self, html_document: str, *, signature: str) -> None:
        """Replace the WebEngine document with *html_document*.

        Never called while ``_web_view.isVisible()`` is False is
        SUFFICIENT but not required: Chromium paints off-screen views
        correctly. We still flush its in-memory cache + cookies
        before every load so a long-running kiosk can't balloon its
        RSS via the overlay.
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

    # ----- high-level display helpers --------------------------------

    def _show_placeholder(self, message: str = "ScreenView") -> None:
        """Show the branded fallback frame on the overlay layer."""
        doc = render_placeholder_layout(
            message,
            resolution=(self.width() or 1920, self.height() or 1080),
        )
        self._paint_overlay(doc, signature=f"placeholder:{message}")
        self._switch_to_overlay()

    def _show_image(self, path: Path) -> None:
        """Show an image on the QLabel layer."""
        self._load_image_on_label(path)
        self._switch_to_image()

    def _show_widget(self, path: Path) -> None:
        """Show an HTML widget on the overlay layer.

        Routed through the layout renderer so widget rendering is
        consistent with Phase 2 Layout/Zone future extensions.
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
        self._switch_to_overlay()

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
        self._stop_video_layer()
        self._show_placeholder("Content unavailable")

    # ---- Render dispatch --------------------------------------------

    def _render(self, entry: PlaylistEntry) -> None:
        """Dispatch to the right surface. Each branch calls exactly
        one ``_switch_to_*`` + one content loader; never two layers'
        content within a single branch."""
        path = entry.path

        if entry.kind == "image":
            if path is None:
                raise ValueError("Image entry missing path")
            self._show_image(path)
            return

        if entry.kind == "video":
            # Order matters on Windows: hide the web view BEFORE
            # calling loadfile, so DWM stops clipping mpv's HWND
            # before the new frames start coming in.
            self._switch_to_video()
            self._play_video_on_mpv(str(path))
            return

        if entry.kind == "stream":
            if not entry.stream_url:
                raise ValueError("Stream item missing upstream URL")
            self._switch_to_video()
            self._play_video_on_mpv(entry.stream_url)
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
