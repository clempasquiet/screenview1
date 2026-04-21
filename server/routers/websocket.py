"""WebSocket endpoint for live device signalling."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlmodel import Session

from ..database import engine
from ..models import Device, DeviceStatus
from ..ws_manager import manager

logger = logging.getLogger(__name__)

router = APIRouter(tags=["websocket"])


def _mark_device(device_id: UUID, status: DeviceStatus | None, touch_ping: bool = False) -> bool:
    with Session(engine) as session:
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


@router.websocket("/ws/player/{device_id}")
async def player_ws(websocket: WebSocket, device_id: UUID) -> None:
    """Persistent per-device WebSocket.

    Protocol is deliberately trivial, JSON-over-text:
      * Player -> Server: {"type": "hello"} | {"type": "pong"} | {"type": "status", ...}
      * Server -> Player: {"action": "sync_required", ...} | {"type": "ping"}

    Unknown device IDs are rejected *after* accepting the handshake and
    closing with code 4404. Closing a WebSocket *before* ``accept()``
    would cause Starlette to reply with HTTP 403 on the handshake, which
    looks like a generic auth failure to older clients and doesn't carry
    the close reason. Accepting first lets the client receive the 4404
    code cleanly so it can drop its stale ``device_id`` and re-register.
    """
    if not _mark_device(device_id, DeviceStatus.active, touch_ping=True):
        try:
            await websocket.accept()
            await websocket.close(code=4404, reason="unknown device")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to signal unknown device %s: %s", device_id, exc)
        return

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
