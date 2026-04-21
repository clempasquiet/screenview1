"""Pydantic/SQLModel payload schemas used by the API layer."""
from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from .models import DeviceStatus, MediaType


class DeviceRegisterIn(BaseModel):
    mac_address: str
    hardware_id: Optional[str] = None
    name: Optional[str] = None


class DeviceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    mac_address: str
    hardware_id: Optional[str]
    status: DeviceStatus
    last_ping: Optional[datetime]
    registered_at: datetime
    current_schedule_id: Optional[int]


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
