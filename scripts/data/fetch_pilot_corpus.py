"""Offline-first Phase 4.5 Stage 1 pilot ingestion CLI.

This command never creates a network client. Exact provider responses must be
placed in ``--input-dir`` by an explicitly authorized acquisition process.
"""
import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import UUID

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

BACKEND_ROOT = Path(__file__).resolve().parents[2] / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import get_settings  # noqa: E402
from app.db.models.configuration import Instrument  # noqa: E402
from engine.data.corpus_qualifier import (  # noqa: E402
    CorpusQualificationEngine,
    CorpusQualificationInput,
    PilotDecisionPoint,
)
from engine.data.provider_adapter import ThetaDataProviderAdapter  # noqa: E402
from engine.validation.feed_loader import DataNormalizer, HistoricalDatasetRegistry  # noqa: E402
from engine.validation.models import SourceTimestampConvention, StreamRole  # noqa: E402
from engine.validation.session_calendar import SessionCalendarResolver  # noqa: E402


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ingest and qualify an authorized pilot corpus")
    parser.add_argument("--provider", choices=("theta",), required=True)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--symbols", required=True)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--storage-root", type=Path, default=Path("data/historical-market"))
    parser.add_argument("--output", type=Path, default=Path("corpus-qualification-manifest.json"))
    parser.add_argument("--qualify", action="store_true", required=True)
    return parser.parse_args()


class _FileTransport:
    def __init__(self, root: Path) -> None:
        self.root = root

    def __call__(self, request_kind: str, parameters: dict) -> bytes:
        symbol = parameters["symbol"]
        if request_kind == ThetaDataProviderAdapter.BAR_REQUEST_KIND:
            path = self.root / f"{symbol}.bars.csv"
        else:
            path = self.root / f"{symbol}.options.json"
        if not path.is_file():
            raise FileNotFoundError(f"authorized provider response is absent: {path}")
        return path.read_bytes()


def _instrument(session: Session, symbol: str) -> Instrument:
    row = session.scalar(
        select(Instrument).where(
            Instrument.symbol == symbol,
            Instrument.asset_class != "OPTION",
            Instrument.retired_at.is_(None),
        )
    )
    if row is None:
        raise ValueError(f"canonical underlying instrument is absent: {symbol}")
    return row


def main() -> int:
    args = _arguments()
    calendar = SessionCalendarResolver()
    session_count = len(calendar.sessions(args.start, args.end))
    if session_count < 30 or session_count > 61:
        raise ValueError("Stage 1 pilot window must contain 30 through 61 canonical sessions")
    symbols = tuple(item.strip().upper() for item in args.symbols.split(",") if item.strip())
    if not symbols or len(set(symbols)) != len(symbols):
        raise ValueError("symbols must be a unique non-empty comma-separated list")

    signal_payload = json.loads((args.input_dir / "signals.json").read_bytes())
    decisions = tuple(PilotDecisionPoint.model_validate(item) for item in signal_payload)
    adapter = ThetaDataProviderAdapter(_FileTransport(args.input_dir))
    engine = create_engine(get_settings().runtime_database_url)
    try:
        with Session(engine, expire_on_commit=False) as session, session.begin():
            normalizer = DataNormalizer(session, calendar)
            registry = HistoricalDatasetRegistry(session, args.storage_root)
            bars = []
            snapshots = []
            raw_hashes = []
            streams = []
            ordinal = 0
            for symbol in symbols:
                instrument = _instrument(session, symbol)
                raw_bars = adapter.fetch_equity_minute_bars(
                    symbol=symbol, start_session=args.start, end_session=args.end
                )
                normalized_bars = normalizer.normalize_bars(
                    raw_bars.content,
                    instrument_id=instrument.instrument_id,
                    symbol=symbol,
                    source_timezone="America/New_York",
                    timestamp_convention=SourceTimestampConvention.INTERVAL_BEGIN,
                    bar_interval_seconds=60,
                )
                if not normalized_bars:
                    raise ValueError(f"normalized bar evidence is empty for {symbol}")
                bars.extend(normalized_bars)
                raw_hashes.append(raw_bars.content_sha256)
                streams.append(
                    {
                        "instrument_id": instrument.instrument_id,
                        "symbol": symbol,
                        "stream_role": StreamRole.UNDERLYING_SIGNAL_BARS,
                        "stream_ordinal": ordinal,
                        "raw_bytes": raw_bars.content,
                        "raw_mime_type": raw_bars.mime_type,
                        "normalized_bytes": normalizer.normalized_bytes(normalized_bars),
                        "bar_count": len(normalized_bars),
                        "first_bar_start_at": normalized_bars[0].interval_start_at,
                        "last_bar_completed_at": normalized_bars[-1].completed_at,
                    }
                )
                ordinal += 1

                signal_times = tuple(
                    item.signal_at for item in decisions if item.symbol == symbol
                )
                if not signal_times:
                    raise ValueError(f"option decision evidence is empty for {symbol}")
                response = adapter.fetch_option_neighborhoods(
                    symbol=symbol, signal_times=signal_times
                )
                raw_hashes.append(response.content_sha256)
                symbol_snapshots = list(normalizer.normalize_option_chains(response.content))
                if not symbol_snapshots:
                    raise ValueError(f"normalized option evidence is empty for {symbol}")
                snapshots.extend(symbol_snapshots)
                streams.append(
                    {
                        "instrument_id": instrument.instrument_id,
                        "symbol": symbol,
                        "stream_role": StreamRole.OPTION_CHAIN_QUOTES,
                        "stream_ordinal": ordinal,
                        "raw_bytes": response.content,
                        "raw_mime_type": response.mime_type,
                        "normalized_bytes": normalizer.normalized_bytes(tuple(symbol_snapshots)),
                        "bar_count": len(symbol_snapshots),
                        "first_bar_start_at": symbol_snapshots[0].canonical_completed_at,
                        "last_bar_completed_at": symbol_snapshots[-1].canonical_completed_at,
                    }
                )
                ordinal += 1

            ingested_at = datetime.now(timezone.utc)
            dataset = registry.register_dataset(
                dataset_name=f"THETA-PILOT-{args.start.isoformat()}-{args.end.isoformat()}",
                provider_name="THETA_DATA",
                bar_interval_seconds=60,
                source_timezone="America/New_York",
                source_timestamp_convention="INTERVAL_BEGIN",
                liquidity_fidelity_tier="TIER_1_QUOTE_DEPTH",
                price_adjustment_mode="RAW_UNADJUSTED",
                adjustment_policy_version=None,
                normalization_policy_version="NORM-PILOT-CORPUS-v1",
                ingested_at=ingested_at,
                streams=tuple(streams),
                calendar=calendar,
            )
            qualifier = CorpusQualificationEngine(session, calendar)
            manifest = qualifier.qualify(
                CorpusQualificationInput(
                    provider_code="THETA_DATA",
                    start_session=args.start,
                    end_session=args.end,
                    symbols=symbols,
                    bars=tuple(bars),
                    option_snapshots=tuple(snapshots),
                    decision_points=decisions,
                    raw_artifact_sha256s=tuple(raw_hashes),
                    normalized_dataset_manifest_sha256=dataset.dataset_manifest_sha256,
                )
            )
            qualifier.persist_manifest(registry, manifest, created_at=ingested_at)
            args.output.write_bytes(manifest.canonical_bytes())
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
