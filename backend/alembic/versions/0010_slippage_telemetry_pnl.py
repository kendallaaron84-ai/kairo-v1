"""Treat modeled slippage as telemetry, not a second economic P&L deduction.

Revision ID: 0010
Revises: 0009
"""

from collections.abc import Sequence

from alembic import op


revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        op.f("ck_risk_governor_state_net_pnl_consistent"),
        "risk_governor_state",
        type_="check",
    )
    op.execute(
        "UPDATE risk_governor_state SET session_net_pnl = "
        "session_realized_pnl + session_unrealized_pnl - session_fees_usd"
    )
    op.create_check_constraint(
        op.f("ck_risk_governor_state_net_pnl_consistent"),
        "risk_governor_state",
        "session_net_pnl = session_realized_pnl + session_unrealized_pnl "
        "- session_fees_usd",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_risk_governor_state_net_pnl_consistent"),
        "risk_governor_state",
        type_="check",
    )
    op.execute(
        "UPDATE risk_governor_state SET session_net_pnl = "
        "session_realized_pnl + session_unrealized_pnl "
        "- session_fees_usd - session_slippage_usd"
    )
    op.create_check_constraint(
        op.f("ck_risk_governor_state_net_pnl_consistent"),
        "risk_governor_state",
        "session_net_pnl = session_realized_pnl + session_unrealized_pnl "
        "- session_fees_usd - session_slippage_usd",
    )
