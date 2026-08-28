from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError


pytestmark = pytest.mark.integration


def test_runtime_role_can_append_and_read_but_not_mutate(
    migrated_database: tuple[str, str],
) -> None:
    admin_url, runtime_url = migrated_database
    event_id, cell_id = uuid4(), uuid4()
    runtime_engine = create_engine(runtime_url)
    admin_engine = create_engine(admin_url)
    try:
        with runtime_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO cell_events "
                    "(event_id, cell_id, event_type, occurred_at, payload) "
                    "VALUES (:event_id, :cell_id, 'TEST', now(), '{}'::jsonb)"
                ),
                {"event_id": event_id, "cell_id": cell_id},
            )
        with runtime_engine.connect() as connection:
            assert connection.scalar(
                text("SELECT count(*) FROM cell_events WHERE event_id=:event_id"),
                {"event_id": event_id},
            ) == 1
        with pytest.raises(DBAPIError):
            with runtime_engine.begin() as connection:
                connection.execute(
                    text("UPDATE cell_events SET event_type='MUTATED' WHERE event_id=:event_id"),
                    {"event_id": event_id},
                )
        with pytest.raises(DBAPIError):
            with runtime_engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM cell_events WHERE event_id=:event_id"),
                    {"event_id": event_id},
                )
    finally:
        with admin_engine.begin() as connection:
            connection.execute(
                text("DELETE FROM cell_events WHERE event_id=:event_id"), {"event_id": event_id}
            )
        runtime_engine.dispose()
        admin_engine.dispose()
