import hashlib
import inspect
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, inspect as sa_inspect, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.db.models.intelligence import (
    IntelligenceEntityLink,
    IntelligenceEvidenceLedger,
    IntelligenceRawArtifact,
)
from engine.intelligence.evidence_store import EvidenceStore
from engine.intelligence.feed_coordinator import FeedCoordinator
from engine.intelligence.feeds.bls import BlsAdapter
from engine.intelligence.feeds.corporate_ir import (
    CorporateIrAdapter,
    CorporateIrFormat,
    CorporateIrProfile,
)
from engine.intelligence.feeds.federal_reserve import FederalReserveAdapter
from engine.intelligence.feeds.http_client import (
    FeedHttpError,
    HttpClientPolicy,
    HttpResponse,
    ResilientHttpClient,
)
from engine.intelligence.feeds.sec_edgar import SecEdgarAdapter
from engine.intelligence.models import EventType
from engine.intelligence.storage_driver import LocalContentAddressedStorage


pytestmark = pytest.mark.integration
NOW = datetime(2026, 9, 1, 15, 0, tzinfo=UTC)
SCHEDULE_URL = "https://primary.test/bls/schedule"
SERIES_URL = "https://primary.test/bls/series"


class StubClient:
    def __init__(self, responses: dict[str, HttpResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get(self, url: str, *, headers: dict[str, str] | None = None) -> HttpResponse:
        self.calls.append((url, dict(headers or {})))
        return self.responses[url]


class SequenceTransport:
    def __init__(self, responses: dict[str, list[HttpResponse]]) -> None:
        self.responses = responses

    def get(self, url: str, **_: object) -> HttpResponse:
        values = self.responses[url]
        return values.pop(0) if len(values) > 1 else values[0]


def response(value: object, *, headers: dict[str, str] | None = None) -> HttpResponse:
    return HttpResponse(
        status_code=200,
        content=json.dumps(value, sort_keys=True).encode(),
        headers=headers or {"Content-Type": "application/json"},
    )


def evidence_store(session: Session, root: Path) -> EvidenceStore:
    return EvidenceStore(session, LocalContentAddressedStorage(root), clock=lambda: NOW)


def coordinator(session: Session, root: Path) -> FeedCoordinator:
    return FeedCoordinator(session, evidence_store(session, root))


def bls_client(*, consensus: bool = False) -> StubClient:
    released = {
        "series_id": "CPI",
        "actual_value": "2.9",
        "previous_value": "3.0",
        "published_at": "2026-09-01T12:30:00Z",
        "title": "CPI official release",
    }
    if consensus:
        released["consensus_value"] = "2.8"
    return StubClient({
        SCHEDULE_URL: response({"releases": [{
            "series_id": "CPI",
            "scheduled_release_at": "2026-09-01T12:30:00Z",
            "published_at": "2026-08-20T14:00:00Z",
            "title": "CPI release calendar",
        }]}),
        SERIES_URL: response({"series": [released]}),
    })


def bls_adapter(client: object) -> BlsAdapter:
    return BlsAdapter(
        client,
        schedule_url=SCHEDULE_URL,
        series_url=SERIES_URL,
        observed_clock=lambda: NOW,
    )


def sec_fixture(*, document_response: HttpResponse | None = None) -> tuple[StubClient, str]:
    adapter_probe = SecEdgarAdapter(StubClient({}), cik="1652044", ticker="GOOG")
    document_url = (
        "https://www.sec.gov/Archives/edgar/data/1652044/"
        "000165204426000001/goog-20251231.htm"
    )
    metadata = response({"filings": {"recent": {
        "accessionNumber": ["0001652044-26-000001"],
        "primaryDocument": ["goog-20251231.htm"],
        "form": ["10-K"],
        "filingDate": ["2026-02-05"],
    }}})
    return StubClient({
        adapter_probe.submissions_url: metadata,
        document_url: document_response or HttpResponse(
            200, b"<html>official 10-K</html>", {"Content-Type": "text/html"}
        ),
    }), document_url


def test_bls_scheduled_release_and_released_fact_are_distinct_immutable_events(
    db_session: Session, tmp_path: Path
) -> None:
    rows = coordinator(db_session, tmp_path).poll_adapter(bls_adapter(bls_client()))
    assert [row.release_status for row in rows] == ["SCHEDULED", "RELEASED"]
    assert rows[0].event_id != rows[1].event_id
    for row in rows:
        with pytest.raises(DBAPIError, match="immutable"), db_session.begin_nested():
            db_session.execute(text(
                "UPDATE intelligence_evidence_ledger SET title='changed' WHERE event_id=:id"
            ), {"id": row.event_id})


def test_federal_reserve_adapter_ingests_fomc_statement(
    db_session: Session, tmp_path: Path
) -> None:
    client = StubClient({
        "calendar": response({"meetings": []}),
        "statements": response({"statements": [{
            "title": "FOMC statement",
            "official_text": "The Committee maintained its target range.",
            "published_at": "2026-07-29T18:00:00Z",
        }]}),
    })
    adapter = FederalReserveAdapter(
        client, calendar_url="calendar", statements_url="statements",
        observed_clock=lambda: NOW,
    )
    row = coordinator(db_session, tmp_path).poll_adapter(adapter)[0]
    assert row.source_name == "FEDERAL_RESERVE"
    assert row.release_status == "RELEASED"
    assert "maintained" in row.summary


def test_primary_feed_does_not_fabricate_consensus_value() -> None:
    clean = bls_adapter(bls_client()).poll_feed()
    assert all("consensus" not in item.summary.lower() for item in clean)
    with pytest.raises(ValueError, match="consensus"):
        bls_adapter(bls_client(consensus=True)).poll_feed()


def test_sec_adapter_fetches_primary_document_from_accession_metadata() -> None:
    client, document_url = sec_fixture()
    adapter = SecEdgarAdapter(client, cik="1652044", ticker="GOOG", observed_clock=lambda: NOW)
    payload = adapter.poll_feed()[0]
    assert client.calls[1][0] == document_url
    assert payload.source_uri == document_url
    assert payload.raw_content_bytes == b"<html>official 10-K</html>"


def test_sec_rate_limit_failure_creates_no_partial_evidence(
    db_session: Session, tmp_path: Path
) -> None:
    probe = SecEdgarAdapter(StubClient({}), cik="1652044", ticker="GOOG")
    metadata = response({"filings": {"recent": {
        "accessionNumber": ["0001652044-26-000001"],
        "primaryDocument": ["goog.htm"], "form": ["10-K"],
        "filingDate": ["2026-02-05"],
    }}})
    document_url = "https://www.sec.gov/Archives/edgar/data/1652044/000165204426000001/goog.htm"
    transport = SequenceTransport({
        probe.submissions_url: [metadata],
        document_url: [HttpResponse(429, b"", {})],
    })
    client = ResilientHttpClient(
        HttpClientPolicy(
            user_agent="Kairo test operator@example.com",
            minimum_interval_seconds=0,
            max_429_retries=1,
            initial_backoff_seconds=0,
        ),
        transport,
        sleeper=lambda _: None,
    )
    adapter = SecEdgarAdapter(client, cik="1652044", ticker="GOOG", observed_clock=lambda: NOW)
    with pytest.raises(FeedHttpError, match="rate limit exhausted"):
        coordinator(db_session, tmp_path).poll_adapter(adapter)
    assert db_session.scalar(select(func.count()).select_from(IntelligenceRawArtifact)) == 0
    assert db_session.scalar(select(func.count()).select_from(IntelligenceEvidenceLedger)) == 0


def test_corporate_ir_profiles_support_provider_specific_parsers() -> None:
    fixtures = {
        "aapl": HttpResponse(200, b"<rss><channel><item><title>RSS</title><description>r</description><pubDate>2026-09-01T12:00:00Z</pubDate></item></channel></rss>"),
        "msft": HttpResponse(200, b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>ATOM</title><summary>a</summary><updated>2026-09-01T12:00:00Z</updated></entry></feed>'),
        "goog": response({"items": [{"title": "JSON", "summary": "j", "published_at": "2026-09-01T12:00:00Z"}]}),
        "nvda": HttpResponse(200, b'<article class="release" data-title="HTML" data-published-at="2026-09-01T12:00:00Z">h</article>'),
    }
    profiles = tuple(
        CorporateIrProfile(symbol=symbol, url=symbol.lower(), feed_format=format_)
        for symbol, format_ in (
            ("AAPL", CorporateIrFormat.RSS), ("MSFT", CorporateIrFormat.ATOM),
            ("GOOG", CorporateIrFormat.JSON), ("NVDA", CorporateIrFormat.HTML),
        )
    )
    payloads = CorporateIrAdapter(
        StubClient(fixtures), profiles=profiles, observed_clock=lambda: NOW
    ).poll_feed()
    assert [payload.title for payload in payloads] == ["RSS", "ATOM", "JSON", "HTML"]
    assert [payload.entity_links[0].entity_symbol for payload in payloads] == [
        "AAPL", "MSFT", "GOOG", "NVDA"
    ]


def test_sec_edgar_adapter_ingests_and_stores_10k_filing(
    db_session: Session, tmp_path: Path
) -> None:
    client, _ = sec_fixture()
    adapter = SecEdgarAdapter(client, cik="1652044", ticker="GOOG", observed_clock=lambda: NOW)
    row = coordinator(db_session, tmp_path).poll_adapter(adapter)[0]
    artifact = db_session.get(IntelligenceRawArtifact, row.artifact_id)
    assert row.event_type == "EARNINGS"
    assert row.title.endswith("10-K 0001652044-26-000001")
    assert LocalContentAddressedStorage(tmp_path).read_bytes(artifact.storage_uri) == b"<html>official 10-K</html>"


def test_bls_adapter_persists_cpi_event_with_critical_urgency(
    db_session: Session, tmp_path: Path
) -> None:
    rows = coordinator(db_session, tmp_path).poll_adapter(bls_adapter(bls_client()))
    released = next(row for row in rows if row.release_status == "RELEASED")
    assert released.urgency == "CRITICAL"
    assert released.event_type == "MACRO"


def test_corporate_ir_adapter_persists_earnings_announcement(
    db_session: Session, tmp_path: Path
) -> None:
    profile = CorporateIrProfile(
        symbol="NVDA", url="ir", feed_format=CorporateIrFormat.JSON,
        event_type=EventType.EARNINGS,
    )
    client = StubClient({"ir": response({"items": [{
        "title": "NVDA earnings", "summary": "Official quarterly results",
        "published_at": "2026-08-26T20:05:00Z",
    }]})})
    row = coordinator(db_session, tmp_path).poll_adapter(
        CorporateIrAdapter(client, profiles=(profile,), observed_clock=lambda: NOW)
    )[0]
    assert row.event_type == "EARNINGS"
    assert row.urgency == "HIGH"


def test_feed_coordinator_deduplicates_identical_feed_polls(
    db_session: Session, tmp_path: Path
) -> None:
    runner = coordinator(db_session, tmp_path)
    adapter = bls_adapter(bls_client())
    first = runner.poll_adapter(adapter)
    second = runner.poll_adapter(adapter)
    assert [row.event_id for row in second] == [row.event_id for row in first]
    assert db_session.scalar(select(func.count()).select_from(IntelligenceEvidenceLedger)) == 2


def test_feed_ingestion_preserves_immutable_sha256_content_hash(
    db_session: Session, tmp_path: Path
) -> None:
    raw = b"<html>canonical primary bytes\r\n</html>"
    client, _ = sec_fixture(document_response=HttpResponse(200, raw, {"Content-Type": "text/html"}))
    row = coordinator(db_session, tmp_path).poll_adapter(
        SecEdgarAdapter(client, cik="1652044", ticker="GOOG", observed_clock=lambda: NOW)
    )[0]
    assert row.raw_content_sha256 == hashlib.sha256(raw).hexdigest()
    with pytest.raises(DBAPIError, match="immutable"), db_session.begin_nested():
        db_session.execute(text(
            "UPDATE intelligence_evidence_ledger SET raw_content_sha256=:hash WHERE event_id=:id"
        ), {"hash": "0" * 64, "id": row.event_id})


def test_entity_linkages_correctly_map_to_tickers_and_macro_factors(
    db_session: Session, tmp_path: Path
) -> None:
    rows = coordinator(db_session, tmp_path).poll_adapter(bls_adapter(bls_client()))
    links = list(db_session.execute(select(
        IntelligenceEntityLink.entity_type, IntelligenceEntityLink.entity_symbol
    ).where(IntelligenceEntityLink.event_id.in_([row.event_id for row in rows]))))
    assert ("MACRO_FACTOR", "CPI") in links
    assert ("TICKER", "QQQ") in links


def test_malformed_feed_payload_fails_closed_without_corrupting_evidence_ledger(
    db_session: Session, tmp_path: Path
) -> None:
    profile = CorporateIrProfile(symbol="NVDA", url="ir", feed_format=CorporateIrFormat.JSON)
    client = StubClient({"ir": response({"items": [
        {"title": "valid", "summary": "valid", "published_at": "2026-09-01T12:00:00Z"},
        {"title": "missing date", "summary": "invalid"},
    ]})})
    with pytest.raises(ValueError, match="lacks canonical fields"):
        coordinator(db_session, tmp_path).poll_adapter(
            CorporateIrAdapter(client, profiles=(profile,), observed_clock=lambda: NOW)
        )
    assert db_session.scalar(select(func.count()).select_from(IntelligenceEvidenceLedger)) == 0


def test_zero_trade_authority_leakage_from_primary_feed_ingestion() -> None:
    from engine.intelligence import feed_coordinator
    from engine.intelligence.feeds import base, bls, corporate_ir, federal_reserve, sec_edgar

    modules = (feed_coordinator, base, bls, corporate_ir, federal_reserve, sec_edgar)
    source = "\n".join(inspect.getsource(module) for module in modules).lower()
    assert all(module.BaseFeedAdapter.authority_mode == "OBSERVE_ONLY" for module in (bls, corporate_ir, federal_reserve, sec_edgar))
    assert "engine.risk" not in source
    assert "engine.execution" not in source
    assert "orderintent" not in source


def _alembic_config() -> Config:
    backend_root = Path(__file__).resolve().parents[3]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    return config


def _insert_lifecycle_rows(engine: object, *, status: str, with_reference: bool) -> None:
    artifact_id, first_id, second_id = uuid4(), uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO intelligence_raw_artifacts
              (artifact_id, content_sha256, mime_type, byte_size, storage_uri, created_at)
            VALUES (:artifact, :hash, 'text/plain', 1, 'file:///migration-test', :now)
        """), {"artifact": artifact_id, "hash": "a" * 64, "now": NOW})
        common = {
            "artifact": artifact_id, "published": NOW, "observed": NOW,
            "created": NOW, "hash": "a" * 64,
        }
        connection.execute(text("""
            INSERT INTO intelligence_evidence_ledger
              (event_id, artifact_id, source_type, source_name, event_type, title,
               summary, published_at, observed_at, impact_scope, urgency,
               confidence_score, time_horizon, raw_content_sha256, created_at,
               release_status, referenced_event_id)
            VALUES (:event, :artifact, 'PRIMARY', 'TEST', 'MACRO', 'first',
               'first', :published, :observed, 'MARKET', 'LOW', 100, 'DAYS',
               :hash, :created, :status, NULL)
        """), {**common, "event": first_id, "status": status})
        if with_reference:
            connection.execute(text("""
                INSERT INTO intelligence_evidence_ledger
                  (event_id, artifact_id, source_type, source_name, event_type, title,
                   summary, published_at, observed_at, impact_scope, urgency,
                   confidence_score, time_horizon, raw_content_sha256, created_at,
                   release_status, referenced_event_id)
                VALUES (:event, :artifact, 'PRIMARY', 'TEST', 'MACRO', 'second',
                   'second', :published, :observed, 'MARKET', 'LOW', 100, 'DAYS',
                   :hash, :created, 'RELEASED', :reference)
            """), {**common, "event": second_id, "reference": first_id})


def test_migration_0017_upgrade_and_downgrade_are_clean_and_data_safe(
    migrated_database: tuple[str, str]
) -> None:
    admin_url, _ = migrated_database
    config = _alembic_config()
    engine = create_engine(admin_url)
    try:
        command.downgrade(config, "0016")
        assert "release_status" not in {
            column["name"] for column in sa_inspect(engine).get_columns("intelligence_evidence_ledger")
        }
        command.upgrade(config, "0017")
        columns = {column["name"] for column in sa_inspect(engine).get_columns("intelligence_evidence_ledger")}
        assert {"release_status", "referenced_event_id"} <= columns

        _insert_lifecycle_rows(engine, status="SCHEDULED", with_reference=False)
        with pytest.raises(Exception, match="lifecycle lineage"):
            command.downgrade(config, "0016")

        with engine.begin() as connection:
            connection.execute(text(
                "TRUNCATE intelligence_entity_links, intelligence_evidence_ledger, intelligence_raw_artifacts CASCADE"
            ))
        _insert_lifecycle_rows(engine, status="RELEASED", with_reference=True)
        with pytest.raises(Exception, match="lifecycle lineage"):
            command.downgrade(config, "0016")

        with engine.begin() as connection:
            connection.execute(text(
                "TRUNCATE intelligence_entity_links, intelligence_evidence_ledger, intelligence_raw_artifacts CASCADE"
            ))
        command.downgrade(config, "0016")
        command.upgrade(config, "head")
    finally:
        engine.dispose()
