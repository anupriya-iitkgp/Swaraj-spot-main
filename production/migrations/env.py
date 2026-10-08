"""Alembic environment.

The schema is written as explicit DDL in the revision files rather than
generated from ORM metadata, so `--autogenerate` is intentionally not wired up:
the constraints and partial indexes here encode design decisions (the reaper's
partial index, the append-only trigger, the deliberate *absence* of a
reserved <= sellable check) that autogenerate would happily erase.

Migrations run inside a transaction *and* take an advisory lock, so several
replicas starting at once — a rolling deploy, or `docker compose up` with
three API containers — cannot race each other through the same revision.
"""

from __future__ import annotations

import asyncio
import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import async_engine_from_config

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

#: Arbitrary but fixed: any process running these migrations uses this key.
MIGRATION_LOCK_ID = 0x50073001


def _database_url() -> str:
    url = os.environ.get("SPOT_DATABASE_URL")
    if not url:
        raise RuntimeError(
            "SPOT_DATABASE_URL is not set. Migrations must target the same "
            "database as the service; there is no default on purpose."
        )
    if url.startswith("postgresql+asyncpg://"):
        return url
    return url.replace("postgresql://", "postgresql+asyncpg://", 1)


def run_migrations_offline() -> None:
    """Emit SQL to stdout for review, without connecting.

    Used by `spotd migrate --sql` and in change-management workflows where a DBA
    signs off on the statements before they run.
    """
    context.configure(
        url=_database_url().replace("postgresql+asyncpg://", "postgresql://", 1),
        target_metadata=None,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    connection.execute(text(f"SELECT pg_advisory_lock({MIGRATION_LOCK_ID})"))
    context.configure(connection=connection, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()

    engine = async_engine_from_config(
        section, prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_do_run_migrations)
            await connection.commit()
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
