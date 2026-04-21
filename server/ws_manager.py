"""In-memory WebSocket connection manager.

Each registered device opens a persistent WebSocket to the server. The server
uses this channel for lightweight signalling only (ping/pong, "sync_required"
triggers). Heavy payloads (manifests, media files) are served over REST.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: dict[UUID, WebSocket] = {}
        self._lock = asyncio.Lock()

    async def connect(self, device_id: UUID, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            existing = self._connections.get(device_id)
            if existing is not None:
                try:
                    await existing.close(code=4000, reason="replaced")
                except Exception:  # noqa: BLE001
                    pass
            self._connections[device_id] = websocket
        logger.info("Device %s connected via WebSocket", device_id)

    async def disconnect(self, device_id: UUID, websocket: WebSocket) -> None:
        async with self._lock:
            if self._connections.get(device_id) is websocket:
                self._connections.pop(device_id, None)
        logger.info("Device %s disconnected", device_id)

    async def send(self, device_id: UUID, message: dict[str, Any]) -> bool:
        ws = self._connections.get(device_id)
        if ws is None:
            return False
        try:
            await ws.send_json(message)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to send to %s: %s", device_id, exc)
            await self.disconnect(device_id, ws)
            return False

    async def broadcast(self, message: dict[str, Any]) -> int:
        sent = 0
        for device_id in list(self._connections.keys()):
            if await self.send(device_id, message):
                sent += 1
        return sent

    def connected_devices(self) -> list[UUID]:
        return list(self._connections.keys())


manager = ConnectionManager()
