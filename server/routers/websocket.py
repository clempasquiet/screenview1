"""WebSocket endpoint for live device signalling."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from sqlmodel import Session

from .. import database as _db_module
from ..device_auth import tokens_match
from ..models import Device, DeviceStatus
from ..ws_manager import manager

logger = logging.getLogger(__name__)

router = APIRouter(tags=["websocket"])


# Close codes (custom, in the private range 4000-4999):
#   4404 — unknown device_id
#   4401 — bad api_token for a known device (player must re-register)
WS_CLOSE_UNKNOWN_DEVICE = 4404
WS_CLOSE_BAD_TOKEN = 4401


def _mark_device(device_id: UUID, status: DeviceStatus | None, touch_ping: bool = False) -> bool:
    with Session(_db_module.engine) as session:
        device = session.get(Device, device_id)
        if not device:
            return False
        if status is not None and device.status != DeviceStatus.pending:
            device.status = status
        if touch_ping:
            device.last_ping = datetime.utcnow()
        session.add(device)
        session.commit()
        return True


def _lookup_device_and_token(device_id: UUID) -> tuple[bool, Optional[str]]:
    """Return ``(exists, stored_token)`` for *device_id*.

    Done in a dedicated session because the caller is on the async
    event loop and we just need a quick synchronous lookup.
    Resolves the engine lazily via the ``database`` module so tests can
    swap it at runtime (the fixture reloads ``server.database``).
    """
    with Session(_db_module.engine) as session:
        device = session.get(Device, device_id)
        if device is None:
            return False, None
        return True, device.api_token


@router.websocket("/ws/player/{device_id}")
async def player_ws(
    websocket: WebSocket,
    device_id: UUID,
    token: Optional[str] = Query(default=None),
) -> None:
    """Persistent per-device WebSocket.

    Authentication: ``?token=<api_token>`` query-string parameter. The
    WebSocket protocol doesn't let browsers attach custom headers during
    the handshake, so we take the token from the query string — it's
    transported over TLS in production, and every other endpoint also
    binds to ``device_id``.

    Protocol (JSON-over-text):
      * Player -> Server: {"type": "hello"} | {"type": "pong"} | {"type": "status", ...}
      * Server -> Player: {"action": "sync_required", ...} | {"type": "ping"}

    Close codes:
      * 4404 — unknown device_id (player clears local state and re-registers).
      * 4401 — token mismatch (player clears local state and re-registers).

    Both forms are accepted before being closed so that Starlette sends a
    proper WebSocket close frame instead of an HTTP 403 handshake
    rejection — the latter hides the close code from the client,
    especially through reverse proxies.
    """
    exists, stored_token = _lookup_device_and_token(device_id)
    if not exists:
        try:
            await websocket.accept()
            await websocket.close(code=WS_CLOSE_UNKNOWN_DEVICE, reason="unknown device")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to signal unknown device %s: %s", device_id, exc)
        return

    if not tokens_match(token, stored_token):
        try:
            await websocket.accept()
            await websocket.close(code=WS_CLOSE_BAD_TOKEN, reason="invalid token")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to reject bad token for %s: %s", device_id, exc)
        return

    _mark_device(device_id, DeviceStatus.active, touch_ping=True)

    await manager.connect(device_id, websocket)
    keepalive_task = asyncio.create_task(_keepalive(device_id, websocket))
    try:
        while True:
            message = await websocket.receive_json()
            msg_type = message.get("type")
            if msg_type in {"hello", "pong", "status"}:
                _mark_device(device_id, DeviceStatus.active, touch_ping=True)
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("WS error for %s: %s", device_id, exc)
    finally:
        keepalive_task.cancel()
        await manager.disconnect(device_id, websocket)
        _mark_device(device_id, DeviceStatus.offline)


async def _keepalive(device_id: UUID, websocket: WebSocket) -> None:
    try:
        while True:
            await asyncio.sleep(30)
            try:
                await websocket.send_json({"type": "ping", "ts": datetime.utcnow().isoformat()})
            except Exception:  # noqa: BLE001
                return
    except asyncio.CancelledError:
        return
