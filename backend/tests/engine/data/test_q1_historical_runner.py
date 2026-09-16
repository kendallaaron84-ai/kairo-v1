from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pyarrow.parquet as pq
import pyarrow as pa
import pytest

from engine.data.dynamic_option_slicer import (
    CompletedUnderlyingBar,
    DynamicOptionEvidenceSlicer,
    ProviderExpirationListing,
    ProviderListedContract,
    ProviderListingSnapshot,
)
from engine.data.thetadata_quote_provider import (
    ThetaDataHistoricalQuoteProvider,
    ThetaQuoteIdentityError,
)
from engine.data import q1_historical_runner as runner


NY = ZoneInfo("America/New_York")
SESSION = date(2024, 1, 2)


class SyntheticUnderlyingSource:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[tuple[str, date]] = []

    def load_session(self, symbol: str, session: date) -> runner.SealedUnderlyingSession:
        self.calls.append((symbol, session))
        path = self.root / f"{symbol}-{session}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(f"sealed:{symbol}:{session}".encode())
        bars = tuple(
            CompletedUnderlyingBar(
                symbol=symbol,
                completed_at=datetime.combine(session, time(9, 31), NY)
                + timedelta(minutes=index),
                close=Decimal("50.00"),
            )
            for index in range(390)
        )
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return runner.SealedUnderlyingSession(bars, path, digest)


class SyntheticListingProvider:
    def __init__(self, paths) -> None:
        self.paths = tuple(paths)

    def discover_listings(self, **kwargs):
        return SimpleNamespace(**kwargs, discovery_succeeded=True, expirations=())


class SyntheticQuoteProvider:
    def __init__(self, outcomes=()) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def acquire_quotes(self, **kwargs):
        self.calls += 1
        if self.outcomes:
            value = self.outcomes.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        return SimpleNamespace(acquisition_succeeded=True, quotes=())


class RowFrame:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.columns = list(_theta_row().keys())

    def to_dicts(self) -> list[dict[str, Any]]:
        return list(self._rows)


class SessionFrameClient:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[dict[str, Any]] = []

    def option_history_quote(self, **kwargs: Any) -> RowFrame:
        self.calls.append(kwargs)
        start = kwargs["start_time"]
        end = kwargs["end_time"]
        selected = [
            row
            for row in self.rows
            if row["symbol"] == kwargs["symbol"]
            and row["expiration"] == kwargs["expiration"].isoformat()
            and Decimal(str(row["strike"])) == Decimal(kwargs["strike"])
            and row["right"].lower() == kwargs["right"]
            and row["timestamp"].date() == kwargs["date"]
            and start <= row["timestamp"].astimezone(NY).time() <= end
        ]
        return RowFrame(selected)

    def option_list_contracts(self, **kwargs: Any) -> None:
        raise AssertionError("contract discovery is prohibited")


def _listed_contract(
    *,
    strike: str = "50",
    right: str = "CALL",
    expiration: date = date(2024, 1, 5),
) -> ProviderListedContract:
    return ProviderListedContract(
        contract_id=f"TQQQ-{expiration:%Y%m%d}-{strike}-{right}",
        expiration_date=expiration,
        strike=Decimal(strike),
        right=right,
    )


def _theta_row(
    *,
    timestamp: datetime = datetime(2024, 1, 2, 9, 31, tzinfo=NY),
    strike: str = "50",
    right: str = "CALL",
    bid: Any = 1.0,
    ask: Any = 1.05,
    bid_size: Any = 3,
    ask_size: Any = 4,
) -> dict[str, Any]:
    return {
        "symbol": "TQQQ",
        "expiration": "2024-01-05",
        "strike": float(strike),
        "right": right,
        "timestamp": timestamp,
        "bid": bid,
        "ask": ask,
        "bid_size": bid_size,
        "ask_size": ask_size,
        "bid_exchange": 10,
        "ask_exchange": 11,
        "bid_condition": 0,
        "ask_condition": 0,
    }


def _quote_signature(snapshot) -> tuple[Any, ...]:
    def raw(value: Any) -> Any:
        return "NaN" if isinstance(value, float) and math.isnan(value) else value

    return (
        snapshot.symbol,
        snapshot.expiration_date,
        snapshot.timestamp,
        snapshot.acquisition_succeeded,
        tuple(
            (
                quote.contract_id,
                quote.expiration_date,
                quote.strike,
                quote.right,
                quote.bid,
                quote.ask,
                quote.provider_timestamp,
                raw(quote.raw_bid),
                raw(quote.raw_ask),
                quote.bid_size,
                quote.ask_size,
                quote.bid_exchange,
                quote.ask_exchange,
                quote.bid_condition,
                quote.ask_condition,
            )
            for quote in snapshot.quotes
        ),
    )


class SyntheticSlicer:
    def __init__(self, provider) -> None:
        self.provider = provider

    def slice_bar(self, bar):
        self.provider.discover_listings(symbol=bar.symbol, completed_at=bar.completed_at)
        quote = self.provider.acquire_quotes(
            symbol=bar.symbol,
            completed_at=bar.completed_at,
            expiration_date=SESSION,
            contracts=(),
        )
        status = getattr(quote, "status", "ENVELOPE_SATISFIED")
        return (
            SimpleNamespace(
                expiration_date=SESSION,
                listing_discovery_succeeded=True,
                quote_acquisition_succeeded=True,
                requested_contract_ids=(),
                qualification=SimpleNamespace(
                    status=status,
                    atm_strike=Decimal("50"),
                    required_strikes=(),
                    reason_codes=() if status == "ENVELOPE_SATISFIED" else ("EXPECTED_QUOTE_MISSING",),
                ),
                acquired_universe=SimpleNamespace(contracts=()),
            ),
        )


@pytest.fixture
def synthetic_components(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "DatabentoListingProvider", SyntheticListingProvider)
    monkeypatch.setattr(runner, "DynamicOptionEvidenceSlicer", SyntheticSlicer)
    source = SyntheticUnderlyingSource(tmp_path / "sealed")
    return source, SyntheticQuoteProvider()


def _prepare_definitions(root: Path, symbols=("SQQQ", "TQQQ")) -> None:
    for symbol in symbols:
        path = root / "definitions" / symbol / f"{symbol}_definition_{SESSION}.dbn"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"definition:{symbol}:{SESSION}".encode())


def _build(root: Path, source, quotes, symbols=("SQQQ", "TQQQ"), sleeper=lambda _: None):
    return runner.Q1HistoricalRunner(
        scratch_root=root,
        underlying_source=source,
        quote_provider=quotes,
        sessions=(SESSION,),
        symbols=symbols,
        sleeper=sleeper,
    )


def _contract_path() -> Path:
    return Path(runner.__file__).resolve().parents[2] / "docs" / "q1-2024-dynamic-acquisition-contract-v1.0.md"


def test_contract_identity_gate_accepts_only_frozen_bytes(tmp_path, synthetic_components):
    assert runner.verify_contract_identity(_contract_path()) == runner.CONTRACT_SHA256

    source, quotes = synthetic_components
    missing_root = tmp_path / "missing-root"
    with pytest.raises(RuntimeError, match="^HARD_STOP_CONTRACT_IDENTITY_MISMATCH$"):
        runner.Q1HistoricalRunner(
            scratch_root=missing_root,
            underlying_source=source,
            quote_provider=quotes,
            sessions=(SESSION,),
            symbols=("SQQQ",),
            contract_path=tmp_path / "missing-contract.md",
        ).run()
    assert not missing_root.exists()
    assert source.calls == []

    mutated = tmp_path / "contract.md"
    mutated.write_bytes(_contract_path().read_bytes() + b"mutation")
    mutated_root = tmp_path / "mutated-root"
    with pytest.raises(RuntimeError, match="^HARD_STOP_CONTRACT_IDENTITY_MISMATCH$"):
        runner.Q1HistoricalRunner(
            scratch_root=mutated_root,
            underlying_source=source,
            quote_provider=quotes,
            sessions=(SESSION,),
            symbols=("SQQQ",),
            contract_path=mutated,
        ).run()
    assert not mutated_root.exists()
    assert source.calls == []


def test_complete_one_day_writes_parquet_receipts_state_and_manifest(
    tmp_path, synthetic_components
):
    source, quotes = synthetic_components
    root = tmp_path / "scratch" / "q1_2024_dynamic"
    _prepare_definitions(root)

    result = _build(root, source, quotes).run()

    assert result is not None
    assert result.completed_cells == 2
    assert result.cumulative_evaluated == 780
    assert result.cumulative_satisfied == 780
    for symbol in ("SQQQ", "TQQQ"):
        partition = root / "intervals" / f"symbol={symbol}" / "date=20240102" / "intervals.parquet"
        parquet = pq.ParquetFile(partition)
        assert parquet.metadata.num_rows == 390
        assert parquet.metadata.num_row_groups == 1
        receipt = json.loads(
            (root / "receipts" / "daily" / f"receipt_{symbol}_2024-01-02.json").read_bytes()
        )
        assert receipt["interval_counts"] == {"evaluated": 390, "failed": 0, "satisfied": 390}
        assert receipt["operational_counters"]["retries"] == 0
    checkpoint = json.loads((root / "state" / "checkpoint.json").read_bytes())
    assert checkpoint["completed_cells_count"] == 2
    assert checkpoint["contract_sha256"] == runner.CONTRACT_SHA256
    assert checkpoint["runner_git_identity"] == runner.current_runner_git_identity()
    assert checkpoint["cell_receipt_sha256"] == checkpoint["completed_cells"][-1]["receipt"]["sha256"]
    assert checkpoint["completed_cells"][0]["partition"]["semantic_sha256"]
    assert checkpoint["completed_cells"][0]["partition"]["semantic_sha256"] != checkpoint["completed_cells"][0]["partition"]["sha256"]
    assert result.manifest_path.read_bytes().endswith(b"\n")
    assert result.manifest_root_sha256 == hashlib.sha256(result.manifest_path.read_bytes()).hexdigest()


def test_checkpoint_resume_skips_completed_cell(tmp_path, synthetic_components):
    source, quotes = synthetic_components
    root = tmp_path / "scratch"
    _prepare_definitions(root)
    first = _build(root, source, quotes)

    assert first.run(stop_after_cells=1) is None
    assert source.calls == [("SQQQ", SESSION)]
    resumed = _build(root, source, quotes).run()

    assert resumed is not None
    assert resumed.completed_cells == 2
    assert source.calls == [("SQQQ", SESSION), ("TQQQ", SESSION)]


@pytest.mark.parametrize("source_kind", ["definition", "underlying"])
def test_resume_rehashes_every_completed_source(tmp_path, synthetic_components, source_kind):
    source, quotes = synthetic_components
    root = tmp_path / "scratch"
    _prepare_definitions(root, symbols=("SQQQ",))
    built = _build(root, source, quotes, symbols=("SQQQ",))
    assert built.run(stop_after_cells=1) is None
    checkpoint = json.loads((root / "state" / "checkpoint.json").read_bytes())
    cell = checkpoint["completed_cells"][0]
    if source_kind == "definition":
        target = root / cell["definition_source"]["relative_path"]
    else:
        target = Path(cell["underlying_source"]["path"])
    target.write_bytes(target.read_bytes() + b"tampered")

    with pytest.raises(runner.ResumeProvenanceError) as raised:
        _build(root, source, quotes, symbols=("SQQQ",)).run()
    assert str(raised.value) == "RESUME_ABORTED_PROVENANCE_MISMATCH"


def test_resume_rejects_runner_identity_mismatch(tmp_path, synthetic_components):
    source, quotes = synthetic_components
    root = tmp_path / "scratch"
    _prepare_definitions(root, symbols=("SQQQ",))
    built = _build(root, source, quotes, symbols=("SQQQ",))
    assert built.run(stop_after_cells=1) is None
    checkpoint_path = root / "state" / "checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_bytes())
    checkpoint["runner_git_identity"] = "0" * 40
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")

    with pytest.raises(runner.ResumeProvenanceError) as raised:
        _build(root, source, quotes, symbols=("SQQQ",)).run()
    assert str(raised.value) == "RESUME_ABORTED_PROVENANCE_MISMATCH"


def test_resume_recomputes_semantic_partition_identity(tmp_path, synthetic_components):
    source, quotes = synthetic_components
    root = tmp_path / "scratch"
    _prepare_definitions(root, symbols=("SQQQ",))
    built = _build(root, source, quotes, symbols=("SQQQ",))
    assert built.run(stop_after_cells=1) is None
    checkpoint_path = root / "state" / "checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_bytes())
    checkpoint["completed_cells"][0]["partition"]["semantic_sha256"] = "0" * 64
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")

    with pytest.raises(runner.ResumeProvenanceError) as raised:
        _build(root, source, quotes, symbols=("SQQQ",)).run()
    assert str(raised.value) == "RESUME_ABORTED_PROVENANCE_MISMATCH"


def test_duplicate_completed_cell_cannot_double_count(tmp_path, synthetic_components):
    source, quotes = synthetic_components
    root = tmp_path / "scratch"
    _prepare_definitions(root, symbols=("SQQQ",))
    built = _build(root, source, quotes, symbols=("SQQQ",))
    assert built.run(stop_after_cells=1) is None
    checkpoint_path = root / "state" / "checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_bytes())
    checkpoint["completed_cells"].append(checkpoint["completed_cells"][0])
    checkpoint["completed_cells_count"] = 2
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")

    with pytest.raises(runner.ResumeProvenanceError):
        _build(root, source, quotes, symbols=("SQQQ",)).run()


@pytest.mark.parametrize("target", ["partition", "checkpoint"])
def test_resume_fails_closed_on_provenance_mismatch(
    tmp_path, synthetic_components, target
):
    source, quotes = synthetic_components
    root = tmp_path / "scratch"
    _prepare_definitions(root, symbols=("SQQQ",))
    built = _build(root, source, quotes, symbols=("SQQQ",))
    assert built.run(stop_after_cells=1) is None
    checkpoint_path = root / "state" / "checkpoint.json"
    if target == "partition":
        partition = root / "intervals" / "symbol=SQQQ" / "date=20240102" / "intervals.parquet"
        partition.write_bytes(partition.read_bytes() + b"tampered")
    else:
        payload = json.loads(checkpoint_path.read_bytes())
        payload["completed_cells"][0]["receipt"]["sha256"] = "0" * 64
        checkpoint_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(runner.ResumeProvenanceError, match="RESUME_ABORTED_PROVENANCE_MISMATCH"):
        _build(root, source, quotes, symbols=("SQQQ",)).run()


def test_early_abort_fires_on_exactly_2380th_failure():
    budget = runner._FailureBudget(evaluated=2_379, satisfied=0)

    with pytest.raises(runner.IrrecoverableCompletenessError) as raised:
        budget.record(False)

    assert raised.value.failed == 2_380
    assert str(raised.value) == "EARLY_ABORT: IRRECOVERABLE_COMPLETENESS_DEFICIT"


def test_2379_failures_remain_eligible():
    budget = runner._FailureBudget(evaluated=2_378, satisfied=0)
    budget.record(False)
    assert budget.failed == 2_379


def test_transport_retries_are_bounded_and_nan_evidence_is_not_retried():
    sleeps: list[float] = []
    transient = runner.ThetaQuoteTransportError("temporary")
    quotes = SyntheticQuoteProvider([transient, transient, transient, transient])
    provider = runner._RetryingSplitProvider(
        SyntheticListingProvider((Path("definition.dbn"),)),
        quotes,
        sleeper=sleeps.append,
    )

    with pytest.raises(runner.ThetaQuoteTransportError):
        provider.acquire_quotes()
    assert quotes.calls == 4
    assert provider.retries == 3
    assert sleeps == [1.0, 2.0, 4.0]

    nan_snapshot = SimpleNamespace(bid=None, ask=None)
    no_retry_quotes = SyntheticQuoteProvider([nan_snapshot])
    no_retry = runner._RetryingSplitProvider(
        SyntheticListingProvider((Path("definition.dbn"),)),
        no_retry_quotes,
        sleeper=sleeps.append,
    )
    assert no_retry.acquire_quotes() is nan_snapshot
    assert no_retry_quotes.calls == 1
    assert no_retry.retries == 0


def test_transient_transport_failure_retries_then_succeeds_with_exact_counter():
    sleeps: list[float] = []
    success = SimpleNamespace(acquisition_succeeded=True, quotes=())
    quotes = SyntheticQuoteProvider(
        [runner.ThetaQuoteTransportError("one"), runner.ThetaQuoteTransportError("two"), success]
    )
    provider = runner._RetryingSplitProvider(
        SyntheticListingProvider((Path("definition.dbn"),)),
        quotes,
        sleeper=sleeps.append,
    )

    assert provider.acquire_quotes() is success
    assert quotes.calls == 3
    assert provider.retries == 2
    assert sleeps == [1.0, 2.0]


@pytest.mark.parametrize(
    "rows,completed_at",
    [
        ([_theta_row(bid=1.00, ask=1.05, bid_size=7, ask_size=9)], datetime(2024, 1, 2, 9, 31, tzinfo=NY)),
        ([_theta_row(bid=float("nan"), ask=float("nan"))], datetime(2024, 1, 2, 9, 31, tzinfo=NY)),
        ([], datetime(2024, 1, 2, 9, 31, tzinfo=NY)),
        ([_theta_row(bid=0.0, ask=0.01)], datetime(2024, 1, 2, 9, 31, tzinfo=NY)),
        ([_theta_row(bid=1.20, ask=1.10)], datetime(2024, 1, 2, 9, 31, tzinfo=NY)),
        ([_theta_row(timestamp=datetime(2024, 1, 2, 16, 0, tzinfo=NY))], datetime(2024, 1, 2, 16, 0, tzinfo=NY)),
    ],
    ids=("finite", "nan", "missing", "zero-bid", "inverted", "boundary"),
)
def test_session_cache_is_field_for_field_equivalent_to_per_minute_provider(
    rows, completed_at
):
    contract = _listed_contract()
    direct_client = SessionFrameClient(rows)
    cache_client = SessionFrameClient(rows)
    direct = ThetaDataHistoricalQuoteProvider(direct_client)
    cached = runner._ExpectedDrivenSessionQuoteCache(
        ThetaDataHistoricalQuoteProvider(cache_client)
    )

    expected = direct.acquire_quotes(
        symbol="TQQQ",
        completed_at=completed_at,
        expiration_date=contract.expiration_date,
        contracts=(contract,),
    )
    observed = cached.acquire_quotes(
        symbol="TQQQ",
        completed_at=completed_at,
        expiration_date=contract.expiration_date,
        contracts=(contract,),
    )

    assert _quote_signature(observed) == _quote_signature(expected)
    assert cached.network_fetches == 1
    assert len(cache_client.calls) == 1


def test_session_cache_preserves_multiple_contracts_call_put_separation():
    completed = datetime(2024, 1, 2, 9, 31, tzinfo=NY)
    contracts = (
        _listed_contract(right="CALL"),
        _listed_contract(right="PUT"),
        _listed_contract(strike="51", right="CALL"),
    )
    rows = [
        _theta_row(right="CALL", bid=1.0, ask=1.1),
        _theta_row(right="PUT", bid=2.0, ask=2.1),
        _theta_row(strike="51", right="CALL", bid=0.5, ask=0.6),
    ]
    direct = ThetaDataHistoricalQuoteProvider(SessionFrameClient(rows))
    cached = runner._ExpectedDrivenSessionQuoteCache(
        ThetaDataHistoricalQuoteProvider(SessionFrameClient(rows))
    )

    expected = direct.acquire_quotes(
        symbol="TQQQ",
        completed_at=completed,
        expiration_date=date(2024, 1, 5),
        contracts=contracts,
    )
    observed = cached.acquire_quotes(
        symbol="TQQQ",
        completed_at=completed,
        expiration_date=date(2024, 1, 5),
        contracts=contracts,
    )

    assert _quote_signature(observed) == _quote_signature(expected)
    assert cached.network_fetches == 3
    assert {(quote.strike, quote.right) for quote in observed.quotes} == {
        (Decimal("50"), "CALL"),
        (Decimal("50"), "PUT"),
        (Decimal("51"), "CALL"),
    }


def test_session_cache_reduces_390_minute_requests_to_one_network_fetch():
    session_start = datetime(2024, 1, 2, 9, 31, tzinfo=NY)
    rows = [
        _theta_row(timestamp=session_start + timedelta(minutes=index))
        for index in range(390)
    ]
    client = SessionFrameClient(rows)
    cached = runner._ExpectedDrivenSessionQuoteCache(
        ThetaDataHistoricalQuoteProvider(client)
    )
    contract = _listed_contract()

    for index in range(390):
        snapshot = cached.acquire_quotes(
            symbol="TQQQ",
            completed_at=session_start + timedelta(minutes=index),
            expiration_date=contract.expiration_date,
            contracts=(contract,),
        )
        assert len(snapshot.quotes) == 1

    assert cached.network_fetches == 1
    assert len(client.calls) == 1
    assert client.calls[0]["start_time"] == time(9, 31)
    assert client.calls[0]["end_time"] == time(16, 0, 59, 999_000)


def test_session_cache_key_isolates_contract_and_adjacent_session_date():
    day_one = datetime(2024, 1, 2, 9, 31, tzinfo=NY)
    day_two = datetime(2024, 1, 3, 9, 31, tzinfo=NY)
    rows = [
        _theta_row(timestamp=day_one, right="CALL"),
        _theta_row(timestamp=day_one, right="PUT"),
        _theta_row(timestamp=day_one, strike="51"),
        _theta_row(timestamp=day_two, right="CALL"),
    ]
    client = SessionFrameClient(rows)
    cached = runner._ExpectedDrivenSessionQuoteCache(
        ThetaDataHistoricalQuoteProvider(client)
    )
    call = _listed_contract(right="CALL")
    put = _listed_contract(right="PUT")
    other_strike = _listed_contract(strike="51")

    for completed, contract in (
        (day_one, call),
        (day_one, put),
        (day_one, other_strike),
        (day_two, call),
    ):
        cached.acquire_quotes(
            symbol="TQQQ",
            completed_at=completed,
            expiration_date=contract.expiration_date,
            contracts=(contract,),
        )

    assert cached.network_fetches == 4
    assert len(client.calls) == 4
    cached.clear()
    cached.acquire_quotes(
        symbol="TQQQ",
        completed_at=day_one,
        expiration_date=call.expiration_date,
        contracts=(call,),
    )
    assert cached.network_fetches == 5


def test_runner_clears_session_cache_at_cell_boundary(
    tmp_path, synthetic_components, monkeypatch
):
    source, quotes = synthetic_components
    root = tmp_path / "scratch"
    _prepare_definitions(root, symbols=("SQQQ",))
    original = runner._ExpectedDrivenSessionQuoteCache
    cleared: list[int] = []

    class TrackingCache(original):
        def clear(self):
            super().clear()
            cleared.append(len(self._windows))

    monkeypatch.setattr(runner, "_ExpectedDrivenSessionQuoteCache", TrackingCache)

    assert _build(root, source, quotes, symbols=("SQQQ",)).run(stop_after_cells=1) is None
    assert cleared == [0]


def test_session_cache_identity_contradiction_matches_per_minute_failure():
    contradictory = [_theta_row(right="PUT")]

    class ContradictingClient(SessionFrameClient):
        def option_history_quote(self, **kwargs: Any) -> RowFrame:
            self.calls.append(kwargs)
            return RowFrame(contradictory)

    contract = _listed_contract(right="CALL")
    completed = datetime(2024, 1, 2, 9, 31, tzinfo=NY)
    direct = ThetaDataHistoricalQuoteProvider(ContradictingClient(contradictory))
    cached = runner._ExpectedDrivenSessionQuoteCache(
        ThetaDataHistoricalQuoteProvider(ContradictingClient(contradictory))
    )

    for provider in (direct, cached):
        with pytest.raises(ThetaQuoteIdentityError):
            provider.acquire_quotes(
                symbol="TQQQ",
                completed_at=completed,
                expiration_date=contract.expiration_date,
                contracts=(contract,),
            )


def test_session_cache_preserves_downstream_qualification_input():
    completed = datetime(2024, 1, 2, 9, 31, tzinfo=NY)
    strikes = tuple(Decimal(value) for value in range(40, 61))
    contracts = tuple(
        _listed_contract(strike=format(strike, "f"), right=right)
        for strike in strikes
        for right in ("CALL", "PUT")
    )
    rows = [
        _theta_row(strike=format(contract.strike, "f"), right=contract.right)
        for contract in contracts
    ]

    class Listing:
        def discover_listings(self, *, symbol, completed_at):
            return ProviderListingSnapshot(
                symbol=symbol,
                timestamp=completed_at,
                discovery_succeeded=True,
                expirations=(
                    ProviderExpirationListing(
                        expiration_date=date(2024, 1, 5),
                        strikes=strikes,
                        contracts=contracts,
                    ),
                ),
            )

    class Combined:
        def __init__(self, quotes):
            self.quotes = quotes

        def discover_listings(self, **kwargs):
            return Listing().discover_listings(**kwargs)

        def acquire_quotes(self, **kwargs):
            return self.quotes.acquire_quotes(**kwargs)

    bar = CompletedUnderlyingBar("TQQQ", completed, Decimal("50"))
    direct_result = DynamicOptionEvidenceSlicer(
        Combined(ThetaDataHistoricalQuoteProvider(SessionFrameClient(rows)))
    ).slice_bar(bar)
    cached_result = DynamicOptionEvidenceSlicer(
        Combined(
            runner._ExpectedDrivenSessionQuoteCache(
                ThetaDataHistoricalQuoteProvider(SessionFrameClient(rows))
            )
        )
    ).slice_bar(bar)

    assert cached_result == direct_result


@pytest.mark.parametrize("defect", ["missing", "duplicate", "out_of_order", "invalid"])
def test_canonical_bar_defects_fail_closed(defect, tmp_path):
    bars = list(
        SyntheticUnderlyingSource(tmp_path / "sealed").load_session("SQQQ", SESSION).bars
    )
    if defect == "missing":
        bars.pop()
    elif defect == "duplicate":
        bars[2] = bars[1]
    elif defect == "out_of_order":
        bars[1], bars[2] = bars[2], bars[1]
    else:
        original = bars[0]
        bars[0] = CompletedUnderlyingBar(
            symbol="TQQQ", completed_at=original.completed_at, close=original.close
        )

    with pytest.raises(ValueError):
        runner._validate_bars(tuple(bars), "SQQQ", SESSION)


def test_semantic_digest_is_deterministic_and_distinct_from_physical(tmp_path):
    bars = SyntheticUnderlyingSource(tmp_path / "sealed").load_session("SQQQ", SESSION).bars
    slicer = SyntheticSlicer(
        runner._RetryingSplitProvider(
            SyntheticListingProvider((Path("definition.dbn"),)),
            SyntheticQuoteProvider(),
            sleeper=lambda _: None,
        )
    )
    rows = [runner._interval_row(bar, slicer.slice_bar(bar), True) for bar in bars[:2]]
    forward = runner.semantic_rows_sha256(rows)
    reverse = runner.semantic_rows_sha256(reversed(rows))
    assert forward == reverse

    snappy = tmp_path / "snappy.parquet"
    gzip = tmp_path / "gzip.parquet"
    table = pa.Table.from_pylist(rows, schema=runner.INTERVAL_SCHEMA)
    pq.write_table(table, snappy, compression="snappy", use_dictionary=False)
    pq.write_table(table, gzip, compression="gzip", use_dictionary=False)
    snappy_physical = runner.file_identity(snappy)[0]
    gzip_physical = runner.file_identity(gzip)[0]
    assert snappy_physical != gzip_physical
    assert runner.semantic_partition_sha256(snappy) == forward
    assert runner.semantic_partition_sha256(gzip) == forward
    assert forward not in {snappy_physical, gzip_physical}


def test_manifest_is_sorted_canonical_and_representation_bound(tmp_path):
    root = tmp_path / "scratch"
    (root / "z").mkdir(parents=True)
    (root / "z" / "last.bin").write_bytes(b"last")
    (root / "a.bin").write_bytes(b"first")

    manifest, digest = runner.write_manifest(root)
    payload = manifest.read_bytes()
    lines = payload.decode("utf-8").splitlines()

    assert payload.endswith(b"\n")
    assert [line.split(" ", 2)[2] for line in lines] == ["a.bin", "z/last.bin"]
    assert all(len(line.split(" ", 2)[0]) == 64 for line in lines)
    assert digest == hashlib.sha256(payload).hexdigest()
    assert "MANIFEST.sha256" not in payload.decode("utf-8")


def test_manifest_root_is_independent_of_file_creation_order(tmp_path):
    roots = (tmp_path / "first", tmp_path / "second")
    orders = (("z/last.bin", "a.bin"), ("a.bin", "z/last.bin"))
    payloads = []
    digests = []
    for root, order in zip(roots, orders):
        for relative in order:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(relative.encode())
        manifest, digest = runner.write_manifest(root)
        payloads.append(manifest.read_bytes())
        digests.append(digest)
    assert payloads[0] == payloads[1]
    assert digests[0] == digests[1]
