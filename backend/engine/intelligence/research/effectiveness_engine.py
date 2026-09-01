import hashlib
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.intelligence import (
    IntelligenceResearchCategorySlice,
    IntelligenceResearchRun,
    OrderContextEvaluation,
)
from app.db.models.ledger import Fill, FillRealizedPnL, KairoOrder, OrderIntent
from engine.intelligence.research.models import (
    RESEARCH_SEMANTICS,
    ResearchMethod,
    compute_max_drawdown,
    serialize_research_manifest,
)


CENT = Decimal("0.01")
ORDERING_RULE = "filled_at ASC, fill_id ASC, intent_id ASC"


class EffectivenessEngine:
    """Descriptive trade-removal research with zero runtime authority."""

    authority_mode = "OFFLINE_RESEARCH_ONLY"
    research_method = ResearchMethod.TRADE_REMOVAL_COUNTERFACTUAL

    def __init__(
        self,
        db_session: Session,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.db = db_session
        self.clock = clock or (lambda: datetime.now(UTC))
        self.last_manifest: dict[str, Any] | None = None

    def run_trade_removal_analysis(
        self,
        cell_id: UUID | None,
        sample_start: datetime,
        sample_end: datetime,
    ) -> IntelligenceResearchRun:
        self._require_aware(sample_start, "sample_start")
        self._require_aware(sample_end, "sample_end")
        if sample_end < sample_start:
            raise ValueError("sample_end cannot precede sample_start")

        statement = (
            select(
                OrderIntent.intent_id,
                OrderIntent.cell_id.label("intent_cell_id"),
                OrderIntent.created_at.label("intent_created_at"),
                Fill.fill_id,
                Fill.filled_at.label("trade_close_at"),
                FillRealizedPnL.cell_id.label("realized_cell_id"),
                FillRealizedPnL.realized_pnl_usd.label("realized_pnl"),
                OrderContextEvaluation.counterfactual_opinion,
                OrderContextEvaluation.veto_reason_code,
                OrderContextEvaluation.evaluated_at.label("eval_at"),
            )
            .join(KairoOrder, KairoOrder.intent_id == OrderIntent.intent_id)
            .join(Fill, Fill.kairo_order_id == KairoOrder.kairo_order_id)
            .join(FillRealizedPnL, FillRealizedPnL.fill_id == Fill.fill_id)
            .outerjoin(
                OrderContextEvaluation,
                OrderContextEvaluation.intent_id == OrderIntent.intent_id,
            )
            .where(
                FillRealizedPnL.position_effect == "CLOSING",
                Fill.filled_at >= sample_start,
                Fill.filled_at <= sample_end,
            )
            .order_by(Fill.filled_at, Fill.fill_id, OrderIntent.intent_id)
        )
        if cell_id is not None:
            statement = statement.where(OrderIntent.cell_id == cell_id)
        trades = list(self.db.execute(statement))

        baseline_pnl = Decimal("0.00")
        counterfactual_pnl = Decimal("0.00")
        losses_avoided = Decimal("0.00")
        profits_forfeited = Decimal("0.00")
        baseline_curve = [Decimal("0.00")]
        counterfactual_curve = [Decimal("0.00")]
        evaluated = vetoes = veto_losses = veto_wins = veto_breakeven = 0
        excluded_causal = 0
        category_stats: dict[str, dict[str, Any]] = defaultdict(
            lambda: {
                "count": 0,
                "loss_count": 0,
                "losses_avoided": Decimal("0.00"),
                "profits_forfeited": Decimal("0.00"),
            }
        )
        trade_facts: list[dict[str, Any]] = []

        for trade in trades:
            if trade.intent_cell_id != trade.realized_cell_id:
                raise ValueError(
                    f"fill {trade.fill_id} has conflicting intent and realized-P&L cell lineage"
                )
            pnl = Decimal(trade.realized_pnl).quantize(CENT, ROUND_HALF_EVEN)
            baseline_pnl += pnl
            baseline_curve.append(baseline_pnl)
            causal_valid = trade.eval_at is None or trade.eval_at <= trade.intent_created_at
            attributed = trade.counterfactual_opinion is not None and causal_valid
            vetoed = attributed and trade.counterfactual_opinion == "WOULD_HAVE_VETOED"

            if trade.counterfactual_opinion is not None and not causal_valid:
                excluded_causal += 1
            elif attributed:
                evaluated += 1

            if vetoed:
                vetoes += 1
                category = trade.veto_reason_code or "UNSPECIFIED_VETO"
                stats = category_stats[category]
                stats["count"] += 1
                if pnl < 0:
                    veto_losses += 1
                    avoided = abs(pnl)
                    losses_avoided += avoided
                    stats["losses_avoided"] += avoided
                    stats["loss_count"] += 1
                elif pnl > 0:
                    veto_wins += 1
                    profits_forfeited += pnl
                    stats["profits_forfeited"] += pnl
                else:
                    veto_breakeven += 1
            else:
                counterfactual_pnl += pnl
                counterfactual_curve.append(counterfactual_pnl)

            trade_facts.append({
                "intent_id": str(trade.intent_id),
                "fill_id": str(trade.fill_id),
                "trade_close_at": trade.trade_close_at.isoformat(),
                "realized_pnl": str(pnl),
                "counterfactual_opinion": trade.counterfactual_opinion,
                "veto_reason_code": trade.veto_reason_code,
                "evaluation_at": trade.eval_at.isoformat() if trade.eval_at else None,
                "intent_created_at": trade.intent_created_at.isoformat(),
                "causal_valid": causal_valid,
                "trade_removed": vetoed,
            })

        net_alpha = losses_avoided - profits_forfeited
        precision = (
            (Decimal(veto_losses) / Decimal(vetoes) * Decimal("100")).quantize(CENT)
            if vetoes
            else Decimal("0.00")
        )
        baseline_dd = compute_max_drawdown(baseline_curve).quantize(CENT)
        counterfactual_dd = compute_max_drawdown(counterfactual_curve).quantize(CENT)
        vetoed_pnl = profits_forfeited - losses_avoided
        if baseline_pnl != counterfactual_pnl + vetoed_pnl:
            raise AssertionError("trade-removal P&L conservation failed")
        if net_alpha != losses_avoided - profits_forfeited:
            raise AssertionError("trade-removal net alpha conservation failed")

        category_payload = [
            self._category_payload(category, category_stats[category])
            for category in sorted(category_stats)
        ]
        base_manifest = {
            "cell_id": str(cell_id) if cell_id else None,
            "research_method": self.research_method.value,
            "research_semantics": RESEARCH_SEMANTICS,
            "ordering_rule": ORDERING_RULE,
            "sample_start": sample_start.isoformat(),
            "sample_end": sample_end.isoformat(),
            "population": {
                "total_baseline_trades": len(trades),
                "total_context_evaluated_trades": evaluated,
                "total_veto_opportunities": vetoes,
                "vetoed_losing_trades": veto_losses,
                "vetoed_winning_trades": veto_wins,
                "vetoed_breakeven_trades": veto_breakeven,
                "excluded_causal_invalid_trades": excluded_causal,
            },
            "financials": {
                "baseline_net_pnl": str(baseline_pnl),
                "counterfactual_net_pnl": str(counterfactual_pnl),
                "losses_avoided_usd": str(losses_avoided),
                "profits_forfeited_usd": str(profits_forfeited),
                "net_alpha_usd": str(net_alpha),
                "baseline_max_drawdown_usd": str(baseline_dd),
                "counterfactual_max_drawdown_usd": str(counterfactual_dd),
                "veto_precision_pct": str(precision),
            },
            "category_slices": category_payload,
            "trade_facts": trade_facts,
        }
        lineage_hash = hashlib.sha256(
            serialize_research_manifest(base_manifest)
        ).hexdigest()
        run_id = uuid5(NAMESPACE_URL, f"kairo:effectiveness:{lineage_hash}")
        manifest = {"run_id": str(run_id), **base_manifest}
        manifest_hash = hashlib.sha256(
            serialize_research_manifest(manifest)
        ).hexdigest()
        self.last_manifest = manifest

        existing = self.db.get(IntelligenceResearchRun, run_id)
        if existing is not None:
            if existing.research_manifest_sha256 != manifest_hash:
                raise ValueError("conflicting deterministic research run identity")
            return existing

        executed_at = self.clock()
        self._require_aware(executed_at, "research clock")
        run = IntelligenceResearchRun(
            run_id=run_id,
            cell_id=cell_id,
            research_method=self.research_method.value,
            sample_start_time=sample_start,
            sample_end_time=sample_end,
            total_baseline_trades=len(trades),
            total_context_evaluated_trades=evaluated,
            total_veto_opportunities=vetoes,
            vetoed_losing_trades=veto_losses,
            vetoed_winning_trades=veto_wins,
            vetoed_breakeven_trades=veto_breakeven,
            excluded_causal_invalid_trades=excluded_causal,
            baseline_net_pnl=baseline_pnl,
            counterfactual_net_pnl=counterfactual_pnl,
            losses_avoided_usd=losses_avoided,
            profits_forfeited_usd=profits_forfeited,
            net_alpha_usd=net_alpha,
            baseline_max_drawdown_usd=baseline_dd,
            counterfactual_max_drawdown_usd=counterfactual_dd,
            veto_precision_pct=precision,
            research_manifest_sha256=manifest_hash,
            executed_at=executed_at,
        )
        self.db.add(run)
        self.db.flush()
        for category in category_payload:
            self.db.add(IntelligenceResearchCategorySlice(
                slice_id=uuid5(
                    NAMESPACE_URL,
                    f"kairo:effectiveness-slice:{run_id}:{category['category_code']}",
                ),
                run_id=run_id,
                category_code=category["category_code"],
                vetoed_trades_count=category["vetoed_trades_count"],
                losses_avoided_usd=Decimal(category["losses_avoided_usd"]),
                profits_forfeited_usd=Decimal(category["profits_forfeited_usd"]),
                slice_net_alpha_usd=Decimal(category["slice_net_alpha_usd"]),
                slice_precision_pct=Decimal(category["slice_precision_pct"]),
            ))
        self.db.flush()
        return run

    @staticmethod
    def _category_payload(category: str, stats: dict[str, Any]) -> dict[str, Any]:
        precision = (
            (Decimal(stats["loss_count"]) / Decimal(stats["count"]) * Decimal("100"))
            .quantize(CENT)
            if stats["count"]
            else Decimal("0.00")
        )
        alpha = stats["losses_avoided"] - stats["profits_forfeited"]
        return {
            "category_code": category,
            "vetoed_trades_count": stats["count"],
            "losses_avoided_usd": str(stats["losses_avoided"]),
            "profits_forfeited_usd": str(stats["profits_forfeited"]),
            "slice_net_alpha_usd": str(alpha),
            "slice_precision_pct": str(precision),
        }

    @staticmethod
    def _require_aware(value: datetime, field: str) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{field} must be timezone-aware")
