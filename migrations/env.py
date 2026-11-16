"""Execution des migrations Alembic.

L'adresse de la base est, dans l'ordre : l'option `sqlalchemy.url` passee par
le code (tests), puis DATABASE_URL, puis la base SQLite locale.
"""

from alembic import context
from sqlalchemy import pool

from app.config import load_settings
from app.models import Base, make_engine

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    return config.get_main_option("sqlalchemy.url") or load_settings().database_url


def run_offline() -> None:
    context.configure(
        url=_url(), target_metadata=target_metadata, literal_binds=True, render_as_batch=True
    )
    with context.begin_transaction():
        context.run_migrations()


def run_online() -> None:
    engine = make_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        # render_as_batch : permet de modifier des colonnes sous SQLite.
        context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_offline()
else:
    run_online()
