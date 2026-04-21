"""Helpers to derive a stable hardware identifier for registration."""
from __future__ import annotations

import uuid
from pathlib import Path


MACHINE_ID_PATHS = (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id"))


def get_mac_address() -> str:
    """Return the primary MAC address as AA:BB:CC:DD:EE:FF."""
    mac_int = uuid.getnode()
    return ":".join(f"{(mac_int >> ele) & 0xff:02x}" for ele in range(40, -1, -8))


def get_hardware_id() -> str:
    """Return a stable per-machine identifier.

    Prefers `/etc/machine-id` on Linux systems; falls back to the MAC address.
    """
    for path in MACHINE_ID_PATHS:
        try:
            value = path.read_text().strip()
            if value:
                return value
        except OSError:
            continue
    return get_mac_address().replace(":", "")
