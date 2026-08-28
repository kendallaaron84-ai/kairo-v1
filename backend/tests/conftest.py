import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import Session


@pytest.fixture(scope="session")
def database_urls() -> tuple[str, str]:
    admin_url = os.getenv("KAIRO_DATABASE_URL")
    runtime_url = os.getenv("KAIRO_RUNTIME_DATABASE_URL")
    if not admin_url or not runtime_url:
        pytest.skip("PostgreSQL integration URLs are not configured")
    return admin_url, runtime_url


@pytest.fixture(scope="session")
def migrated_database(database_urls: tuple[str, str]) -> Iterator[tuple[str, str]]:
    from app.db.bootstrap import main as bootstrap_runtime_role

    bootstrap_runtime_role()
    backend_root = Path(__file__).resolve().parents[1]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    yield database_urls
    command.downgrade(config, "base")


@pytest.fixture
def db_session(migrated_database: tuple[str, str]) -> Iterator[Session]:
    engine = create_engine(migrated_database[0])
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()
        engine.dispose()
