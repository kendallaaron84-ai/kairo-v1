"""Enforce complete simulated-fill provenance at the PostgreSQL boundary.

Revision ID: 0009
Revises: 0008
"""

from collections.abc import Sequence

from alembic import op


revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


CONSTRAINT_NAME = "ck_fills_simulated_execution_metadata"


def upgrade() -> None:
    op.drop_constraint(op.f(CONSTRAINT_NAME), "fills", type_="check")
    op.create_check_constraint(
        op.f(CONSTRAINT_NAME),
        "fills",
        "commission_fee_usd >= 0 AND "
        "(is_simulated = false OR (source_snapshot_id IS NOT NULL "
        "AND reference_price IS NOT NULL AND reference_price > 0 "
        "AND contract_multiplier IS NOT NULL AND contract_multiplier > 0 "
        "AND slippage_usd IS NOT NULL AND slippage_usd >= 0 "
        "AND liquidity_fidelity_tier IS NOT NULL "
        "AND liquidity_fidelity_tier IN ('TIER_1_QUOTE_DEPTH', "
        "'TIER_2_TRADE_HISTORY', 'TIER_3_BAR_ONLY') "
        "AND simulation_model IS NOT NULL "
        "AND simulation_policy_version IS NOT NULL "
        "AND simulation_metadata IS NOT NULL "
        "AND (simulation_metadata->>'synthetic' = 'true') IS TRUE "
        "AND simulation_metadata ? 'execution_guaranteed'))",
    )


def downgrade() -> None:
    op.drop_constraint(op.f(CONSTRAINT_NAME), "fills", type_="check")
    op.create_check_constraint(
        op.f(CONSTRAINT_NAME),
        "fills",
        "commission_fee_usd >= 0 AND "
        "(is_simulated = false OR (reference_price > 0 "
        "AND contract_multiplier > 0 AND slippage_usd >= 0 "
        "AND liquidity_fidelity_tier IN ('TIER_1_QUOTE_DEPTH', "
        "'TIER_2_TRADE_HISTORY', 'TIER_3_BAR_ONLY') "
        "AND simulation_model IS NOT NULL "
        "AND simulation_policy_version IS NOT NULL))",
    )
