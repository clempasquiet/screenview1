"""Kiosk-mode UI for the ScreenView Windows player.

Phase 2 Step 4 architecture — Layout-aware rendering
====================================================

The player now consumes a tree-shaped manifest (see
``server/schemas.py :: LayoutManifest``): a list of *slides*, each
carrying a *layout* with one or more *zones*, each zone holding an
ordered playlist of media items. Every slide has a global duration;
the player cycles slide-by-slide, not item-by-item.

Single-visible-surface dispatch (from PR #17 / #15) is preserved.
Three full-window children, exactly one visible at any moment:

    ┌────────────────────────────────────────────┐
    │  self (QWidget, frameless, always-on-top)  │
    │                                            │
    │   _video_container   (QWidget)   —  mpv    │  Layer A
    │   _image_label       (QLabel)    —  images │  Layer B
    │   _web_view          (QWebEngineView)      │  Layer C
    │                                            │
    └────────────────────────────────────────────┘

Visibility transitions go through ``_switch_to_video`` /
``_switch_to_image`` / ``_switch_to_overlay`` helpers. We tried a
truly layered design (transparent WebEngine over libmpv) three times
in Phase 2; DWM on Windows does not alpha-blend sibling HWNDs so a
visible web view would always occlude mpv. See README principle 8.

Slide dispatch (three branches, with the mixed-layout trade-off)
----------------------------------------------------------------

For each slide the player picks one of three rendering paths:

  1. **Pure image** (single zone, single image item, full-canvas) →
     ``_switch_to_image`` + ``QLabel.setPixmap``. Cheapest, sharpest,
     used whenever the slide's shape matches (covers all pre-Phase-2
     legacy playlists).

  2. **HTML-only layout** (no video/stream anywhere in the slide) →
     ``_switch_to_overlay`` + ``render_layout_html``. Multi-zone
     composition with absolute-positioned divs for images, widgets
     (iframes with sandbox), and text. Zone geometry is preserved.

  3. **Video-bearing slide** (any zone contains a video or stream) →
     ``_switch_to_video`` + ``mpv.loadfile`` on the FIRST video/stream
     item encountered. The video plays **full-screen** with loop,
     other zones are dropped with a warning log.

     This is the documented Phase 2 trade-off: proper video-in-zone
     compositing requires migrating mpv to ``mpv.render_api`` +
     ``QOpenGLWidget`` so everything lives in one GL surface.
     Deferred until a concrete use case requires it — see README
     principle 8 for the (a)/(b) discussion.

Timer
-----

Auto-advance is driven by ``slide.duration`` — the server-computed
total on-screen time for the slide — not per-item timings. Video
slides loop the playing clip for the full slide duration so a short
clip in a long slot doesn't freeze on its last frame.

Failure handling (preserved)
----------------------------

Each slide carries a failure counter keyed on ``slide_id``. After
``MAX_SLIDE_FAILURES`` consecutive render failures on the same
slide we skip it for the rest of the playlist; if every slide fails
we fall back to the placeholder until a new manifest arrives.
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
    render_single_image_layout,
)
from libmpv_fetch import ensure_libmpv
from worker_network import Slide, Zone, ZoneItem

logger = logging.getLogger(__name__)


# Maximum number of consecutive render failures we accept for a single
# slide before permanently skipping it within the current playlist.
MAX_SLIDE_FAILURES = 2

# Retry/backoff used after any render failure. Keeps the UI thread sane
# and the logs readable.
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

    Consumes ``list[Slide]`` playlists emitted by the network worker.
    See the module docstring for the slide-dispatch contract.
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

        # Top-level window is opaque black. DO NOT make this
        # translucent — it breaks the HWND-level compositing the
        # children rely on. See README principle 8.
        self.setStyleSheet("background-color: black;")

        # --- Layer A: native container for libmpv ---
        self._video_container = QWidget(self)
        self._video_container.setStyleSheet("background-color: black;")
        self._video_container.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors)
        self._video_container.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)

        # --- Layer B: QLabel for single images ---
        # Stays on the QLabel path rather than going through the
        # WebEngine so the fast-path for plain-image playlists does
        # not have to wake Chromium's HWND.
        self._image_label = QLabel(self)
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_label.setStyleSheet("background-color: black;")
        self._image_label.hide()

        # --- Layer C: QWebEngineView for widgets + multi-zone HTML + placeholder ---
        self._web_view: QWebEngineView | None = None
        if HAS_WEBENGINE:
            self._web_view = QWebEngineView(self)
            self._web_view.setStyleSheet("background-color: black;")
            try:
                profile = self._web_view.page().profile()
                profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.MemoryHttpCache)
                profile.setPersistentCookiesPolicy(
                    QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies
                )
                profile.setHttpCacheMaximumSize(50 * 1024 * 1024)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Could not tune QWebEngineProfile: %s", exc)
            self._web_view.raise_()
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
            except Exception as exc:  # noqa: BLE001
                logger.warning("mpv initialisation failed: %s", exc)
                self._mpv = None

        # --- playlist / scheduling state ---
        self._playlist: list[Slide] = []
        self._pending_playlist: Optional[list[Slide]] = None
        self._index = 0
        # Failure counters keyed by slide_id so cross-playlist swaps
        # don't bleed state between runs.
        self._failures: dict[str, int] = {}
        self._playlist_broken = False

        self._advance_timer = QTimer(self)
        self._advance_timer.setSingleShot(True)
        self._advance_timer.timeout.connect(self._advance)

        # Short-circuit identical repaints.
        self._overlay_signature: Optional[str] = None

        self._disable_close_shortcut()

        if fullscreen:
            self.showFullScreen()
        else:
            self.resize(1280, 720)
            self.show()

        self._show_placeholder("Waiting for schedule…")

    # ----- resize handling ---------------------------------------------

    def resizeEvent(self, event) -> None:  # noqa: N802
        """Keep every child filling the window on resize."""
        w, h = self.width(), self.height()
        self._video_container.setGeometry(0, 0, w, h)
        self._image_label.setGeometry(0, 0, w, h)
        if self._web_view is not None:
            self._web_view.setGeometry(0, 0, w, h)
            if self._web_view.isVisible():
                self._web_view.raise_()
        super().resizeEvent(event)

    # ----- public slots ------------------------------------------------

    def set_playlist(self, slides: list[Slide]) -> None:
        """Queue a new Layout-tree playlist; swaps in at end of current slide.

        Accepts the output of ``NetworkWorker.playlist_ready`` — a
        list of :class:`worker_network.Slide`. Empty list means "no
        schedule assigned": stop video, show the placeholder.
        """
        if not slides:
            self._playlist = []
            self._pending_playlist = None
            self._failures.clear()
            self._playlist_broken = False
            self._stop_video_layer()
            self._show_placeholder("Waiting for schedule…")
            return

        if not self._playlist or self._playlist_broken:
            self._playlist = slides
            self._index = 0
            self._failures.clear()
            self._playlist_broken = False
            self._play_current()
            return

        # Defer: finish the current slide before swapping.
        self._pending_playlist = slides

    def show_status(self, level: str, message: str) -> None:
        logger.log(
            {"info": logging.INFO, "warn": logging.WARNING, "error": logging.ERROR}.get(
                level, logging.INFO
            ),
            "status: %s",
            message,
        )

    # ----- Layer switching (only visibility mutation points) ----------

    def _switch_to_video(self) -> None:
        """Make libmpv the visible surface."""
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

        Used for multi-zone layouts, single widgets, and the branded
        placeholder. We re-raise on every show so even if Windows
        reshuffled the z-order while the view was hidden, it comes
        back on top.
        """
        if self._web_view is None:
            return
        if self._image_label.isVisible():
            self._image_label.hide()
        if not self._web_view.isVisible():
            self._web_view.show()
        self._web_view.raise_()

    # ----- Layer A: libmpv controls -----------------------------------

    def _play_video_on_mpv(self, file_or_url: str, *, loop: bool = False) -> None:
        """Send a video/stream to libmpv.

        When ``loop`` is True, the clip loops forever until the slide
        timer expires or we move on. This is the right default for
        signage slides with a slot duration longer than the clip
        itself — otherwise mpv would freeze on the last frame.
        """
        if self._mpv is None:
            raise RuntimeError(
                "libmpv not available — install libmpv-2.dll or run "
                "scripts\\fetch-libmpv.ps1"
            )
        try:
            self._mpv.loop_file = "inf" if loop else "no"
        except Exception as exc:  # noqa: BLE001
            logger.debug("mpv loop_file set failed: %s", exc)
        self._mpv.loadfile(file_or_url, mode="replace")
        self._mpv.pause = False

    def _stop_video_layer(self) -> None:
        """Stop libmpv playback."""
        if self._mpv is None:
            return
        try:
            self._mpv.command("stop")
        except Exception as exc:  # noqa: BLE001
            logger.debug("mpv stop failed: %s", exc)

    # ----- Layer C: WebEngine overlay ---------------------------------

    def _paint_overlay(self, html_document: str, *, signature: str) -> None:
        """Replace the overlay document, skipping identical repaints."""
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

    # ----- high-level display helpers ---------------------------------

    def _show_placeholder(self, message: str = "ScreenView") -> None:
        doc = render_placeholder_layout(
            message,
            resolution=(self.width() or 1920, self.height() or 1080),
        )
        self._paint_overlay(doc, signature=f"placeholder:{message}")
        self._switch_to_overlay()

    def _show_image(self, path: Path) -> None:
        """Render one image on the QLabel fast path."""
        pix = QPixmap(str(path))
        if pix.isNull():
            raise ValueError("Unable to load image")
        scaled = pix.scaled(
            self.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._image_label.setPixmap(scaled)
        self._switch_to_image()

    def _show_layout_html(self, slide: Slide) -> None:
        """Render a multi-zone layout via the HTML overlay.

        Each zone becomes an absolutely-positioned ``<div>`` using
        the layout's authoring resolution as the coordinate space.
        Items' first-in-order is picked to represent each zone (a
        zone's own mini-playlist rotation is a later increment
        that would need per-zone timers inside the HTML; not in
        this PR).
        """
        if self._web_view is None:
            raise RuntimeError("QWebEngineView not available — cannot render layouts")

        zones_dict = []
        for zone in slide.zones:
            if not zone.items:
                # Empty zone → leave a transparent placeholder region.
                zones_dict.append(_zone_to_dict(zone, kind="empty", payload={}))
                continue
            # For now pick the first item in the zone. Zone-level
            # playlists will get their own timers in a follow-up.
            item = zone.items[0]
            if item.kind == "image":
                src = ensure_absolute_url(str(item.path)) if item.path else ""
                zones_dict.append(_zone_to_dict(zone, kind="image", payload={"src": src}))
            elif item.kind == "widget":
                src = ensure_absolute_url(str(item.path)) if item.path else ""
                zones_dict.append(_zone_to_dict(zone, kind="widget", payload={"src": src}))
            else:
                # video/stream items in an HTML-only slide shouldn't
                # reach this code path — the dispatcher routes video-
                # bearing slides to the mpv layer instead. If it does
                # (e.g. a stream with no URL), render an empty zone.
                logger.warning(
                    "Zone %s has a %s item in an HTML-only render path; "
                    "rendering as empty.",
                    zone.name, item.kind,
                )
                zones_dict.append(_zone_to_dict(zone, kind="empty", payload={}))

        doc = render_layout_html(
            {
                "resolution_w": slide.resolution_w,
                "resolution_h": slide.resolution_h,
                "zones": zones_dict,
            }
        )
        # Signature: slide_id + the zone content hashes so a cycle
        # through identical slides doesn't trigger useless reloads.
        self._paint_overlay(doc, signature=f"slide:{slide.slide_id}")
        self._switch_to_overlay()

    # ---- playlist cursor / advance logic -----------------------------

    def _play_current(self) -> None:
        if not self._playlist:
            self._show_placeholder()
            return

        # Skip slides that have already exceeded their failure budget.
        start = self._index
        n = len(self._playlist)
        attempts = 0
        while attempts < n:
            slide = self._playlist[self._index]
            if self._failures.get(slide.slide_id, 0) < MAX_SLIDE_FAILURES:
                break
            self._index = (self._index + 1) % n
            attempts += 1
            if self._index == start:
                break

        slide = self._playlist[self._index]
        if self._failures.get(slide.slide_id, 0) >= MAX_SLIDE_FAILURES:
            self._mark_playlist_broken()
            return

        self._advance_timer.stop()
        try:
            self._render_slide(slide)
        except Exception as exc:  # noqa: BLE001
            fails = self._failures.get(slide.slide_id, 0) + 1
            self._failures[slide.slide_id] = fails
            logger.warning(
                "Render failed for slide %s (%s/%s): %s",
                slide.slide_id, fails, MAX_SLIDE_FAILURES, exc,
            )
            self.show_status(
                "warn", f"Render failed for slide {slide.slide_id}: {exc}",
            )
            if fails >= MAX_SLIDE_FAILURES and self._all_slides_broken():
                self._mark_playlist_broken()
                return
            self._index = (self._index + 1) % len(self._playlist)
            QTimer.singleShot(RENDER_ERROR_RETRY_MS, self._play_current)
            return

        # Global slide timer drives every transition. Even video-
        # bearing slides are timer-driven: mpv loops the clip inside
        # the slot so a short clip in a long slot doesn't freeze on
        # its last frame.
        duration_ms = max(MIN_DURATION_MS, slide.duration * 1000)
        self._advance_timer.start(duration_ms)

    def _all_slides_broken(self) -> bool:
        return all(
            self._failures.get(s.slide_id, 0) >= MAX_SLIDE_FAILURES
            for s in self._playlist
        )

    def _mark_playlist_broken(self) -> None:
        if not self._playlist_broken:
            logger.error(
                "Every slide in the current playlist failed to render; "
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

    # ---- Render dispatch ---------------------------------------------

    def _render_slide(self, slide: Slide) -> None:
        """Pick one of three render paths based on slide shape.

        The picks are intentionally conservative (prefer the simplest
        path that matches) and each path leaves the other two
        surfaces in a consistent "off" state.
        """

        # Case 3 (checked first — wins over multi-zone composition per
        # the documented Phase 2 trade-off): any video or stream item
        # in the slide plays full-screen, other zones omitted.
        if slide.has_video:
            item = slide.first_video_item()
            assert item is not None  # has_video invariant
            dropped_count = sum(len(z.items) for z in slide.zones) - 1
            if dropped_count > 0:
                logger.warning(
                    "Slide %s: playing %s full-screen; %d other zone item(s) "
                    "omitted (single-visible-surface constraint, see README "
                    "principle 8).",
                    slide.slide_id, item.kind, dropped_count,
                )
            self._switch_to_video()
            # Loop the clip so a short video doesn't freeze in a long
            # slot. Streams loop implicitly (mpv keeps the last frame
            # until the remote end sends more).
            loop_it = item.kind == "video" and item.duration < slide.duration
            if item.kind == "video":
                if item.path is None:
                    raise ValueError("Video item missing cached path")
                self._play_video_on_mpv(str(item.path), loop=loop_it)
            else:  # stream
                if not item.stream_url:
                    raise ValueError("Stream item missing upstream URL")
                self._play_video_on_mpv(item.stream_url, loop=False)
            return

        # Case 1 (fast path): single zone, single image, full-canvas.
        # Matches all legacy single-media slots as well as authored
        # single-image layouts.
        if (
            len(slide.zones) == 1
            and len(slide.zones[0].items) == 1
            and slide.zones[0].items[0].kind == "image"
            and _zone_is_full_canvas(slide.zones[0], slide.resolution_w, slide.resolution_h)
        ):
            item = slide.zones[0].items[0]
            if item.path is None:
                raise ValueError("Image item missing cached path")
            self._stop_video_layer()
            self._show_image(item.path)
            return

        # Case 2: any other HTML-only slide → multi-zone layout HTML.
        self._stop_video_layer()
        self._show_layout_html(slide)

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _zone_is_full_canvas(zone: Zone, canvas_w: int, canvas_h: int) -> bool:
    """True if ``zone`` covers the whole authoring canvas at (0,0)."""
    return (
        zone.position_x == 0
        and zone.position_y == 0
        and zone.width == canvas_w
        and zone.height == canvas_h
    )


def _zone_to_dict(zone: Zone, *, kind: str, payload: dict) -> dict:
    """Turn a :class:`Zone` into the dict shape ``render_layout_html``
    expects. Layer the kind + payload over the zone's geometry."""
    base = {
        "id": zone.zone_id if zone.zone_id is not None else zone.name,
        "kind": kind,
        "name": zone.name,
        "position_x": zone.position_x,
        "position_y": zone.position_y,
        "width": zone.width,
        "height": zone.height,
        "z_index": zone.z_index,
    }
    base.update(payload)
    return base


def screen_size() -> QSize:
    screen = QGuiApplication.primaryScreen()
    if screen is None:
        return QSize(1920, 1080)
    return screen.size()


__all__ = ["PlayerWindow", "QApplication"]
