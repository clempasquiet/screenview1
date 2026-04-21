"""Entry point for the ScreenView Linux player.

Strict UI/worker thread separation:
  * Main thread: runs the Qt event loop, owns the kiosk window, renders media.
  * Worker thread: owns all network I/O (REST, WebSocket, downloads, MD5).

The two communicate exclusively via PyQt signals. See `worker_network.py`
for the network side and `player_ui.py` for the rendering side.
"""
from __future__ import annotations

import logging
import signal
import sys

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from config import PlayerConfig
from player_ui import PlayerWindow
from worker_network import start_in_thread


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("screenview.player")


def main() -> int:
    config = PlayerConfig.load()

    app = QApplication(sys.argv)
    app.setApplicationName("ScreenView Player")

    window = PlayerWindow(fullscreen=config.fullscreen, show_cursor=config.show_cursor)

    thread, worker = start_in_thread(config)

    worker.playlist_ready.connect(
        window.set_playlist, type=Qt.ConnectionType.QueuedConnection
    )
    worker.status_changed.connect(
        window.show_status, type=Qt.ConnectionType.QueuedConnection
    )
    worker.sync_progress.connect(
        lambda done, total: logger.info("Sync progress: %d/%d", done, total),
        type=Qt.ConnectionType.QueuedConnection,
    )

    def _shutdown(*_args: object) -> None:
        logger.info("Shutting down…")
        worker.stop()
        thread.quit()
        thread.wait(3000)
        app.quit()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    app.aboutToQuit.connect(lambda: (worker.stop(), thread.quit(), thread.wait(3000)))

    thread.start()
    try:
        return app.exec()
    finally:
        worker.stop()
        thread.quit()
        thread.wait(3000)


if __name__ == "__main__":
    raise SystemExit(main())
