"""SQLModel ORM definitions for ScreenView.

Entities:
  - Device: a registered player/screen.
  - Media: a file (video/image/widget/stream) available to the CMS.
  - Layout: a canvas definition (resolution + zero-or-more Zones) for the
    multi-zone rendering architecture introduced in Phase 2.
  - Zone: a rectangular region inside a Layout with its own playlist and
    z-index. Zero or more media items are attached via ``ZoneItem``.
  - ZoneItem: join row giving ordering + per-item duration override within
    a Zone's playlist (the Zone-level equivalent of ``ScheduleItem``).
  - Schedule: an ordered playlist assigned to devices.
  - ScheduleItem: join row. Points **either** at a Media (legacy single-
    media playlist entry) **or** at a Layout (new multi-zone playlist
    entry). Exactly one of ``media_id`` / ``layout_id`` must be set —
    see the XOR validator in the schedules router.

The module orders class definitions so that forward-referenced
relationships (e.g. ``Device.schedule``) land after their target is
already declared, because SQLModel's SQLAlchemy mapper introspects
annotations at class-creation time.
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
    zone_items: list["ZoneItem"] = Relationship(back_populates="media")


class Layout(SQLModel, table=True):
    """A named multi-zone canvas.

    A Layout is *what* appears on screen during a single ``ScheduleItem``
    play-slot: a fixed-resolution rectangle populated by one or more
    ``Zone`` regions. Each Zone has its own ordered playlist. The player
    composes the result by (a) playing video Zones through ``libmpv``'s
    hardware surface at the bottom of the stack, and (b) overlaying a
    transparent ``QWebEngineView`` that renders absolutely-positioned
    ``<div>`` elements for image / widget / HTML Zones.

    Resolution
    ----------
    ``resolution_w`` / ``resolution_h`` are the *authoring* resolution.
    The player scales the whole composition to its actual screen size
    (``object-fit: contain`` semantics) so a 1920×1080 layout looks the
    same on a 4K display — with letterboxing, not stretching. The
    defaults match the most common 1080p signage display.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    description: Optional[str] = None
    resolution_w: int = Field(default=1920, ge=1, description="Authoring width, px")
    resolution_h: int = Field(default=1080, ge=1, description="Authoring height, px")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    zones: list["Zone"] = Relationship(
        back_populates="layout",
        sa_relationship_kwargs={
            "order_by": "Zone.z_index",
            "cascade": "all, delete-orphan",
        },
    )
    # Back-ref so we can forbid deleting a Layout that's still attached
    # to at least one ScheduleItem. Enforced in the router.
    schedule_items: list["ScheduleItem"] = Relationship(back_populates="layout")


class Zone(SQLModel, table=True):
    """A single region inside a Layout.

    Zones carry an absolute position + size **in the authoring-resolution
    coordinate space** (pixels, not percentages) and a z-index that
    determines stacking order at composition time — higher = drawn on
    top. The Phase 2 rendering contract is:

      * One Zone of kind ``video`` / ``stream`` is picked as the
        "background layer" and sent to libmpv with its geometry.
      * Every other Zone is rendered as a ``<div>`` in the transparent
        WebEngineView overlay, with ``position: absolute`` + its own
        z-index.

    Zone kinds are intentionally decoupled from ``MediaType``: a single
    Zone can cycle through mixed media (e.g. image → widget → image).
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    layout_id: int = Field(foreign_key="layout.id", index=True)
    name: str = Field(default="Zone")
    position_x: int = Field(default=0, ge=0)
    position_y: int = Field(default=0, ge=0)
    width: int = Field(default=1, ge=1)
    height: int = Field(default=1, ge=1)
    z_index: int = Field(default=0, description="Higher = drawn on top")

    layout: Optional[Layout] = Relationship(back_populates="zones")
    items: list["ZoneItem"] = Relationship(
        back_populates="zone",
        sa_relationship_kwargs={
            "order_by": "ZoneItem.order",
            "cascade": "all, delete-orphan",
        },
    )


class ZoneItem(SQLModel, table=True):
    """Ordered playlist of media for a single Zone.

    Mirrors ``ScheduleItem`` but at the Zone granularity. Each ZoneItem
    points at a Media; the ``order`` and ``duration_override`` fields
    work identically. A Zone with zero items renders as a blank region
    (background visible through it for the transparent overlay).
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    zone_id: int = Field(foreign_key="zone.id", index=True)
    media_id: int = Field(foreign_key="media.id", index=True)
    order: int = Field(default=0)
    duration_override: Optional[int] = Field(
        default=None, description="Overrides Media.default_duration if set"
    )

    zone: Optional[Zone] = Relationship(back_populates="items")
    media: Optional[Media] = Relationship(back_populates="zone_items")


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
    """A single entry in a Schedule's playlist.

    Dual shape, enforced by the XOR validator in ``routers/schedules.py``:

      * **Legacy single-media slot**: ``media_id`` set, ``layout_id`` NULL.
        The player plays the Media for ``duration_override`` seconds
        (or the media's default), alone on screen — the pre-Phase-2
        behaviour that every existing playlist uses today.
      * **Multi-zone slot**: ``layout_id`` set, ``media_id`` NULL.
        The player renders the Layout (its Zones, each with its own
        playlist) for ``duration_override`` seconds on screen.

    Both shapes coexist: an existing Schedule can freely mix single-
    media and Layout entries on successive ``order`` positions, which
    lets operators migrate playlists incrementally without a big-bang
    cut-over.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    schedule_id: int = Field(foreign_key="schedule.id", index=True)
    # Exactly one of these two is populated at any given time. The
    # database has no CHECK constraint because SQLite won't let us add
    # one retroactively without a table rebuild; the invariant is held
    # by ``routers/schedules._validate_xor`` on every write.
    media_id: Optional[int] = Field(default=None, foreign_key="media.id", index=True)
    layout_id: Optional[int] = Field(default=None, foreign_key="layout.id", index=True)
    order: int = Field(default=0)
    duration_override: Optional[int] = Field(
        default=None,
        description="Overrides the effective duration if set (seconds)",
    )

    schedule: Optional[Schedule] = Relationship(back_populates="items")
    media: Optional[Media] = Relationship(back_populates="items")
    layout: Optional[Layout] = Relationship(back_populates="schedule_items")


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
