"""Authorized Theta Phase 4.5 Stage 1 acquisition and qualification CLI.

The paid transport remains fail-closed unless the operator supplies the explicit
authorization flag. Provider bytes are decoded only by the strict provenance
reader and enter DataNormalizer through typed records, never CSV/JSON shims.
"""
import argparse
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

BACKEND_ROOT = Path(__file__).resolve().parents[2] / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import get_settings  # noqa: E402
from app.db.models.configuration import Instrument  # noqa: E402
from engine.data.corpus_qualifier import CorpusQualificationEngine, CorpusQualificationInput, PilotDecisionPoint  # noqa: E402
from engine.data.provider_adapter import ThetaDataProviderAdapter  # noqa: E402
from engine.data.option_enrollment import (  # noqa: E402
    CanonicalResolutionAccounting,
    HistoricalOptionEnrollmentGate,
)
from engine.data.theta_v3 import ThetaDataV3ClientTransport, ThetaDecodedArtifactReader  # noqa: E402
from engine.strategy.ema_cross_strategy import EMACrossStrategy  # noqa: E402
from engine.validation.feed_loader import DataNormalizer, HistoricalDatasetRegistry  # noqa: E402
from engine.validation.models import CanonicalMarketBar, StreamRole  # noqa: E402
from engine.validation.session_calendar import SessionCalendarResolver  # noqa: E402


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Acquire and qualify an authorized Theta pilot corpus")
    parser.add_argument("--provider", choices=("theta",), required=True)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--symbols", required=True)
    parser.add_argument("--storage-root", type=Path, default=Path("data/historical-market"))
    parser.add_argument("--output", type=Path, default=Path("corpus-qualification-manifest.json"))
    parser.add_argument("--qualify", action="store_true", required=True)
    parser.add_argument(
        "--authorize-paid-theta-history", action="store_true", required=True,
        help="explicitly authorize the paid historical calls made by this invocation",
    )
    return parser.parse_args(argv)


def _instrument(session: Session, symbol: str) -> Instrument:
    row = session.scalar(select(Instrument).where(
        Instrument.symbol == symbol, Instrument.asset_class != "OPTION", Instrument.retired_at.is_(None)
    ))
    if row is None:
        raise ValueError(f"canonical underlying instrument is absent: {symbol}")
    return row


def derive_strategy_001_decisions(
    bars: tuple[CanonicalMarketBar, ...],
) -> tuple[PilotDecisionPoint, ...]:
    """Replay completed bars through the frozen strategy to discover entry crosses."""
    by_session: dict[date, list[CanonicalMarketBar]] = defaultdict(list)
    for bar in bars:
        by_session[bar.completed_at.astimezone(SessionCalendarResolver.eastern).date()].append(bar)
    decisions = []
    for session_date in sorted(by_session):
        strategy = EMACrossStrategy(settled_cash=Decimal("0"))
        for bar in sorted(by_session[session_date], key=lambda item: (item.completed_at, item.symbol)):
            strategy.on_bar(symbol=bar.symbol, close=bar.close, timestamp=bar.completed_at)
            if strategy.last_missing_execution_evidence == "ENTRY_OPTION_QUOTE_MISSING":
                decisions.append(PilotDecisionPoint(
                    underlying_instrument_id=bar.instrument_id, symbol=bar.symbol,
                    signal_at=bar.completed_at, underlying_spot=bar.close,
                ))
    return tuple(decisions)


def main(argv: list[str] | None = None) -> int:
    args = _arguments(argv)
    calendar = SessionCalendarResolver()
    session_count = len(calendar.sessions(args.start, args.end))
    if session_count < 30 or session_count > 61:
        raise ValueError("Stage 1 pilot window must contain 30 through 61 canonical sessions")
    symbols = tuple(item.strip().upper() for item in args.symbols.split(",") if item.strip())
    if symbols != ("TQQQ", "SQQQ"):
        raise ValueError("the frozen Stage 1 Theta pilot requires symbols TQQQ,SQQQ in that order")

    engine = create_engine(get_settings().runtime_database_url)
    try:
        with Session(engine, expire_on_commit=False) as session, session.begin():
            instruments = {symbol: _instrument(session, symbol) for symbol in symbols}
            transport = ThetaDataV3ClientTransport.from_local_environment(
                allow_paid_historical_retrieval=args.authorize_paid_theta_history,
                dotenv_path=BACKEND_ROOT / ".env",
            )
            adapter = ThetaDataProviderAdapter(transport)
            reader = ThetaDecodedArtifactReader()
            normalizer = DataNormalizer(session, calendar)
            registry = HistoricalDatasetRegistry(session, args.storage_root)
            all_bars: list[CanonicalMarketBar] = []
            raw_hashes = []
            streams = []
            enrollment_accounting = []
            ordinal = 0

            for symbol in symbols:
                response = adapter.fetch_equity_minute_bars(
                    symbol=symbol, start_session=args.start, end_session=args.end
                )
                decoded = reader.read_provider_artifact(response)
                normalized = normalizer.normalize_theta_bars(
                    decoded.sections, instrument_id=instruments[symbol].instrument_id, symbol=symbol
                )
                all_bars.extend(normalized)
                raw_hashes.append(decoded.content_sha256)
                streams.append({
                    "instrument_id": instruments[symbol].instrument_id, "symbol": symbol,
                    "stream_role": StreamRole.UNDERLYING_SIGNAL_BARS, "stream_ordinal": ordinal,
                    **response.registry_raw_fields(), "normalized_bytes": normalizer.normalized_bytes(normalized),
                    "bar_count": len(normalized), "first_bar_start_at": normalized[0].interval_start_at,
                    "last_bar_completed_at": normalized[-1].completed_at,
                })
                ordinal += 1

            decisions = derive_strategy_001_decisions(tuple(all_bars))
            all_snapshots = []
            for symbol in symbols:
                signal_times = tuple(item.signal_at for item in decisions if item.symbol == symbol)
                if not signal_times:
                    raise ValueError(f"Strategy 001 produced no pilot decision points for {symbol}")
                response = adapter.fetch_option_neighborhoods(symbol=symbol, signal_times=signal_times)
                decoded = reader.read_provider_artifact(response)
                enrollment = HistoricalOptionEnrollmentGate(session).enroll_theta_sections(
                    decoded.sections,
                    underlying_instrument_id=instruments[symbol].instrument_id,
                    underlying_symbol=symbol,
                    research_replay_mode=True,
                )
                enrollment_accounting.append(enrollment.accounting)
                snapshots = normalizer.normalize_theta_option_sections(
                    decoded.sections,
                    underlying_instrument_id=instruments[symbol].instrument_id,
                    symbol=symbol,
                    accepted_contract_keys=enrollment.accepted_contract_keys,
                )
                all_snapshots.extend(snapshots)
                raw_hashes.append(decoded.content_sha256)
                streams.append({
                    "instrument_id": instruments[symbol].instrument_id, "symbol": symbol,
                    "stream_role": StreamRole.OPTION_CHAIN_QUOTES, "stream_ordinal": ordinal,
                    **response.registry_raw_fields(), "normalized_bytes": normalizer.normalized_bytes(snapshots),
                    "bar_count": len(snapshots), "first_bar_start_at": snapshots[0].canonical_completed_at,
                    "last_bar_completed_at": snapshots[-1].canonical_completed_at,
                })
                ordinal += 1

            ingested_at = datetime.now(timezone.utc)
            dataset = registry.register_dataset(
                dataset_name=f"THETA-PILOT-{args.start.isoformat()}-{args.end.isoformat()}",
                provider_name="THETA_DATA", bar_interval_seconds=60, source_timezone="America/New_York",
                source_timestamp_convention="INTERVAL_BEGIN", liquidity_fidelity_tier="TIER_1_QUOTE_DEPTH",
                price_adjustment_mode="RAW_UNADJUSTED", adjustment_policy_version=None,
                normalization_policy_version="NORM-PILOT-CORPUS-v1", ingested_at=ingested_at,
                streams=tuple(streams), calendar=calendar,
            )
            qualifier = CorpusQualificationEngine(session, calendar)
            manifest = qualifier.qualify(CorpusQualificationInput(
                provider_code="THETA_DATA", start_session=args.start, end_session=args.end,
                symbols=symbols, bars=tuple(all_bars), option_snapshots=tuple(all_snapshots),
                decision_points=decisions, raw_artifact_sha256s=tuple(raw_hashes),
                normalized_dataset_manifest_sha256=dataset.dataset_manifest_sha256,
                resolution_accounting=CanonicalResolutionAccounting.combine(
                    enrollment_accounting
                ),
            ))
            qualifier.persist_manifest(registry, manifest, created_at=ingested_at)
            args.output.write_bytes(manifest.canonical_bytes())
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
