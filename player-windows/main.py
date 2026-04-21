"""Entry point for the ScreenView Windows player.

Strict UI/worker thread separation:
  * Main thread: runs the Qt event loop, owns the kiosk window, renders media.
  * Worker thread: owns all network I/O (REST, WebSocket, downloads, MD5).

The two communicate exclusively via PyQt signals. See `worker_network.py`
for the network side and `player_ui.py` for the rendering side.

Windows-specific adaptations vs. the Linux player:
  * Machine ID derived from the Windows registry (MachineGuid) with MAC
    and WMIC fallbacks.
  * Persistent state stored under ``%LOCALAPPDATA%\\ScreenView``.
  * Per-monitor v2 DPI awareness so fullscreen matches the physical screen.
  * ``SetThreadExecutionState`` keeps the display/system awake.
  * Named-mutex single-instance guard.
  * ``CREATE_NO_WINDOW`` flag on subprocesses to avoid console flashes.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Anchor the process's working directory to the script location *before*
# importing any PyQt / mpv modules. Task Scheduler launches processes
# with ``CWD=C:\Windows\System32``, which would otherwise cause:
#   * Qt to write ``QtWebEngineProcess.exe``-relative caches in System32,
#   * mpv to miss any ``mpv.conf`` / ``scripts/`` sitting next to the exe,
#   * any relative path accidentally escaping our own resolution helpers
#     to land somewhere unwritable.
# Having a stable CWD is cheap insurance. Our own code still uses
# absolute paths everywhere (see ``config.resolve_app_path``) — the
# chdir is belt-and-braces for third-party libraries.
_APP_DIR = Path(__file__).resolve().parent
try:
    os.chdir(_APP_DIR)
except OSError:
    # Read-only mount, exotic filesystem, etc. — logging not yet
    # configured, so we stay silent. Our own path resolution doesn't
    # rely on CWD anyway.
    pass

from PyQt6.QtCore import Qt  # noqa: E402  # after chdir
from PyQt6.QtWidgets import QApplication  # noqa: E402

from config import PlayerConfig  # noqa: E402
from player_ui import PlayerWindow  # noqa: E402
from power import enable_dpi_awareness, prevent_display_sleep, restore_power_state  # noqa: E402
from single_instance import SingleInstance  # noqa: E402
from worker_network import start_in_thread  # noqa: E402


def _configure_logging(config: PlayerConfig) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Always log to stderr so the Task Scheduler output file captures it.
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    root.addHandler(stream)

    # Rotating log file under %LOCALAPPDATA%\ScreenView\logs
    try:
        file_handler = RotatingFileHandler(
            config.log_path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError as exc:  # pragma: no cover - defensive
        logging.warning("Could not open log file at %s: %s", config.log_path, exc)


def main() -> int:
    enable_dpi_awareness()
    config = PlayerConfig.load()
    _configure_logging(config)
    logger = logging.getLogger("screenview.player")

    with SingleInstance() as guard:
        if not guard.acquired:
            logger.error("Another ScreenView player instance is already running.")
            return 0

        if config.prevent_display_sleep:
            prevent_display_sleep()

        app = QApplication(sys.argv)
        app.setApplicationName("ScreenView Player")
        app.setQuitOnLastWindowClosed(True)

        # Feed the resolved libmpv dir (anchored to APP_DIR, never CWD)
        # rather than the raw config string.
        resolved_libmpv = config.libmpv_search_dir
        window = PlayerWindow(
            fullscreen=config.fullscreen,
            show_cursor=config.show_cursor,
            libmpv_dir=str(resolved_libmpv) if resolved_libmpv else None,
            libmpv_app_data_dir=config.app_data_dir,
            libmpv_auto_download=config.libmpv_auto_download,
        )

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

        # SIGINT/SIGTERM best-effort handling (useful during dev, no-op under
        # a packaged GUI binary launched by Task Scheduler).
        try:
            signal.signal(signal.SIGINT, _shutdown)
            signal.signal(signal.SIGTERM, _shutdown)
        except (ValueError, OSError):
            pass

        app.aboutToQuit.connect(lambda: (worker.stop(), thread.quit(), thread.wait(3000)))

        thread.start()
        try:
            return app.exec()
        finally:
            worker.stop()
            thread.quit()
            thread.wait(3000)
            restore_power_state()


if __name__ == "__main__":
    raise SystemExit(main())
