"""Persist session marks for portfolio-wide unrealized P&L aggregation.

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


MONEY = sa.Numeric(28, 10)
UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "risk_instrument_marks",
        sa.Column(
            "session_id",
            sa.String(64),
            sa.ForeignKey("risk_sessions.session_id"),
            primary_key=True,
        ),
        sa.Column(
            "instrument_id",
            UUID,
            sa.ForeignKey("instruments.instrument_id"),
            primary_key=True,
        ),
        sa.Column("mark_price", MONEY, nullable=False),
        sa.Column("source_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "mark_price > 0", name=op.f("ck_risk_instrument_marks_positive_mark_price")
        ),
        sa.CheckConstraint(
            "received_at >= source_timestamp",
            name=op.f("ck_risk_instrument_marks_valid_mark_provenance"),
        ),
    )
    op.create_index(
        "ix_risk_instrument_marks_session_received",
        "risk_instrument_marks",
        ["session_id", "received_at"],
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON risk_instrument_marks TO kairo_runtime"
    )
    op.execute("REVOKE DELETE ON risk_instrument_marks FROM kairo_runtime")


def downgrade() -> None:
    op.drop_index(
        "ix_risk_instrument_marks_session_received",
        table_name="risk_instrument_marks",
    )
    op.drop_table("risk_instrument_marks")
