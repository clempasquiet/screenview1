"""Windows-specific helpers to derive a stable machine identifier.

Order of preference:
  1. Registry `HKLM\\SOFTWARE\\Microsoft\\Cryptography\\MachineGuid` — stable
     across reboots, persists through network adapter changes.
  2. `wmic csproduct get UUID` fallback (works on older systems where the
     registry key is missing or the process lacks HKLM read access).
  3. MAC address of the primary adapter (last resort).
"""
from __future__ import annotations

import logging
import subprocess
import sys
import uuid

logger = logging.getLogger(__name__)


def get_mac_address() -> str:
    """Return the primary MAC address formatted as AA:BB:CC:DD:EE:FF."""
    mac_int = uuid.getnode()
    return ":".join(f"{(mac_int >> ele) & 0xff:02x}" for ele in range(40, -1, -8))


def _machine_guid_from_registry() -> str | None:
    if sys.platform != "win32":
        return None
    try:
        import winreg  # type: ignore[import-not-found]
    except ImportError:
        return None

    # 64-bit view of HKLM, even when running as a 32-bit process.
    try:
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
            0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        )
    except OSError as exc:
        logger.debug("Could not open MachineGuid key: %s", exc)
        return None

    try:
        value, _ = winreg.QueryValueEx(key, "MachineGuid")
        return str(value).strip() or None
    except OSError:
        return None
    finally:
        winreg.CloseKey(key)


def _machine_guid_from_wmic() -> str | None:
    if sys.platform != "win32":
        return None
    try:
        output = subprocess.check_output(
            ["wmic", "csproduct", "get", "UUID"],
            stderr=subprocess.DEVNULL,
            timeout=5,
            creationflags=_no_window_flag(),
        ).decode("utf-8", errors="ignore")
    except (OSError, subprocess.SubprocessError):
        return None

    for line in output.splitlines():
        line = line.strip()
        if line and line.lower() != "uuid":
            return line
    return None


def _no_window_flag() -> int:
    # Avoid flashing a console window when the player is packaged as a GUI app.
    if sys.platform == "win32":
        return 0x08000000  # CREATE_NO_WINDOW
    return 0


def get_hardware_id() -> str:
    """Return a stable per-machine identifier, falling back gracefully."""
    return (
        _machine_guid_from_registry()
        or _machine_guid_from_wmic()
        or get_mac_address().replace(":", "")
    )
