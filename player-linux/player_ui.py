"""Kiosk-mode UI for the ScreenView player.

Runs exclusively on the main (UI) thread. Responsibilities:
  * Render the current playlist to screen using libmpv (video) or QLabel
    (image) or QWebEngineView (widget/HTML).
  * Advance through the playlist on timers / libmpv end-of-file events.
  * Never block on network I/O and never crash into a black screen: if no
    playlist is available, show a branded placeholder frame.

Swapping the playlist is atomic: we keep playing the current media until it
ends (or a configurable grace window passes) before loading the new list.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QSize, Qt, QTimer, QUrl
from PyQt6.QtGui import QGuiApplication, QPixmap
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

try:
    import mpv  # type: ignore[import-untyped]

    HAS_MPV = True
except Exception:  # noqa: BLE001
    HAS_MPV = False

from worker_network import PlaylistEntry

logger = logging.getLogger(__name__)


class PlayerWindow(QWidget):
    """Fullscreen, borderless, always-on-top kiosk window."""

    def __init__(self, fullscreen: bool = True, show_cursor: bool = False) -> None:
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

        self._mpv: Optional["mpv.MPV"] = None
        if HAS_MPV:
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
