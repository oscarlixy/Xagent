from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, inspect

from alembic import command


def test_initial_migration_creates_core_tables(tmp_path: Path) -> None:
    database_path = tmp_path / "x_digest.sqlite3"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{database_path}")

    command.upgrade(config, "head")

    tables = inspect(create_engine(f"sqlite+pysqlite:///{database_path}")).get_table_names()
    assert {"x_lists", "posts", "summaries", "digests", "sync_runs"}.issubset(tables)
