"""Phase 3.5 Step 5.5: Stateful Replay Counterfactual Engine.

Revision ID: 0021
Revises: 0020
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "intelligence_stateful_replay_runs",
        sa.Column("replay_run_id", UUID, primary_key=True),
        sa.Column(
            "cell_id", UUID, sa.ForeignKey("capital_cells.cell_id"), nullable=False
        ),
        sa.Column(
            "research_method",
            sa.String(64),
            server_default="STATEFUL_REPLAY_COUNTERFACTUAL",
            nullable=False,
        ),
        sa.Column("sample_start_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sample_end_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("baseline_trade_count", sa.Integer(), nullable=False),
        sa.Column("counterfactual_trade_count", sa.Integer(), nullable=False),
        sa.Column("direct_vetoed_trades_count", sa.Integer(), nullable=False),
        sa.Column("induced_trades_taken_count", sa.Integer(), nullable=False),
        sa.Column("induced_trades_missed_count", sa.Integer(), nullable=False),
        sa.Column("baseline_net_pnl", sa.Numeric(12, 2), nullable=False),
        sa.Column("counterfactual_net_pnl", sa.Numeric(12, 2), nullable=False),
        sa.Column("stateful_net_alpha_usd", sa.Numeric(12, 2), nullable=False),
        sa.Column("baseline_max_drawdown_usd", sa.Numeric(12, 2), nullable=False),
        sa.Column("counterfactual_max_drawdown_usd", sa.Numeric(12, 2), nullable=False),
        sa.Column("drawdown_reduction_usd", sa.Numeric(12, 2), nullable=False),
        sa.Column("baseline_halt_count", sa.Integer(), nullable=False),
        sa.Column("counterfactual_halt_count", sa.Integer(), nullable=False),
        sa.Column("siphon_delta_treasury_usd", sa.Numeric(12, 2), nullable=False),
        sa.Column("siphon_delta_replication_usd", sa.Numeric(12, 2), nullable=False),
        sa.Column("siphon_delta_safety_usd", sa.Numeric(12, 2), nullable=False),
        sa.Column("baseline_cell_count", sa.Integer(), nullable=False),
        sa.Column("counterfactual_cell_count", sa.Integer(), nullable=False),
        sa.Column("genesis_timing_delta_sessions", sa.Integer()),
        sa.Column("stateful_replay_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "research_method = 'STATEFUL_REPLAY_COUNTERFACTUAL'",
            name="ck_replay_method_stateful",
        ),
        sa.CheckConstraint(
            "sample_end_time >= sample_start_time", name="ck_stateful_replay_window"
        ),
        sa.CheckConstraint(
            "baseline_trade_count >= 0 AND counterfactual_trade_count >= 0 "
            "AND direct_vetoed_trades_count >= 0 "
            "AND induced_trades_taken_count >= 0 "
            "AND induced_trades_missed_count >= 0 "
            "AND baseline_halt_count >= 0 AND counterfactual_halt_count >= 0",
            name="ck_stateful_replay_counts_nonnegative",
        ),
        sa.CheckConstraint(
            "baseline_cell_count >= 1", name="ck_stateful_base_cells_pos"
        ),
        sa.CheckConstraint(
            "counterfactual_cell_count >= 1", name="ck_stateful_cf_cells_pos"
        ),
        sa.CheckConstraint(
            "stateful_net_alpha_usd = counterfactual_net_pnl - baseline_net_pnl",
            name="ck_stateful_alpha_exact",
        ),
        sa.CheckConstraint(
            "drawdown_reduction_usd = baseline_max_drawdown_usd "
            "- counterfactual_max_drawdown_usd",
            name="ck_stateful_drawdown_delta_exact",
        ),
        sa.CheckConstraint(
            "baseline_max_drawdown_usd >= 0 "
            "AND counterfactual_max_drawdown_usd >= 0",
            name="ck_stateful_drawdowns_nonnegative",
        ),
        sa.CheckConstraint(
            "stateful_replay_manifest_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_stateful_replay_manifest_sha256",
        ),
    )
    op.create_index(
        "idx_stateful_replay_cell",
        "intelligence_stateful_replay_runs",
        ["cell_id", "executed_at"],
    )
    op.create_table(
        "stateful_replay_session_deltas",
        sa.Column("session_delta_id", UUID, primary_key=True),
        sa.Column(
            "replay_run_id",
            UUID,
            sa.ForeignKey("intelligence_stateful_replay_runs.replay_run_id"),
            nullable=False,
        ),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("baseline_session_pnl", sa.Numeric(12, 2), nullable=False),
        sa.Column("counterfactual_session_pnl", sa.Numeric(12, 2), nullable=False),
        sa.Column("session_alpha_usd", sa.Numeric(12, 2), nullable=False),
        sa.Column("baseline_halted", sa.Boolean(), nullable=False),
        sa.Column("counterfactual_halted", sa.Boolean(), nullable=False),
        sa.Column("vetoed_in_session_count", sa.Integer(), nullable=False),
        sa.Column("induced_in_session_count", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "session_alpha_usd = counterfactual_session_pnl - baseline_session_pnl",
            name="ck_stateful_session_alpha_exact",
        ),
        sa.CheckConstraint(
            "vetoed_in_session_count >= 0 AND induced_in_session_count >= 0",
            name="ck_stateful_session_counts_nonnegative",
        ),
        sa.UniqueConstraint(
            "replay_run_id", "session_date", name="uq_stateful_session_date"
        ),
    )
    op.create_index(
        "idx_session_deltas_run_id",
        "stateful_replay_session_deltas",
        ["replay_run_id"],
    )
    op.execute(
        """
        CREATE TRIGGER trg_stateful_replay_runs_immutable
        BEFORE UPDATE OR DELETE ON intelligence_stateful_replay_runs
        FOR EACH ROW EXECUTE FUNCTION reject_intelligence_fact_mutation();

        CREATE TRIGGER trg_stateful_session_deltas_immutable
        BEFORE UPDATE OR DELETE ON stateful_replay_session_deltas
        FOR EACH ROW EXECUTE FUNCTION reject_intelligence_fact_mutation();
        """
    )
    op.execute(
        """
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kairo_runtime') THEN
            REVOKE ALL ON intelligence_stateful_replay_runs FROM kairo_runtime;
            GRANT SELECT, INSERT ON intelligence_stateful_replay_runs TO kairo_runtime;
            REVOKE ALL ON stateful_replay_session_deltas FROM kairo_runtime;
            GRANT SELECT, INSERT ON stateful_replay_session_deltas TO kairo_runtime;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM intelligence_stateful_replay_runs)
             OR EXISTS (SELECT 1 FROM stateful_replay_session_deltas) THEN
            RAISE EXCEPTION
              'Refusing 0021 downgrade: immutable stateful replay evidence exists';
          END IF;
        END $$;
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_stateful_session_deltas_immutable "
        "ON stateful_replay_session_deltas"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_stateful_replay_runs_immutable "
        "ON intelligence_stateful_replay_runs"
    )
    op.drop_table("stateful_replay_session_deltas")
    op.drop_table("intelligence_stateful_replay_runs")
