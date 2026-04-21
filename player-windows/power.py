"""Windows power/display management.

Prevents the monitor from going to sleep or the screensaver from kicking in
while the player is running. No-op on non-Windows platforms.

Uses the Win32 API `SetThreadExecutionState` with the following flags:
  * ES_CONTINUOUS         – keep the setting in effect until we clear it.
  * ES_DISPLAY_REQUIRED   – keep the display on.
  * ES_SYSTEM_REQUIRED    – keep the system awake.
"""
from __future__ import annotations

import ctypes
import logging
import sys

logger = logging.getLogger(__name__)

_ES_CONTINUOUS = 0x80000000
_ES_DISPLAY_REQUIRED = 0x00000002
_ES_SYSTEM_REQUIRED = 0x00000001


def prevent_display_sleep() -> bool:
    if sys.platform != "win32":
        return False
    try:
        result = ctypes.windll.kernel32.SetThreadExecutionState(
            _ES_CONTINUOUS | _ES_DISPLAY_REQUIRED | _ES_SYSTEM_REQUIRED
        )
    except OSError as exc:
        logger.warning("SetThreadExecutionState failed: %s", exc)
        return False
    if result == 0:
        logger.warning("SetThreadExecutionState returned 0 (call rejected).")
        return False
    logger.info("Display sleep inhibited.")
    return True


def restore_power_state() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
    except OSError:
        pass


def enable_dpi_awareness() -> None:
    """Enable per-monitor v2 DPI awareness so fullscreen matches the physical screen."""
    if sys.platform != "win32":
        return
    try:
        # Windows 10 1703+
        ctypes.windll.user32.SetProcessDpiAwarenessContext(
            ctypes.c_void_p(-4)  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        )
        return
    except (OSError, AttributeError):
        pass
    try:
        # Windows 8.1+
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except (OSError, AttributeError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (OSError, AttributeError):
            pass
