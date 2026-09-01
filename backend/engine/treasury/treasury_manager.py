from collections import defaultdict
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from typing import Callable, Mapping
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.configuration import CellTreasuryConfig, Instrument
from app.db.models.ledger import (
    MarketSnapshot,
    SiphonAllocation,
    SiphonEvent,
    TreasuryCashConsumption,
    TreasuryExecution,
    TreasuryRegimeObservation,
)
from app.db.models.projections import CapitalCell, OwnershipTreasuryHolding
from engine.treasury.models import TreasuryExecutionPolicyConfig, TreasuryExecutionResult
from engine.treasury.safety_gates import evaluate_execution_gates


CENT = Decimal("0.01")


def _floor(value: Decimal, quantum: Decimal) -> Decimal:
    return value.quantize(quantum, rounding=ROUND_DOWN)


class TreasuryManager:
    """PAPER_ONLY conversion of bound target allocations into fractional holdings."""

    def __init__(
        self,
        session: Session,
        policy: TreasuryExecutionPolicyConfig | None = None,
        identity_factory: Callable[[str, str], UUID] | None = None,
    ) -> None:
        self.session = session
        self.policy = policy or TreasuryExecutionPolicyConfig()
        self.identity_factory = identity_factory
        if self.policy.clearance != "PAPER_ONLY":
            raise ValueError("Phase 4 Step 2 supports PAPER_ONLY clearance")

    def _identity(self, record_type: str, stable_key: str) -> UUID:
        return (
            self.identity_factory(record_type, stable_key)
            if self.identity_factory is not None
            else uuid4()
        )

    def execute_available(
        self,
        *,
        cell_id: UUID,
        is_synthetic: bool,
        market_snapshot_ids: Mapping[UUID, UUID],
        occurred_at: datetime,
        estimated_fee_usd: Decimal = Decimal("0"),
        actual_fee_usd: Decimal = Decimal("0"),
        vix: Decimal | None = None,
        spy_daily_drop_pct: Decimal | None = None,
    ) -> list[TreasuryExecutionResult]:
        self._aware(occurred_at)
        if estimated_fee_usd < 0 or actual_fee_usd < 0:
            raise ValueError("fees cannot be negative")

        # Resolve all eligible facts before locking. Unknown legacy provenance is
        # an explicit stop, not a row silently omitted by a nullable predicate.
        unresolved = self.session.scalar(
            select(func.count())
            .select_from(SiphonAllocation)
            .join(SiphonEvent, SiphonEvent.siphon_id == SiphonAllocation.siphon_id)
            .where(
                SiphonEvent.cell_id == cell_id,
                SiphonAllocation.bucket_type == "TARGET_TREASURY",
                (SiphonEvent.policy_id == "LEGACY-SIPHON-v0")
                | SiphonEvent.target_config_id.is_(None),
            )
        )
        if unresolved:
            raise ValueError("target allocations include unresolved legacy/config lineage")

        consumed = (
            select(
                TreasuryCashConsumption.allocation_id,
                func.sum(TreasuryCashConsumption.consumed_usd).label("consumed"),
            )
            .group_by(TreasuryCashConsumption.allocation_id)
            .subquery()
        )
        rows = list(
            self.session.execute(
                select(
                    SiphonAllocation,
                    SiphonEvent,
                    func.coalesce(consumed.c.consumed, 0),
                )
                .join(SiphonEvent, SiphonEvent.siphon_id == SiphonAllocation.siphon_id)
                .outerjoin(consumed, consumed.c.allocation_id == SiphonAllocation.allocation_id)
                .where(
                    SiphonEvent.cell_id == cell_id,
                    SiphonEvent.is_synthetic.is_(is_synthetic),
                    SiphonEvent.policy_id != "LEGACY-SIPHON-v0",
                    SiphonEvent.target_config_id.is_not(None),
                    SiphonAllocation.bucket_type == "TARGET_TREASURY",
                    SiphonAllocation.allocated_usd > func.coalesce(consumed.c.consumed, 0),
                )
                .order_by(SiphonAllocation.occurred_at, SiphonAllocation.allocation_id)
                .with_for_update(of=SiphonAllocation)
            )
        )
        grouped: dict[UUID, list[tuple[SiphonAllocation, Decimal]]] = defaultdict(list)
        for allocation, event, already_consumed in rows:
            assert event.target_config_id is not None
            grouped[event.target_config_id].append(
                (allocation, Decimal(allocation.allocated_usd) - Decimal(already_consumed))
            )

        results: list[TreasuryExecutionResult] = []
        for config_id in sorted(grouped, key=str):
            available_rows = grouped[config_id]
            available = sum((amount for _, amount in available_rows), Decimal("0"))
            if available < self.policy.effective_minimum_usd:
                continue
            snapshot_id = market_snapshot_ids.get(config_id)
            if snapshot_id is None:
                raise ValueError(f"no market snapshot supplied for target config {config_id}")
            result = self._execute_group(
                cell_id=cell_id,
                config_id=config_id,
                is_synthetic=is_synthetic,
                available_rows=available_rows,
                snapshot_id=snapshot_id,
                occurred_at=occurred_at,
                estimated_fee_usd=estimated_fee_usd,
                actual_fee_usd=actual_fee_usd,
                vix=vix,
                spy_daily_drop_pct=spy_daily_drop_pct,
            )
            if result is not None:
                results.append(result)
        if results:
            self.rebuild_holdings_projection(cell_id=cell_id, is_synthetic=is_synthetic)
        return results

    def _execute_group(
        self,
        *,
        cell_id: UUID,
        config_id: UUID,
        is_synthetic: bool,
        available_rows: list[tuple[SiphonAllocation, Decimal]],
        snapshot_id: UUID,
        occurred_at: datetime,
        estimated_fee_usd: Decimal,
        actual_fee_usd: Decimal,
        vix: Decimal | None,
        spy_daily_drop_pct: Decimal | None,
    ) -> TreasuryExecutionResult | None:
        config = self.session.get(CellTreasuryConfig, config_id)
        if config is None or config.cell_id != cell_id:
            raise ValueError("bound target configuration cannot be resolved for cell")
        if config.target_type != "SINGLE_ASSET":
            raise ValueError("only SINGLE_ASSET treasury targets are supported")
        instrument = self.session.get(Instrument, config.target_instrument_id)
        if instrument is None or instrument.retired_at is not None:
            raise ValueError("bound target instrument is absent or retired")
        if instrument.symbol != config.target_symbol:
            raise ValueError("bound target symbol differs from canonical instrument")
        snapshot = self.session.get(MarketSnapshot, snapshot_id)
        if snapshot is None or snapshot.instrument_id != instrument.instrument_id:
            raise ValueError("market snapshot does not resolve to the bound target instrument")

        gate = evaluate_execution_gates(snapshot, occurred_at=occurred_at, policy=self.policy)
        if not gate.allowed:
            self.session.add(
                TreasuryRegimeObservation(
                    event_id=self._identity(
                        "treasury_regime_observation",
                        f"{cell_id}:{config_id}:{snapshot_id}:{occurred_at.isoformat()}:SAFETY_GATE",
                    ),
                    cell_id=cell_id,
                    event_type="SAFETY_GATE",
                    gate_name=gate.failed_gate or "UNKNOWN",
                    status="BLOCKED",
                    observed_metric_value=gate.observed_value,
                    threshold_value=gate.threshold_value,
                    market_snapshot_id=snapshot_id,
                    is_synthetic=is_synthetic,
                    occurred_at=occurred_at,
                )
            )
            self.session.flush()
            return None

        self._observe_macro(
            cell_id, snapshot_id, is_synthetic, occurred_at, vix, spy_daily_drop_pct
        )
        available = sum((amount for _, amount in available_rows), Decimal("0"))
        ask = Decimal(snapshot.ask)
        quantum = Decimal(1).scaleb(-self.policy.share_precision)
        shares = _floor((available - estimated_fee_usd) / ask, quantum)
        if shares <= 0:
            return None
        gross = _floor(shares * ask, CENT)
        net = gross + actual_fee_usd
        if net > available:
            shares = _floor((available - actual_fee_usd) / ask, quantum)
            if shares <= 0:
                return None
            gross = _floor(shares * ask, CENT)
            net = gross + actual_fee_usd
        if net > available or net < self.policy.broker_fractional_minimum_usd:
            return None

        execution = TreasuryExecution(
            execution_id=self._identity(
                "treasury_execution",
                f"{cell_id}:{config_id}:{snapshot_id}:{occurred_at.isoformat()}",
            ),
            cell_id=cell_id,
            target_config_id=config_id,
            instrument_id=instrument.instrument_id,
            symbol=instrument.symbol,
            shares_executed=shares,
            execution_price_usd=ask,
            gross_amount_usd=gross,
            fee_usd=actual_fee_usd,
            net_amount_usd=net,
            market_snapshot_id=snapshot_id,
            is_synthetic=is_synthetic,
            occurred_at=occurred_at,
        )
        self.session.add(execution)
        self.session.flush()
        remaining = net
        consumptions: list[TreasuryCashConsumption] = []
        for allocation, row_available in available_rows:
            if remaining <= 0:
                break
            amount = min(row_available, remaining)
            if amount <= 0:
                continue
            row = TreasuryCashConsumption(
                consumption_id=self._identity(
                    "treasury_cash_consumption",
                    f"{execution.execution_id}:{allocation.allocation_id}",
                ),
                execution_id=execution.execution_id,
                allocation_id=allocation.allocation_id,
                consumed_usd=amount,
                occurred_at=occurred_at,
            )
            self.session.add(row)
            self.session.flush()  # fires serialized ceiling + lineage checks per fact
            consumptions.append(row)
            remaining -= amount
        if remaining != 0:
            raise RuntimeError("execution net amount could not be consumed exactly")
        return TreasuryExecutionResult(
            execution_id=execution.execution_id,
            cell_id=cell_id,
            target_config_id=config_id,
            instrument_id=instrument.instrument_id,
            symbol=instrument.symbol,
            shares_executed=shares,
            execution_price_usd=ask,
            gross_amount_usd=gross,
            fee_usd=actual_fee_usd,
            net_amount_usd=net,
            market_snapshot_id=snapshot_id,
            is_synthetic=is_synthetic,
            consumption_ids=[row.consumption_id for row in consumptions],
            occurred_at=occurred_at,
        )

    def _observe_macro(
        self,
        cell_id: UUID,
        snapshot_id: UUID,
        is_synthetic: bool,
        occurred_at: datetime,
        vix: Decimal | None,
        spy_drop: Decimal | None,
    ) -> None:
        for name, value, threshold in (
            ("VIX", vix, Decimal("35.0")),
            ("SPY_DAILY_DROP_PCT", spy_drop, Decimal("3.5")),
        ):
            if value is None:
                continue
            self.session.add(
                TreasuryRegimeObservation(
                    event_id=self._identity(
                        "treasury_regime_observation",
                        f"{cell_id}:{snapshot_id}:{occurred_at.isoformat()}:{name}",
                    ),
                    cell_id=cell_id,
                    event_type="MACRO_OBSERVATION",
                    gate_name=name,
                    status="OBSERVE_ONLY",
                    observed_metric_value=value,
                    threshold_value=threshold,
                    market_snapshot_id=snapshot_id,
                    is_synthetic=is_synthetic,
                    occurred_at=occurred_at,
                )
            )

    def rebuild_holdings_projection(self, *, cell_id: UUID, is_synthetic: bool) -> None:
        rows = list(
            self.session.execute(
                select(
                    TreasuryExecution.instrument_id,
                    TreasuryExecution.symbol,
                    func.sum(TreasuryExecution.shares_executed),
                    func.sum(TreasuryExecution.net_amount_usd),
                )
                .where(
                    TreasuryExecution.cell_id == cell_id,
                    TreasuryExecution.is_synthetic.is_(is_synthetic),
                )
                .group_by(TreasuryExecution.instrument_id, TreasuryExecution.symbol)
            )
        )
        cell = self.session.get(CapitalCell, cell_id)
        if cell is None:
            raise ValueError("capital cell cannot be resolved")
        for instrument_id, symbol, shares_value, basis_value in rows:
            shares, basis = Decimal(shares_value), Decimal(basis_value)
            average = (basis / shares).quantize(Decimal("0.0001")) if shares else Decimal("0")
            holding = self.session.scalar(
                select(OwnershipTreasuryHolding).where(
                    OwnershipTreasuryHolding.cell_id == cell_id,
                    OwnershipTreasuryHolding.instrument_id == instrument_id,
                    OwnershipTreasuryHolding.is_synthetic.is_(is_synthetic),
                )
            )
            if holding is None:
                holding = OwnershipTreasuryHolding(
                    holding_id=self._identity(
                        "ownership_treasury_holding",
                        f"{cell_id}:{instrument_id}:{is_synthetic}",
                    ),
                    treasury_code=cell.target_treasury_code,
                    cell_id=cell_id,
                    instrument_id=instrument_id,
                    symbol=symbol,
                    dollars_contributed=basis,
                    fractional_shares=shares,
                    total_shares=shares,
                    cumulative_cost_basis_usd=basis,
                    average_entry_price_usd=average,
                    market_value_usd=Decimal("0"),
                    unrealized_pnl_usd=Decimal("0"),
                    is_synthetic=is_synthetic,
                    legacy_values_equivalent=True,
                    updated_at=max(
                        row.occurred_at
                        for row in self.session.scalars(
                            select(TreasuryExecution).where(
                                TreasuryExecution.cell_id == cell_id,
                                TreasuryExecution.instrument_id == instrument_id,
                                TreasuryExecution.is_synthetic.is_(is_synthetic),
                            )
                        )
                    ),
                )
                self.session.add(holding)
            else:
                holding.total_shares = shares
                holding.cumulative_cost_basis_usd = basis
                holding.average_entry_price_usd = average
                holding.symbol = symbol
                if holding.legacy_values_equivalent:
                    holding.dollars_contributed = basis
                    holding.fractional_shares = shares
        self.session.flush()

    @staticmethod
    def _aware(value: datetime) -> None:
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError("occurred_at must be timezone-aware")
