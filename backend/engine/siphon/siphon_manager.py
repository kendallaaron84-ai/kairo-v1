import hashlib
import json
from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.configuration import CellTreasuryConfig, Instrument
from app.db.models.ledger import (
    BrokerCashSnapshot,
    Fill,
    FillRealizedPnL,
    SiphonAllocation,
    SiphonEvent,
    SiphonProfitAttribution,
)
from engine.siphon.models import (
    CellTreasuryConfigInput,
    ProfitAttributionItem,
    SiphonBucket,
    SiphonEventResult,
    SiphonPolicyConfig,
    SyntheticSettlementMetadata,
    TargetType,
)
from engine.siphon.remainder import allocate_exact_cents, floor_cents
from engine.risk.pnl_tracker import net_realized_pnl


class SiphonManager:
    """Allocates already-computed canonical realized profit; it never computes trade P&L."""

    def __init__(self, session: Session, policy: SiphonPolicyConfig | None = None):
        self.session = session
        self.policy = policy or SiphonPolicyConfig()

    def record_canonical_realized_pnl(
        self,
        *,
        fill_id: UUID,
        cell_id: UUID,
        position_effect: str,
        realized_pnl_usd: Decimal,
        occurred_at: datetime,
        source_authority: str = "KAIRO_PNL_TRACKER",
        realization_id: UUID | None = None,
    ) -> FillRealizedPnL:
        self._require_aware(occurred_at, "occurred_at")
        if source_authority != "KAIRO_PNL_TRACKER":
            raise ValueError("realized P&L must come from Kairo's canonical PnLTracker")
        if position_effect not in {"OPENING", "CLOSING"}:
            raise ValueError("position_effect must be OPENING or CLOSING")
        if position_effect == "OPENING" and realized_pnl_usd != 0:
            raise ValueError("opening fills cannot realize profit")
        fact = FillRealizedPnL(
            realization_id=realization_id or uuid4(),
            fill_id=fill_id,
            cell_id=cell_id,
            position_effect=position_effect,
            realized_pnl_usd=realized_pnl_usd,
            source_authority=source_authority,
            occurred_at=occurred_at,
        )
        self.session.add(fact)
        self.session.flush()
        return fact

    def create_target_config(self, config: CellTreasuryConfigInput) -> CellTreasuryConfig:
        if config.target_type is not TargetType.SINGLE_ASSET:
            raise ValueError("PROFIT-ALLOC-v1.0 supports only SINGLE_ASSET targets")
        instrument = self.session.get(Instrument, config.target_instrument_id)
        if instrument is None or instrument.retired_at is not None:
            raise ValueError("target instrument does not resolve to an active canonical instrument")
        if instrument.symbol != config.target_symbol:
            raise ValueError("target symbol does not match canonical instrument identity")
        row = CellTreasuryConfig(**config.model_dump(mode="python"))
        self.session.add(row)
        self.session.flush()
        return row

    def qualify_and_allocate(
        self,
        *,
        cell_id: UUID,
        occurred_at: datetime,
        committed_order_cash_usd: Decimal = Decimal("0"),
        existing_reserved_cash_usd: Decimal = Decimal("0"),
        broker_account_id: UUID | None = None,
        settlement_snapshot_id: UUID | None = None,
        synthetic_settled_cash_usd: Decimal | None = None,
        synthetic_settlement_metadata: SyntheticSettlementMetadata | None = None,
        siphon_id: UUID | None = None,
    ) -> SiphonEventResult | None:
        self._require_aware(occurred_at, "occurred_at")
        if committed_order_cash_usd < 0 or existing_reserved_cash_usd < 0:
            raise ValueError("committed and reserved cash cannot be negative")
        synthetic = synthetic_settlement_metadata is not None
        settled_cash, settlement_cutoff = self._settlement_evidence(
            synthetic=synthetic,
            broker_account_id=broker_account_id,
            settlement_snapshot_id=settlement_snapshot_id,
            synthetic_settled_cash_usd=synthetic_settled_cash_usd,
            metadata=synthetic_settlement_metadata,
        )
        target = self._active_target(cell_id)
        settled_rows = list(
            self.session.execute(
                select(FillRealizedPnL, Fill)
                .join(Fill, Fill.fill_id == FillRealizedPnL.fill_id)
                .where(
                    FillRealizedPnL.cell_id == cell_id,
                    FillRealizedPnL.occurred_at <= settlement_cutoff,
                    FillRealizedPnL.source_authority == "KAIRO_PNL_TRACKER",
                    Fill.filled_at <= settlement_cutoff,
                    Fill.is_simulated.is_(synthetic),
                    *(
                        [Fill.broker_account_id == broker_account_id]
                        if broker_account_id is not None
                        else []
                    ),
                )
                .order_by(FillRealizedPnL.occurred_at, FillRealizedPnL.fill_id)
                .with_for_update()
            )
        )
        sources = [
            fact
            for fact, _fill in settled_rows
            if fact.position_effect == "CLOSING" and fact.realized_pnl_usd > 0
        ]
        fill_ids = [source.fill_id for source in sources]
        attributed: dict[UUID, Decimal] = {}
        if fill_ids:
            attributed = {
                fill_id: Decimal(total)
                for fill_id, total in self.session.execute(
                    select(
                        SiphonProfitAttribution.source_fill_id,
                        func.sum(SiphonProfitAttribution.attributed_profit_usd),
                    )
                    .where(SiphonProfitAttribution.source_fill_id.in_(fill_ids))
                    .group_by(SiphonProfitAttribution.source_fill_id)
                )
            }
        available = [
            (source, max(Decimal("0"), source.realized_pnl_usd - attributed.get(source.fill_id, Decimal("0"))))
            for source in sources
        ]
        unsiphoned_profit = floor_cents(sum((amount for _, amount in available), Decimal("0")))
        canonical_net_settled_profit = net_realized_pnl(
            sum(
                (fact.realized_pnl_usd for fact, _fill in settled_rows),
                Decimal("0"),
            ),
            sum(
                (fill.commission_fee_usd for _fact, fill in settled_rows),
                Decimal("0"),
            ),
        )
        cumulative_prior_siphoned = Decimal(
            self.session.scalar(
                select(func.coalesce(func.sum(SiphonEvent.qualified_profit_usd), 0)).where(
                    SiphonEvent.cell_id == cell_id
                )
            )
            or 0
        )
        remaining_net_settled_profit = floor_cents(
            max(Decimal("0"), canonical_net_settled_profit - cumulative_prior_siphoned)
        )
        prior_siphon_reserves = Decimal(
            self.session.scalar(
                select(func.coalesce(func.sum(SiphonAllocation.allocated_usd), 0))
                .join(SiphonEvent, SiphonEvent.siphon_id == SiphonAllocation.siphon_id)
                .where(SiphonEvent.cell_id == cell_id)
            )
            or 0
        )
        total_existing_reserved = existing_reserved_cash_usd + prior_siphon_reserves
        headroom = max(
            Decimal("0"),
            settled_cash
            - self.policy.protected_seed_floor_usd
            - committed_order_cash_usd
            - total_existing_reserved,
        )
        qualified = floor_cents(
            max(
                Decimal("0"),
                min(unsiphoned_profit, remaining_net_settled_profit, headroom),
            )
        )
        if qualified < self.policy.minimum_siphon_threshold_usd:
            return None

        allocations = allocate_exact_cents(qualified, self.policy)
        attribution_rows: list[SiphonProfitAttribution] = []
        remaining = qualified
        event_id = siphon_id or uuid4()
        for source, source_available in available:
            if remaining == 0:
                break
            amount = floor_cents(min(source_available, remaining))
            if amount <= 0:
                continue
            attribution_rows.append(
                SiphonProfitAttribution(
                    attribution_id=uuid4(),
                    siphon_id=event_id,
                    source_fill_id=source.fill_id,
                    attributed_profit_usd=amount,
                    occurred_at=occurred_at,
                )
            )
            remaining -= amount
        if remaining != 0:
            raise RuntimeError("qualified profit could not be attributed exactly")

        source_ids = [row.source_fill_id for row in attribution_rows]
        manifest_hash = self._manifest(
            cell_id=cell_id,
            target_config_id=target.config_id,
            occurred_at=occurred_at,
            qualified=qualified,
            source_rows=attribution_rows,
            settlement_snapshot_id=settlement_snapshot_id,
            synthetic_metadata=synthetic_settlement_metadata,
            positive_unattributed_profit=unsiphoned_profit,
            canonical_net_settled_profit=canonical_net_settled_profit,
            cumulative_prior_siphoned=cumulative_prior_siphoned,
            remaining_net_settled_profit=remaining_net_settled_profit,
            seed_cash_headroom=headroom,
        )
        event = SiphonEvent(
            siphon_id=event_id,
            cell_id=cell_id,
            treasury_code=target.target_symbol,
            amount=qualified,
            occurred_at=occurred_at,
            reason_code="SETTLED_REALIZED_PROFIT",
            policy_id=self.policy.policy_id,
            policy_version=self.policy.policy_version,
            broker_account_id=broker_account_id,
            settlement_snapshot_id=settlement_snapshot_id,
            source_fill_ids=source_ids,
            qualified_profit_usd=qualified,
            safety_reserve_usd=allocations[SiphonBucket.SAFETY_RESERVE],
            target_treasury_usd=allocations[SiphonBucket.TARGET_TREASURY],
            replication_pool_usd=allocations[SiphonBucket.REPLICATION_POOL],
            target_config_id=target.config_id,
            is_synthetic=synthetic,
            synthetic_settlement_metadata=(
                synthetic_settlement_metadata.model_dump(mode="json")
                if synthetic_settlement_metadata
                else None
            ),
            source_manifest_hash=manifest_hash,
        )
        self.session.add(event)
        self.session.flush()
        self.session.add_all(attribution_rows)
        running = total_existing_reserved
        for bucket in SiphonBucket:
            running += allocations[bucket]
            self.session.add(
                SiphonAllocation(
                    allocation_id=uuid4(),
                    siphon_id=event_id,
                    bucket_type=bucket.value,
                    allocated_usd=allocations[bucket],
                    unallocated_cash_balance_usd=running,
                    occurred_at=occurred_at,
                )
            )
        self.session.flush()
        return SiphonEventResult(
            siphon_id=event_id,
            cell_id=cell_id,
            broker_account_id=broker_account_id,
            settlement_snapshot_id=settlement_snapshot_id,
            qualified_profit_usd=qualified,
            safety_reserve_usd=allocations[SiphonBucket.SAFETY_RESERVE],
            target_treasury_usd=allocations[SiphonBucket.TARGET_TREASURY],
            replication_pool_usd=allocations[SiphonBucket.REPLICATION_POOL],
            target_config_id=target.config_id,
            is_synthetic=synthetic,
            synthetic_settlement_metadata=synthetic_settlement_metadata,
            source_fill_ids=source_ids,
            attributions=[
                ProfitAttributionItem(
                    attribution_id=row.attribution_id,
                    source_fill_id=row.source_fill_id,
                    attributed_profit_usd=row.attributed_profit_usd,
                    occurred_at=row.occurred_at,
                )
                for row in attribution_rows
            ],
            source_manifest_hash=manifest_hash,
            occurred_at=occurred_at,
        )

    def _active_target(self, cell_id: UUID) -> CellTreasuryConfig:
        target = self.session.scalar(
            select(CellTreasuryConfig).where(
                CellTreasuryConfig.cell_id == cell_id,
                CellTreasuryConfig.is_active.is_(True),
            )
        )
        if target is None:
            raise ValueError("cell has no active treasury target configuration")
        if target.target_type != TargetType.SINGLE_ASSET.value:
            raise ValueError("PROFIT-ALLOC-v1.0 supports only SINGLE_ASSET targets")
        instrument = self.session.get(Instrument, target.target_instrument_id)
        if instrument is None or instrument.retired_at is not None:
            raise ValueError("target instrument does not resolve canonically")
        if instrument.symbol != target.target_symbol:
            raise ValueError("target configuration symbol differs from canonical instrument")
        return target

    def _settlement_evidence(
        self,
        *,
        synthetic: bool,
        broker_account_id: UUID | None,
        settlement_snapshot_id: UUID | None,
        synthetic_settled_cash_usd: Decimal | None,
        metadata: SyntheticSettlementMetadata | None,
    ) -> tuple[Decimal, datetime]:
        if synthetic:
            if settlement_snapshot_id is not None:
                raise ValueError("synthetic settlement cannot reference a live cash snapshot")
            if synthetic_settled_cash_usd is None or synthetic_settled_cash_usd < 0:
                raise ValueError("synthetic settlement requires non-negative settled cash evidence")
            assert metadata is not None
            return synthetic_settled_cash_usd, metadata.synthetic_settled_at
        if settlement_snapshot_id is None or broker_account_id is None:
            raise ValueError("live settlement requires snapshot and broker account lineage")
        if synthetic_settled_cash_usd is not None:
            raise ValueError("live settlement cannot use synthetic settled cash")
        snapshot = self.session.get(BrokerCashSnapshot, settlement_snapshot_id)
        if snapshot is None or snapshot.broker_account_id != broker_account_id:
            raise ValueError("settlement snapshot does not match broker account")
        return snapshot.settled_cash, snapshot.captured_at

    @staticmethod
    def _manifest(**values: object) -> str:
        normalized = {
            key: (
                value.model_dump(mode="json")
                if isinstance(value, SyntheticSettlementMetadata)
                else [
                    {"fill_id": str(row.source_fill_id), "amount": str(row.attributed_profit_usd)}
                    for row in value
                ]
                if key == "source_rows"
                else str(value)
                if isinstance(value, (UUID, Decimal, datetime))
                else value
            )
            for key, value in values.items()
        }
        return hashlib.sha256(
            json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @staticmethod
    def _require_aware(value: datetime, field_name: str) -> None:
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError(f"{field_name} must be timezone-aware")
