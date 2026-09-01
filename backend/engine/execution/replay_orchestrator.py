import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.configuration import Instrument
from app.db.models.ledger import (
    FillRealizedPnL,
    Fill,
    KairoOrder,
    MarketSnapshot,
    OrderIntent,
    OrderObservation,
    RiskDecision,
    SyntheticEvidenceManifest,
)
from app.db.models.projections import CurrentPosition
from app.db.models.risk import (
    RiskGovernorState,
    RiskInstrumentMark,
    RiskSession,
    RiskStateEvent,
)
from app.domain.enums import OptionRight
from app.domain.instruments import CanonicalInstrument
from engine.execution.models import (
    ExecutionQuote,
    LiquidityFidelityTier,
    PaperEngineConfig,
    SimulatedFillPayload,
)
from engine.execution.paper_broker import PaperExecutionEngine
from engine.execution.virtual_clock import ReplayIdentityFactory, VirtualClock
from engine.risk.governor import RiskGovernor
from engine.risk.models import (
    DecisionVerdict,
    ExecutionEnvironment,
    FillAccountingEvent,
    InstrumentRiskProfile,
    IntentEvaluationInput,
    MarketMark,
    PositionSnapshot,
    RiskEvaluationRequest,
    RiskSessionSpec,
    StrategyClearance,
)
from engine.risk.pnl_tracker import realized_round_trip_pnl
from engine.strategy.ema_cross_strategy import (
    EMACrossStrategy,
    StrategyContract,
    StrategyOrderSignal,
    StrategyPosition,
)
from engine.strategy.market_data import (
    LegacyReplayProvider,
    MarketDataLineage,
    ResearchEventKind,
    ResearchMarketEvent,
    ResearchReplayProvider,
    SampledPriceObservation,
)
from engine.strategy.option_resolver import (
    LegacySessionExpirationResolver,
    OptionContractCandidate,
    ResolvedOptionContract,
    resolve_legacy_option,
    validate_candidate_identity,
)


class ReplaySessionConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: str
    cell_id: UUID
    broker_account_id: UUID
    session_open: datetime
    session_close: datetime
    execution_authorized_for_replay: bool
    strategy_id: str = "EMA-CROSS-001"
    strategy_version: str = "1.0.0"
    environment: str = "PAPER"
    initial_cash_usd: Decimal = Field(default=Decimal("100.00"), ge=0)

    @model_validator(mode="after")
    def timestamps_are_aware(self) -> "ReplaySessionConfig":
        if any(
            item.tzinfo is None or item.utcoffset() is None
            for item in (self.session_open, self.session_close)
        ):
            raise ValueError("replay session timestamps must be timezone-aware")
        if self.session_close <= self.session_open:
            raise ValueError("replay session_close must be after session_open")
        return self


class ReplayOptionCandidate(BaseModel):
    """Source-supported option-chain row, not a preselected strategy contract."""

    model_config = ConfigDict(frozen=True)

    instrument_id: UUID
    underlying_symbol: str = Field(min_length=1)
    expiration_date: date
    strike_price: Decimal = Field(gt=0)
    option_right: OptionRight
    contract_symbol: str
    contract_multiplier: Decimal = Field(gt=0)
    listing_type: str = Field(min_length=1)
    bid: Decimal
    ask: Decimal
    bid_size: Decimal = Field(ge=0)
    ask_size: Decimal = Field(ge=0)
    volume: int | None = Field(default=None, ge=0)
    open_interest: int | None = Field(default=None, ge=0)

    def resolver_candidate(self) -> OptionContractCandidate:
        return OptionContractCandidate(
            instrument_id=self.instrument_id,
            underlying_symbol=self.underlying_symbol,
            expiration_date=self.expiration_date,
            strike_price=self.strike_price,
            option_right=self.option_right,
            contract_symbol=self.contract_symbol,
            contract_multiplier=self.contract_multiplier,
            listing_type=self.listing_type,
            bid=self.bid,
            ask=self.ask,
            volume=self.volume,
            open_interest=self.open_interest,
        )


class ReplayOptionChainEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    timestamp: datetime
    underlying_symbol: str = Field(min_length=1)
    underlying_instrument_id: UUID
    candidates: tuple[ReplayOptionCandidate, ...]

    @model_validator(mode="after")
    def valid_chain(self) -> "ReplayOptionChainEvent":
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("option-chain timestamp must be timezone-aware")
        if any(
            item.underlying_symbol != self.underlying_symbol
            for item in self.candidates
        ):
            raise ValueError("option chain cannot mix underlying symbols")
        return self


@dataclass(frozen=True)
class LegacyReplayInput:
    provider: LegacyReplayProvider
    observations: tuple[SampledPriceObservation, ...]
    option_chains: tuple[ReplayOptionChainEvent, ...] = ()


@dataclass(frozen=True)
class ResearchReplayInput:
    provider: ResearchReplayProvider
    events: tuple[ResearchMarketEvent, ...]
    option_chains: tuple[ReplayOptionChainEvent, ...] = ()


class ReplayRunResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    manifest_hash: str
    manifest_id: UUID
    financial_ids: tuple[UUID, ...]
    event_count: int
    lineage: tuple[MarketDataLineage, ...]


@dataclass(frozen=True)
class _NormalizedMarketEvent:
    instrument_id: UUID
    symbol: str
    timestamp: datetime
    mark_price: Decimal
    completed_close: Decimal | None
    lineage: MarketDataLineage
    source_payload: dict[str, Any]
    option_chain: ReplayOptionChainEvent | None
    stable_order: int


class _DatabaseInstrumentLookup:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, instrument_id: UUID) -> CanonicalInstrument | None:
        row = self.session.get(Instrument, instrument_id)
        if row is None or row.retired_at is not None:
            return None
        return CanonicalInstrument(
            instrument_id=row.instrument_id,
            symbol=row.symbol,
            asset_class=row.asset_class,
            currency=row.currency,
            exchange=row.exchange,
            underlying_symbol=row.underlying_symbol,
            contract_symbol=row.contract_symbol,
            expiration_date=row.expiration_date,
            strike_price=row.strike_price,
            option_right=row.option_right,
            contract_multiplier=row.contract_multiplier,
            listing_type=row.listing_type,
            effective_from=row.effective_from,
            retired_at=row.retired_at,
        )


class ReplayOrchestrator:
    def __init__(self, session: Session, config: ReplaySessionConfig) -> None:
        self.session = session
        self.config = config
        self.clock = VirtualClock(config.session_open)
        self.identities = ReplayIdentityFactory(config.session_id)
        self.governor = RiskGovernor(
            session, cell_id=config.cell_id, clock=self.clock, identities=self.identities
        )
        self.paper = PaperExecutionEngine(
            session,
            PaperEngineConfig(broker_account_id=config.broker_account_id),
            clock=self.clock,
            identities=self.identities,
        )
        self.strategy = EMACrossStrategy(settled_cash=config.initial_cash_usd)
        self.expirations = LegacySessionExpirationResolver(
            session_date=config.session_open.date()
        )
        self.canonical_lookup = _DatabaseInstrumentLookup(session)
        self._snapshot_ids: list[UUID] = []
        self._intent_ids: list[UUID] = []
        self._event_count = 0

    def initialize(self) -> None:
        state = self.governor.initialize_session(
            RiskSessionSpec(
                session_id=self.config.session_id,
                trading_date=self.config.session_open.date(),
                session_open=self.config.session_open,
                session_close=self.config.session_close,
            )
        )
        if (
            state.operational_state == "DISARMED"
            and self.config.execution_authorized_for_replay
        ):
            self.governor.arm(authorized_cash_usd=self.config.initial_cash_usd)

    def replay_legacy(
        self, streams: tuple[LegacyReplayInput, ...]
    ) -> ReplayRunResult:
        normalized: list[_NormalizedMarketEvent] = []
        lineages: list[MarketDataLineage] = []
        all_chains: list[ReplayOptionChainEvent] = []
        stable_order = 0
        for stream in streams:
            result = stream.provider.replay(stream.observations)
            lineage = result.lineage
            lineages.append(lineage)
            chain_by_time = self._chain_index(stream.option_chains, lineage.symbol, lineage.instrument_id)
            all_chains.extend(stream.option_chains)
            completed_by_time = {
                item.completed_at: item for item in result.completed_minutes
            }
            for observation in result.observations:
                self._require_aware(observation.timestamp)
                completed = completed_by_time.get(observation.timestamp)
                normalized.append(
                    _NormalizedMarketEvent(
                        instrument_id=observation.instrument_id,
                        symbol=observation.symbol,
                        timestamp=observation.timestamp,
                        mark_price=observation.price,
                        completed_close=completed.close if completed else None,
                        lineage=lineage,
                        source_payload={
                            "sampled_price": _decimal_text(observation.price),
                            "completed_minute_start": (
                                completed.minute_start.isoformat() if completed else None
                            ),
                            "source_observation_timestamp": (
                                completed.source_observation_timestamp.isoformat()
                                if completed
                                else None
                            ),
                        },
                        option_chain=chain_by_time.get(observation.timestamp),
                        stable_order=stable_order,
                    )
                )
                stable_order += 1
        return self._run_normalized(normalized, lineages, all_chains)

    def replay_research(
        self, streams: tuple[ResearchReplayInput, ...]
    ) -> ReplayRunResult:
        normalized: list[_NormalizedMarketEvent] = []
        lineages: list[MarketDataLineage] = []
        all_chains: list[ReplayOptionChainEvent] = []
        stable_order = 0
        for stream in streams:
            result = stream.provider.ingest(stream.events)
            lineage = result.lineage
            lineages.append(lineage)
            chain_by_time = self._chain_index(stream.option_chains, lineage.symbol, lineage.instrument_id)
            all_chains.extend(stream.option_chains)
            for source_event in result.events:
                self._require_aware(source_event.timestamp)
                mark_price, completed_close = self._research_prices(source_event)
                normalized.append(
                    _NormalizedMarketEvent(
                        instrument_id=source_event.instrument_id,
                        symbol=source_event.symbol,
                        timestamp=source_event.timestamp,
                        mark_price=mark_price,
                        completed_close=completed_close,
                        lineage=lineage,
                        source_payload=source_event.model_dump(mode="json"),
                        option_chain=chain_by_time.get(source_event.timestamp),
                        stable_order=stable_order,
                    )
                )
                stable_order += 1
        return self._run_normalized(normalized, lineages, all_chains)

    def _run_normalized(
        self,
        events: list[_NormalizedMarketEvent],
        lineages: list[MarketDataLineage],
        chains: list[ReplayOptionChainEvent],
    ) -> ReplayRunResult:
        self._prime_expirations(chains)
        self.initialize()
        for event in sorted(events, key=lambda item: (item.timestamp, item.stable_order)):
            self._process_event(event)
        manifest_hash, ids = self.build_manifest()
        manifest_id = self._persist_manifest(manifest_hash, ids)
        return ReplayRunResult(
            manifest_hash=manifest_hash,
            manifest_id=manifest_id,
            financial_ids=ids,
            event_count=self._event_count,
            lineage=tuple(lineages),
        )

    def _persist_manifest(self, manifest_hash: str, ids: tuple[UUID, ...]) -> UUID:
        identity = (
            f"kairo:synthetic-evidence:REPLAY_RUN:{self.config.cell_id}:"
            f"{self.config.session_id}:{manifest_hash}"
        )
        manifest_id = uuid5(NAMESPACE_URL, identity)
        existing = self.session.get(SyntheticEvidenceManifest, manifest_id)
        values = {
            "manifest_type": "REPLAY_RUN",
            "manifest_hash": manifest_hash,
            "manifest_algorithm": "REPLAY-MANIFEST-v1",
            "cell_id": self.config.cell_id,
            "source_count": len(ids),
            "source_refs": {"financial_ids": [str(item) for item in ids]},
            "model_identifier": self.config.strategy_id,
            "model_version": self.config.strategy_version,
            "created_at": self.config.session_close,
        }
        if existing is None:
            self.session.add(SyntheticEvidenceManifest(manifest_id=manifest_id, **values))
            self.session.flush()
        elif any(getattr(existing, key) != value for key, value in values.items()):
            raise ValueError("deterministic manifest identity conflicts with persisted evidence")
        return manifest_id

    @staticmethod
    def _require_aware(timestamp: datetime) -> None:
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("replay market event timestamp must be timezone-aware")

    @staticmethod
    def _chain_index(
        chains: tuple[ReplayOptionChainEvent, ...], symbol: str, instrument_id: UUID
    ) -> dict[datetime, ReplayOptionChainEvent]:
        index: dict[datetime, ReplayOptionChainEvent] = {}
        for chain in chains:
            if chain.underlying_symbol != symbol:
                raise ValueError("option chain does not match replay stream symbol")
            if chain.underlying_instrument_id != instrument_id:
                raise ValueError("option chain does not match replay stream instrument")
            if chain.timestamp in index:
                raise ValueError("duplicate option-chain timestamp for replay stream")
            index[chain.timestamp] = chain
        return index

    @staticmethod
    def _research_prices(event: ResearchMarketEvent) -> tuple[Decimal, Decimal | None]:
        if event.kind is ResearchEventKind.BAR:
            assert event.close is not None
            return event.close, event.close
        if event.kind in {ResearchEventKind.TICK, ResearchEventKind.TRADE}:
            assert event.price is not None
            return event.price, None
        assert event.bid is not None and event.ask is not None
        return (event.bid + event.ask) / Decimal("2"), None

    def _prime_expirations(self, chains: list[ReplayOptionChainEvent]) -> None:
        first_by_symbol: dict[str, ReplayOptionChainEvent] = {}
        for chain in sorted(chains, key=lambda item: item.timestamp):
            if chain.candidates:
                first_by_symbol.setdefault(chain.underlying_symbol, chain)
        for symbol, chain in first_by_symbol.items():
            self.expirations.resolve(
                symbol,
                tuple(item.expiration_date for item in chain.candidates),
            )

    def _process_event(self, event: _NormalizedMarketEvent) -> None:
        self.clock.advance_to(event.timestamp)
        self._event_count += 1
        self._persist_underlying_snapshot(event)
        execution_quotes = self._persist_option_chain(event.option_chain)

        # Liquidation marks and Governor boundaries always precede strategy work.
        self._record_marks(event)
        if event.completed_close is None:
            return
        contracts = {
            right: self._resolve_contract(
                symbol=event.symbol,
                option_right=right,
                spot_price=event.completed_close,
                chain=event.option_chain,
            )
            for right in (OptionRight.CALL, OptionRight.PUT)
        }
        position = self.strategy.positions.get(event.symbol)
        position_bid = None
        if position is not None and event.option_chain is not None:
            candidate = self._candidate_by_id(
                event.option_chain, position.instrument_id
            )
            position_bid = candidate.bid if candidate is not None else None
        signal = self.strategy.on_bar(
            symbol=event.symbol,
            close=event.completed_close,
            timestamp=self.clock.now(),
            call_contract=contracts[OptionRight.CALL],
            put_contract=contracts[OptionRight.PUT],
            position_quote_bid=position_bid,
        )
        if signal is None:
            return
        execution_quote = execution_quotes.get(signal.instrument_id)
        if execution_quote is None:
            return
        self._route_signal(signal, execution_quote)

    def _persist_underlying_snapshot(self, event: _NormalizedMarketEvent) -> None:
        snapshot_id = self.identities.generate_id(
            "market_snapshot",
            event.instrument_id,
            self.clock.now(),
            parent_id=f"event:{self._event_count}",
        )
        self.session.add(
            MarketSnapshot(
                snapshot_id=snapshot_id,
                instrument_id=event.instrument_id,
                captured_at=self.clock.now(),
                last=event.mark_price,
                payload={
                    "source": "REPLAY_ORCHESTRATOR",
                    "replay_mode": event.lineage.replay_mode.value,
                    "source_id": event.lineage.source_id,
                    "source_kind": event.lineage.source_kind,
                    "exact_prototype_replay": event.lineage.exact_prototype_replay,
                    "transformation": event.lineage.transformation,
                    "completed_close": (
                        _decimal_text(event.completed_close)
                        if event.completed_close is not None
                        else None
                    ),
                    "source_payload": event.source_payload,
                },
            )
        )
        self._snapshot_ids.append(snapshot_id)
        self.session.flush()

    def _persist_option_chain(
        self, chain: ReplayOptionChainEvent | None
    ) -> dict[UUID, ExecutionQuote]:
        if chain is None:
            return {}
        quotes: dict[UUID, ExecutionQuote] = {}
        for candidate in chain.candidates:
            snapshot_id = self.identities.generate_id(
                "market_snapshot",
                candidate.instrument_id,
                self.clock.now(),
                parent_id=f"event:{self._event_count}",
            )
            self.session.add(
                MarketSnapshot(
                    snapshot_id=snapshot_id,
                    instrument_id=candidate.instrument_id,
                    captured_at=self.clock.now(),
                    bid=candidate.bid if candidate.bid > 0 else None,
                    ask=candidate.ask if candidate.ask > 0 else None,
                    last=(
                        (candidate.bid + candidate.ask) / Decimal("2")
                        if candidate.bid > 0 and candidate.ask > 0
                        else None
                    ),
                    payload={
                        "source": "REPLAY_OPTION_CHAIN",
                        "bid_size": _decimal_text(candidate.bid_size),
                        "ask_size": _decimal_text(candidate.ask_size),
                        "volume": candidate.volume,
                        "open_interest": candidate.open_interest,
                        "expiration_date": candidate.expiration_date.isoformat(),
                        "strike_price": _decimal_text(candidate.strike_price),
                        "option_right": candidate.option_right.value,
                    },
                )
            )
            self._snapshot_ids.append(snapshot_id)
            if candidate.bid > 0 and candidate.ask > 0:
                quotes[candidate.instrument_id] = ExecutionQuote(
                    snapshot_id=snapshot_id,
                    instrument_id=candidate.instrument_id,
                    bid=candidate.bid,
                    ask=candidate.ask,
                    bid_size=candidate.bid_size,
                    ask_size=candidate.ask_size,
                    captured_at=self.clock.now(),
                    fidelity_tier=LiquidityFidelityTier.TIER_1_QUOTE_DEPTH,
                )
        self.session.flush()
        return quotes

    def _record_marks(self, event: _NormalizedMarketEvent) -> None:
        marks = [(event.instrument_id, event.mark_price)]
        if event.option_chain is not None:
            for position in self._position_snapshots():
                candidate = self._candidate_by_id(
                    event.option_chain, position.instrument_id
                )
                if candidate is None:
                    continue
                self._validate_candidate(candidate)
                liquidation_mark = (
                    candidate.bid if position.quantity > 0 else candidate.ask
                )
                if liquidation_mark > 0:
                    marks.append((position.instrument_id, liquidation_mark))
        for instrument_id, price in marks:
            self.governor.record_market_mark(
                MarketMark(
                    instrument_id=instrument_id,
                    mark_price=price,
                    source_timestamp=self.clock.now(),
                    received_at=self.clock.now(),
                ),
                positions=self._position_snapshots(),
                authorized_cash_usd=self.config.initial_cash_usd,
            )

    def _resolve_contract(
        self,
        *,
        symbol: str,
        option_right: OptionRight,
        spot_price: Decimal,
        chain: ReplayOptionChainEvent | None,
    ) -> StrategyContract | None:
        if chain is None or not chain.candidates:
            return None
        expiration = self.expirations.resolve(
            symbol,
            tuple(item.expiration_date for item in chain.candidates),
        )
        resolved = resolve_legacy_option(
            candidates=tuple(item.resolver_candidate() for item in chain.candidates),
            underlying_symbol=symbol,
            expiration_date=expiration,
            option_right=option_right,
            spot_price=spot_price,
            canonical_lookup=self.canonical_lookup,
        )
        return self._strategy_contract(resolved) if resolved is not None else None

    @staticmethod
    def _strategy_contract(resolved: ResolvedOptionContract) -> StrategyContract:
        return StrategyContract(
            instrument_id=resolved.instrument_id,
            underlying_symbol=resolved.underlying_symbol,
            option_right=resolved.option_right,
            bid=resolved.bid,
            ask=resolved.ask,
            contract_multiplier=resolved.contract_multiplier,
        )

    @staticmethod
    def _candidate_by_id(
        chain: ReplayOptionChainEvent, instrument_id: UUID
    ) -> ReplayOptionCandidate | None:
        return next(
            (item for item in chain.candidates if item.instrument_id == instrument_id),
            None,
        )

    def _validate_candidate(self, candidate: ReplayOptionCandidate) -> None:
        canonical = self.canonical_lookup.get(candidate.instrument_id)
        if canonical is None:
            raise ValueError("option-chain instrument is absent or retired")
        validate_candidate_identity(candidate.resolver_candidate(), canonical)

    def _route_signal(
        self, signal: StrategyOrderSignal, quote: ExecutionQuote
    ) -> None:
        intent_id = self.identities.generate_id(
            "order_intent",
            signal.instrument_id,
            self.clock.now(),
            parent_id=f"strategy:{signal.underlying_symbol}",
        )
        intent = OrderIntent(
            intent_id=intent_id,
            cell_id=self.config.cell_id,
            strategy_id=self.config.strategy_id,
            strategy_version=self.config.strategy_version,
            instrument_id=signal.instrument_id,
            client_order_key=f"replay:{self.config.session_id}:{intent_id}",
            order_purpose=signal.order_purpose.value,
            side=signal.side.value,
            target_quantity=signal.quantity,
            order_type="LIMIT",
            limit_price=signal.limit_price,
            created_at=self.clock.now(),
        )
        self.session.add(intent)
        self._intent_ids.append(intent_id)
        self.session.flush()
        mark_price = quote.ask if signal.side.value == "BUY" else quote.bid
        instrument = self.session.get(Instrument, signal.instrument_id)
        if instrument is None:
            raise ValueError("strategy signal lacks canonical instrument identity")
        position = self._position_for_instrument(signal.instrument_id)
        result = self.governor.evaluate(
            RiskEvaluationRequest(
                intent=IntentEvaluationInput(
                    intent_id=intent_id,
                    cell_id=self.config.cell_id,
                    strategy_id=self.config.strategy_id,
                    strategy_version=self.config.strategy_version,
                    instrument_id=signal.instrument_id,
                    order_purpose=signal.order_purpose,
                    side=signal.side,
                    target_quantity=signal.quantity,
                    order_type="LIMIT",
                ),
                broker_account_id=self.config.broker_account_id,
                instrument=InstrumentRiskProfile(
                    instrument_id=signal.instrument_id,
                    asset_class=instrument.asset_class,
                    contract_multiplier=instrument.contract_multiplier,
                ),
                capability=None,
                current_position=self._snapshot(position, instrument),
                market_mark=MarketMark(
                    instrument_id=signal.instrument_id,
                    mark_price=mark_price,
                    source_timestamp=self.clock.now(),
                    received_at=self.clock.now(),
                ),
                strategy_clearance=StrategyClearance.PAPER_ONLY,
                execution_environment=ExecutionEnvironment.PAPER,
                authorized_trading_cash=self.config.initial_cash_usd,
                authorized_exposure_usd=self.config.initial_cash_usd,
                current_exposure_usd=Decimal("0"),
            )
        )
        if result.verdict is not DecisionVerdict.AUTHORIZED:
            return
        order_id = self.identities.generate_id(
            "kairo_order",
            signal.instrument_id,
            self.clock.now(),
            parent_id=intent_id,
        )
        self.session.add(
            KairoOrder(
                kairo_order_id=order_id,
                intent_id=intent_id,
                risk_decision_id=result.decision_id,
                broker_account_id=self.config.broker_account_id,
                status="PENDING_SUBMIT",
                submitted_at=self.clock.now(),
            )
        )
        self.session.flush()
        receipt = asyncio.run(self.paper.submit_order(order_id, quote))
        if receipt.fill_records:
            self._apply_fills(signal.underlying_symbol, receipt.fill_records)

    def _apply_fills(
        self, underlying_symbol: str, fills: list[SimulatedFillPayload]
    ) -> None:
        for fill in fills:
            position = self._position_for_instrument(fill.instrument_id)
            realized = Decimal("0")
            position_effect = "OPENING"
            if fill.side == "BUY":
                if position is None:
                    position = CurrentPosition(
                        position_id=self.identities.generate_id(
                            "current_position",
                            fill.instrument_id,
                            self.clock.now(),
                            parent_id=fill.fill_id,
                        ),
                        cell_id=self.config.cell_id,
                        broker_account_id=self.config.broker_account_id,
                        instrument_id=fill.instrument_id,
                        quantity=fill.quantity,
                        average_price=fill.fill_price,
                        updated_at=self.clock.now(),
                    )
                    self.session.add(position)
                else:
                    total = position.quantity + fill.quantity
                    position.average_price = (
                        position.average_price * position.quantity
                        + fill.fill_price * fill.quantity
                    ) / total
                    position.quantity = total
                    position.updated_at = self.clock.now()
                instrument = self.session.get(Instrument, fill.instrument_id)
                if instrument is None:
                    raise ValueError("fill lacks canonical instrument identity")
                self.strategy.record_open(
                    StrategyPosition(
                        instrument_id=fill.instrument_id,
                        underlying_symbol=underlying_symbol,
                        option_right=OptionRight(instrument.option_right),
                        quantity=fill.quantity,
                        entry_price=fill.fill_price,
                        contract_multiplier=fill.contract_multiplier,
                    )
                )
            elif position is not None:
                position_effect = "CLOSING"
                realized = realized_round_trip_pnl(
                    entry_price=position.average_price,
                    exit_price=fill.fill_price,
                    quantity=fill.quantity,
                    contract_multiplier=fill.contract_multiplier,
                    position_side="LONG",
                )
                position.quantity -= fill.quantity
                position.updated_at = self.clock.now()
                self.strategy.record_close(
                    underlying_symbol, realized_pnl=realized
                )
            self.session.add(
                FillRealizedPnL(
                    realization_id=self.identities.generate_id(
                        "fill_realized_pnl",
                        fill.instrument_id,
                        self.clock.now(),
                        parent_id=fill.fill_id,
                    ),
                    fill_id=fill.fill_id,
                    cell_id=self.config.cell_id,
                    position_effect=position_effect,
                    realized_pnl_usd=realized,
                    source_authority="KAIRO_PNL_TRACKER",
                    occurred_at=self.clock.now(),
                )
            )
            self.session.flush()
            self.governor.record_market_mark(
                MarketMark(
                    instrument_id=fill.instrument_id,
                    mark_price=fill.fill_price,
                    source_timestamp=self.clock.now(),
                    received_at=self.clock.now(),
                ),
                positions=self._position_snapshots(),
                authorized_cash_usd=self.config.initial_cash_usd,
            )
            self.governor.record_fill_accounting(
                FillAccountingEvent(
                    fill_id=fill.fill_id,
                    kairo_order_id=fill.kairo_order_id,
                    broker_account_id=fill.broker_account_id,
                    instrument_id=fill.instrument_id,
                    realized_pnl_delta_usd=realized,
                    commission_fees_usd=fill.commission_fee_usd,
                    slippage_usd=fill.slippage_usd,
                    fill_price=fill.fill_price,
                    filled_qty=fill.quantity,
                    timestamp=self.clock.now(),
                ),
                authorized_cash_usd=self.config.initial_cash_usd,
                open_positions=self._position_snapshots(),
            )

    def _position_for_instrument(self, instrument_id: UUID) -> CurrentPosition | None:
        return self.session.scalar(
            select(CurrentPosition).where(
                CurrentPosition.cell_id == self.config.cell_id,
                CurrentPosition.broker_account_id == self.config.broker_account_id,
                CurrentPosition.instrument_id == instrument_id,
                CurrentPosition.quantity != 0,
            )
        )

    def _position_snapshots(self) -> list[PositionSnapshot]:
        rows = self.session.execute(
            select(CurrentPosition, Instrument)
            .join(Instrument, Instrument.instrument_id == CurrentPosition.instrument_id)
            .where(
                CurrentPosition.cell_id == self.config.cell_id,
                CurrentPosition.broker_account_id == self.config.broker_account_id,
                CurrentPosition.quantity != 0,
            )
        ).all()
        return [self._snapshot(position, instrument) for position, instrument in rows]

    @staticmethod
    def _snapshot(
        position: CurrentPosition | None, instrument: Instrument
    ) -> PositionSnapshot | None:
        if position is None:
            return None
        return PositionSnapshot(
            position_id=position.position_id,
            cell_id=position.cell_id,
            broker_account_id=position.broker_account_id,
            instrument_id=position.instrument_id,
            quantity=position.quantity,
            average_price=position.average_price,
            contract_multiplier=instrument.contract_multiplier or Decimal("1"),
        )

    def build_manifest(self) -> tuple[str, tuple[UUID, ...]]:
        models_and_rows = [
            (
                "market_snapshots",
                [self.session.get(MarketSnapshot, item) for item in self._snapshot_ids],
            ),
            (
                "order_intents",
                [self.session.get(OrderIntent, item) for item in self._intent_ids],
            ),
        ]
        decisions = list(
            self.session.scalars(
                select(RiskDecision).where(
                    RiskDecision.session_id == self.config.session_id
                )
            )
        )
        orders = (
            list(
                self.session.scalars(
                    select(KairoOrder).where(
                        KairoOrder.intent_id.in_(self._intent_ids)
                    )
                )
            )
            if self._intent_ids
            else []
        )
        order_ids = [item.kairo_order_id for item in orders]
        models_and_rows.extend(
            [
                ("risk_decisions", decisions),
                ("kairo_orders", orders),
                (
                    "order_observations",
                    list(
                        self.session.scalars(
                            select(OrderObservation).where(
                                OrderObservation.kairo_order_id.in_(order_ids)
                            )
                        )
                    )
                    if order_ids
                    else [],
                ),
                (
                    "fills",
                    list(
                        self.session.scalars(
                            select(Fill).where(Fill.kairo_order_id.in_(order_ids))
                        )
                    )
                    if order_ids
                    else [],
                ),
                (
                    "fill_realized_pnl",
                    list(
                        self.session.scalars(
                            select(FillRealizedPnL).where(
                                FillRealizedPnL.fill_id.in_(
                                    select(Fill.fill_id).where(
                                        Fill.kairo_order_id.in_(order_ids)
                                    )
                                )
                            )
                        )
                    )
                    if order_ids
                    else [],
                ),
                (
                    "risk_state_events",
                    list(
                        self.session.scalars(
                            select(RiskStateEvent).where(
                                RiskStateEvent.session_id == self.config.session_id
                            )
                        )
                    ),
                ),
                (
                    "risk_instrument_marks",
                    list(
                        self.session.scalars(
                            select(RiskInstrumentMark).where(
                                RiskInstrumentMark.session_id
                                == self.config.session_id
                            )
                        )
                    ),
                ),
                (
                    "current_positions",
                    list(
                        self.session.scalars(
                            select(CurrentPosition).where(
                                CurrentPosition.cell_id == self.config.cell_id
                            )
                        )
                    ),
                ),
                (
                    "risk_sessions",
                    [self.session.get(RiskSession, self.config.session_id)],
                ),
                ("risk_governor_state", [self.session.get(RiskGovernorState, self.config.cell_id)]),
            ]
        )
        manifest: list[dict[str, Any]] = []
        financial_ids: list[UUID] = []
        for table, rows in models_and_rows:
            for row in rows:
                if row is None:
                    continue
                values = {
                    column.name: _json_value(getattr(row, column.name))
                    for column in row.__table__.columns
                }
                manifest.append({"table": table, "values": values})
                for column in row.__table__.primary_key.columns:
                    value = getattr(row, column.name)
                    if isinstance(value, UUID):
                        financial_ids.append(value)
        manifest.sort(
            key=lambda item: (
                item["table"],
                json.dumps(item["values"], sort_keys=True),
            )
        )
        encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest(), tuple(
            sorted(set(financial_ids), key=str)
        )


def _json_value(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return _decimal_text(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _decimal_text(value: Decimal) -> str:
    """Canonical plain-decimal text independent of PostgreSQL NUMERIC scale."""

    if value == 0:
        return "0"
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"
