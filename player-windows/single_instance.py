"""Single-instance guard.

On Windows, the Task Scheduler can misfire or a user can double-click the
shortcut and end up with two kiosk windows fighting for the screen. We use
a named mutex to prevent that. On other platforms we fall back to a lock
file in the config directory.
"""
from __future__ import annotations

import ctypes
import logging
import os
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


_ERROR_ALREADY_EXISTS = 183


class SingleInstance:
    def __init__(self, name: str = "ScreenViewPlayer") -> None:
        self._name = name
        self._handle: Optional[int] = None
        self._lock_path: Optional[Path] = None
        self._lock_fd: Optional[int] = None
        self.acquired = False

    def __enter__(self) -> "SingleInstance":
        if sys.platform == "win32":
            self._acquire_windows_mutex()
        else:
            self._acquire_lock_file()
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._handle is not None:
            try:
                ctypes.windll.kernel32.CloseHandle(self._handle)
            except OSError:
                pass
        if self._lock_fd is not None:
            try:
                os.close(self._lock_fd)
            except OSError:
                pass
            if self._lock_path and self._lock_path.exists():
                try:
                    self._lock_path.unlink()
                except OSError:
                    pass

    def _acquire_windows_mutex(self) -> None:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, False, f"Global\\{self._name}")
        last_err = kernel32.GetLastError()
        if handle == 0:
            logger.warning("CreateMutexW failed (err=%s); proceeding anyway.", last_err)
            self.acquired = True
            return
        self._handle = handle
        self.acquired = last_err != _ERROR_ALREADY_EXISTS

    def _acquire_lock_file(self) -> None:
        base = Path(os.environ.get("TMPDIR", "/tmp"))
        self._lock_path = base / f"{self._name}.lock"
        try:
            fd = os.open(
                str(self._lock_path),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o644,
            )
            os.write(fd, str(os.getpid()).encode())
            self._lock_fd = fd
            self.acquired = True
        except FileExistsError:
            self.acquired = False
