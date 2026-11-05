"""Alembic: the schema is examples.db.metadata; the database is DATABASE_URL."""

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from examples import db  # noqa: E402
from examples.context import load_dotenv  # noqa: E402

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
load_dotenv(ROOT / ".env")
URL = config.get_main_option("sqlalchemy.url") or os.environ["DATABASE_URL"]


def render_item(type_, obj, autogen_context):
    """Write the pgvector column as Vector(768), not as the wrapper type."""
    if type_ == "type" and isinstance(obj, db.Embedding):
        autogen_context.imports.add("import pgvector.sqlalchemy")
        return f"pgvector.sqlalchemy.Vector({db.EMBED_DIM})"
    return False


def run_migrations_offline() -> None:
    context.configure(url=URL, target_metadata=db.metadata, literal_binds=True, render_item=render_item)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(URL)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=db.metadata, render_item=render_item)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
