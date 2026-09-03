import hashlib
import importlib.util
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from thetadata.errors import NoDataFoundError

from app.domain.enums import OptionRight
from engine.data.corpus_qualifier import CorpusQualificationEngine, PilotDecisionPoint
from engine.data.option_enrollment import CanonicalResolutionAccounting
from engine.data.provider_adapter import ProviderArtifactType, ThetaDataProviderAdapter
from engine.data.theta_v3 import (
    DecodedThetaSection,
    ProviderMissingEvidence,
    ThetaDataV3ClientTransport,
    ThetaDecodedArtifactReader,
    ThetaDecodedArtifactSerializer,
)
from engine.validation.feed_loader import DataNormalizer


SCRIPT_PATH = Path(__file__).resolve().parents[4] / "scripts" / "data" / "fetch_pilot_corpus.py"
SCRIPT_SPEC = importlib.util.spec_from_file_location("fetch_pilot_corpus_no_data", SCRIPT_PATH)
assert SCRIPT_SPEC is not None and SCRIPT_SPEC.loader is not None
fetch_pilot_corpus = importlib.util.module_from_spec(SCRIPT_SPEC)
SCRIPT_SPEC.loader.exec_module(fetch_pilot_corpus)

SIGNAL = datetime(2024, 1, 10, 15, 0, tzinfo=timezone.utc)


class RowFrame:
    def __init__(self, rows):
        self.rows = rows

    def to_dicts(self):
        return list(self.rows)


class NoDataClient:
    def __init__(self, missing):
        self.missing = set(missing)
        self.calls = []

    def _call(self, endpoint, kwargs, rows):
        self.calls.append((endpoint, kwargs))
        if endpoint in self.missing:
            raise NoDataFoundError(f"no data: {endpoint}")
        return RowFrame(rows)

    def stock_history_ohlc(self, **kwargs):
        return self._call("stock_history_ohlc", kwargs, [])

    def option_list_contracts(self, **kwargs):
        return self._call("option_list_contracts", kwargs, [{"expiration": "2024-01-10"}])

    def option_history_quote(self, **kwargs):
        return self._call("option_history_quote", kwargs, [])

    def option_history_open_interest(self, **kwargs):
        return self._call("option_history_open_interest", kwargs, [])

    def option_history_ohlc(self, **kwargs):
        return self._call("option_history_ohlc", kwargs, [])

    def option_history_trade(self, **kwargs):
        return self._call("option_history_trade", kwargs, [])


def _fetch_options(client):
    adapter = ThetaDataProviderAdapter(
        ThetaDataV3ClientTransport(client, allow_paid_historical_retrieval=True)
    )
    return adapter.fetch_option_neighborhood(symbol="TQQQ", signal_at=SIGNAL)


def _underlying_session(instrument_id):
    return SimpleNamespace(get=lambda _model, _key: SimpleNamespace(
        instrument_id=instrument_id, symbol="TQQQ", asset_class="ETF", retired_at=None
    ))


def test_theta_no_data_found_maps_to_typed_provider_no_data_record():
    artifact = _fetch_options(NoDataClient({"option_list_contracts"}))
    decoded = ThetaDecodedArtifactReader().read_provider_artifact(artifact)

    assert len(decoded.sections) == 1
    missing = decoded.sections[0].missing_evidence
    assert missing == ProviderMissingEvidence(
        provider="THETA_DATA", endpoint="option_list_contracts", symbol="TQQQ",
        session=date(2024, 1, 10), reason_code="PROVIDER_NO_DATA",
        records_count=0, context=decoded.sections[0].parameters,
    )
    assert decoded.sections[0].records == ()


def test_decision_point_scored_incomplete_when_contract_list_returns_no_data():
    instrument_id = uuid4()
    decoded = ThetaDecodedArtifactReader().read_provider_artifact(
        _fetch_options(NoDataClient({"option_list_contracts"}))
    )
    normalizer = DataNormalizer(_underlying_session(instrument_id))
    snapshots = normalizer.normalize_theta_option_sections(
        decoded.sections, underlying_instrument_id=instrument_id, symbol="TQQQ",
        accepted_contract_keys=frozenset(),
    )
    decision = PilotDecisionPoint(
        underlying_instrument_id=instrument_id, symbol="TQQQ",
        signal_at=SIGNAL, underlying_spot=Decimal("50"),
    )

    assert len(snapshots) == 1 and snapshots[0].contracts == ()
    assert CorpusQualificationEngine(SimpleNamespace())._complete_decisions(
        SimpleNamespace(option_snapshots=snapshots, decision_points=(decision,))
    ) == 0


def test_quote_oi_trade_no_data_maps_to_explicit_null_semantics():
    decoded = ThetaDecodedArtifactReader().read_provider_artifact(_fetch_options(NoDataClient({
        "option_history_quote", "option_history_open_interest",
        "option_history_ohlc", "option_history_trade",
    })))
    missing = {section.endpoint: section for section in decoded.sections if section.missing_evidence}
    assert set(missing) == {
        "option_history_quote", "option_history_open_interest",
        "option_history_ohlc", "option_history_trade",
    }
    assert all(section.records == () for section in missing.values())
    normalizer = DataNormalizer(SimpleNamespace())
    key = (date(2024, 1, 10), Decimal("50"), OptionRight.CALL)
    assert normalizer._theta_latest_value(
        decoded.sections, "option_history_open_interest", key,
        date(2024, 1, 10), "open_interest",
    ) is None
    assert normalizer._theta_volume_through(
        decoded.sections, key, date(2024, 1, 10), SIGNAL
    ) is None


def test_network_acquisition_executes_outside_active_db_write_transaction(monkeypatch):
    monkeypatch.setattr(fetch_pilot_corpus, "Session", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("network acquisition opened a database session")
    ))
    adapter = SimpleNamespace(
        fetch_equity_minute_bars=lambda **kwargs: kwargs,
        fetch_option_neighborhoods=lambda **kwargs: kwargs,
    )
    decisions = tuple(PilotDecisionPoint(
        underlying_instrument_id=uuid4(), symbol=symbol, signal_at=SIGNAL,
        underlying_spot=Decimal("50"),
    ) for symbol in ("TQQQ", "SQQQ"))

    assert set(fetch_pilot_corpus.acquire_underlying_evidence(
        adapter, ("TQQQ", "SQQQ"), date(2024, 1, 2), date(2024, 3, 28)
    )) == {"TQQQ", "SQQQ"}
    assert set(fetch_pilot_corpus.acquire_option_evidence(
        adapter, ("TQQQ", "SQQQ"), decisions
    )) == {"TQQQ", "SQQQ"}


def test_underlying_normalization_uses_short_session_and_closes_before_option_network_io(monkeypatch):
    instrument_id = uuid4()
    active = {"session": False}
    events = []

    class ShortSession:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            active["session"] = True
            events.append("open")
            return self

        def __exit__(self, *_args):
            active["session"] = False
            events.append("close")

        def scalar(self, _query):
            return SimpleNamespace(instrument_id=instrument_id, symbol="TQQQ", asset_class="ETF", retired_at=None)

        def get(self, _model, _key):
            return self.scalar(None)

    monkeypatch.setattr(fetch_pilot_corpus, "Session", ShortSession)
    content = ThetaDecodedArtifactSerializer().serialize([
        DecodedThetaSection("stock_history_ohlc", {"symbol": "TQQQ"}, [{
            "timestamp": datetime(2024, 1, 10, 14, 30, tzinfo=timezone.utc),
            "open": Decimal("50"), "high": Decimal("50"), "low": Decimal("50"),
            "close": Decimal("50"), "volume": 1,
        }])
    ], acquisition_request={"symbol": "TQQQ"})
    response = SimpleNamespace(
        artifact_type=ProviderArtifactType.DECODED_PROVIDER_ARTIFACT,
        format_version=ThetaDecodedArtifactSerializer.FORMAT_VERSION,
        serializer_version=ThetaDecodedArtifactSerializer.SERIALIZER_VERSION,
        content=content, content_sha256=hashlib.sha256(content).hexdigest(),
        registry_raw_fields=lambda: {"raw_bytes": content, "raw_mime_type": "test"},
    )

    _, bars, _ = fetch_pilot_corpus.normalize_underlying_evidence(
        object(), fetch_pilot_corpus.SessionCalendarResolver(), ("TQQQ",), {"TQQQ": response}
    )
    assert events == ["open", "close"] and bars
    adapter = SimpleNamespace(fetch_option_neighborhoods=lambda **_kwargs: (
        (_ for _ in ()).throw(AssertionError("session remained open during option I/O"))
        if active["session"] else response
    ))
    decision = PilotDecisionPoint(
        underlying_instrument_id=instrument_id, symbol="TQQQ", signal_at=SIGNAL,
        underlying_spot=Decimal("50"),
    )
    fetch_pilot_corpus.acquire_option_evidence(adapter, ("TQQQ",), (decision,))


def test_qualification_accounting_and_conservation_remains_exact_under_no_data():
    accounting = CanonicalResolutionAccounting.combine((CanonicalResolutionAccounting(
        discovered_contracts_count=0, resolved_existing_contracts_count=0,
        newly_enrolled_contracts_count=0, resolved_contracts_count=0,
        rejected_contracts_count=0,
    ),))

    assert accounting.discovered_contracts_count == (
        accounting.resolved_existing_contracts_count
        + accounting.newly_enrolled_contracts_count
        + accounting.rejected_contracts_count
    )
    assert accounting.resolution_percentage == Decimal("0.00")
