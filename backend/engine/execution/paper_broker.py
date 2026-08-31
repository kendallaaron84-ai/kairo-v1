from abc import ABC, abstractmethod
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.ledger import Fill, KairoOrder, OrderObservation
from engine.execution.cancellation import CancellationStatus, resolve_cancellation
from engine.execution.fill_model import adverse_slippage_usd, model_execution_price
from engine.execution.lineage_gate import AuthorizedExecution, ExecutionLineageGate
from engine.execution.liquidity import evaluate_liquidity
from engine.execution.models import (
    ExecutionQuote,
    PaperEngineConfig,
    PaperExecutionReceipt,
    SimulatedFillPayload,
)


class BaseBrokerAdapter(ABC):
    @abstractmethod
    async def submit_order(
        self, kairo_order_id: UUID, quote: ExecutionQuote
    ) -> PaperExecutionReceipt:
        raise NotImplementedError

    @abstractmethod
    async def request_cancel(self, kairo_order_id: UUID) -> dict[str, Any]:
        raise NotImplementedError


class PaperExecutionEngine(BaseBrokerAdapter):
    def __init__(self, session: Session, config: PaperEngineConfig) -> None:
        self.session = session
        self.config = config
        self.gate = ExecutionLineageGate(session, config)

    async def submit_order(
        self, kairo_order_id: UUID, quote: ExecutionQuote
    ) -> PaperExecutionReceipt:
        context = self.gate.authorize(kairo_order_id, quote)
        order = context.order
        broker_order_id = order.broker_order_id or (
            f"{self.config.broker_code}-{order.kairo_order_id}"
        )
        order.broker_order_id = broker_order_id
        if not self._observation_exists(f"{broker_order_id}:SUBMITTED"):
            order.status = "SUBMITTED"
            self._append_observation(
                context=context,
                key=f"{broker_order_id}:SUBMITTED",
                event_type="SUBMISSION",
                status="SUBMITTED",
                payload=self._provenance(
                    quote,
                    {
                        "gateway_ack_latency_ms": self.config.gateway_ack_latency_ms,
                    },
                ),
            )
            order.status = "ACCEPTED"
            self._append_observation(
                context=context,
                key=f"{broker_order_id}:ACCEPTED",
                event_type="ACKNOWLEDGEMENT",
                status="ACCEPTED",
                payload=self._provenance(
                    quote,
                    {"matching_latency_ms": self.config.matching_latency_ms},
                ),
            )
        return self._match(context, quote)

    async def process_quote(
        self, kairo_order_id: UUID, quote: ExecutionQuote
    ) -> PaperExecutionReceipt:
        context = self.gate.authorize(kairo_order_id, quote)
        if context.order.broker_order_id is None:
            return await self.submit_order(kairo_order_id, quote)
        return self._match(context, quote)

    async def request_cancel(self, kairo_order_id: UUID) -> dict[str, Any]:
        order = self._canonical_order(kairo_order_id)
        key = f"{order.broker_order_id}:CANCEL_REQUESTED"
        if not self._observation_exists(key):
            self._append_raw_observation(
                order=order,
                key=key,
                event_type="CANCELLATION",
                status=CancellationStatus.CANCEL_REQUESTED,
                payload=self._base_provenance(),
            )
        if order.status != "FILLED":
            order.status = CancellationStatus.CANCEL_REQUESTED
        self.session.flush()
        return {"kairo_order_id": str(kairo_order_id), "status": "CANCEL_REQUESTED"}

    async def resolve_cancel(self, kairo_order_id: UUID) -> dict[str, Any]:
        order = self._canonical_order(kairo_order_id)
        request_key = f"{order.broker_order_id}:CANCEL_REQUESTED"
        if not self._observation_exists(request_key):
            raise RuntimeError("cancellation must be requested before it can be resolved")
        status = resolve_cancellation(fully_filled=order.status == "FILLED")
        key = f"{order.broker_order_id}:{status.value}"
        if not self._observation_exists(key):
            self._append_raw_observation(
                order=order,
                key=key,
                event_type="CANCELLATION",
                status=status,
                payload=self._base_provenance(),
            )
        order.status = status
        self.session.flush()
        return {"kairo_order_id": str(kairo_order_id), "status": status.value}

    def _match(
        self, context: AuthorizedExecution, quote: ExecutionQuote
    ) -> PaperExecutionReceipt:
        order = context.order
        assert order.broker_order_id is not None
        match_key = f"{order.broker_order_id}:MATCH:{quote.snapshot_id}"
        target = self._target_quantity(order, context=context, quote=quote)
        if self._observation_exists(match_key):
            return self._receipt(
                context,
                target_quantity=target,
                observation_payload={"idempotent_replay": True},
            )

        cumulative = self._cumulative_quantity(order.kairo_order_id)
        remaining = max(Decimal("0"), target - cumulative)
        if remaining == 0:
            order.status = "FILLED"
            return self._receipt(
                context,
                target_quantity=target,
                observation_payload={"already_filled": True},
            )

        pricing = model_execution_price(
            side=context.intent.side,
            order_type=context.intent.order_type,
            limit_price=context.intent.limit_price,
            quote=quote,
            slippage_rate=self.config.default_slippage_bps,
        )
        if not pricing.matched or pricing.effective_price is None:
            order.status = "PENDING_MATCH"
            payload = self._provenance(quote, {"reason": "LIMIT_NOT_MARKETABLE"})
            self._append_observation(
                context=context,
                key=match_key,
                event_type="MATCH",
                status="PENDING_MATCH",
                payload=payload,
            )
            return self._receipt(
                context, target_quantity=target, observation_payload=payload
            )

        liquidity = evaluate_liquidity(
            side=context.intent.side,
            remaining_quantity=remaining,
            quote=quote,
            config=self.config,
        )
        if liquidity.fill_quantity <= 0:
            order.status = (
                "REJECTED" if self.config.reject_illiquid_quotes else "PENDING_MATCH"
            )
            payload = self._provenance(
                quote, {"reason": "NO_SUPPORTED_LIQUIDITY"}
            )
            self._append_observation(
                context=context,
                key=match_key,
                event_type="MATCH",
                status=order.status,
                payload=payload,
            )
            return self._receipt(
                context, target_quantity=target, observation_payload=payload
            )

        slippage = adverse_slippage_usd(
            reference_price=pricing.reference_price,
            execution_price=pricing.effective_price,
            quantity=liquidity.fill_quantity,
            contract_multiplier=context.contract_multiplier,
        )
        fill_id = uuid4()
        metadata = {
            **liquidity.metadata,
            "source": "PAPER_ENGINE",
            "synthetic": True,
            "execution_guaranteed": False,
            "liquidity_fidelity_tier": quote.fidelity_tier.value,
        }
        fill = Fill(
            fill_id=fill_id,
            kairo_order_id=order.kairo_order_id,
            broker_account_id=context.broker.broker_account_id,
            broker_fill_id=f"{order.broker_order_id}:{quote.snapshot_id}:FILL",
            instrument_id=context.instrument.instrument_id,
            side=context.intent.side,
            quantity=liquidity.fill_quantity,
            price=pricing.effective_price,
            reference_price=pricing.reference_price,
            contract_multiplier=context.contract_multiplier,
            slippage_usd=slippage,
            commission_fee_usd=Decimal("0"),
            is_simulated=True,
            liquidity_fidelity_tier=quote.fidelity_tier.value,
            simulation_model=self.config.simulation_model,
            simulation_policy_version=liquidity.policy_version,
            source_snapshot_id=quote.snapshot_id,
            simulation_metadata=metadata,
            filled_at=datetime.now(UTC),
        )
        self.session.add(fill)
        new_cumulative = cumulative + liquidity.fill_quantity
        order.status = "FILLED" if new_cumulative >= target else "PARTIALLY_FILLED"
        payload = self._provenance(
            quote,
            {
                **metadata,
                "simulation_policy_version": liquidity.policy_version,
                "target_quantity": str(target),
                "cumulative_filled_qty": str(new_cumulative),
                "remaining_qty": str(max(Decimal("0"), target - new_cumulative)),
            },
        )
        self._append_observation(
            context=context,
            key=match_key,
            event_type="FILL",
            status=order.status,
            payload=payload,
        )
        self.session.flush()
        return self._receipt(
            context, target_quantity=target, observation_payload=payload
        )

    def _target_quantity(
        self,
        order: KairoOrder,
        *,
        context: AuthorizedExecution | None = None,
        quote: ExecutionQuote | None = None,
    ) -> Decimal:
        from app.db.models.ledger import OrderIntent

        intent = (
            context.intent
            if context is not None
            else self.session.get(OrderIntent, order.intent_id)
        )
        if intent is None:
            raise RuntimeError("order intent is missing")
        if intent.target_quantity is not None:
            return intent.target_quantity
        if context is None or quote is None or intent.target_notional_usd is None:
            raise RuntimeError("notional order quantity requires current execution evidence")
        pricing = model_execution_price(
            side=intent.side,
            order_type=intent.order_type,
            limit_price=intent.limit_price,
            quote=quote,
            slippage_rate=self.config.default_slippage_bps,
        )
        if pricing.effective_price is None:
            return Decimal("0")
        return intent.target_notional_usd / (
            pricing.effective_price * context.contract_multiplier
        )

    def _receipt(
        self,
        context: AuthorizedExecution,
        *,
        target_quantity: Decimal,
        observation_payload: dict[str, Any],
    ) -> PaperExecutionReceipt:
        order = context.order
        cumulative = self._cumulative_quantity(order.kairo_order_id)
        records = list(
            self.session.scalars(
                select(Fill)
                .where(Fill.kairo_order_id == order.kairo_order_id)
                .order_by(Fill.filled_at, Fill.fill_id)
            )
        )
        payloads = [self._fill_payload(item) for item in records]
        return PaperExecutionReceipt(
            kairo_order_id=order.kairo_order_id,
            broker_order_id=order.broker_order_id or "",
            status=order.status,
            cumulative_filled_qty=cumulative,
            remaining_qty=max(Decimal("0"), target_quantity - cumulative),
            fill_records=payloads,
            observation_payload=observation_payload,
        )

    @staticmethod
    def _fill_payload(fill: Fill) -> SimulatedFillPayload:
        return SimulatedFillPayload(
            fill_id=fill.fill_id,
            kairo_order_id=fill.kairo_order_id,
            broker_account_id=fill.broker_account_id,
            instrument_id=fill.instrument_id,
            side=fill.side,
            fill_price=fill.price,
            reference_price=fill.reference_price,
            quantity=fill.quantity,
            contract_multiplier=fill.contract_multiplier,
            slippage_usd=fill.slippage_usd,
            commission_fee_usd=fill.commission_fee_usd,
            liquidity_fidelity_tier=fill.liquidity_fidelity_tier,
            simulation_model=fill.simulation_model,
            simulation_policy_version=fill.simulation_policy_version,
            source_snapshot_id=fill.source_snapshot_id,
            simulation_metadata=fill.simulation_metadata,
            timestamp=fill.filled_at,
        )

    def _append_observation(
        self,
        *,
        context: AuthorizedExecution,
        key: str,
        event_type: str,
        status: str,
        payload: dict[str, Any],
    ) -> None:
        self._append_raw_observation(
            order=context.order,
            key=key,
            event_type=event_type,
            status=status,
            payload=payload,
        )

    def _append_raw_observation(
        self,
        *,
        order: KairoOrder,
        key: str,
        event_type: str,
        status: str,
        payload: dict[str, Any],
    ) -> None:
        self.session.add(
            OrderObservation(
                observation_id=uuid4(),
                kairo_order_id=order.kairo_order_id,
                broker_account_id=self.config.broker_account_id,
                broker_observation_key=key,
                broker_order_id=order.broker_order_id or "",
                event_type=event_type,
                status=str(status),
                observed_at=datetime.now(UTC),
                payload=payload,
            )
        )
        self.session.flush()

    def _canonical_order(self, kairo_order_id: UUID) -> KairoOrder:
        order = self.session.get(KairoOrder, kairo_order_id)
        if order is None or order.broker_account_id != self.config.broker_account_id:
            raise RuntimeError("canonical paper order is unavailable")
        if order.broker_order_id is None:
            raise RuntimeError("order has not been submitted")
        return order

    def _observation_exists(self, key: str) -> bool:
        return self.session.scalar(
            select(OrderObservation.observation_id).where(
                OrderObservation.broker_account_id == self.config.broker_account_id,
                OrderObservation.broker_observation_key == key,
            )
        ) is not None

    def _cumulative_quantity(self, kairo_order_id: UUID) -> Decimal:
        return self.session.scalar(
            select(func.coalesce(func.sum(Fill.quantity), 0)).where(
                Fill.kairo_order_id == kairo_order_id
            )
        )

    def _base_provenance(self) -> dict[str, Any]:
        return {
            "source": "PAPER_ENGINE",
            "simulation_model": self.config.simulation_model,
            "synthetic": True,
        }

    def _policy_version(self, quote: ExecutionQuote) -> str:
        policies = {
            "TIER_1_QUOTE_DEPTH": self.config.quote_depth_policy_version,
            "TIER_2_TRADE_HISTORY": self.config.trade_history_policy_version,
            "TIER_3_BAR_ONLY": self.config.bar_only_policy_version,
        }
        return policies[quote.fidelity_tier.value]

    def _provenance(
        self, quote: ExecutionQuote, additions: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            **self._base_provenance(),
            "liquidity_fidelity_tier": quote.fidelity_tier.value,
            "simulation_policy_version": self._policy_version(quote),
            "source_snapshot_id": str(quote.snapshot_id),
            **additions,
        }
