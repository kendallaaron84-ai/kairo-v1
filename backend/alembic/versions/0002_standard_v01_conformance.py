"""Restore frozen Standard v0.1 financial contract fields and lineage.

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


MONEY = sa.Numeric(28, 10)
UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    for column in (
        sa.Column("underlying_symbol", sa.String(32)),
        sa.Column("contract_symbol", sa.String(64)),
        sa.Column("expiration_date", sa.Date()),
        sa.Column("strike_price", MONEY),
        sa.Column("option_right", sa.String(8)),
        sa.Column("contract_multiplier", MONEY),
        sa.Column("listing_type", sa.String(32)),
    ):
        op.add_column("instruments", column)
    op.create_unique_constraint(
        "uq_instruments_contract_symbol", "instruments", ["contract_symbol"]
    )
    op.create_check_constraint(
        op.f("ck_instruments_complete_option_identity"),
        "instruments",
        "asset_class <> 'OPTION' OR (underlying_symbol IS NOT NULL "
        "AND contract_symbol IS NOT NULL AND expiration_date IS NOT NULL "
        "AND strike_price IS NOT NULL AND strike_price > 0 "
        "AND option_right IS NOT NULL AND option_right IN ('CALL', 'PUT') "
        "AND contract_multiplier IS NOT NULL AND contract_multiplier > 0 "
        "AND listing_type IS NOT NULL)",
    )

    for name in (
        "notional_orders_supported",
        "options_supported",
        "extended_hours_supported",
    ):
        op.add_column(
            "broker_instrument_capabilities",
            sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.false()),
        )
        op.alter_column("broker_instrument_capabilities", name, server_default=None)

    op.add_column("order_intents", sa.Column("order_purpose", sa.String(32)))
    op.add_column("order_intents", sa.Column("target_notional_usd", MONEY))
    op.add_column("order_intents", sa.Column("target_quantity", MONEY))
    op.add_column("order_intents", sa.Column("stop_price", MONEY))
    op.execute(
        "UPDATE order_intents SET order_purpose = 'ENTRY', target_quantity = quantity"
    )
    op.alter_column("order_intents", "order_purpose", nullable=False)
    op.drop_column("order_intents", "quantity")
    op.create_check_constraint(
        op.f("ck_order_intents_single_sizing_mode"),
        "order_intents",
        "(target_notional_usd IS NOT NULL AND target_quantity IS NULL) OR "
        "(target_notional_usd IS NULL AND target_quantity IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_order_intents_valid_order_purpose"),
        "order_intents",
        "order_purpose IN ('ENTRY', 'TAKE_PROFIT', 'STOP_LOSS', "
        "'EMERGENCY_EXIT', 'TREASURY_PURCHASE')",
    )
    op.create_check_constraint(
        op.f("ck_order_intents_positive_notional"),
        "order_intents",
        "target_notional_usd IS NULL OR target_notional_usd > 0",
    )
    op.create_check_constraint(
        op.f("ck_order_intents_positive_quantity"),
        "order_intents",
        "target_quantity IS NULL OR target_quantity > 0",
    )
    op.create_check_constraint(
        op.f("ck_order_intents_canonical_order_prices"),
        "order_intents",
        "(order_type = 'MARKET' AND limit_price IS NULL AND stop_price IS NULL) OR "
        "(order_type = 'LIMIT' AND limit_price IS NOT NULL "
        "AND limit_price > 0 AND stop_price IS NULL) OR "
        "(order_type = 'STOP' AND limit_price IS NULL "
        "AND stop_price IS NOT NULL AND stop_price > 0)",
    )

    op.add_column("broker_cash_snapshots", sa.Column("broker_cash", MONEY))
    op.add_column("broker_cash_snapshots", sa.Column("unsettled_cash", MONEY))
    op.execute(
        "UPDATE broker_cash_snapshots "
        "SET broker_cash = settled_cash, unsettled_cash = 0"
    )
    op.alter_column("broker_cash_snapshots", "broker_cash", nullable=False)
    op.alter_column("broker_cash_snapshots", "unsettled_cash", nullable=False)
    op.create_check_constraint(
        op.f("ck_broker_cash_snapshots_broker_cash_nonnegative"),
        "broker_cash_snapshots",
        "broker_cash >= 0",
    )
    op.create_check_constraint(
        op.f("ck_broker_cash_snapshots_unsettled_nonnegative"),
        "broker_cash_snapshots",
        "unsettled_cash >= 0",
    )
    op.create_unique_constraint(
        "uq_broker_cash_snapshot_account",
        "broker_cash_snapshots",
        ["snapshot_id", "broker_account_id"],
    )

    op.add_column(
        "kairo_capital_authorizations", sa.Column("broker_snapshot_id", UUID)
    )
    op.add_column(
        "kairo_capital_authorizations", sa.Column("broker_account_id", UUID)
    )
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM kairo_capital_authorizations) THEN "
        "RAISE EXCEPTION 'capital authorizations require explicit broker snapshot backfill'; "
        "END IF; END $$;"
    )
    op.alter_column(
        "kairo_capital_authorizations", "broker_snapshot_id", nullable=False
    )
    op.alter_column(
        "kairo_capital_authorizations", "broker_account_id", nullable=False
    )
    op.create_foreign_key(
        "fk_capital_authorizations_broker_account",
        "kairo_capital_authorizations",
        "broker_accounts",
        ["broker_account_id"],
        ["broker_account_id"],
    )
    op.create_foreign_key(
        "fk_capital_authorizations_snapshot_account",
        "kairo_capital_authorizations",
        "broker_cash_snapshots",
        ["broker_snapshot_id", "broker_account_id"],
        ["snapshot_id", "broker_account_id"],
    )
    op.create_index(
        "ix_capital_authorizations_snapshot_account",
        "kairo_capital_authorizations",
        ["broker_snapshot_id", "broker_account_id"],
    )

    op.alter_column("trust_evaluations", "score", nullable=True)
    op.add_column(
        "trust_evaluations",
        sa.Column(
            "eligible_for_promotion",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "trust_evaluations",
        sa.Column(
            "evidence_trade_count", sa.Integer(), nullable=False, server_default="0"
        ),
    )
    op.add_column(
        "trust_evaluations",
        sa.Column(
            "disqualifiers",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "trust_evaluations",
        sa.Column(
            "factor_breakdown",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.execute(
        "UPDATE trust_evaluations SET evidence_trade_count = 1 WHERE score IS NOT NULL"
    )
    for name in (
        "eligible_for_promotion",
        "evidence_trade_count",
        "disqualifiers",
        "factor_breakdown",
    ):
        op.alter_column("trust_evaluations", name, server_default=None)
    op.create_check_constraint(
        op.f("ck_trust_evaluations_evidence_score_semantics"),
        "trust_evaluations",
        "(evidence_trade_count = 0 AND score IS NULL "
        "AND eligible_for_promotion = false) OR "
        "(evidence_trade_count > 0 AND score IS NOT NULL)",
    )

    op.create_foreign_key(
        "fk_current_positions_cell_id_capital_cells",
        "current_positions",
        "capital_cells",
        ["cell_id"],
        ["cell_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_current_positions_cell_id_capital_cells",
        "current_positions",
        type_="foreignkey",
    )

    op.drop_constraint(
        op.f("ck_trust_evaluations_evidence_score_semantics"),
        "trust_evaluations",
        type_="check",
    )
    op.execute("UPDATE trust_evaluations SET score = 0 WHERE score IS NULL")
    for name in (
        "factor_breakdown",
        "disqualifiers",
        "evidence_trade_count",
        "eligible_for_promotion",
    ):
        op.drop_column("trust_evaluations", name)
    op.alter_column("trust_evaluations", "score", nullable=False)

    op.drop_index(
        "ix_capital_authorizations_snapshot_account",
        table_name="kairo_capital_authorizations",
    )
    op.drop_constraint(
        "fk_capital_authorizations_snapshot_account",
        "kairo_capital_authorizations",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_capital_authorizations_broker_account",
        "kairo_capital_authorizations",
        type_="foreignkey",
    )
    op.drop_column("kairo_capital_authorizations", "broker_account_id")
    op.drop_column("kairo_capital_authorizations", "broker_snapshot_id")

    op.drop_constraint(
        "uq_broker_cash_snapshot_account",
        "broker_cash_snapshots",
        type_="unique",
    )
    op.drop_constraint(
        op.f("ck_broker_cash_snapshots_unsettled_nonnegative"),
        "broker_cash_snapshots",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_broker_cash_snapshots_broker_cash_nonnegative"),
        "broker_cash_snapshots",
        type_="check",
    )
    op.drop_column("broker_cash_snapshots", "unsettled_cash")
    op.drop_column("broker_cash_snapshots", "broker_cash")

    for name in (
        "ck_order_intents_canonical_order_prices",
        "ck_order_intents_positive_quantity",
        "ck_order_intents_positive_notional",
        "ck_order_intents_valid_order_purpose",
        "ck_order_intents_single_sizing_mode",
    ):
        op.drop_constraint(op.f(name), "order_intents", type_="check")
    op.add_column("order_intents", sa.Column("quantity", MONEY))
    op.execute(
        "UPDATE order_intents SET quantity = COALESCE(target_quantity, 1)"
    )
    op.alter_column("order_intents", "quantity", nullable=False)
    op.drop_column("order_intents", "stop_price")
    op.drop_column("order_intents", "target_quantity")
    op.drop_column("order_intents", "target_notional_usd")
    op.drop_column("order_intents", "order_purpose")
    op.create_check_constraint(
        op.f("ck_order_intents_positive_quantity"),
        "order_intents",
        "quantity > 0",
    )

    for name in (
        "extended_hours_supported",
        "options_supported",
        "notional_orders_supported",
    ):
        op.drop_column("broker_instrument_capabilities", name)

    op.drop_constraint(
        op.f("ck_instruments_complete_option_identity"), "instruments", type_="check"
    )
    op.drop_constraint(
        "uq_instruments_contract_symbol", "instruments", type_="unique"
    )
    for name in (
        "listing_type",
        "contract_multiplier",
        "option_right",
        "strike_price",
        "expiration_date",
        "contract_symbol",
        "underlying_symbol",
    ):
        op.drop_column("instruments", name)
