from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

from app.db.base import Base
import app.db.models  # noqa: F401 -- registers every mapped table


EXPECTED_TABLES = {
    "broker_accounts",
    "instruments",
    "broker_instrument_capabilities",
    "strategy_registry",
    "trust_policies",
    "risk_policies",
    "cell_events",
    "market_snapshots",
    "siphon_events",
    "order_intents",
    "risk_decisions",
    "kairo_orders",
    "order_observations",
    "fills",
    "broker_cash_snapshots",
    "kairo_capital_authorizations",
    "trust_evaluations",
    "capital_cells",
    "ownership_treasury_holdings",
    "current_positions",
    "risk_sessions",
    "risk_state_events",
    "risk_governor_state",
    "risk_instrument_marks",
    "cell_treasury_configs",
    "fill_realized_pnl",
    "siphon_profit_attributions",
    "siphon_allocations",
    "treasury_executions",
    "treasury_cash_consumptions",
    "treasury_regime_observations",
    "synthetic_evidence_manifests",
    "cell_replication_proposals",
    "replication_proposal_events",
    "replication_proposal_reservations",
    "replication_reservation_events",
    "replication_authorizations",
    "replication_cash_consumptions",
    "cell_genesis_events",
    "intelligence_raw_artifacts",
    "intelligence_evidence_ledger",
    "intelligence_entity_links",
}


def test_frozen_schema_tables_are_registered() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_order_intent_lineage_and_idempotency_are_declared() -> None:
    table = Base.metadata.tables["order_intents"]
    foreign_keys = {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }
    assert "fk_order_intents_strategy_version" in foreign_keys
    assert any(
        isinstance(constraint, UniqueConstraint)
        and {column.name for column in constraint.columns} == {"client_order_key"}
        for constraint in table.constraints
    )


def test_economic_amount_checks_are_declared_in_models() -> None:
    expected = {
        "siphon_events": {"ck_siphon_events_ck_siphon_events_positive_amount"},
        "order_intents": {"ck_order_intents_positive_quantity"},
        "fills": {
            "ck_fills_ck_fills_positive_quantity",
            "ck_fills_ck_fills_positive_price",
        },
        "capital_cells": {"ck_capital_cells_ck_capital_cells_seed_nonnegative"},
    }
    for table_name, constraint_names in expected.items():
        actual = {
            constraint.name
            for constraint in Base.metadata.tables[table_name].constraints
            if isinstance(constraint, CheckConstraint)
        }
        assert constraint_names <= actual
