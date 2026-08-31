"""Add explicit paper-execution decision lineage and simulated-fill audit facts.

Revision ID: 0008
Revises: 0007
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_risk_decisions_decision_intent",
        "risk_decisions",
        ["decision_id", "intent_id"],
    )
    op.add_column(
        "kairo_orders",
        sa.Column("risk_decision_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_kairo_orders_risk_decision_intent",
        "kairo_orders",
        "risk_decisions",
        ["risk_decision_id", "intent_id"],
        ["decision_id", "intent_id"],
    )

    op.add_column("fills", sa.Column("reference_price", sa.Numeric(28, 10)))
    op.add_column("fills", sa.Column("contract_multiplier", sa.Numeric(28, 10)))
    op.add_column("fills", sa.Column("slippage_usd", sa.Numeric(28, 10)))
    op.add_column(
        "fills",
        sa.Column(
            "commission_fee_usd",
            sa.Numeric(28, 10),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "fills",
        sa.Column(
            "is_simulated", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column("fills", sa.Column("liquidity_fidelity_tier", sa.String(32)))
    op.add_column("fills", sa.Column("simulation_model", sa.String(64)))
    op.add_column("fills", sa.Column("simulation_policy_version", sa.String(64)))
    op.add_column(
        "fills", sa.Column("source_snapshot_id", postgresql.UUID(as_uuid=True))
    )
    op.add_column(
        "fills",
        sa.Column(
            "simulation_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.create_foreign_key(
        "fk_fills_source_snapshot_id_market_snapshots",
        "fills",
        "market_snapshots",
        ["source_snapshot_id"],
        ["snapshot_id"],
    )
    op.create_check_constraint(
        op.f("ck_fills_simulated_execution_metadata"),
        "fills",
        "commission_fee_usd >= 0 AND "
        "(is_simulated = false OR (reference_price > 0 "
        "AND contract_multiplier > 0 AND slippage_usd >= 0 "
        "AND liquidity_fidelity_tier IN ('TIER_1_QUOTE_DEPTH', "
        "'TIER_2_TRADE_HISTORY', 'TIER_3_BAR_ONLY') "
        "AND simulation_model IS NOT NULL "
        "AND simulation_policy_version IS NOT NULL))",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_fills_simulated_execution_metadata"), "fills", type_="check"
    )
    op.drop_constraint(
        "fk_fills_source_snapshot_id_market_snapshots", "fills", type_="foreignkey"
    )
    for column in (
        "simulation_metadata",
        "source_snapshot_id",
        "simulation_policy_version",
        "simulation_model",
        "liquidity_fidelity_tier",
        "is_simulated",
        "commission_fee_usd",
        "slippage_usd",
        "contract_multiplier",
        "reference_price",
    ):
        op.drop_column("fills", column)
    op.drop_constraint(
        "fk_kairo_orders_risk_decision_intent", "kairo_orders", type_="foreignkey"
    )
    op.drop_column("kairo_orders", "risk_decision_id")
    op.drop_constraint(
        "uq_risk_decisions_decision_intent", "risk_decisions", type_="unique"
    )
