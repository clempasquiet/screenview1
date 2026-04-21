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


def init_db() -> None:
    """Create tables and run lightweight migrations."""
    from . import models  # noqa: F401  # ensure models are registered

    SQLModel.metadata.create_all(engine)

    # Lightweight, idempotent migrations. Kept here (and not in Alembic)
    # because the MVP targets SQLite and we want zero-config upgrades.
    _ensure_column("device", "api_token", "VARCHAR")
    _ensure_column("device", "api_token_issued_at", "DATETIME")
    _backfill_missing_tokens()


def get_session() -> Iterator[Session]:
    """FastAPI dependency that yields a DB session."""
    with Session(engine) as session:
        yield session
