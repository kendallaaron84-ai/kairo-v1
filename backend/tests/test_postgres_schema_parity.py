import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import CheckConstraint, create_engine

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
            differences = compare_metadata(context, Base.metadata)
            # PostgreSQL truncates long convention-generated check names and Alembic
            # reflects their hashed physical names. Their behavior is covered directly
            # by the conformance constraint tests; all other schema drift remains fatal.
            material_differences = [
                difference
                for difference in differences
                if not (
                    difference[0] in {"add_constraint", "remove_constraint"}
                    and isinstance(difference[1], CheckConstraint)
                )
            ]
            assert material_differences == []
    finally:
        engine.dispose()
