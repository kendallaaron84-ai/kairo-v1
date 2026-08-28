from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CellEvent(Base):
    __tablename__ = "cell_events"
    __table_args__ = (Index("ix_cell_events_cell_occurred", "cell_id", "occurred_at"),)
    event_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    cell_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class MarketSnapshot(Base):
    __tablename__ = "market_snapshots"
    snapshot_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    instrument_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("instruments.instrument_id"), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)
    bid: Mapped[Decimal | None] = mapped_column(Numeric(28, 10))
    ask: Mapped[Decimal | None] = mapped_column(Numeric(28, 10))
    last: Mapped[Decimal | None] = mapped_column(Numeric(28, 10))
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class SiphonEvent(Base):
    __tablename__ = "siphon_events"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_siphon_events_positive_amount"),
        Index("ix_siphon_events_cell_occurred", "cell_id", "occurred_at"),
    )
    siphon_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    cell_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    treasury_code: Mapped[str] = mapped_column(String(50), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    reason_code: Mapped[str] = mapped_column(String(100), nullable=False)


class OrderIntent(Base):
    __tablename__ = "order_intents"
    __table_args__ = (
        ForeignKeyConstraint(
            ["strategy_id", "strategy_version"],
            ["strategy_registry.strategy_id", "strategy_registry.version_tag"],
            name="fk_order_intents_strategy_version",
        ),
        CheckConstraint(
            "(target_notional_usd IS NOT NULL AND target_quantity IS NULL) OR "
            "(target_notional_usd IS NULL AND target_quantity IS NOT NULL)",
            name="single_sizing_mode",
        ),
        CheckConstraint(
            "order_purpose IN ('ENTRY', 'TAKE_PROFIT', 'STOP_LOSS', "
            "'EMERGENCY_EXIT', 'TREASURY_PURCHASE')",
            name="valid_order_purpose",
        ),
        CheckConstraint(
            "target_notional_usd IS NULL OR target_notional_usd > 0",
            name="positive_notional",
        ),
        CheckConstraint(
            "target_quantity IS NULL OR target_quantity > 0",
            name="positive_quantity",
        ),
        CheckConstraint(
            "(order_type = 'MARKET' AND limit_price IS NULL AND stop_price IS NULL) OR "
            "(order_type = 'LIMIT' AND limit_price IS NOT NULL "
            "AND limit_price > 0 AND stop_price IS NULL) OR "
            "(order_type = 'STOP' AND limit_price IS NULL "
            "AND stop_price IS NOT NULL AND stop_price > 0)",
            name="canonical_order_prices",
        ),
        Index("ix_order_intents_strategy_version", "strategy_id", "strategy_version"),
        Index("ix_order_intents_cell_created", "cell_id", "created_at"),
    )
    intent_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    cell_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    strategy_id: Mapped[str] = mapped_column(String(100), nullable=False)
    strategy_version: Mapped[str] = mapped_column(String(50), nullable=False)
    instrument_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("instruments.instrument_id"), nullable=False)
    siphon_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), ForeignKey("siphon_events.siphon_id"))
    client_order_key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    order_purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    target_notional_usd: Mapped[Decimal | None] = mapped_column(Numeric(28, 10))
    target_quantity: Mapped[Decimal | None] = mapped_column(Numeric(28, 10))
    order_type: Mapped[str] = mapped_column(String(16), nullable=False)
    limit_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 10))
    stop_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 10))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class RiskDecision(Base):
    __tablename__ = "risk_decisions"
    __table_args__ = (
        Index("ix_risk_decisions_session_decided", "session_id", "decided_at"),
    )
    decision_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    intent_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("order_intents.intent_id"), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("risk_sessions.session_id"), nullable=False
    )
    verdict: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(100), nullable=False)
    operational_state: Mapped[str] = mapped_column(String(32), nullable=False)
    intent_classification: Mapped[str] = mapped_column(String(32), nullable=False)
    session_net_pnl: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False)
    authorized_cash_usd: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False)
    requested_cash_usd: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False)
    projected_exposure_usd: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False)
    max_contractual_loss_usd: Mapped[Decimal | None] = mapped_column(Numeric(28, 10))
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class KairoOrder(Base):
    __tablename__ = "kairo_orders"
    __table_args__ = (
        UniqueConstraint("intent_id", name="uq_kairo_orders_intent_id"),
        Index("ix_kairo_orders_intent_id", "intent_id"),
    )
    kairo_order_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    intent_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("order_intents.intent_id"), nullable=False
    )
    broker_account_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("broker_accounts.broker_account_id"), nullable=False)
    broker_order_id: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class OrderObservation(Base):
    __tablename__ = "order_observations"
    __table_args__ = (
        UniqueConstraint("broker_account_id", "broker_observation_key", name="uq_order_observation_broker_message"),
        Index("ix_order_observations_kairo_order_observed", "kairo_order_id", "observed_at"),
    )
    observation_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    kairo_order_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("kairo_orders.kairo_order_id"), nullable=False)
    broker_account_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("broker_accounts.broker_account_id"), nullable=False)
    broker_observation_key: Mapped[str] = mapped_column(String(200), nullable=False)
    broker_order_id: Mapped[str] = mapped_column(String(200), nullable=False)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class Fill(Base):
    __tablename__ = "fills"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_fills_positive_quantity"),
        CheckConstraint("price > 0", name="ck_fills_positive_price"),
        UniqueConstraint("broker_account_id", "broker_fill_id", name="uq_fills_broker_fill"),
        Index("ix_fills_kairo_order_filled", "kairo_order_id", "filled_at"),
        Index("ix_fills_broker_account_filled", "broker_account_id", "filled_at"),
    )
    fill_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    kairo_order_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("kairo_orders.kairo_order_id"), nullable=False)
    broker_account_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("broker_accounts.broker_account_id"), nullable=False)
    broker_fill_id: Mapped[str] = mapped_column(String(200), nullable=False)
    instrument_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("instruments.instrument_id"), nullable=False)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False)
    filled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class BrokerCashSnapshot(Base):
    __tablename__ = "broker_cash_snapshots"
    __table_args__ = (
        CheckConstraint(
            "broker_cash >= 0", name="broker_cash_nonnegative"
        ),
        CheckConstraint(
            "settled_cash >= 0", name="ck_broker_cash_snapshots_settled_nonnegative"
        ),
        CheckConstraint(
            "unsettled_cash >= 0", name="unsettled_nonnegative"
        ),
        CheckConstraint(
            "buying_power >= 0", name="ck_broker_cash_snapshots_buying_power_nonnegative"
        ),
        UniqueConstraint("broker_account_id", "captured_at", name="uq_broker_cash_snapshot_time"),
        UniqueConstraint("snapshot_id", "broker_account_id", name="uq_broker_cash_snapshot_account"),
        Index("ix_broker_cash_account_captured", "broker_account_id", "captured_at"),
    )
    snapshot_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    broker_account_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("broker_accounts.broker_account_id"), nullable=False)
    broker_cash: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False)
    settled_cash: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False)
    unsettled_cash: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False)
    buying_power: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class KairoCapitalAuthorizationRecord(Base):
    __tablename__ = "kairo_capital_authorizations"
    __table_args__ = (
        CheckConstraint(
            "settled_cash >= 0 AND safety_reserve >= 0 "
            "AND ownership_treasury_reserved >= 0 AND replication_reserve >= 0 "
            "AND committed_obligations >= 0 AND authorized_trading_cash >= 0",
            name="ck_kairo_capital_authorizations_nonnegative",
        ),
        ForeignKeyConstraint(
            ["broker_snapshot_id", "broker_account_id"],
            ["broker_cash_snapshots.snapshot_id", "broker_cash_snapshots.broker_account_id"],
            name="fk_capital_authorizations_snapshot_account",
        ),
        Index("ix_capital_authorizations_cell_computed", "cell_id", "computed_at"),
        Index(
            "ix_capital_authorizations_snapshot_account",
            "broker_snapshot_id",
            "broker_account_id",
        ),
    )
    authorization_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    cell_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    broker_snapshot_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    broker_account_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey(
            "broker_accounts.broker_account_id",
            name="fk_capital_authorizations_broker_account",
        ),
        nullable=False,
    )
    settled_cash: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False)
    safety_reserve: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False)
    ownership_treasury_reserved: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False)
    replication_reserve: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False)
    committed_obligations: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False)
    authorized_trading_cash: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class TrustEvaluation(Base):
    __tablename__ = "trust_evaluations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["policy_id", "policy_version"],
            ["trust_policies.policy_id", "trust_policies.version_tag"],
            name="fk_trust_evaluations_policy_version",
        ),
        CheckConstraint(
            "(evidence_trade_count = 0 AND score IS NULL AND eligible_for_promotion = false) "
            "OR (evidence_trade_count > 0 AND score IS NOT NULL)",
            name="evidence_score_semantics",
        ),
        Index("ix_trust_evaluations_cell_evaluated", "cell_id", "evaluated_at"),
    )
    evaluation_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    cell_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    policy_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(50), nullable=False)
    score: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    eligible_for_promotion: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    evidence_trade_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    disqualifiers: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    factor_breakdown: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
