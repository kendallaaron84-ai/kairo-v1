import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from app.db import models  # noqa: F401 -- register complete mapped schema
from app.db.base import Base


pytestmark = pytest.mark.integration


def test_migrated_postgres_schema_matches_sqlalchemy_metadata(
    migrated_database: tuple[str, str],
) -> None:
    admin_url, _ = migrated_database
    engine = create_engine(admin_url)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection)
            assert compare_metadata(context, Base.metadata) == []
    finally:
        engine.dispose()
