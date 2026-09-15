from pathlib import Path

import pytest
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


def test_migration_prefers_database_url_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured_path = tmp_path / "configured.sqlite3"
    environment_path = tmp_path / "environment.sqlite3"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{configured_path}")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{environment_path}")

    command.upgrade(config, "head")

    assert environment_path.exists()
    tables = inspect(
        create_engine(f"sqlite+pysqlite:///{environment_path}")
    ).get_table_names()
    assert "alembic_version" in tables
    assert not configured_path.exists()
