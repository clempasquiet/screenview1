"""SQLModel ORM definitions for ScreenView.

Entities:
  - Device: a registered player/screen.
  - Media: a file (video/image/widget) uploaded to the CMS.
  - Schedule: an ordered playlist of media items assigned to devices.
  - ScheduleItem: join row giving ordering + per-item duration override.

The module defines `Media`, `Schedule`, `ScheduleItem` *before* `Device`
because `Device.schedule` is typed with a forward reference that SQLModel's
SQLAlchemy mapper introspects at class-creation time.
"""
from datetime import datetime
from enum import Enum
from typing import Optional
from uuid import UUID, uuid4

from sqlmodel import Field, Relationship, SQLModel


class DeviceStatus(str, Enum):
    pending = "pending"
    active = "active"
    offline = "offline"
    rejected = "rejected"


class MediaType(str, Enum):
    video = "video"
    image = "image"
    widget = "widget"
    # Live network stream (HLS .m3u8, RTSP, RTMP, SRT, etc). Unlike the
    # other types, ``stream`` items are NOT cached locally — the player
    # hands the URL to libmpv directly. They break the offline-first
    # guarantee for themselves only; the rest of the playlist still
    # plays from the validated cache.
    stream = "stream"


class Media(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    # ``filename`` and ``md5_hash`` are NULL for streams since there's no
    # local file. Uploaded files always populate both. The migration in
    # ``database.py`` relaxes the NOT NULL constraints in place for
    # SQLite databases that pre-date this change.
    filename: Optional[str] = None  # path on disk, relative to upload_dir
    original_name: str
    type: MediaType = Field(default=MediaType.image)
    md5_hash: Optional[str] = Field(default=None, index=True)
    size_bytes: int = 0
    default_duration: int = Field(default=10, description="Default play duration in seconds")
    mime_type: Optional[str] = None
    # For ``MediaType.stream``: the upstream URL (HLS, RTSP, RTMP, SRT).
    # Players pass it straight to libmpv. Validated server-side for an
    # allow-list of schemes so the CMS can't be tricked into pointing
    # players at e.g. file:// URLs that would resolve on the player's
    # local filesystem.
    stream_url: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)

    items: list["ScheduleItem"] = Relationship(back_populates="media")


class Schedule(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    description: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    items: list["ScheduleItem"] = Relationship(
        back_populates="schedule",
        sa_relationship_kwargs={"order_by": "ScheduleItem.order", "cascade": "all, delete-orphan"},
    )
    devices: list["Device"] = Relationship(back_populates="schedule")


class ScheduleItem(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    schedule_id: int = Field(foreign_key="schedule.id", index=True)
    media_id: int = Field(foreign_key="media.id", index=True)
    order: int = Field(default=0)
    duration_override: Optional[int] = Field(
        default=None, description="Overrides Media.default_duration if set"
    )

    schedule: Optional[Schedule] = Relationship(back_populates="items")
    media: Optional[Media] = Relationship(back_populates="items")


class Device(SQLModel, table=True):
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    name: str = Field(default="New Player")
    mac_address: str = Field(index=True, unique=True)
    hardware_id: Optional[str] = Field(default=None, index=True)
    status: DeviceStatus = Field(default=DeviceStatus.pending)
    last_ping: Optional[datetime] = None
    registered_at: datetime = Field(default_factory=datetime.utcnow)
    current_schedule_id: Optional[int] = Field(default=None, foreign_key="schedule.id")

    # Per-device API token, generated at registration and persisted by the
    # player in its local config. Rotated on demand from the CMS. Used both
    # as a bearer credential on REST + WebSocket and as the HMAC key that
    # signs the device's media download URLs.
    api_token: Optional[str] = Field(
        default=None,
        index=True,
        description="Long random string; opaque to the client.",
    )
    api_token_issued_at: Optional[datetime] = Field(default=None)

    schedule: Optional[Schedule] = Relationship(back_populates="devices")
