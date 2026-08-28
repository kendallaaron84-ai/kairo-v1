"""Initial Kairo ledger, configuration, and projection schema.

Revision ID: 0001
Revises: None
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UUID = postgresql.UUID(as_uuid=True)
MONEY = sa.Numeric(28, 10)
TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "broker_accounts",
        sa.Column("broker_account_id", UUID, primary_key=True),
        sa.Column("account_key", sa.String(100), nullable=False),
        sa.Column("broker_name", sa.String(100), nullable=False),
        sa.Column("environment", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("effective_from", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("retired_at", TZ),
        sa.UniqueConstraint("account_key", name="uq_broker_accounts_account_key"),
    )
    op.create_table(
        "instruments",
        sa.Column("instrument_id", UUID, primary_key=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("asset_class", sa.String(32), nullable=False),
        sa.Column("currency", sa.String(8), nullable=False),
        sa.Column("exchange", sa.String(32)),
        sa.Column("effective_from", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("retired_at", TZ),
        sa.UniqueConstraint("symbol", name="uq_instruments_symbol"),
    )
    op.create_table(
        "strategy_registry",
        sa.Column("strategy_id", sa.String(100), primary_key=True),
        sa.Column("version_tag", sa.String(50), primary_key=True),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("configuration", postgresql.JSONB, nullable=False),
        sa.Column("registered_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("retired_at", TZ),
    )
    op.create_table(
        "trust_policies",
        sa.Column("policy_id", UUID, primary_key=True),
        sa.Column("version_tag", sa.String(50), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("policy_document", postgresql.JSONB, nullable=False),
        sa.Column("effective_from", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("retired_at", TZ),
    )
    op.create_table(
        "broker_instrument_capabilities",
        sa.Column("capability_id", UUID, primary_key=True),
        sa.Column("broker_account_id", UUID, sa.ForeignKey("broker_accounts.broker_account_id"), nullable=False),
        sa.Column("instrument_id", UUID, sa.ForeignKey("instruments.instrument_id"), nullable=False),
        sa.Column("can_trade", sa.Boolean, nullable=False),
        sa.Column("can_fractional", sa.Boolean, nullable=False),
        sa.Column("can_short", sa.Boolean, nullable=False),
        sa.Column("minimum_quantity", MONEY),
        sa.Column("effective_from", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("retired_at", TZ),
        sa.UniqueConstraint("broker_account_id", "instrument_id", "effective_from", name="uq_broker_capability_version"),
    )

    op.create_table(
        "cell_events",
        sa.Column("event_id", UUID, primary_key=True),
        sa.Column("cell_id", UUID, nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("occurred_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("payload", postgresql.JSONB, nullable=False),
    )
    op.create_index("ix_cell_events_cell_occurred", "cell_events", ["cell_id", "occurred_at"])
    op.create_table(
        "market_snapshots",
        sa.Column("snapshot_id", UUID, primary_key=True),
        sa.Column("instrument_id", UUID, sa.ForeignKey("instruments.instrument_id"), nullable=False),
        sa.Column("captured_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("bid", MONEY),
        sa.Column("ask", MONEY),
        sa.Column("last", MONEY),
        sa.Column("payload", postgresql.JSONB, nullable=False),
    )
    op.create_index("ix_market_snapshots_captured_at", "market_snapshots", ["captured_at"])
    op.create_table(
        "siphon_events",
        sa.Column("siphon_id", UUID, primary_key=True),
        sa.Column("cell_id", UUID, nullable=False),
        sa.Column("treasury_code", sa.String(50), nullable=False),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("occurred_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("reason_code", sa.String(100), nullable=False),
        sa.CheckConstraint("amount > 0", name="ck_siphon_events_positive_amount"),
    )
    op.create_index("ix_siphon_events_cell_occurred", "siphon_events", ["cell_id", "occurred_at"])
    op.create_table(
        "order_intents",
        sa.Column("intent_id", UUID, primary_key=True),
        sa.Column("cell_id", UUID, nullable=False),
        sa.Column("strategy_id", sa.String(100), nullable=False),
        sa.Column("strategy_version", sa.String(50), nullable=False),
        sa.Column("instrument_id", UUID, sa.ForeignKey("instruments.instrument_id"), nullable=False),
        sa.Column("siphon_id", UUID, sa.ForeignKey("siphon_events.siphon_id")),
        sa.Column("client_order_key", sa.String(200), nullable=False),
        sa.Column("side", sa.String(16), nullable=False),
        sa.Column("quantity", MONEY, nullable=False),
        sa.Column("order_type", sa.String(16), nullable=False),
        sa.Column("limit_price", MONEY),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["strategy_id", "strategy_version"],
            ["strategy_registry.strategy_id", "strategy_registry.version_tag"],
            name="fk_order_intents_strategy_version",
        ),
        sa.UniqueConstraint("client_order_key", name="uq_order_intents_client_order_key"),
        sa.CheckConstraint("quantity > 0", name="ck_order_intents_positive_quantity"),
    )
    op.create_index("ix_order_intents_strategy_version", "order_intents", ["strategy_id", "strategy_version"])
    op.create_index("ix_order_intents_cell_created", "order_intents", ["cell_id", "created_at"])
    op.create_table(
        "risk_decisions",
        sa.Column("decision_id", UUID, primary_key=True),
        sa.Column("intent_id", UUID, sa.ForeignKey("order_intents.intent_id"), nullable=False),
        sa.Column("verdict", sa.String(32), nullable=False),
        sa.Column("reason_code", sa.String(100), nullable=False),
        sa.Column("decided_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("details", postgresql.JSONB, nullable=False),
    )
    op.create_index("ix_risk_decisions_intent_id", "risk_decisions", ["intent_id"])
    op.create_table(
        "kairo_orders",
        sa.Column("kairo_order_id", UUID, primary_key=True),
        sa.Column("intent_id", UUID, sa.ForeignKey("order_intents.intent_id"), nullable=False),
        sa.Column("broker_account_id", UUID, sa.ForeignKey("broker_accounts.broker_account_id"), nullable=False),
        sa.Column("broker_order_id", sa.String(200)),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("submitted_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("intent_id", name="uq_kairo_orders_intent_id"),
    )
    op.create_index("ix_kairo_orders_intent_id", "kairo_orders", ["intent_id"])
    op.create_table(
        "order_observations",
        sa.Column("observation_id", UUID, primary_key=True),
        sa.Column("kairo_order_id", UUID, sa.ForeignKey("kairo_orders.kairo_order_id"), nullable=False),
        sa.Column("broker_account_id", UUID, sa.ForeignKey("broker_accounts.broker_account_id"), nullable=False),
        sa.Column("broker_observation_key", sa.String(200), nullable=False),
        sa.Column("broker_order_id", sa.String(200), nullable=False),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("observed_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.UniqueConstraint("broker_account_id", "broker_observation_key", name="uq_order_observation_broker_message"),
    )
    op.create_index("ix_order_observations_kairo_order_observed", "order_observations", ["kairo_order_id", "observed_at"])
    op.create_table(
        "fills",
        sa.Column("fill_id", UUID, primary_key=True),
        sa.Column("kairo_order_id", UUID, sa.ForeignKey("kairo_orders.kairo_order_id"), nullable=False),
        sa.Column("broker_account_id", UUID, sa.ForeignKey("broker_accounts.broker_account_id"), nullable=False),
        sa.Column("broker_fill_id", sa.String(200), nullable=False),
        sa.Column("instrument_id", UUID, sa.ForeignKey("instruments.instrument_id"), nullable=False),
        sa.Column("side", sa.String(16), nullable=False),
        sa.Column("quantity", MONEY, nullable=False),
        sa.Column("price", MONEY, nullable=False),
        sa.Column("filled_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("broker_account_id", "broker_fill_id", name="uq_fills_broker_fill"),
        sa.CheckConstraint("quantity > 0", name="ck_fills_positive_quantity"),
        sa.CheckConstraint("price > 0", name="ck_fills_positive_price"),
    )
    op.create_index("ix_fills_kairo_order_filled", "fills", ["kairo_order_id", "filled_at"])
    op.create_index("ix_fills_broker_account_filled", "fills", ["broker_account_id", "filled_at"])
    op.create_table(
        "broker_cash_snapshots",
        sa.Column("snapshot_id", UUID, primary_key=True),
        sa.Column("broker_account_id", UUID, sa.ForeignKey("broker_accounts.broker_account_id"), nullable=False),
        sa.Column("settled_cash", MONEY, nullable=False),
        sa.Column("buying_power", MONEY, nullable=False),
        sa.Column("currency", sa.String(8), nullable=False),
        sa.Column("captured_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("broker_account_id", "captured_at", name="uq_broker_cash_snapshot_time"),
        sa.CheckConstraint("settled_cash >= 0", name="ck_broker_cash_snapshots_settled_nonnegative"),
        sa.CheckConstraint("buying_power >= 0", name="ck_broker_cash_snapshots_buying_power_nonnegative"),
    )
    op.create_index("ix_broker_cash_account_captured", "broker_cash_snapshots", ["broker_account_id", "captured_at"])
    op.create_table(
        "kairo_capital_authorizations",
        sa.Column("authorization_id", UUID, primary_key=True),
        sa.Column("cell_id", UUID, nullable=False),
        sa.Column("settled_cash", MONEY, nullable=False),
        sa.Column("safety_reserve", MONEY, nullable=False),
        sa.Column("ownership_treasury_reserved", MONEY, nullable=False),
        sa.Column("replication_reserve", MONEY, nullable=False),
        sa.Column("committed_obligations", MONEY, nullable=False),
        sa.Column("authorized_trading_cash", MONEY, nullable=False),
        sa.Column("computed_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "settled_cash >= 0 AND safety_reserve >= 0 AND ownership_treasury_reserved >= 0 "
            "AND replication_reserve >= 0 AND committed_obligations >= 0 "
            "AND authorized_trading_cash >= 0",
            name="ck_kairo_capital_authorizations_nonnegative",
        ),
    )
    op.create_index("ix_capital_authorizations_cell_computed", "kairo_capital_authorizations", ["cell_id", "computed_at"])
    op.create_table(
        "trust_evaluations",
        sa.Column("evaluation_id", UUID, primary_key=True),
        sa.Column("cell_id", UUID, nullable=False),
        sa.Column("policy_id", UUID, nullable=False),
        sa.Column("policy_version", sa.String(50), nullable=False),
        sa.Column("score", sa.Numeric(10, 4), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("evaluated_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("details", postgresql.JSONB, nullable=False),
        sa.ForeignKeyConstraint(
            ["policy_id", "policy_version"],
            ["trust_policies.policy_id", "trust_policies.version_tag"],
            name="fk_trust_evaluations_policy_version",
        ),
    )
    op.create_index("ix_trust_evaluations_cell_evaluated", "trust_evaluations", ["cell_id", "evaluated_at"])

    op.create_table(
        "capital_cells",
        sa.Column("cell_id", UUID, primary_key=True),
        sa.Column("cell_code", sa.String(50), nullable=False),
        sa.Column("seed_capital", MONEY, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("strategy_id", sa.String(100), nullable=False),
        sa.Column("strategy_version", sa.String(50), nullable=False),
        sa.Column("target_treasury_code", sa.String(50), nullable=False),
        sa.Column("updated_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["strategy_id", "strategy_version"],
            ["strategy_registry.strategy_id", "strategy_registry.version_tag"],
            name="fk_capital_cells_strategy_version",
        ),
        sa.UniqueConstraint("cell_code", name="uq_capital_cells_cell_code"),
        sa.CheckConstraint("seed_capital >= 0", name="ck_capital_cells_seed_nonnegative"),
    )
    op.create_table(
        "ownership_treasury_holdings",
        sa.Column("holding_id", UUID, primary_key=True),
        sa.Column("treasury_code", sa.String(50), nullable=False),
        sa.Column("instrument_id", UUID, sa.ForeignKey("instruments.instrument_id"), nullable=False),
        sa.Column("dollars_contributed", MONEY, nullable=False),
        sa.Column("fractional_shares", MONEY, nullable=False),
        sa.Column("updated_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("treasury_code", "instrument_id", name="uq_treasury_holding_instrument"),
        sa.CheckConstraint("dollars_contributed >= 0", name="ck_treasury_holdings_dollars_nonnegative"),
        sa.CheckConstraint("fractional_shares >= 0", name="ck_treasury_holdings_shares_nonnegative"),
    )
    op.create_table(
        "current_positions",
        sa.Column("position_id", UUID, primary_key=True),
        sa.Column("cell_id", UUID, nullable=False),
        sa.Column("broker_account_id", UUID, sa.ForeignKey("broker_accounts.broker_account_id"), nullable=False),
        sa.Column("instrument_id", UUID, sa.ForeignKey("instruments.instrument_id"), nullable=False),
        sa.Column("quantity", MONEY, nullable=False),
        sa.Column("average_price", MONEY, nullable=False),
        sa.Column("updated_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("cell_id", "broker_account_id", "instrument_id", name="uq_current_position_identity"),
        sa.CheckConstraint("average_price >= 0", name="ck_current_positions_price_nonnegative"),
    )

    immutable_tables = [
        "cell_events", "market_snapshots", "siphon_events", "order_intents",
        "risk_decisions", "kairo_orders", "order_observations", "fills",
        "broker_cash_snapshots", "kairo_capital_authorizations", "trust_evaluations",
    ]
    mutable_tables = [
        "broker_accounts", "instruments", "broker_instrument_capabilities",
        "strategy_registry", "trust_policies", "capital_cells",
        "ownership_treasury_holdings", "current_positions",
    ]
    op.execute("GRANT USAGE ON SCHEMA public TO kairo_runtime")
    op.execute(f"GRANT SELECT, INSERT ON {', '.join(immutable_tables)} TO kairo_runtime")
    op.execute(f"REVOKE UPDATE, DELETE ON {', '.join(immutable_tables)} FROM kairo_runtime")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON {', '.join(mutable_tables)} TO kairo_runtime")
    op.execute(f"REVOKE DELETE ON {', '.join(mutable_tables)} FROM kairo_runtime")


def downgrade() -> None:
    for table in [
        "current_positions",
        "ownership_treasury_holdings",
        "capital_cells",
        "trust_evaluations",
        "kairo_capital_authorizations",
        "broker_cash_snapshots",
        "fills",
        "order_observations",
        "kairo_orders",
        "risk_decisions",
        "order_intents",
        "siphon_events",
        "market_snapshots",
        "cell_events",
        "broker_instrument_capabilities",
        "trust_policies",
        "strategy_registry",
        "instruments",
        "broker_accounts",
    ]:
        op.drop_table(table)
