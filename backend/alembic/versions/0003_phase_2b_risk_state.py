"""Add deterministic Risk Governor sessions, events, projection, and decisions.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


MONEY = sa.Numeric(28, 10)
UUID = postgresql.UUID(as_uuid=True)
VALID_STATES = (
    "'DISARMED', 'ARMED', 'LOCKED_FOR_DAY', 'HALTED_HARD', "
    "'FLAT_LOCKED', 'MANUAL_PAUSE'"
)


def upgrade() -> None:
    op.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')
    op.create_table(
        "risk_sessions",
        sa.Column("session_id", sa.String(64), primary_key=True),
        sa.Column("trading_date", sa.Date(), nullable=False),
        sa.Column(
            "market_timezone",
            sa.String(64),
            nullable=False,
            server_default="America/New_York",
        ),
        sa.Column("session_open", sa.DateTime(timezone=True), nullable=False),
        sa.Column("session_close", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "session_close > session_open", name=op.f("ck_risk_sessions_valid_window")
        ),
    )
    op.create_index(
        "ix_risk_sessions_trading_date_window",
        "risk_sessions",
        ["trading_date", "session_open", "session_close"],
    )
    op.create_table(
        "risk_state_events",
        sa.Column(
            "event_id",
            UUID,
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        sa.Column(
            "session_id",
            sa.String(64),
            sa.ForeignKey("risk_sessions.session_id"),
            nullable=False,
        ),
        sa.Column("previous_state", sa.String(32), nullable=False),
        sa.Column("new_state", sa.String(32), nullable=False),
        sa.Column("trigger_reason", sa.String(256), nullable=False),
        sa.Column("current_session_net_pnl", MONEY, nullable=False),
        sa.Column("authorized_cash_usd", MONEY, nullable=False),
        sa.Column(
            "recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            f"previous_state IN ({VALID_STATES})",
            name=op.f("ck_risk_state_events_valid_previous_state"),
        ),
        sa.CheckConstraint(
            f"new_state IN ({VALID_STATES})",
            name=op.f("ck_risk_state_events_valid_new_state"),
        ),
        sa.CheckConstraint(
            "authorized_cash_usd >= 0",
            name=op.f("ck_risk_state_events_authorized_cash_nonnegative"),
        ),
    )
    op.create_index(
        "ix_risk_state_events_session_recorded",
        "risk_state_events",
        ["session_id", "recorded_at"],
    )
    op.create_table(
        "risk_governor_state",
        sa.Column("singleton_key", sa.Integer(), primary_key=True, server_default="1"),
        sa.Column(
            "current_session_id",
            sa.String(64),
            sa.ForeignKey("risk_sessions.session_id"),
            nullable=False,
        ),
        sa.Column("operational_state", sa.String(32), nullable=False, server_default="DISARMED"),
        sa.Column("session_realized_pnl", MONEY, nullable=False, server_default="0"),
        sa.Column("session_unrealized_pnl", MONEY, nullable=False, server_default="0"),
        sa.Column("session_fees_usd", MONEY, nullable=False, server_default="0"),
        sa.Column("session_slippage_usd", MONEY, nullable=False, server_default="0"),
        sa.Column("session_net_pnl", MONEY, nullable=False, server_default="0"),
        sa.Column(
            "last_state_change_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "singleton_key = 1", name=op.f("ck_risk_governor_state_singleton")
        ),
        sa.CheckConstraint(
            f"operational_state IN ({VALID_STATES})",
            name=op.f("ck_risk_governor_state_valid_state"),
        ),
        sa.CheckConstraint(
            "session_fees_usd >= 0 AND session_slippage_usd >= 0",
            name=op.f("ck_risk_governor_state_costs_nonnegative"),
        ),
        sa.CheckConstraint(
            "session_net_pnl = session_realized_pnl + session_unrealized_pnl "
            "- session_fees_usd - session_slippage_usd",
            name=op.f("ck_risk_governor_state_net_pnl_consistent"),
        ),
    )

    for column in (
        sa.Column("session_id", sa.String(64)),
        sa.Column("operational_state", sa.String(32)),
        sa.Column("intent_classification", sa.String(32)),
        sa.Column("session_net_pnl", MONEY),
        sa.Column("authorized_cash_usd", MONEY),
        sa.Column("requested_cash_usd", MONEY),
        sa.Column("projected_exposure_usd", MONEY),
        sa.Column("max_contractual_loss_usd", MONEY),
    ):
        op.add_column("risk_decisions", column)
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM risk_decisions) THEN "
        "RAISE EXCEPTION 'risk decisions require explicit Phase 2B evidence backfill'; "
        "END IF; END $$;"
    )
    for name in (
        "session_id",
        "operational_state",
        "intent_classification",
        "session_net_pnl",
        "authorized_cash_usd",
        "requested_cash_usd",
        "projected_exposure_usd",
    ):
        op.alter_column("risk_decisions", name, nullable=False)
    op.create_foreign_key(
        "fk_risk_decisions_session_id_risk_sessions",
        "risk_decisions",
        "risk_sessions",
        ["session_id"],
        ["session_id"],
    )
    op.create_index(
        "ix_risk_decisions_session_decided",
        "risk_decisions",
        ["session_id", "decided_at"],
    )

    op.execute("GRANT SELECT, INSERT ON risk_sessions, risk_state_events TO kairo_runtime")
    op.execute("REVOKE UPDATE, DELETE ON risk_sessions, risk_state_events FROM kairo_runtime")
    op.execute("GRANT SELECT, INSERT, UPDATE ON risk_governor_state TO kairo_runtime")
    op.execute("REVOKE DELETE ON risk_governor_state FROM kairo_runtime")


def downgrade() -> None:
    op.drop_index("ix_risk_decisions_session_decided", table_name="risk_decisions")
    op.drop_constraint(
        "fk_risk_decisions_session_id_risk_sessions",
        "risk_decisions",
        type_="foreignkey",
    )
    for name in (
        "max_contractual_loss_usd",
        "projected_exposure_usd",
        "requested_cash_usd",
        "authorized_cash_usd",
        "session_net_pnl",
        "intent_classification",
        "operational_state",
        "session_id",
    ):
        op.drop_column("risk_decisions", name)
    op.drop_table("risk_governor_state")
    op.drop_index("ix_risk_state_events_session_recorded", table_name="risk_state_events")
    op.drop_table("risk_state_events")
    op.drop_index("ix_risk_sessions_trading_date_window", table_name="risk_sessions")
    op.drop_table("risk_sessions")
