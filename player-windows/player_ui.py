"""Kiosk-mode UI for the ScreenView Windows player.

Runs exclusively on the main (UI) thread. Responsibilities:
  * Render the current playlist using libmpv (video), QLabel (image), or
    QWebEngineView (widget / HTML).
  * Advance through the playlist on timers / libmpv end-of-file events.
  * Never block on network I/O and never show a black screen: if no
    playlist is available, show a branded placeholder frame.
  * Disable native OS shortcuts that could break kiosk mode (Alt+F4).

Playlist swaps are atomic: we keep the current media playing until it ends
before loading the next list, preventing visual glitches.
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

from worker_network import PlaylistEntry

logger = logging.getLogger(__name__)


def _ensure_libmpv_on_path(extra_dir: str | os.PathLike[str] | None = None) -> None:
    """Make sure Windows can locate ``libmpv-2.dll`` / ``mpv-2.dll``.

    python-mpv loads the shared library via ``ctypes.CDLL``. On Windows it
    searches:
      1. the application directory (where the .exe lives),
      2. ``PATH``,
      3. Python 3.8+ DLL search paths added via ``os.add_dll_directory``.

    We proactively add the app dir and an optional user-supplied override.
    Missing libmpv isn't fatal: the UI will still boot and play images /
    widgets, falling back to placeholder on video items.
    """
    candidates: list[Path] = []
    if extra_dir:
        candidates.append(Path(extra_dir))

    bundle_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    candidates.append(bundle_dir)
    candidates.append(bundle_dir / "mpv")
    candidates.append(Path(__file__).resolve().parent)

    for cand in candidates:
        if not cand.is_dir():
            continue
        if sys.platform == "win32" and hasattr(os, "add_dll_directory"):
            try:
                os.add_dll_directory(str(cand))
            except (OSError, FileNotFoundError):
                pass
        os.environ["PATH"] = str(cand) + os.pathsep + os.environ.get("PATH", "")


def _try_load_mpv(libmpv_dir: str | None):
    """Import python-mpv lazily so the rest of the UI can boot without it."""
    _ensure_libmpv_on_path(libmpv_dir)
    try:
        import mpv  # type: ignore[import-untyped]
    except (ImportError, OSError) as exc:
        logger.warning("libmpv not available: %s", exc)
        return None
    return mpv


class PlayerWindow(QWidget):
    """Fullscreen, borderless, always-on-top kiosk window."""

    def __init__(
        self,
        fullscreen: bool = True,
        show_cursor: bool = False,
        libmpv_dir: str | None = None,
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
        mpv = _try_load_mpv(libmpv_dir)
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
        """Queue a new playlist. It swaps in at the end of the current item."""
        if not entries:
            self._playlist = []
            self._pending_playlist = None
            self._show_placeholder("Waiting for schedule…")
            return

        if not self._playlist:
            self._playlist = entries
            self._index = 0
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
        entry = self._playlist[self._index]
        self._advance_timer.stop()
        try:
            self._render(entry)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to render %s", entry.path)
            self.show_status("warn", f"Render failed for {entry.original_name}: {exc}")
            QTimer.singleShot(1000, self._advance)
            return

        if entry.kind != "video" or self._mpv is None:
            self._advance_timer.start(max(1, entry.duration) * 1000)

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
                raise RuntimeError("libmpv not available")
            self._stack.setCurrentWidget(self._video_container)
            self._mpv.loadfile(str(path), mode="replace")
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
