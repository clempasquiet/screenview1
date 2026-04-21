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
    filename: str
    original_name: str
    type: MediaType
    md5_hash: str
    size_bytes: int
    default_duration: int
    mime_type: Optional[str]
    created_at: datetime


class MediaUpdate(BaseModel):
    original_name: Optional[str] = None
    default_duration: Optional[int] = None
    type: Optional[MediaType] = None


class ScheduleItemIn(BaseModel):
    media_id: int
    order: int = 0
    duration_override: Optional[int] = None


class ScheduleItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    media_id: int
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
    """Single item returned in the sync manifest downloaded by the player."""

    media_id: int
    order: int
    type: MediaType
    original_name: str
    url: str
    md5_hash: str
    size_bytes: int
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
