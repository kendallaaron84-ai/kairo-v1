from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from app.db.models.configuration import Instrument
from app.db.models.ledger import (
    CellEvent,
    Fill,
    KairoOrder,
    OrderIntent,
    RiskDecision,
)
from app.db.models.risk import RiskStateEvent
from app.domain.enums import OrderSide
from engine.trust.models import (
    ClosedTradeEvidence,
    EquityPoint,
    ExecutionEvidence,
    GovernorAuditEvidence,
    SafetyAuditEvidence,
    TrustEvidenceBundle,
)


class TrustEvidenceCollector:
    """Collects only canonical facts already persisted by Kairo."""

    def __init__(self, session: Session):
        self.session = session

    def collect(self, cell_id: UUID, window_size: int) -> TrustEvidenceBundle:
        closed_events = list(
            self.session.scalars(
                select(CellEvent)
                .where(
                    CellEvent.cell_id == cell_id,
                    CellEvent.event_type == "TRADE_CLOSED",
                )
                .order_by(CellEvent.occurred_at.desc(), CellEvent.event_id.desc())
                .limit(window_size)
            )
        )
        closed_events.reverse()
        trades = tuple(self._closed_trade(event) for event in closed_events)
        window_start = trades[0].closed_at if trades else None
        window_end = trades[-1].closed_at if trades else None

        equity_query = select(CellEvent).where(
            CellEvent.cell_id == cell_id,
            CellEvent.event_type == "EQUITY_POINT",
        )
        if window_start is not None:
            equity_query = equity_query.where(CellEvent.occurred_at >= window_start)
        if window_end is not None:
            equity_query = equity_query.where(CellEvent.occurred_at <= window_end)
        equity_events = self.session.scalars(
            equity_query.order_by(CellEvent.occurred_at, CellEvent.event_id)
        )
        equity = tuple(
            EquityPoint(
                timestamp=event.occurred_at,
                equity=Decimal(str(event.payload["equity"])),
            )
            for event in equity_events
            if event.payload.get("equity") is not None
        )

        executions = self._execution_evidence(cell_id, window_start, window_end)
        safety = self._safety_evidence(cell_id)
        governor = self._governor_evidence(cell_id, window_start, window_end)
        return TrustEvidenceBundle(
            cell_id=cell_id,
            closed_trades=trades,
            equity_curve=equity,
            executions=executions,
            safety=safety,
            governor=governor,
        )

    @staticmethod
    def _closed_trade(event: CellEvent) -> ClosedTradeEvidence:
        payload = event.payload
        return ClosedTradeEvidence(
            trade_id=UUID(str(payload.get("trade_id", event.event_id))),
            closed_at=event.occurred_at,
            realized_pnl_usd=(
                Decimal(str(payload["realized_pnl_usd"]))
                if payload.get("realized_pnl_usd") is not None
                else None
            ),
            planned_risk_usd=(
                Decimal(str(payload["planned_risk_usd"]))
                if payload.get("planned_risk_usd") is not None
                else None
            ),
            mfe_r=(
                Decimal(str(payload["mfe_r"]))
                if payload.get("mfe_r") is not None
                else None
            ),
            mae_r=(
                Decimal(str(payload["mae_r"]))
                if payload.get("mae_r") is not None
                else None
            ),
            regime=payload.get("regime"),
            strategy_compliant=payload.get("strategy_compliant"),
            settlement_verified=payload.get("settlement_verified"),
        )

    def _execution_evidence(
        self, cell_id: UUID, window_start: datetime | None, window_end: datetime | None
    ) -> tuple[ExecutionEvidence, ...]:
        reference_events = self.session.scalars(
            select(CellEvent).where(
                CellEvent.cell_id == cell_id,
                CellEvent.event_type == "EXECUTION_REFERENCE",
            )
        )
        references = {
            UUID(str(event.payload["fill_id"])): Decimal(
                str(event.payload["reference_price"])
            )
            for event in reference_events
            if event.payload.get("fill_id") and event.payload.get("reference_price")
        }
        query = (
            select(Fill, Instrument)
            .join(KairoOrder, KairoOrder.kairo_order_id == Fill.kairo_order_id)
            .join(OrderIntent, OrderIntent.intent_id == KairoOrder.intent_id)
            .join(Instrument, Instrument.instrument_id == Fill.instrument_id)
            .where(OrderIntent.cell_id == cell_id)
        )
        if window_start is not None:
            query = query.where(Fill.filled_at >= window_start)
        if window_end is not None:
            query = query.where(Fill.filled_at <= window_end)
        rows = self.session.execute(query.order_by(Fill.filled_at, Fill.fill_id)).all()
        return tuple(
            ExecutionEvidence(
                fill_id=fill.fill_id,
                filled_at=fill.filled_at,
                side=OrderSide(fill.side),
                fill_price=fill.price,
                reference_price=references.get(fill.fill_id),
                quantity=fill.quantity,
                contract_multiplier=instrument.contract_multiplier or Decimal("1"),
            )
            for fill, instrument in rows
        )

    def _safety_evidence(self, cell_id: UUID) -> SafetyAuditEvidence:
        events = list(
            self.session.scalars(
                select(CellEvent)
                .where(CellEvent.cell_id == cell_id)
                .order_by(CellEvent.occurred_at, CellEvent.event_id)
            )
        )
        latest: dict[str, bool] = {}
        breach_types: set[str] = set()
        for event in events:
            if event.event_type == "BROKER_RECONCILIATION_AUDIT":
                latest["reconciliation"] = event.payload.get("verified") is True
            elif event.event_type == "POST_HALT_CONTROL_AUDIT":
                latest["post_halt"] = event.payload.get("clean") is True
            elif event.event_type == "PARAMETER_CONTROL_AUDIT":
                latest["parameter"] = event.payload.get("clean") is True
            elif event.event_type == "SAFETY_BYPASS_CONFIRMED":
                breach_types.add(str(event.payload.get("bypass_type", "UNKNOWN")))
        authorized_decision = exists(
            select(RiskDecision.decision_id).where(
                RiskDecision.intent_id == KairoOrder.intent_id,
                RiskDecision.verdict == "AUTHORIZED",
            )
        )
        unauthorized_order = self.session.scalar(
            select(func.count())
            .select_from(KairoOrder)
            .join(OrderIntent, OrderIntent.intent_id == KairoOrder.intent_id)
            .where(OrderIntent.cell_id == cell_id, ~authorized_decision)
        )
        post_halt_order = self.session.scalar(
            select(func.count(func.distinct(KairoOrder.kairo_order_id)))
            .select_from(KairoOrder)
            .join(OrderIntent, OrderIntent.intent_id == KairoOrder.intent_id)
            .join(RiskDecision, RiskDecision.intent_id == OrderIntent.intent_id)
            .where(
                OrderIntent.cell_id == cell_id,
                exists(
                    select(RiskStateEvent.event_id).where(
                        RiskStateEvent.session_id == RiskDecision.session_id,
                        RiskStateEvent.new_state == "HALTED_HARD",
                        RiskStateEvent.recorded_at <= KairoOrder.submitted_at,
                    )
                ),
            )
        )
        return SafetyAuditEvidence(
            broker_reconciliation_verified=latest.get("reconciliation"),
            post_halt_trading_verified_clean=latest.get("post_halt"),
            parameter_controls_verified_clean=latest.get("parameter"),
            unauthorized_execution_detected=(
                "UNAUTHORIZED_EXECUTION" in breach_types or bool(unauthorized_order)
            ),
            post_halt_execution_detected=(
                "POST_HALT_EXECUTION" in breach_types or bool(post_halt_order)
            ),
            parameter_bypass_detected="PARAMETER_BYPASS" in breach_types,
        )

    def _governor_evidence(
        self, cell_id: UUID, window_start: datetime | None, window_end: datetime | None
    ) -> GovernorAuditEvidence:
        conditions = [OrderIntent.cell_id == cell_id]
        if window_start is not None:
            conditions.append(RiskDecision.decided_at >= window_start)
        if window_end is not None:
            conditions.append(RiskDecision.decided_at <= window_end)
        rows = self.session.execute(
            select(RiskDecision.verdict, func.count())
            .join(OrderIntent, OrderIntent.intent_id == RiskDecision.intent_id)
            .where(*conditions)
            .group_by(RiskDecision.verdict)
        ).all()
        counts = {verdict: count for verdict, count in rows}
        return GovernorAuditEvidence(
            authorized_intents=counts.get("AUTHORIZED", 0),
            rejected_intents=counts.get("REJECTED", 0),
        )
