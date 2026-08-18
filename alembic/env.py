"""Alembic environment (ADR-010).

The managed schema is the authoritative production schema. The legacy
`clawbox.common.db.Base` tables are NOT yet under Alembic; they keep their
dev-stage create_all until migrated (tracked in ADR-010 migration notes).
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from clawbox.managed.db import ManagedBase
from clawbox.managed import models  # noqa: F401  (import side effects)
from clawbox.managed import db as managed_db  # noqa: F401  (register rows)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = ManagedBase.metadata


def get_url() -> str:
    return os.getenv("DATABASE_URL", "sqlite:///./clawbox.db")


def run_migrations_offline() -> None:
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = get_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
