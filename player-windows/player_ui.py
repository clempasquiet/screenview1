"""Kiosk-mode UI for the ScreenView Windows player.

Runs exclusively on the main (UI) thread. Responsibilities:
  * Render the current playlist using libmpv (video), QLabel (image), or
    QWebEngineView (widget / HTML).
  * Advance through the playlist on timers / libmpv end-of-file events.
  * Never block on network I/O and never show a black screen: if no
    playlist is available, or if every item fails to render, show a
    branded placeholder frame.
  * Disable native OS shortcuts that could break kiosk mode (Alt+F4).

Playlist swaps are atomic: we keep the current media playing until it ends
before loading the next list, preventing visual glitches.

Failure handling:
  * Render errors never cascade into an infinite retry loop. Each entry
    tracks a failure count; we skip past it and, if every entry fails in
    a single round, fall back to the placeholder until the next playlist
    arrives. This avoids burning CPU (and spamming logs) when a required
    component such as libmpv or QWebEngine is unavailable.
"""
from __future__ import annotations

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
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

try:
    from PyQt6.QtWebEngineWidgets import QWebEngineView  # type: ignore[import-not-found]

    HAS_WEBENGINE = True
except Exception:  # noqa: BLE001
    HAS_WEBENGINE = False

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

    Returns the directory containing the DLL so the caller can prepend it
    to PATH, or ``None`` if the DLL is unavailable. Must never raise —
    the player UI has to boot even if this helper explodes in a way
    ``libmpv_fetch`` didn't anticipate.
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


class PlayerWindow(QWidget):
    """Fullscreen, borderless, always-on-top kiosk window."""

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

        self._stack = QStackedWidget(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._stack)

        self._placeholder = _build_placeholder()
        self._stack.addWidget(self._placeholder)

        self._image_label = QLabel()
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_label.setStyleSheet("background-color: #000;")
        self._stack.addWidget(self._image_label)

        self._video_container = QWidget()
        self._video_container.setStyleSheet("background-color: #000;")
        self._video_container.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors)
        self._video_container.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self._stack.addWidget(self._video_container)

        if HAS_WEBENGINE:
            self._web_view = QWebEngineView()
            self._stack.addWidget(self._web_view)
        else:
            self._web_view = None

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
        # Per-entry failure counters (reset on each playlist swap). Keyed by
        # `id(entry)` so identical media_ids in different playlists don't share
        # a counter.
        self._failures: dict[int, int] = {}
        # Set when every item in the current playlist has failed past its
        # retry budget. While true, we show the placeholder and stop
        # advancing until a new playlist arrives.
        self._playlist_broken = False

        self._advance_timer = QTimer(self)
        self._advance_timer.setSingleShot(True)
        self._advance_timer.timeout.connect(self._advance)

        # Keep Alt+F4 from closing the kiosk window; ops must kill the
        # process via Task Manager or the scheduled task.
        self._disable_close_shortcut()

        if fullscreen:
            self.showFullScreen()
        else:
            self.resize(1280, 720)
            self.show()

    # ----- public slots --------------------------------------------------

    def set_playlist(self, entries: list[PlaylistEntry]) -> None:
        """Queue a new playlist. It swaps in at the end of the current item.

        Any previous "this playlist is broken" state is cleared because a
        fresh manifest may include different media types that *can* play.
        """
        if not entries:
            self._playlist = []
            self._pending_playlist = None
            self._failures.clear()
            self._playlist_broken = False
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

    def _show_placeholder(self, message: str = "ScreenView") -> None:
        label = self._placeholder.findChild(QLabel, "placeholder-label")
        if label is not None:
            label.setText(message)
        self._stack.setCurrentWidget(self._placeholder)

    def _play_current(self) -> None:
        if not self._playlist:
            self._show_placeholder()
            return

        # Skip entries that have already exceeded their failure budget. If
        # we loop all the way back to the start without finding a playable
        # one, declare the whole playlist broken and show the placeholder.
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
                # full loop, nothing renderable left
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
            # Move to the next entry after a short delay; do NOT retry the
            # same item in a tight loop.
            self._index = (self._index + 1) % len(self._playlist)
            QTimer.singleShot(RENDER_ERROR_RETRY_MS, self._play_current)
            return

        # Auto-advance policy:
        #   * Recorded videos: advance on libmpv's eof-reached event,
        #     ignoring entry.duration so the full clip plays.
        #   * Live streams: no natural EOF, so we *also* arm the
        #     duration timer. Whichever fires first (timer or EOF on
        #     network error) wins.
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
        self._show_placeholder("Content unavailable")

    def _render(self, entry: PlaylistEntry) -> None:
        path = entry.path
        if entry.kind == "image":
            pix = QPixmap(str(path))
            if pix.isNull():
                raise ValueError("Unable to load image")
            scaled = pix.scaled(
                self.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._image_label.setPixmap(scaled)
            self._stack.setCurrentWidget(self._image_label)
            return

        if entry.kind == "video":
            if self._mpv is None:
                raise RuntimeError(
                    "libmpv not available — install libmpv-2.dll or run "
                    "scripts\\fetch-libmpv.ps1"
                )
            self._stack.setCurrentWidget(self._video_container)
            self._mpv.loadfile(str(path), mode="replace")
            self._mpv.pause = False
            return

        if entry.kind == "stream":
            if self._mpv is None:
                raise RuntimeError(
                    "libmpv not available — cannot play live streams"
                )
            if not entry.stream_url:
                raise ValueError("Stream item missing upstream URL")
            self._stack.setCurrentWidget(self._video_container)
            # Hand the URL straight to libmpv. Network errors surface
            # via the same eof-reached property: if the stream is
            # unreachable, mpv reports EOF almost immediately and the
            # advance timer below kicks in to skip past the broken
            # item rather than freeze the screen.
            self._mpv.loadfile(entry.stream_url, mode="replace")
            self._mpv.pause = False
            return

        if entry.kind == "widget":
            if self._web_view is None:
                raise RuntimeError("QWebEngineView not available")
            self._web_view.load(QUrl.fromLocalFile(str(path)))
            self._stack.setCurrentWidget(self._web_view)
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
        if self._mpv is not None:
            try:
                self._mpv.terminate()
            except Exception:  # noqa: BLE001
                pass
        super().closeEvent(event)


def _build_placeholder() -> QWidget:
    w = QWidget()
    w.setStyleSheet("background-color: #0f1115;")
    layout = QVBoxLayout(w)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

    title = QLabel("ScreenView")
    title.setObjectName("placeholder-title")
    title.setAlignment(Qt.AlignmentFlag.AlignCenter)
    title.setStyleSheet("font-size: 48px; font-weight: 700; color: #4f8cff;")
    layout.addWidget(title)

    msg = QLabel("Waiting for schedule…")
    msg.setObjectName("placeholder-label")
    msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
    msg.setStyleSheet("font-size: 18px; color: #9ba1b0; margin-top: 8px;")
    layout.addWidget(msg)

    return w


def screen_size() -> QSize:
    screen = QGuiApplication.primaryScreen()
    if screen is None:
        return QSize(1920, 1080)
    return screen.size()


__all__ = ["PlayerWindow", "QApplication"]
