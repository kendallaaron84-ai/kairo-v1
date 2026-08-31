from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError


pytestmark = pytest.mark.integration


def test_runtime_role_preserves_risk_event_immutability_and_projection_control(
    migrated_database: tuple[str, str],
) -> None:
    admin_url, runtime_url = migrated_database
    session_id = f"permission-{uuid4()}"
    event_id = uuid4()
    cell_id = uuid4()
    now = datetime.now(UTC)
    runtime_engine = create_engine(runtime_url)
    admin_engine = create_engine(admin_url)
    try:
        with admin_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO capital_cells "
                    "(cell_id, cell_code, seed_capital, status, autonomy_tier, "
                    "strategy_id, strategy_version, target_treasury_code, "
                    "risk_policy_id, economic_domain) VALUES "
                    "(:cell_id, :cell_code, 100, 'ACTIVE', 'APPRENTICE', "
                    "'EMA-CROSS-001', '1.0.0', 'META', "
                    "'a0000000-0000-0000-0000-000000000001', 'LIVE')"
                ),
                {"cell_id": cell_id, "cell_code": f"PERM-{cell_id.hex[:8]}"},
            )
        with runtime_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO risk_sessions "
                    "(session_id, cell_id, trading_date, session_open, session_close) "
                    "VALUES (:session_id, :cell_id, :trading_date, :session_open, :session_close)"
                ),
                {
                    "session_id": session_id,
                    "cell_id": cell_id,
                    "trading_date": date.today(),
                    "session_open": now,
                    "session_close": now + timedelta(hours=6),
                },
            )
            connection.execute(
                text(
                    "INSERT INTO risk_state_events "
                    "(event_id, session_id, cell_id, previous_state, new_state, trigger_reason, "
                    "current_session_net_pnl, authorized_cash_usd) "
                    "VALUES (:event_id, :session_id, :cell_id, 'DISARMED', 'DISARMED', "
                    "'PERMISSION_TEST', 0, 0)"
                ),
                {"event_id": event_id, "session_id": session_id, "cell_id": cell_id},
            )
            connection.execute(
                text(
                    "INSERT INTO risk_governor_state "
                    "(cell_id, current_session_id, operational_state) "
                    "VALUES (:cell_id, :session_id, 'DISARMED')"
                ),
                {"session_id": session_id, "cell_id": cell_id},
            )
        with runtime_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE risk_governor_state SET updated_at=now() WHERE cell_id=:cell_id"
                )
                , {"cell_id": cell_id}
            )
        with runtime_engine.connect() as connection:
            assert connection.scalar(
                text("SELECT count(*) FROM risk_state_events WHERE event_id=:event_id"),
                {"event_id": event_id},
            ) == 1
        with pytest.raises(DBAPIError):
            with runtime_engine.begin() as connection:
                connection.execute(
                    text("UPDATE risk_state_events SET trigger_reason='MUTATED' WHERE event_id=:id"),
                    {"id": event_id},
                )
        with pytest.raises(DBAPIError):
            with runtime_engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM risk_state_events WHERE event_id=:id"), {"id": event_id}
                )
        with pytest.raises(DBAPIError):
            with runtime_engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM risk_governor_state WHERE cell_id=:cell_id"),
                    {"cell_id": cell_id},
                )
        with pytest.raises(DBAPIError):
            with runtime_engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE risk_sessions SET market_timezone='UTC' "
                        "WHERE session_id=:id"
                    ),
                    {"id": session_id},
                )
        with pytest.raises(DBAPIError):
            with runtime_engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM risk_sessions WHERE session_id=:id"),
                    {"id": session_id},
                )
        with admin_engine.connect() as connection:
            assert connection.scalar(
                text(
                    "SELECT has_table_privilege("
                    "'kairo_runtime', 'risk_decisions', 'UPDATE')"
                )
            ) is False
            assert connection.scalar(
                text(
                    "SELECT has_table_privilege("
                    "'kairo_runtime', 'risk_instrument_marks', "
                    "'SELECT,INSERT,UPDATE')"
                )
            ) is True
            assert connection.scalar(
                text(
                    "SELECT has_table_privilege("
                    "'kairo_runtime', 'risk_instrument_marks', 'DELETE')"
                )
            ) is False
    finally:
        with admin_engine.begin() as connection:
            connection.execute(
                text("DELETE FROM risk_governor_state WHERE cell_id=:cell_id"),
                {"cell_id": cell_id},
            )
            connection.execute(
                text("DELETE FROM risk_state_events WHERE event_id=:id"), {"id": event_id}
            )
            connection.execute(
                text("DELETE FROM risk_sessions WHERE session_id=:id"), {"id": session_id}
            )
            connection.execute(
                text("DELETE FROM capital_cells WHERE cell_id=:cell_id"),
                {"cell_id": cell_id},
            )
        runtime_engine.dispose()
        admin_engine.dispose()
