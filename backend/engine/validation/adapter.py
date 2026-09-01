from dataclasses import dataclass

from engine.execution.replay_orchestrator import ReplayOptionCandidate, ReplayOptionChainEvent, ResearchReplayInput
from engine.strategy.market_data import ResearchEventKind, ResearchMarketEvent, ResearchReplayProvider
from engine.validation.models import CanonicalMarketBar, CanonicalOptionChainSnapshot, StreamRole


@dataclass(frozen=True)
class HistoricalReplayAdapter:
    source_id: str

    def bars(self, values: tuple[CanonicalMarketBar, ...], *, stream_role: StreamRole = StreamRole.UNDERLYING_SIGNAL_BARS) -> ResearchReplayInput:
        if stream_role is not StreamRole.UNDERLYING_SIGNAL_BARS:
            raise ValueError("market bars require UNDERLYING_SIGNAL_BARS stream role")
        if not values:
            raise ValueError("cannot adapt an empty bar stream")
        first = values[0]
        provider = ResearchReplayProvider(source_id=self.source_id, source_kind="HISTORICAL_DATASET_BAR", instrument_id=first.instrument_id, symbol=first.symbol)
        events = tuple(ResearchMarketEvent(
            timestamp=item.completed_at, kind=ResearchEventKind.BAR,
            instrument_id=item.instrument_id, symbol=item.symbol, open=item.open,
            high=item.high, low=item.low, close=item.close, volume=item.volume,
        ) for item in values)
        provider.ingest(events)
        return ResearchReplayInput(provider=provider, events=events)

    def option_chains(self, values: tuple[CanonicalOptionChainSnapshot, ...], *, stream_role: StreamRole = StreamRole.OPTION_CHAIN_QUOTES) -> tuple[ReplayOptionChainEvent, ...]:
        if stream_role is not StreamRole.OPTION_CHAIN_QUOTES:
            raise ValueError("option snapshots require OPTION_CHAIN_QUOTES stream role")
        return tuple(ReplayOptionChainEvent(
            timestamp=item.canonical_completed_at, underlying_symbol=item.underlying_symbol,
            underlying_instrument_id=item.underlying_instrument_id,
            candidates=tuple(ReplayOptionCandidate(
                instrument_id=row.contract_instrument_id, underlying_symbol=row.underlying_symbol,
                expiration_date=row.expiration_date, strike_price=row.strike_price,
                option_right=row.option_right, contract_symbol=row.canonical_contract_symbol,
                contract_multiplier=row.contract_multiplier, listing_type=row.listing_type,
                bid=row.bid_price, ask=row.ask_price, bid_size=row.bid_size, ask_size=row.ask_size,
                volume=row.volume, open_interest=row.open_interest,
            ) for row in item.contracts),
        ) for item in values)
