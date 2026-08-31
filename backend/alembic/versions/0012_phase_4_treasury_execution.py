"""Phase 4 Step 2: target-treasury execution and projection.

Revision ID: 0012
Revises: 0011

Historical ``dollars_contributed`` and ``fractional_shares`` are retained
verbatim.  They are not used to seed the new execution-derived projection:
only a zero/zero legacy balance is trivially equivalent to a zero/zero
execution projection.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
CENT_MONEY = sa.Numeric(12, 2)
PRICE = sa.Numeric(12, 4)
SHARES = sa.Numeric(18, 6)
TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "treasury_executions",
        sa.Column("execution_id", UUID, primary_key=True),
        sa.Column("cell_id", UUID, sa.ForeignKey("capital_cells.cell_id"), nullable=False),
        sa.Column(
            "target_config_id",
            UUID,
            sa.ForeignKey("cell_treasury_configs.config_id"),
            nullable=False,
        ),
        sa.Column("instrument_id", UUID, sa.ForeignKey("instruments.instrument_id"), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("shares_executed", SHARES, nullable=False),
        sa.Column("execution_price_usd", PRICE, nullable=False),
        sa.Column("gross_amount_usd", CENT_MONEY, nullable=False),
        sa.Column("fee_usd", CENT_MONEY, nullable=False, server_default="0.00"),
        sa.Column("net_amount_usd", CENT_MONEY, nullable=False),
        sa.Column(
            "market_snapshot_id",
            UUID,
            sa.ForeignKey("market_snapshots.snapshot_id"),
            nullable=False,
        ),
        sa.Column("is_synthetic", sa.Boolean(), nullable=False),
        sa.Column("occurred_at", TZ, nullable=False),
        sa.CheckConstraint("shares_executed > 0", name="treasury_exec_shares_positive"),
        sa.CheckConstraint("execution_price_usd > 0", name="treasury_exec_price_positive"),
        sa.CheckConstraint("gross_amount_usd > 0", name="treasury_exec_gross_positive"),
        sa.CheckConstraint("fee_usd >= 0", name="treasury_exec_fee_nonnegative"),
        sa.CheckConstraint("net_amount_usd > 0", name="treasury_exec_net_positive"),
        sa.CheckConstraint(
            "net_amount_usd = gross_amount_usd + fee_usd",
            name="treasury_exec_net_sum",
        ),
    )
    op.create_index(
        "ix_treasury_exec_cell_occurred",
        "treasury_executions",
        ["cell_id", "occurred_at"],
    )
    op.create_index(
        "ix_treasury_exec_instrument", "treasury_executions", ["instrument_id"]
    )

    op.create_table(
        "treasury_cash_consumptions",
        sa.Column("consumption_id", UUID, primary_key=True),
        sa.Column(
            "execution_id",
            UUID,
            sa.ForeignKey("treasury_executions.execution_id"),
            nullable=False,
        ),
        sa.Column(
            "allocation_id",
            UUID,
            sa.ForeignKey("siphon_allocations.allocation_id"),
            nullable=False,
        ),
        sa.Column("consumed_usd", CENT_MONEY, nullable=False),
        sa.Column("occurred_at", TZ, nullable=False),
        sa.CheckConstraint("consumed_usd > 0", name="treasury_consumed_positive"),
    )
    op.create_index(
        "ix_treasury_consumptions_alloc",
        "treasury_cash_consumptions",
        ["allocation_id"],
    )
    op.create_index(
        "ix_treasury_consumptions_exec",
        "treasury_cash_consumptions",
        ["execution_id"],
    )

    op.create_table(
        "treasury_regime_observations",
        sa.Column("event_id", UUID, primary_key=True),
        sa.Column("cell_id", UUID, sa.ForeignKey("capital_cells.cell_id"), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("gate_name", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("observed_metric_value", sa.Numeric(10, 4)),
        sa.Column("threshold_value", sa.Numeric(10, 4)),
        sa.Column(
            "market_snapshot_id",
            UUID,
            sa.ForeignKey("market_snapshots.snapshot_id"),
        ),
        sa.Column("is_synthetic", sa.Boolean(), nullable=False),
        sa.Column("occurred_at", TZ, nullable=False),
    )
    op.create_index(
        "ix_treasury_regime_cell_occurred",
        "treasury_regime_observations",
        ["cell_id", "occurred_at"],
    )

    # Option 2: evolve the one existing canonical projection in place.
    op.add_column("ownership_treasury_holdings", sa.Column("cell_id", UUID))
    op.add_column("ownership_treasury_holdings", sa.Column("symbol", sa.String(32)))
    op.add_column("ownership_treasury_holdings", sa.Column("total_shares", SHARES))
    op.add_column(
        "ownership_treasury_holdings", sa.Column("cumulative_cost_basis_usd", CENT_MONEY)
    )
    op.add_column(
        "ownership_treasury_holdings", sa.Column("average_entry_price_usd", PRICE)
    )
    op.add_column("ownership_treasury_holdings", sa.Column("last_marked_price_usd", PRICE))
    op.add_column("ownership_treasury_holdings", sa.Column("market_value_usd", CENT_MONEY))
    op.add_column("ownership_treasury_holdings", sa.Column("unrealized_pnl_usd", CENT_MONEY))
    op.add_column("ownership_treasury_holdings", sa.Column("is_synthetic", sa.Boolean()))
    op.add_column(
        "ownership_treasury_holdings",
        sa.Column("legacy_values_equivalent", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    # Existing cell lineage is valid only for a unique treasury-code mapping.
    # Economic-domain lineage is valid only where non-legacy allocation evidence
    # for the same cell/config/instrument resolves to exactly one domain.
    op.execute(
        """
        DO $$
        DECLARE h RECORD; v_cell uuid; v_cell_count integer;
                v_domain boolean; v_domain_count integer; v_symbol text;
        BEGIN
          FOR h IN SELECT * FROM ownership_treasury_holdings LOOP
            SELECT (array_agg(cell_id ORDER BY cell_id))[1], count(*) INTO v_cell, v_cell_count
            FROM capital_cells WHERE target_treasury_code = h.treasury_code;
            IF v_cell_count <> 1 THEN
              RAISE EXCEPTION 'cannot resolve unique cell lineage for treasury holding % (treasury_code %)',
                h.holding_id, h.treasury_code;
            END IF;

            SELECT min(se.is_synthetic::text)::boolean,
                   count(DISTINCT se.is_synthetic)
              INTO v_domain, v_domain_count
            FROM siphon_allocations sa
            JOIN siphon_events se ON se.siphon_id = sa.siphon_id
            JOIN cell_treasury_configs c ON c.config_id = se.target_config_id
            WHERE se.cell_id = v_cell
              AND c.target_instrument_id = h.instrument_id
              AND sa.bucket_type = 'TARGET_TREASURY'
              AND se.policy_id <> 'LEGACY-SIPHON-v0';
            IF v_domain_count <> 1 THEN
              RAISE EXCEPTION 'cannot resolve trustworthy economic domain for treasury holding %',
                h.holding_id;
            END IF;

            SELECT symbol INTO STRICT v_symbol FROM instruments
            WHERE instrument_id = h.instrument_id;
            UPDATE ownership_treasury_holdings
              SET cell_id = v_cell,
                  symbol = v_symbol,
                  is_synthetic = v_domain,
                  total_shares = 0,
                  cumulative_cost_basis_usd = 0,
                  average_entry_price_usd = 0,
                  market_value_usd = 0,
                  unrealized_pnl_usd = 0,
                  legacy_values_equivalent =
                    (h.dollars_contributed = 0 AND h.fractional_shares = 0)
              WHERE holding_id = h.holding_id;
          END LOOP;
        END $$;
        """
    )
    for column in (
        "cell_id",
        "symbol",
        "total_shares",
        "cumulative_cost_basis_usd",
        "average_entry_price_usd",
        "market_value_usd",
        "unrealized_pnl_usd",
        "is_synthetic",
    ):
        op.alter_column("ownership_treasury_holdings", column, nullable=False)
    op.create_foreign_key(
        "fk_ownership_treasury_holdings_cell_id_capital_cells",
        "ownership_treasury_holdings",
        "capital_cells",
        ["cell_id"],
        ["cell_id"],
    )
    op.drop_constraint(
        "uq_treasury_holding_instrument", "ownership_treasury_holdings", type_="unique"
    )
    op.create_unique_constraint(
        "uq_cell_instrument_holding",
        "ownership_treasury_holdings",
        ["cell_id", "instrument_id", "is_synthetic"],
    )
    op.create_check_constraint(
        "treasury_total_shares_nonnegative",
        "ownership_treasury_holdings",
        "total_shares >= 0",
    )
    op.create_check_constraint(
        "treasury_basis_nonnegative",
        "ownership_treasury_holdings",
        "cumulative_cost_basis_usd >= 0",
    )
    op.create_index(
        "ix_ownership_treasury_holdings_cell",
        "ownership_treasury_holdings",
        ["cell_id"],
    )

    # The ceiling trigger serializes on the allocation row.  The lineage trigger
    # resolves every hop explicitly and uses IS DISTINCT FROM, so a missing row or
    # SQL NULL can never turn a mismatch into a pass.
    op.execute(
        """
        CREATE FUNCTION check_treasury_allocation_consumption_ceiling()
        RETURNS trigger AS $$
        DECLARE v_allocated numeric(12,2); v_bucket text; v_total numeric(12,2);
        BEGIN
          SELECT allocated_usd, bucket_type INTO v_allocated, v_bucket
          FROM siphon_allocations WHERE allocation_id = NEW.allocation_id FOR UPDATE;
          IF NOT FOUND THEN
            RAISE EXCEPTION 'target siphon allocation % cannot be resolved', NEW.allocation_id;
          END IF;
          IF v_bucket IS DISTINCT FROM 'TARGET_TREASURY' THEN
            RAISE EXCEPTION 'treasury consumption requires TARGET_TREASURY allocation; got % for %',
              v_bucket, NEW.allocation_id;
          END IF;
          SELECT COALESCE(sum(consumed_usd), 0) INTO v_total
          FROM treasury_cash_consumptions WHERE allocation_id = NEW.allocation_id;
          IF v_total > v_allocated THEN
            RAISE EXCEPTION 'treasury consumption % exceeds allocation % for %',
              v_total, v_allocated, NEW.allocation_id;
          END IF;
          RETURN NEW;
        END; $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_check_treasury_consumption_ceiling
        AFTER INSERT ON treasury_cash_consumptions FOR EACH ROW
        EXECUTE FUNCTION check_treasury_allocation_consumption_ceiling();

        CREATE FUNCTION check_treasury_target_lineage()
        RETURNS trigger AS $$
        DECLARE v_exec_config uuid; v_exec_instrument uuid; v_exec_cell uuid;
                v_siphon uuid; v_alloc_config uuid; v_alloc_cell uuid;
                v_config_instrument uuid; v_config_cell uuid;
        BEGIN
          SELECT target_config_id, instrument_id, cell_id
            INTO v_exec_config, v_exec_instrument, v_exec_cell
          FROM treasury_executions WHERE execution_id = NEW.execution_id;
          IF NOT FOUND THEN
            RAISE EXCEPTION 'treasury execution % cannot be resolved', NEW.execution_id;
          END IF;

          SELECT siphon_id INTO v_siphon FROM siphon_allocations
          WHERE allocation_id = NEW.allocation_id;
          IF NOT FOUND THEN
            RAISE EXCEPTION 'siphon allocation % cannot be resolved', NEW.allocation_id;
          END IF;

          SELECT target_config_id, cell_id INTO v_alloc_config, v_alloc_cell
          FROM siphon_events WHERE siphon_id = v_siphon;
          IF NOT FOUND THEN
            RAISE EXCEPTION 'siphon event % cannot be resolved for allocation %',
              v_siphon, NEW.allocation_id;
          END IF;

          SELECT target_instrument_id, cell_id INTO v_config_instrument, v_config_cell
          FROM cell_treasury_configs WHERE config_id = v_exec_config;
          IF NOT FOUND THEN
            RAISE EXCEPTION 'target config % cannot be resolved for execution %',
              v_exec_config, NEW.execution_id;
          END IF;

          IF v_exec_config IS NULL OR v_alloc_config IS NULL OR
             v_exec_config IS DISTINCT FROM v_alloc_config THEN
            RAISE EXCEPTION 'target config mismatch: execution %, allocation % for %',
              v_exec_config, v_alloc_config, NEW.allocation_id;
          END IF;
          IF v_exec_instrument IS NULL OR v_config_instrument IS NULL OR
             v_exec_instrument IS DISTINCT FROM v_config_instrument THEN
            RAISE EXCEPTION 'instrument mismatch: execution %, config % for %',
              v_exec_instrument, v_config_instrument, v_exec_config;
          END IF;
          IF v_exec_cell IS NULL OR v_alloc_cell IS NULL OR v_config_cell IS NULL OR
             v_exec_cell IS DISTINCT FROM v_alloc_cell OR
             v_exec_cell IS DISTINCT FROM v_config_cell THEN
            RAISE EXCEPTION 'cell lineage mismatch for execution %, allocation %, config %',
              NEW.execution_id, NEW.allocation_id, v_exec_config;
          END IF;
          RETURN NEW;
        END; $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_check_treasury_target_lineage
        AFTER INSERT ON treasury_cash_consumptions FOR EACH ROW
        EXECUTE FUNCTION check_treasury_target_lineage();

        CREATE FUNCTION reject_treasury_fact_mutation() RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'immutable treasury fact cannot be updated or deleted: %', TG_TABLE_NAME;
        END; $$ LANGUAGE plpgsql;
        CREATE TRIGGER trg_treasury_executions_immutable
          BEFORE UPDATE OR DELETE ON treasury_executions FOR EACH ROW
          EXECUTE FUNCTION reject_treasury_fact_mutation();
        CREATE TRIGGER trg_treasury_cash_consumptions_immutable
          BEFORE UPDATE OR DELETE ON treasury_cash_consumptions FOR EACH ROW
          EXECUTE FUNCTION reject_treasury_fact_mutation();
        CREATE TRIGGER trg_treasury_regime_observations_immutable
          BEFORE UPDATE OR DELETE ON treasury_regime_observations FOR EACH ROW
          EXECUTE FUNCTION reject_treasury_fact_mutation();
        """
    )

    op.execute(
        "GRANT SELECT, INSERT ON treasury_executions, treasury_cash_consumptions, "
        "treasury_regime_observations TO kairo_runtime"
    )
    op.execute(
        "REVOKE UPDATE, DELETE ON treasury_executions, treasury_cash_consumptions, "
        "treasury_regime_observations FROM kairo_runtime"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON ownership_treasury_holdings TO kairo_runtime"
    )
    op.execute("REVOKE DELETE ON ownership_treasury_holdings FROM kairo_runtime")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS reject_treasury_fact_mutation() CASCADE")
    op.execute("DROP FUNCTION IF EXISTS check_treasury_target_lineage() CASCADE")
    op.execute("DROP FUNCTION IF EXISTS check_treasury_allocation_consumption_ceiling() CASCADE")
    op.drop_index(
        "ix_ownership_treasury_holdings_cell", table_name="ownership_treasury_holdings"
    )
    op.drop_constraint(
        op.f("ck_ownership_treasury_holdings_treasury_basis_nonnegative"),
        "ownership_treasury_holdings",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_ownership_treasury_holdings_treasury_total_shares_nonnegative"),
        "ownership_treasury_holdings",
        type_="check",
    )
    op.drop_constraint(
        "uq_cell_instrument_holding", "ownership_treasury_holdings", type_="unique"
    )
    op.create_unique_constraint(
        "uq_treasury_holding_instrument",
        "ownership_treasury_holdings",
        ["treasury_code", "instrument_id"],
    )
    op.drop_constraint(
        "fk_ownership_treasury_holdings_cell_id_capital_cells",
        "ownership_treasury_holdings",
        type_="foreignkey",
    )
    for column in (
        "legacy_values_equivalent",
        "is_synthetic",
        "unrealized_pnl_usd",
        "market_value_usd",
        "last_marked_price_usd",
        "average_entry_price_usd",
        "cumulative_cost_basis_usd",
        "total_shares",
        "symbol",
        "cell_id",
    ):
        op.drop_column("ownership_treasury_holdings", column)
    op.drop_table("treasury_regime_observations")
    op.drop_table("treasury_cash_consumptions")
    op.drop_table("treasury_executions")
