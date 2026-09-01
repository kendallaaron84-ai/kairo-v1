"""Phase 3.5 Step 5: Counterfactual Effectiveness Research Engine.

Revision ID: 0020
Revises: 0019
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "intelligence_research_runs",
        sa.Column("run_id", UUID, primary_key=True),
        sa.Column("cell_id", UUID, sa.ForeignKey("capital_cells.cell_id")),
        sa.Column(
            "research_method",
            sa.String(64),
            server_default="TRADE_REMOVAL_COUNTERFACTUAL",
            nullable=False,
        ),
        sa.Column("sample_start_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sample_end_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("total_baseline_trades", sa.Integer(), nullable=False),
        sa.Column("total_context_evaluated_trades", sa.Integer(), nullable=False),
        sa.Column("total_veto_opportunities", sa.Integer(), nullable=False),
        sa.Column("vetoed_losing_trades", sa.Integer(), nullable=False),
        sa.Column("vetoed_winning_trades", sa.Integer(), nullable=False),
        sa.Column("vetoed_breakeven_trades", sa.Integer(), nullable=False),
        sa.Column("excluded_causal_invalid_trades", sa.Integer(), nullable=False),
        sa.Column("baseline_net_pnl", sa.Numeric(12, 2), nullable=False),
        sa.Column("counterfactual_net_pnl", sa.Numeric(12, 2), nullable=False),
        sa.Column("losses_avoided_usd", sa.Numeric(12, 2), nullable=False),
        sa.Column("profits_forfeited_usd", sa.Numeric(12, 2), nullable=False),
        sa.Column("net_alpha_usd", sa.Numeric(12, 2), nullable=False),
        sa.Column("baseline_max_drawdown_usd", sa.Numeric(12, 2), nullable=False),
        sa.Column(
            "counterfactual_max_drawdown_usd", sa.Numeric(12, 2), nullable=False
        ),
        sa.Column("veto_precision_pct", sa.Numeric(5, 2), nullable=False),
        sa.Column("research_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "research_method = 'TRADE_REMOVAL_COUNTERFACTUAL'",
            name="ck_research_method_trade_removal",
        ),
        sa.CheckConstraint(
            "sample_end_time >= sample_start_time", name="ck_research_sample_window"
        ),
        sa.CheckConstraint(
            "total_baseline_trades >= 0 AND total_context_evaluated_trades >= 0 "
            "AND total_veto_opportunities >= 0 "
            "AND vetoed_losing_trades >= 0 AND vetoed_winning_trades >= 0 "
            "AND vetoed_breakeven_trades >= 0 "
            "AND excluded_causal_invalid_trades >= 0",
            name="ck_research_counts_nonnegative",
        ),
        sa.CheckConstraint(
            "total_context_evaluated_trades <= total_baseline_trades",
            name="ck_research_eval_le_baseline",
        ),
        sa.CheckConstraint(
            "total_context_evaluated_trades + excluded_causal_invalid_trades "
            "<= total_baseline_trades",
            name="ck_research_attributed_le_baseline",
        ),
        sa.CheckConstraint(
            "total_veto_opportunities <= total_context_evaluated_trades",
            name="ck_research_veto_le_eval",
        ),
        sa.CheckConstraint(
            "vetoed_losing_trades + vetoed_winning_trades "
            "+ vetoed_breakeven_trades = total_veto_opportunities",
            name="ck_research_veto_sum_exact",
        ),
        sa.CheckConstraint(
            "losses_avoided_usd >= 0 AND profits_forfeited_usd >= 0 "
            "AND baseline_max_drawdown_usd >= 0 "
            "AND counterfactual_max_drawdown_usd >= 0",
            name="ck_research_financials_nonnegative",
        ),
        sa.CheckConstraint(
            "net_alpha_usd = losses_avoided_usd - profits_forfeited_usd",
            name="ck_research_net_alpha_exact",
        ),
        sa.CheckConstraint(
            "veto_precision_pct >= 0.00 AND veto_precision_pct <= 100.00",
            name="ck_research_precision_range",
        ),
        sa.CheckConstraint(
            "research_manifest_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_research_manifest_sha256",
        ),
    )
    op.create_index(
        "idx_research_cell_executed",
        "intelligence_research_runs",
        ["cell_id", "executed_at"],
    )

    op.create_table(
        "intelligence_research_category_slices",
        sa.Column("slice_id", UUID, primary_key=True),
        sa.Column(
            "run_id",
            UUID,
            sa.ForeignKey("intelligence_research_runs.run_id"),
            nullable=False,
        ),
        sa.Column("category_code", sa.String(64), nullable=False),
        sa.Column("vetoed_trades_count", sa.Integer(), nullable=False),
        sa.Column("losses_avoided_usd", sa.Numeric(12, 2), nullable=False),
        sa.Column("profits_forfeited_usd", sa.Numeric(12, 2), nullable=False),
        sa.Column("slice_net_alpha_usd", sa.Numeric(12, 2), nullable=False),
        sa.Column("slice_precision_pct", sa.Numeric(5, 2), nullable=False),
        sa.CheckConstraint(
            "vetoed_trades_count >= 0", name="ck_slice_trades_pos"
        ),
        sa.CheckConstraint(
            "losses_avoided_usd >= 0 AND profits_forfeited_usd >= 0",
            name="ck_slice_financials_nonnegative",
        ),
        sa.CheckConstraint(
            "slice_net_alpha_usd = losses_avoided_usd - profits_forfeited_usd",
            name="ck_slice_net_alpha_exact",
        ),
        sa.CheckConstraint(
            "slice_precision_pct >= 0.00 AND slice_precision_pct <= 100.00",
            name="ck_slice_precision_range",
        ),
        sa.UniqueConstraint(
            "run_id", "category_code", name="uq_research_run_category"
        ),
    )
    op.create_index(
        "idx_category_slices_run_id",
        "intelligence_research_category_slices",
        ["run_id"],
    )

    op.execute(
        """
        CREATE TRIGGER trg_intelligence_research_runs_immutable
        BEFORE UPDATE OR DELETE ON intelligence_research_runs
        FOR EACH ROW EXECUTE FUNCTION reject_intelligence_fact_mutation();

        CREATE TRIGGER trg_intelligence_research_category_slices_immutable
        BEFORE UPDATE OR DELETE ON intelligence_research_category_slices
        FOR EACH ROW EXECUTE FUNCTION reject_intelligence_fact_mutation();
        """
    )
    op.execute(
        """
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kairo_runtime') THEN
            REVOKE ALL ON intelligence_research_runs FROM kairo_runtime;
            GRANT SELECT, INSERT ON intelligence_research_runs TO kairo_runtime;
            REVOKE ALL ON intelligence_research_category_slices FROM kairo_runtime;
            GRANT SELECT, INSERT ON intelligence_research_category_slices TO kairo_runtime;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM intelligence_research_runs)
             OR EXISTS (SELECT 1 FROM intelligence_research_category_slices) THEN
            RAISE EXCEPTION
              'Refusing 0020 downgrade: immutable effectiveness research exists';
          END IF;
        END $$;
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_intelligence_research_category_slices_immutable "
        "ON intelligence_research_category_slices"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_intelligence_research_runs_immutable "
        "ON intelligence_research_runs"
    )
    op.drop_table("intelligence_research_category_slices")
    op.drop_table("intelligence_research_runs")
