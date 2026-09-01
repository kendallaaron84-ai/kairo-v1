from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.db.models.intelligence import (
    IntelligenceEntityLink,
    IntelligenceEvidenceLedger,
    IntelligenceRawArtifact,
)
from engine.intelligence.evidence_store import EvidenceStore
from engine.intelligence.models import (
    EntityLinkPayload,
    EntityType,
    EventType,
    ImpactScope,
    IntelligenceIngestPayload,
    SourceType,
    TimeHorizon,
    UrgencyLevel,
)
from engine.intelligence.storage_driver import LocalContentAddressedStorage


pytestmark = pytest.mark.integration
NOW = datetime(2026, 9, 1, 15, 0, tzinfo=UTC)


def payload(
    *,
    raw: bytes = b'{"form":"8-K","issuer":"META"}',
    source_type: SourceType = SourceType.PRIMARY,
    time_horizon: TimeHorizon = TimeHorizon.DAYS,
    links: tuple[EntityLinkPayload, ...] = (
        EntityLinkPayload(
            entity_type=EntityType.TICKER,
            entity_symbol="meta",
            relevance_score=Decimal("98.50"),
        ),
    ),
) -> IntelligenceIngestPayload:
    return IntelligenceIngestPayload(
        source_type=source_type,
        source_name="SEC EDGAR" if source_type is SourceType.PRIMARY else "Kendall",
        source_uri="https://www.sec.gov/example",
        event_type=EventType.REGULATORY,
        title="META files material 8-K",
        summary="Issuer disclosed a material corporate event.",
        published_at=NOW - timedelta(minutes=5),
        observed_at=NOW,
        impact_scope=ImpactScope.COMPANY,
        urgency=UrgencyLevel.HIGH,
        confidence_score=Decimal("97.25"),
        time_horizon=time_horizon,
        raw_content_bytes=raw,
        mime_type="application/json",
        entity_links=links,
    )


def store(session: Session, root: Path) -> EvidenceStore:
    return EvidenceStore(
        session,
        LocalContentAddressedStorage(root),
        clock=lambda: NOW,
    )


def test_raw_artifact_deduplicates_by_content_sha256(
    db_session: Session, tmp_path: Path
) -> None:
    evidence_store = store(db_session, tmp_path)
    first = evidence_store.ingest_evidence(payload())
    second = evidence_store.ingest_evidence(payload())
    assert first.event_id != second.event_id
    assert first.artifact_id == second.artifact_id
    assert db_session.scalar(select(func.count()).select_from(IntelligenceRawArtifact)) == 1
    artifact = db_session.get(IntelligenceRawArtifact, first.artifact_id)
    assert LocalContentAddressedStorage(tmp_path).read_bytes(artifact.storage_uri) == payload().raw_content_bytes


def test_evidence_ledger_persists_immutable_event_fact(
    db_session: Session, tmp_path: Path
) -> None:
    supplied = payload()
    row = store(db_session, tmp_path).ingest_evidence(supplied)
    assert row.raw_content_sha256 == supplied.compute_raw_content_sha256()
    assert row.published_at == supplied.published_at
    assert row.observed_at == supplied.observed_at
    assert row.created_at == NOW
    artifact = db_session.get(IntelligenceRawArtifact, row.artifact_id)
    assert artifact.byte_size == len(supplied.raw_content_bytes)
    assert artifact.content_sha256 == row.raw_content_sha256


def test_entity_links_correctly_associate_tickers_and_macro_factors(
    db_session: Session, tmp_path: Path
) -> None:
    links = (
        EntityLinkPayload(entity_type=EntityType.TICKER, entity_symbol="tqqq", relevance_score=95),
        EntityLinkPayload(
            entity_type=EntityType.MACRO_FACTOR,
            entity_symbol="fed_rates",
            relevance_score=Decimal("88.25"),
        ),
    )
    event = store(db_session, tmp_path).ingest_evidence(payload(links=links))
    rows = list(db_session.scalars(select(IntelligenceEntityLink).where(
        IntelligenceEntityLink.event_id == event.event_id
    ).order_by(IntelligenceEntityLink.entity_symbol)))
    assert [(row.entity_type, row.entity_symbol) for row in rows] == [
        ("MACRO_FACTOR", "FED_RATES"), ("TICKER", "TQQQ")
    ]


def add_artifact(db_session: Session) -> IntelligenceRawArtifact:
    row = IntelligenceRawArtifact(
        artifact_id=uuid4(), content_sha256="a" * 64, mime_type="text/plain",
        byte_size=1, storage_uri="file:///test/a.txt", created_at=NOW,
    )
    db_session.add(row)
    db_session.flush()
    return row


def raw_event_values(artifact: IntelligenceRawArtifact) -> dict:
    return {
        "event_id": uuid4(), "artifact_id": artifact.artifact_id,
        "source_type": "PRIMARY", "source_name": "TEST", "source_uri": None,
        "event_type": "MACRO", "title": "Test", "summary": "Test evidence",
        "published_at": NOW, "observed_at": NOW, "impact_scope": "MARKET",
        "urgency": "LOW", "confidence_score": Decimal("50"),
        "time_horizon": "DAYS", "raw_content_sha256": artifact.content_sha256,
        "created_at": NOW,
    }


def test_database_rejects_invalid_source_type_or_event_type(db_session: Session) -> None:
    artifact = add_artifact(db_session)
    for column, invalid in (("source_type", "BLOG"), ("event_type", "RUMOR")):
        values = raw_event_values(artifact)
        values[column] = invalid
        with pytest.raises(DBAPIError), db_session.begin_nested():
            db_session.add(IntelligenceEvidenceLedger(**values))
            db_session.flush()


def test_database_rejects_confidence_score_out_of_bounds(db_session: Session) -> None:
    artifact = add_artifact(db_session)
    for score in (Decimal("-0.01"), Decimal("100.01")):
        values = raw_event_values(artifact)
        values["confidence_score"] = score
        with pytest.raises(DBAPIError), db_session.begin_nested():
            db_session.add(IntelligenceEvidenceLedger(**values))
            db_session.flush()


def test_database_rejects_mutation_or_deletion_of_intelligence_records(
    db_session: Session, tmp_path: Path
) -> None:
    event = store(db_session, tmp_path).ingest_evidence(payload())
    link = db_session.scalar(select(IntelligenceEntityLink).where(
        IntelligenceEntityLink.event_id == event.event_id
    ))
    statements = (
        text("UPDATE intelligence_raw_artifacts SET mime_type='x' WHERE artifact_id=:id").bindparams(id=event.artifact_id),
        text("DELETE FROM intelligence_evidence_ledger WHERE event_id=:id").bindparams(id=event.event_id),
        text("DELETE FROM intelligence_entity_links WHERE link_id=:id").bindparams(id=link.link_id),
    )
    for statement in statements:
        with pytest.raises(DBAPIError, match="immutable"), db_session.begin_nested():
            db_session.execute(statement)


def test_evidence_query_by_ticker_and_time_horizon(
    db_session: Session, tmp_path: Path
) -> None:
    evidence_store = store(db_session, tmp_path)
    wanted = evidence_store.ingest_evidence(payload(raw=b"days", time_horizon=TimeHorizon.DAYS))
    evidence_store.ingest_evidence(payload(raw=b"intraday", time_horizon=TimeHorizon.INTRADAY))
    rows = evidence_store.query_by_entity(entity_symbol="meta", time_horizon=TimeHorizon.DAYS)
    assert [row.event_id for row in rows] == [wanted.event_id]


def test_user_submitted_research_payload_persists_with_correct_provenance(
    db_session: Session, tmp_path: Path
) -> None:
    supplied = payload(source_type=SourceType.USER_SUBMITTED, raw=b"operator research note")
    row = store(db_session, tmp_path).ingest_evidence(supplied)
    assert row.source_type == "USER_SUBMITTED"
    assert row.source_name == "Kendall"
    assert row.source_uri == supplied.source_uri
    assert row.raw_content_sha256 == supplied.compute_raw_content_sha256()


def test_migration_0016_upgrade_and_downgrade_are_clean_and_data_safe(
    migrated_database: tuple[str, str],
) -> None:
    admin_url, _ = migrated_database
    backend_root = Path(__file__).resolve().parents[3]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    engine = create_engine(admin_url)
    command.downgrade(config, "0015")
    assert "intelligence_raw_artifacts" not in inspect(engine).get_table_names()
    command.upgrade(config, "0016")
    expected = {
        "intelligence_raw_artifacts",
        "intelligence_evidence_ledger",
        "intelligence_entity_links",
    }
    assert expected <= set(inspect(engine).get_table_names())
    with engine.connect() as connection:
        for table_name in expected:
            assert connection.scalar(text(
                "SELECT has_table_privilege('kairo_runtime', :table, 'SELECT')"
            ), {"table": table_name}) is True
            assert connection.scalar(text(
                "SELECT has_table_privilege('kairo_runtime', :table, 'INSERT')"
            ), {"table": table_name}) is True
            assert connection.scalar(text(
                "SELECT has_table_privilege('kairo_runtime', :table, 'UPDATE')"
            ), {"table": table_name}) is False
            assert connection.scalar(text(
                "SELECT has_table_privilege('kairo_runtime', :table, 'DELETE')"
            ), {"table": table_name}) is False
    command.downgrade(config, "0015")
    assert expected.isdisjoint(inspect(engine).get_table_names())
    command.upgrade(config, "head")
    engine.dispose()
