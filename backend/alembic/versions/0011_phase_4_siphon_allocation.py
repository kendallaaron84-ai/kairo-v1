"""Phase 4 Step 1: settled-profit siphon allocation and attribution.

Revision ID: 0011
Revises: 0010
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
MONEY = sa.Numeric(28, 10)
CENT_MONEY = sa.Numeric(12, 2)
TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "cell_treasury_configs",
        sa.Column("config_id", UUID, primary_key=True),
        sa.Column("cell_id", UUID, sa.ForeignKey("capital_cells.cell_id"), nullable=False),
        sa.Column("target_type", sa.String(32), nullable=False, server_default="SINGLE_ASSET"),
        sa.Column(
            "target_instrument_id", UUID, sa.ForeignKey("instruments.instrument_id"), nullable=False
        ),
        sa.Column("target_symbol", sa.String(32), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("authorized_by", sa.String(64), nullable=False),
        sa.Column("created_at", TZ, nullable=False),
        sa.CheckConstraint(
            "target_type IN ('SINGLE_ASSET', 'BASKET', 'INDEX', 'CASH_GOAL')",
            name=op.f("ck_cell_treasury_configs_valid_target_type"),
        ),
        sa.CheckConstraint(
            "config_version > 0", name=op.f("ck_cell_treasury_configs_positive_config_version")
        ),
        sa.UniqueConstraint(
            "cell_id", "config_version", name="uq_cell_treasury_version"
        ),
    )
    op.create_index(
        "uq_cell_treasury_one_active",
        "cell_treasury_configs",
        ["cell_id"],
        unique=True,
        postgresql_where=sa.text("is_active = true"),
    )

    # The existing table predates settlement provenance. Preserve those rows as
    # explicitly unverified legacy facts; never fabricate live or paper lineage.
    op.add_column("siphon_events", sa.Column("policy_id", sa.String(64)))
    op.add_column("siphon_events", sa.Column("policy_version", sa.String(32)))
    op.add_column("siphon_events", sa.Column("broker_account_id", UUID))
    op.add_column("siphon_events", sa.Column("settlement_snapshot_id", UUID))
    op.add_column(
        "siphon_events",
        sa.Column(
            "source_fill_ids",
            postgresql.ARRAY(UUID),
            nullable=False,
            server_default=sa.text("'{}'::uuid[]"),
        ),
    )
    op.add_column("siphon_events", sa.Column("qualified_profit_usd", CENT_MONEY))
    op.add_column(
        "siphon_events",
        sa.Column("safety_reserve_usd", CENT_MONEY, nullable=False, server_default="0.00"),
    )
    op.add_column(
        "siphon_events",
        sa.Column("target_treasury_usd", CENT_MONEY, nullable=False, server_default="0.00"),
    )
    op.add_column(
        "siphon_events",
        sa.Column("replication_pool_usd", CENT_MONEY, nullable=False, server_default="0.00"),
    )
    op.add_column("siphon_events", sa.Column("target_config_id", UUID))
    op.add_column(
        "siphon_events",
        sa.Column("is_synthetic", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("siphon_events", sa.Column("synthetic_settlement_metadata", postgresql.JSONB()))
    op.add_column("siphon_events", sa.Column("source_manifest_hash", sa.String(64)))
    op.execute(
        "UPDATE siphon_events SET policy_id = 'LEGACY-SIPHON-v0', "
        "policy_version = '0.0.0', qualified_profit_usd = round(amount::numeric, 2), "
        "replication_pool_usd = round(amount::numeric, 2)"
    )
    op.alter_column("siphon_events", "policy_id", nullable=False)
    op.alter_column("siphon_events", "policy_version", nullable=False)
    op.alter_column("siphon_events", "qualified_profit_usd", nullable=False)
    op.create_foreign_key(
        "fk_siphon_events_broker_account_id_broker_accounts",
        "siphon_events",
        "broker_accounts",
        ["broker_account_id"],
        ["broker_account_id"],
    )
    op.create_foreign_key(
        "fk_siphon_events_target_config_id_cell_treasury_configs",
        "siphon_events",
        "cell_treasury_configs",
        ["target_config_id"],
        ["config_id"],
    )
    op.create_foreign_key(
        "fk_siphon_settlement_broker_account",
        "siphon_events",
        "broker_cash_snapshots",
        ["settlement_snapshot_id", "broker_account_id"],
        ["snapshot_id", "broker_account_id"],
    )
    op.create_check_constraint(
        op.f("ck_siphon_events_siphon_allocation_sum"),
        "siphon_events",
        "policy_id = 'LEGACY-SIPHON-v0' OR qualified_profit_usd = "
        "(safety_reserve_usd + target_treasury_usd + replication_pool_usd)",
    )
    op.create_check_constraint(
        op.f("ck_siphon_events_siphon_provenance_mode"),
        "siphon_events",
        "policy_id = 'LEGACY-SIPHON-v0' OR "
        "((is_synthetic = false AND broker_account_id IS NOT NULL "
        "AND settlement_snapshot_id IS NOT NULL "
        "AND synthetic_settlement_metadata IS NULL) OR "
        "(is_synthetic = true AND settlement_snapshot_id IS NULL "
        "AND synthetic_settlement_metadata IS NOT NULL "
        "AND synthetic_settlement_metadata->>'settlement_evidence_type' = "
        "'SYNTHETIC_REPLAY_SETTLEMENT' "
        "AND synthetic_settlement_metadata ? 'synthetic_settled_at' "
        "AND synthetic_settlement_metadata ? 'replay_session_id' "
        "AND synthetic_settlement_metadata ? 'model_version'))",
    )

    op.create_table(
        "fill_realized_pnl",
        sa.Column("realization_id", UUID, primary_key=True),
        sa.Column("fill_id", UUID, sa.ForeignKey("fills.fill_id"), nullable=False),
        sa.Column("cell_id", UUID, sa.ForeignKey("capital_cells.cell_id"), nullable=False),
        sa.Column("position_effect", sa.String(16), nullable=False),
        sa.Column("realized_pnl_usd", MONEY, nullable=False),
        sa.Column("source_authority", sa.String(64), nullable=False),
        sa.Column("occurred_at", TZ, nullable=False),
        sa.CheckConstraint(
            "position_effect IN ('OPENING', 'CLOSING')",
            name=op.f("ck_fill_realized_pnl_valid_position_effect"),
        ),
        sa.CheckConstraint(
            "position_effect <> 'OPENING' OR realized_pnl_usd = 0",
            name=op.f("ck_fill_realized_pnl_opening_realized_pnl_zero"),
        ),
        sa.UniqueConstraint("fill_id", name="uq_fill_realized_pnl_fill"),
    )
    op.create_index(
        "ix_fill_realized_pnl_cell_occurred",
        "fill_realized_pnl",
        ["cell_id", "occurred_at"],
    )
    op.create_table(
        "siphon_profit_attributions",
        sa.Column("attribution_id", UUID, primary_key=True),
        sa.Column("siphon_id", UUID, sa.ForeignKey("siphon_events.siphon_id"), nullable=False),
        sa.Column("source_fill_id", UUID, sa.ForeignKey("fills.fill_id"), nullable=False),
        sa.Column("attributed_profit_usd", CENT_MONEY, nullable=False),
        sa.Column("occurred_at", TZ, nullable=False),
        sa.CheckConstraint(
            "attributed_profit_usd > 0",
            name=op.f("ck_siphon_profit_attributions_attributed_profit_positive"),
        ),
    )
    op.create_index(
        "ix_siphon_profit_attribution_fill", "siphon_profit_attributions", ["source_fill_id"]
    )
    op.create_index(
        "ix_siphon_profit_attribution_siphon", "siphon_profit_attributions", ["siphon_id"]
    )
    op.create_table(
        "siphon_allocations",
        sa.Column("allocation_id", UUID, primary_key=True),
        sa.Column("siphon_id", UUID, sa.ForeignKey("siphon_events.siphon_id"), nullable=False),
        sa.Column("bucket_type", sa.String(32), nullable=False),
        sa.Column("allocated_usd", CENT_MONEY, nullable=False),
        sa.Column("unallocated_cash_balance_usd", CENT_MONEY, nullable=False),
        sa.Column("occurred_at", TZ, nullable=False),
        sa.CheckConstraint(
            "bucket_type IN ('SAFETY_RESERVE', 'TARGET_TREASURY', 'REPLICATION_POOL')",
            name=op.f("ck_siphon_allocations_valid_bucket_type"),
        ),
        sa.CheckConstraint(
            "allocated_usd > 0", name=op.f("ck_siphon_allocations_allocated_usd_positive")
        ),
        sa.UniqueConstraint("siphon_id", "bucket_type", name="uq_siphon_allocation_bucket"),
    )
    op.create_index("ix_siphon_allocations_bucket", "siphon_allocations", ["bucket_type"])

    # Cross-row ceiling enforcement makes direct SQL and concurrent managers fail closed.
    op.execute(
        """
        CREATE FUNCTION enforce_siphon_attribution_ceiling() RETURNS trigger AS $$
        DECLARE source_profit numeric; already_attributed numeric;
        BEGIN
          SELECT realized_pnl_usd INTO source_profit
          FROM fill_realized_pnl
          WHERE fill_id = NEW.source_fill_id AND position_effect = 'CLOSING'
          FOR UPDATE;
          IF source_profit IS NULL OR source_profit <= 0 THEN
            RAISE EXCEPTION 'source fill has no positive canonical closing profit';
          END IF;
          SELECT COALESCE(SUM(attributed_profit_usd), 0) INTO already_attributed
          FROM siphon_profit_attributions WHERE source_fill_id = NEW.source_fill_id;
          IF already_attributed + NEW.attributed_profit_usd > source_profit THEN
            RAISE EXCEPTION 'siphon attribution exceeds canonical realized profit';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER trg_siphon_attribution_ceiling
        BEFORE INSERT ON siphon_profit_attributions
        FOR EACH ROW EXECUTE FUNCTION enforce_siphon_attribution_ceiling();
        """
    )
    op.execute(
        """
        CREATE FUNCTION prevent_siphon_ledger_mutation() RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'siphon and realized-PnL facts are append-only';
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER trg_siphon_events_append_only
          BEFORE UPDATE OR DELETE ON siphon_events
          FOR EACH ROW EXECUTE FUNCTION prevent_siphon_ledger_mutation();
        CREATE TRIGGER trg_fill_realized_pnl_append_only
          BEFORE UPDATE OR DELETE ON fill_realized_pnl
          FOR EACH ROW EXECUTE FUNCTION prevent_siphon_ledger_mutation();
        CREATE TRIGGER trg_siphon_profit_attributions_append_only
          BEFORE UPDATE OR DELETE ON siphon_profit_attributions
          FOR EACH ROW EXECUTE FUNCTION prevent_siphon_ledger_mutation();
        CREATE TRIGGER trg_siphon_allocations_append_only
          BEFORE UPDATE OR DELETE ON siphon_allocations
          FOR EACH ROW EXECUTE FUNCTION prevent_siphon_ledger_mutation();
        """
    )
    op.execute(
        "GRANT SELECT, INSERT ON fill_realized_pnl, siphon_profit_attributions, "
        "siphon_allocations TO kairo_runtime"
    )
    op.execute(
        "REVOKE UPDATE, DELETE ON fill_realized_pnl, siphon_profit_attributions, "
        "siphon_allocations, siphon_events FROM kairo_runtime"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON cell_treasury_configs TO kairo_runtime")
    op.execute("REVOKE DELETE ON cell_treasury_configs FROM kairo_runtime")


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_siphon_events_append_only ON siphon_events")
    op.execute("DROP FUNCTION prevent_siphon_ledger_mutation() CASCADE")
    op.execute("DROP TRIGGER trg_siphon_attribution_ceiling ON siphon_profit_attributions")
    op.execute("DROP FUNCTION enforce_siphon_attribution_ceiling()")
    op.drop_table("siphon_allocations")
    op.drop_table("siphon_profit_attributions")
    op.drop_table("fill_realized_pnl")
    op.drop_constraint(
        op.f("ck_siphon_events_siphon_provenance_mode"), "siphon_events", type_="check"
    )
    op.drop_constraint(
        op.f("ck_siphon_events_siphon_allocation_sum"), "siphon_events", type_="check"
    )
    op.drop_constraint(
        "fk_siphon_settlement_broker_account", "siphon_events", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_siphon_events_target_config_id_cell_treasury_configs",
        "siphon_events",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_siphon_events_broker_account_id_broker_accounts",
        "siphon_events",
        type_="foreignkey",
    )
    for column in (
        "source_manifest_hash",
        "synthetic_settlement_metadata",
        "is_synthetic",
        "target_config_id",
        "replication_pool_usd",
        "target_treasury_usd",
        "safety_reserve_usd",
        "qualified_profit_usd",
        "source_fill_ids",
        "settlement_snapshot_id",
        "broker_account_id",
        "policy_version",
        "policy_id",
    ):
        op.drop_column("siphon_events", column)
    op.drop_table("cell_treasury_configs")
