"""Pydantic/SQLModel payload schemas used by the API layer."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from .models import Device, DeviceStatus, MediaType


def device_to_read(device: "Device") -> "DeviceRead":
    """Serialise a ``Device`` to ``DeviceRead`` without leaking the token.

    Computes the ``has_api_token`` flag from the ORM attribute that
    ``DeviceRead`` itself never references, so the cleartext token is
    never reachable through the API surface accidentally.
    """
    return DeviceRead(
        id=device.id,
        name=device.name,
        mac_address=device.mac_address,
        hardware_id=device.hardware_id,
        status=device.status,
        last_ping=device.last_ping,
        registered_at=device.registered_at,
        current_schedule_id=device.current_schedule_id,
        api_token_issued_at=device.api_token_issued_at,
        has_api_token=bool(device.api_token),
    )


class DeviceRegisterIn(BaseModel):
    mac_address: str
    hardware_id: Optional[str] = None
    name: Optional[str] = None


class DeviceRead(BaseModel):
    """Device info returned to admins. Never contains the ``api_token``;
    the admin only sees whether a token is set + when it was issued."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    mac_address: str
    hardware_id: Optional[str]
    status: DeviceStatus
    last_ping: Optional[datetime]
    registered_at: datetime
    current_schedule_id: Optional[int]
    api_token_issued_at: Optional[datetime] = None
    has_api_token: bool = False


class DeviceCredentials(BaseModel):
    """Extra payload returned by ``POST /api/register`` and
    ``POST /api/devices/{id}/rotate-token``. Contains the cleartext
    token the player must persist. Cleartext is only ever returned by
    these two endpoints — ``GET /api/devices`` never exposes it."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    mac_address: str
    hardware_id: Optional[str]
    status: DeviceStatus
    api_token: str
    api_token_issued_at: Optional[datetime]


class DeviceUpdate(BaseModel):
    name: Optional[str] = None
    status: Optional[DeviceStatus] = None
    current_schedule_id: Optional[int] = None


class MediaRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    # Nullable for streams (no local file, no MD5).
    filename: Optional[str] = None
    original_name: str
    type: MediaType
    md5_hash: Optional[str] = None
    size_bytes: int = 0
    default_duration: int
    mime_type: Optional[str]
    stream_url: Optional[str] = None
    created_at: datetime


class MediaUpdate(BaseModel):
    original_name: Optional[str] = None
    default_duration: Optional[int] = None
    type: Optional[MediaType] = None
    stream_url: Optional[str] = None


class StreamCreate(BaseModel):
    """Payload for ``POST /api/media/stream``.

    Streams have no associated upload; they're a thin wrapper around a
    user-supplied URL that the player hands directly to libmpv.
    """

    name: str
    url: str
    default_duration: int = 30  # how long to keep the stream on screen
    mime_type: Optional[str] = None  # optional hint for the player


class PdfIngestResult(BaseModel):
    """Result of ``POST /api/media/pdf``.

    A single PDF upload produces N ordered image pages on the server.
    Each page is an ordinary ``MediaType.image`` row the admin can
    drop into playlists or Zone items like any other image. The CMS
    can use ``pages_added`` / ``pages_deduplicated`` to tell the
    operator "you uploaded 7 pages, 2 were already in the library".
    """

    pages_added: int
    pages_deduplicated: int
    pages: list[MediaRead]  # every page, in order (new + dedup'd)


class ScheduleItemIn(BaseModel):
    """Payload for creating / updating a single slot in a Schedule.

    Dual shape, XOR-validated by the schedules router (see
    ``_validate_item_xor``). A given ``ScheduleItemIn`` must specify
    exactly one of ``media_id`` (legacy single-media slot) or
    ``layout_id`` (Phase 2 multi-zone slot). Supplying both, or
    neither, is rejected with ``400 Bad Request``.
    """

    media_id: Optional[int] = None
    layout_id: Optional[int] = None
    order: int = 0
    duration_override: Optional[int] = None


class ScheduleItemRead(BaseModel):
    """Mirror of ``ScheduleItemIn``: exactly one of ``media_id`` /
    ``layout_id`` will be populated for any row returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    media_id: Optional[int] = None
    layout_id: Optional[int] = None
    order: int
    duration_override: Optional[int]


class ScheduleCreate(BaseModel):
    name: str
    description: Optional[str] = None
    items: list[ScheduleItemIn] = []


class ScheduleUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    items: Optional[list[ScheduleItemIn]] = None


class ScheduleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: Optional[str]
    created_at: datetime
    updated_at: datetime
    items: list[ScheduleItemRead] = []


class PlaylistManifestItem(BaseModel):
    """Single item returned in the sync manifest downloaded by the player.

    For ``MediaType.stream`` items, ``url`` holds the upstream live-stream
    URL (HLS / RTSP / RTMP / SRT) and ``md5_hash`` / ``size_bytes`` are 0
    / empty — the player skips the cache pipeline entirely and hands the
    URL straight to libmpv.
    """

    media_id: int
    order: int
    type: MediaType
    original_name: str
    url: str
    md5_hash: str = ""  # empty for streams
    size_bytes: int = 0  # 0 for streams
    duration: int


class MediaPreviewUrl(BaseModel):
    """Short-lived, admin-signed URL suitable for ``<img src>``/``<video src>``."""

    media_id: int
    url: str
    mime_type: Optional[str]
    type: MediaType
    original_name: str
    default_duration: int


class SchedulePreviewItem(BaseModel):
    """One entry in the schedule preview playlist. Mirrors
    ``PlaylistManifestItem`` but carries an admin-signed URL instead of
    a device-signed one."""

    media_id: int
    order: int
    type: MediaType
    original_name: str
    mime_type: Optional[str]
    url: str
    duration: int


class SchedulePreview(BaseModel):
    """Result of ``GET /api/schedules/{id}/preview`` — the admin-facing
    equivalent of a player manifest."""

    schedule_id: int
    schedule_name: str
    generated_at: datetime
    items: list[SchedulePreviewItem]


class PlaylistManifest(BaseModel):
    """Manifest fetched via GET /api/schedule/{device_id}.

    The player stores this locally, diffs it against its cache, downloads the
    missing files, verifies MD5 hashes, and only then swaps the active
    playlist in the UI thread.
    """

    device_id: UUID
    schedule_id: Optional[int]
    schedule_name: Optional[str]
    generated_at: datetime
    items: list[PlaylistManifestItem]


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class LoginIn(BaseModel):
    username: str
    password: str


# ---------------------------------------------------------------------------
# Phase 2 — Layouts / Zones / ZoneItems
# ---------------------------------------------------------------------------


class ZoneItemIn(BaseModel):
    """One entry in a Zone's playlist. Mirrors ``ScheduleItemIn`` but
    always points at a Media (Zones cannot nest Layouts)."""

    media_id: int
    order: int = 0
    duration_override: Optional[int] = None


class ZoneItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    media_id: int
    order: int
    duration_override: Optional[int]


class ZoneIn(BaseModel):
    """Create or replace a Zone inside a Layout."""

    name: Optional[str] = "Zone"
    position_x: int = 0
    position_y: int = 0
    width: int
    height: int
    z_index: int = 0
    items: list[ZoneItemIn] = []


class ZoneRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    layout_id: int
    name: str
    position_x: int
    position_y: int
    width: int
    height: int
    z_index: int
    items: list[ZoneItemRead] = []


class LayoutCreate(BaseModel):
    name: str
    description: Optional[str] = None
    resolution_w: int = 1920
    resolution_h: int = 1080
    zones: list[ZoneIn] = []


class LayoutUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    resolution_w: Optional[int] = None
    resolution_h: Optional[int] = None
    # When provided, replaces the Zone list wholesale (same semantics as
    # ``ScheduleUpdate.items``). Leave ``None`` to patch metadata only.
    zones: Optional[list[ZoneIn]] = None


class LayoutRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: Optional[str]
    resolution_w: int
    resolution_h: int
    created_at: datetime
    updated_at: datetime
    zones: list[ZoneRead] = []
