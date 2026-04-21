"""Database engine and session helpers using SQLModel/SQLAlchemy.

Schema migrations are intentionally lightweight. Rather than pulling in
Alembic for a tiny MVP we detect missing columns on boot and issue plain
``ALTER TABLE`` statements. Each migration is idempotent so repeat boots
are no-ops. When the project outgrows SQLite this module is the one to
swap for Alembic.
"""
from __future__ import annotations

import logging
import secrets
from collections.abc import Iterator
from datetime import datetime

from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine

from .config import settings

logger = logging.getLogger(__name__)


connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, echo=False, connect_args=connect_args)


def _generate_api_token() -> str:
    """Opaque, URL-safe token (≈43 characters of base64url)."""
    return secrets.token_urlsafe(32)


def _ensure_column(table: str, column: str, ddl: str) -> None:
    """Add *column* to *table* if SQLAlchemy doesn't already know about it.

    ``ddl`` is the raw SQL type / clause appended after the column name, e.g.
    ``"VARCHAR"`` or ``"DATETIME"``. We don't use ``DEFAULT`` because older
    SQLite builds cap non-constant defaults.
    """
    insp = inspect(engine)
    columns = {c["name"] for c in insp.get_columns(table)}
    if column in columns:
        return
    with engine.begin() as conn:
        conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {ddl}'))
    logger.info("migration: added %s.%s", table, column)


def _backfill_missing_tokens() -> None:
    """Give every pre-existing device an ``api_token`` so the auth layer can
    gate requests without breaking older rows. New devices get one inside
    the registration handler directly."""
    with engine.begin() as conn:
        rows = conn.execute(
            text('SELECT id FROM "device" WHERE api_token IS NULL OR api_token = ""')
        ).fetchall()
        if not rows:
            return
        now = datetime.utcnow().isoformat()
        for (device_id,) in rows:
            conn.execute(
                text(
                    'UPDATE "device" SET api_token = :t, api_token_issued_at = :ts '
                    'WHERE id = :id'
                ),
                {"t": _generate_api_token(), "ts": now, "id": device_id},
            )
        logger.info("migration: backfilled api_token for %d device(s)", len(rows))


def _relax_media_not_null_for_streams() -> None:
    """Drop the NOT NULL constraints on ``media.filename`` and
    ``media.md5_hash`` for legacy SQLite databases.

    Stream rows (``MediaType.stream``) carry no local file, so both
    columns must accept NULL. SQLite cannot ``ALTER COLUMN`` to relax a
    constraint, so we rebuild the table in place when needed. Idempotent.
    """
    if not settings.database_url.startswith("sqlite"):
        # PostgreSQL etc. accept ALTER COLUMN ... DROP NOT NULL natively;
        # operators can run that by hand if they hit the constraint. We
        # don't want to second-guess their schema management here.
        return

    insp = inspect(engine)
    if "media" not in insp.get_table_names():
        return

    cols = {c["name"]: c for c in insp.get_columns("media")}
    needs_rebuild = False
    for col_name in ("filename", "md5_hash"):
        col = cols.get(col_name)
        if col is not None and not col.get("nullable", True):
            needs_rebuild = True
            break

    if not needs_rebuild:
        return

    logger.info("migration: relaxing NOT NULL on media.filename / media.md5_hash for streams")
    # SQLite-specific: rebuild the table without the NOT NULL constraints.
    # This does NOT affect other constraints, indexes, or data — we copy
    # everything verbatim.
    with engine.begin() as conn:
        existing_columns = [c["name"] for c in cols.values()]
        col_list = ", ".join(f'"{c}"' for c in existing_columns)
        conn.execute(text("PRAGMA foreign_keys = OFF"))
        conn.execute(
            text(
                'CREATE TABLE "media__new" ('
                '  "id" INTEGER PRIMARY KEY AUTOINCREMENT,'
                '  "filename" VARCHAR,'
                '  "original_name" VARCHAR NOT NULL,'
                '  "type" VARCHAR NOT NULL,'
                '  "md5_hash" VARCHAR,'
                '  "size_bytes" INTEGER NOT NULL DEFAULT 0,'
                '  "default_duration" INTEGER NOT NULL DEFAULT 10,'
                '  "mime_type" VARCHAR,'
                '  "stream_url" VARCHAR,'
                '  "created_at" DATETIME NOT NULL'
                ')'
            )
        )
        # Common columns between old and new.
        new_cols = (
            "id", "filename", "original_name", "type", "md5_hash",
            "size_bytes", "default_duration", "mime_type",
            "stream_url", "created_at",
        )
        common = [c for c in new_cols if c in existing_columns]
        common_sql = ", ".join(f'"{c}"' for c in common)
        conn.execute(
            text(f'INSERT INTO "media__new" ({common_sql}) SELECT {common_sql} FROM "media"')
        )
        conn.execute(text('DROP TABLE "media"'))
        conn.execute(text('ALTER TABLE "media__new" RENAME TO "media"'))
        # Re-create the md5_hash index that SQLModel declared.
        conn.execute(text('CREATE INDEX IF NOT EXISTS "ix_media_md5_hash" ON "media" ("md5_hash")'))
        conn.execute(text("PRAGMA foreign_keys = ON"))


def _relax_scheduleitem_not_null_for_layouts() -> None:
    """Drop the NOT NULL constraint on ``scheduleitem.media_id``.

    Phase 2 lets a ScheduleItem point at *either* a Media (legacy) or a
    Layout (multi-zone canvas). Exactly one is set per row — see the
    XOR validator in ``routers/schedules.py``. Legacy SQLite databases
    declared ``media_id`` as NOT NULL, which would block any Layout
    entry. SQLite can't ``ALTER COLUMN`` to drop a NOT NULL, so we
    rebuild the table in place. Idempotent; no-op once the column is
    already nullable. Skipped on non-SQLite backends (PostgreSQL
    operators can ``ALTER COLUMN ... DROP NOT NULL`` themselves).

    Preserves every other constraint, the FK targets, and the data.
    """
    if not settings.database_url.startswith("sqlite"):
        return

    insp = inspect(engine)
    if "scheduleitem" not in insp.get_table_names():
        return

    cols = {c["name"]: c for c in insp.get_columns("scheduleitem")}
    media_col = cols.get("media_id")
    if media_col is None:
        return
    # Already nullable → no-op.
    if media_col.get("nullable", True):
        return

    logger.info("migration: relaxing NOT NULL on scheduleitem.media_id for Phase 2 layouts")
    with engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys = OFF"))
        conn.execute(
            text(
                'CREATE TABLE "scheduleitem__new" ('
                '  "id" INTEGER PRIMARY KEY AUTOINCREMENT,'
                '  "schedule_id" INTEGER NOT NULL REFERENCES "schedule"("id"),'
                '  "media_id" INTEGER REFERENCES "media"("id"),'
                '  "layout_id" INTEGER REFERENCES "layout"("id"),'
                '  "order" INTEGER NOT NULL DEFAULT 0,'
                '  "duration_override" INTEGER'
                ')'
            )
        )
        # Copy every column the old table already has. The new
        # ``layout_id`` stays NULL for pre-existing rows, which is
        # exactly what we want (legacy single-media slots).
        new_cols = ("id", "schedule_id", "media_id", "layout_id", "order", "duration_override")
        existing = [c for c in new_cols if c in cols]
        cols_sql = ", ".join(f'"{c}"' for c in existing)
        conn.execute(
            text(
                f'INSERT INTO "scheduleitem__new" ({cols_sql}) '
                f'SELECT {cols_sql} FROM "scheduleitem"'
            )
        )
        conn.execute(text('DROP TABLE "scheduleitem"'))
        conn.execute(text('ALTER TABLE "scheduleitem__new" RENAME TO "scheduleitem"'))
        # Re-create the indexes SQLModel declared on the original.
        conn.execute(
            text('CREATE INDEX IF NOT EXISTS "ix_scheduleitem_schedule_id" '
                 'ON "scheduleitem" ("schedule_id")')
        )
        conn.execute(
            text('CREATE INDEX IF NOT EXISTS "ix_scheduleitem_media_id" '
                 'ON "scheduleitem" ("media_id")')
        )
        conn.execute(
            text('CREATE INDEX IF NOT EXISTS "ix_scheduleitem_layout_id" '
                 'ON "scheduleitem" ("layout_id")')
        )
        conn.execute(text("PRAGMA foreign_keys = ON"))


def init_db() -> None:
    """Create tables and run lightweight migrations."""
    from . import models  # noqa: F401  # ensure models are registered

    SQLModel.metadata.create_all(engine)

    # Lightweight, idempotent migrations. Kept here (and not in Alembic)
    # because the MVP targets SQLite and we want zero-config upgrades.
    _ensure_column("device", "api_token", "VARCHAR")
    _ensure_column("device", "api_token_issued_at", "DATETIME")
    _backfill_missing_tokens()
    # Live-stream support: streams have no local file so ``filename``
    # and ``md5_hash`` are nullable, and a new ``stream_url`` column
    # carries the upstream URL.
    _ensure_column("media", "stream_url", "VARCHAR")
    _relax_media_not_null_for_streams()
    # Phase 2 multi-zone support: ScheduleItem can point at a Layout
    # instead of a single Media. ``layout_id`` column + relaxed
    # ``media_id`` NOT NULL. The ``layout`` / ``zone`` / ``zoneitem``
    # tables themselves are created by ``SQLModel.metadata.create_all``
    # above — only the ``scheduleitem`` delta needs hand-crafting.
    _ensure_column("scheduleitem", "layout_id", "INTEGER REFERENCES layout(id)")
    _relax_scheduleitem_not_null_for_layouts()


def get_session() -> Iterator[Session]:
    """FastAPI dependency that yields a DB session."""
    with Session(engine) as session:
        yield session
