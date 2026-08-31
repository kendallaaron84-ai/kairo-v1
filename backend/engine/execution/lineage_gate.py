from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from sqlalchemy.orm import Session

from app.db.models.broker import BrokerAccount
from app.db.models.configuration import Instrument
from app.db.models.ledger import (
    KairoOrder,
    MarketSnapshot,
    OrderIntent,
    RiskDecision,
)
from engine.execution.models import ExecutionQuote, LiquidityFidelityTier, PaperEngineConfig


class ExecutionAuthorizationError(RuntimeError):
    pass


@dataclass(frozen=True)
class AuthorizedExecution:
    order: KairoOrder
    intent: OrderIntent
    decision: RiskDecision
    broker: BrokerAccount
    instrument: Instrument
    snapshot: MarketSnapshot
    contract_multiplier: Decimal


class ExecutionLineageGate:
    def __init__(self, session: Session, config: PaperEngineConfig) -> None:
        self.session = session
        self.config = config

    def authorize(
        self, kairo_order_id: UUID, quote: ExecutionQuote
    ) -> AuthorizedExecution:
        order = self.session.get(KairoOrder, kairo_order_id)
        if order is None:
            raise ExecutionAuthorizationError("Kairo order does not exist")
        intent = self.session.get(OrderIntent, order.intent_id)
        if intent is None:
            raise ExecutionAuthorizationError("Kairo order intent lineage is missing")
        if order.risk_decision_id is None:
            raise ExecutionAuthorizationError("explicit risk decision lineage is missing")
        decision = self.session.get(RiskDecision, order.risk_decision_id)
        if decision is None or decision.intent_id != intent.intent_id:
            raise ExecutionAuthorizationError("risk decision does not belong to the order intent")
        if decision.verdict != "AUTHORIZED":
            raise ExecutionAuthorizationError("risk decision is not AUTHORIZED")

        broker = self.session.get(BrokerAccount, self.config.broker_account_id)
        if broker is None or broker.retired_at is not None or broker.status != "ACTIVE":
            raise ExecutionAuthorizationError("canonical paper broker account is not active")
        if broker.environment != self.config.environment or broker.environment != "PAPER":
            raise ExecutionAuthorizationError("broker account is not a PAPER environment")
        if order.broker_account_id != broker.broker_account_id:
            raise ExecutionAuthorizationError(
                "order broker UUID conflicts with engine configuration"
            )

        instrument = self.session.get(Instrument, intent.instrument_id)
        if instrument is None or instrument.retired_at is not None:
            raise ExecutionAuthorizationError("canonical instrument is missing or retired")
        if quote.instrument_id != instrument.instrument_id:
            raise ExecutionAuthorizationError("execution evidence instrument lineage mismatch")
        snapshot = self.session.get(MarketSnapshot, quote.snapshot_id)
        if snapshot is None or snapshot.instrument_id != instrument.instrument_id:
            raise ExecutionAuthorizationError("market snapshot lineage mismatch")
        if snapshot.captured_at != quote.captured_at:
            raise ExecutionAuthorizationError("market snapshot timestamp lineage mismatch")
        self._validate_quote_against_snapshot(quote, snapshot)

        multiplier = (
            instrument.contract_multiplier
            if instrument.asset_class == "OPTION"
            else Decimal("1")
        )
        if multiplier is None or multiplier <= 0:
            raise ExecutionAuthorizationError("canonical contract multiplier is unavailable")
        return AuthorizedExecution(
            order, intent, decision, broker, instrument, snapshot, multiplier
        )

    @staticmethod
    def _validate_quote_against_snapshot(
        quote: ExecutionQuote, snapshot: MarketSnapshot
    ) -> None:
        payload = snapshot.payload or {}
        if quote.fidelity_tier is LiquidityFidelityTier.TIER_1_QUOTE_DEPTH:
            expected = {
                "bid": snapshot.bid,
                "ask": snapshot.ask,
                "bid_size": payload.get("bid_size"),
                "ask_size": payload.get("ask_size"),
            }
        elif quote.fidelity_tier is LiquidityFidelityTier.TIER_2_TRADE_HISTORY:
            expected = {
                "trade_price": payload.get("trade_price", snapshot.last),
                "trade_size": payload.get("trade_size"),
            }
        else:
            expected = {
                "bar_open": payload.get("bar_open"),
                "bar_high": payload.get("bar_high"),
                "bar_low": payload.get("bar_low"),
                "bar_close": payload.get("bar_close"),
                "bar_volume": payload.get("bar_volume"),
            }
        mismatches = []
        for field, source_value in expected.items():
            quote_value = getattr(quote, field)
            if quote_value is None and source_value is None:
                continue
            if source_value is None or quote_value != Decimal(str(source_value)):
                mismatches.append(field)
        if mismatches:
            raise ExecutionAuthorizationError(
                "execution quote conflicts with persisted market evidence: "
                + ", ".join(mismatches)
            )
