import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import create_engine, func, inspect as sa_inspect, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.db.models.intelligence import (
    IntelligenceCaseConclusion,
    IntelligenceCaseFinding,
    IntelligenceEvidenceLedger,
    IntelligenceFindingCitation,
    IntelligenceInvestigationCase,
)
from engine.intelligence.cases.case_engine import CaseEngine
from engine.intelligence.cases.models import (
    CitationPayload,
    CitationRole,
    FindingPayload,
    FindingType,
    InvestigationVerdict,
    TemporalStatus,
    compute_case_manifest_sha256,
)
from engine.intelligence.evidence_store import EvidenceStore
from engine.intelligence.models import (
    EventType,
    ImpactScope,
    IntelligenceIngestPayload,
    SourceType,
    TimeHorizon,
    UrgencyLevel,
)
from engine.intelligence.storage_driver import LocalContentAddressedStorage


pytestmark = pytest.mark.integration
NOW = datetime(2026, 9, 1, 16, 0, tzinfo=UTC)


def case_engine(session: Session) -> CaseEngine:
    return CaseEngine(session, clock=lambda: NOW)


def open_case(engine: CaseEngine) -> IntelligenceInvestigationCase:
    return engine.open_case(
        "Did the issuer confirm the claimed milestone?",
        "JOBY",
        "EVTOL",
        "The issuer confirmed the milestone.",
    )


def add_evidence(
    session: Session,
    root: Path,
    *,
    source_name: str = "SEC_EDGAR",
    raw: bytes | None = None,
) -> IntelligenceEvidenceLedger:
    content = raw or f"official evidence {uuid4()}".encode()
    store = EvidenceStore(
        session, LocalContentAddressedStorage(root), clock=lambda: NOW
    )
    return store.ingest_evidence(IntelligenceIngestPayload(
        source_type=SourceType.PRIMARY,
        source_name=source_name,
        source_uri="https://primary.example/filing",
        event_type=EventType.REGULATORY,
        title="Official primary filing",
        summary="The issuer published an official primary-source fact.",
        published_at=NOW - timedelta(hours=1),
        observed_at=NOW,
        impact_scope=ImpactScope.COMPANY,
        urgency=UrgencyLevel.HIGH,
        confidence_score=Decimal("100.00"),
        time_horizon=TimeHorizon.MONTHS,
        raw_content_bytes=content,
        mime_type="text/html",
    ))


def citation(
    event_id: object,
    *,
    role: CitationRole = CitationRole.PRIMARY_PROOF,
    temporal: TemporalStatus = TemporalStatus.ACTIVE,
) -> CitationPayload:
    return CitationPayload(
        event_id=event_id,
        citation_role=role,
        temporal_status=temporal,
        citation_relevance=Decimal("100.00"),
    )


def finding(
    finding_type: FindingType,
    *citations: CitationPayload,
    search_scope: dict | None = None,
) -> FindingPayload:
    return FindingPayload(
        finding_type=finding_type,
        claim_assertion=f"{finding_type.value} assertion",
        finding_narrative=f"Evidence-grounded {finding_type.value} narrative.",
        search_scope_json=search_scope,
        citations=citations,
    )


def conclude(engine: CaseEngine, case_id: object) -> IntelligenceCaseConclusion:
    return engine.conclude_case(
        case_id,
        ImpactScope.COMPANY,
        TimeHorizon.MONTHS,
        "Evidence-grounded deterministic synthesis.",
        Decimal("92.00"),
    )


def test_open_case_does_not_require_or_fabricate_final_conclusion(
    db_session: Session,
) -> None:
    case = open_case(case_engine(db_session))
    assert case.opened_at == NOW
    assert db_session.get(IntelligenceCaseConclusion, case.case_id) is None
    assert db_session.scalar(
        select(func.count()).select_from(IntelligenceCaseConclusion)
    ) == 0


def test_case_conclusion_is_append_only_and_one_per_case(
    db_session: Session,
) -> None:
    engine = case_engine(db_session)
    case = open_case(engine)
    conclusion = conclude(engine, case.case_id)
    assert conclusion.case_id == case.case_id
    with pytest.raises(ValueError, match="already concluded"):
        conclude(engine, case.case_id)
    with pytest.raises(DBAPIError, match="immutable"), db_session.begin_nested():
        db_session.execute(text(
            "UPDATE intelligence_case_conclusions SET verdict='UNSUPPORTED' "
            "WHERE conclusion_id=:id"
        ), {"id": conclusion.conclusion_id})


def test_gap_finding_requires_search_scope_but_not_fabricated_citation(
    db_session: Session,
) -> None:
    with pytest.raises(ValidationError, match="search_scope_json"):
        finding(FindingType.GAP_IDENTIFIED)
    engine = case_engine(db_session)
    case = open_case(engine)
    rows = engine.add_findings(case.case_id, [finding(
        FindingType.GAP_IDENTIFIED,
        search_scope={"sources_checked": ["SEC_EDGAR", "CORPORATE_IR"]},
    )])
    assert rows[0].search_scope_json["sources_checked"] == [
        "SEC_EDGAR", "CORPORATE_IR"
    ]
    assert db_session.scalar(
        select(func.count()).select_from(IntelligenceFindingCitation)
    ) == 0
    assert conclude(engine, case.case_id).verdict == "INSUFFICIENT_EVIDENCE"


def test_supporting_and_contradictory_findings_require_real_evidence(
    db_session: Session,
) -> None:
    for finding_type in (FindingType.SUPPORTING, FindingType.CONTRADICTORY):
        with pytest.raises(ValidationError, match="requires at least 1 citation"):
            finding(finding_type)
    engine = case_engine(db_session)
    case = open_case(engine)
    with pytest.raises(ValueError, match="nonexistent evidence"):
        engine.add_findings(case.case_id, [finding(
            FindingType.SUPPORTING, citation(uuid4())
        )])
    assert db_session.scalar(
        select(func.count()).select_from(IntelligenceCaseFinding)
    ) == 0


def test_citation_role_and_temporal_status_are_independent(
    db_session: Session, tmp_path: Path
) -> None:
    first = add_evidence(db_session, tmp_path)
    second = add_evidence(db_session, tmp_path)
    engine = case_engine(db_session)
    case = open_case(engine)
    created = engine.add_findings(case.case_id, [finding(
        FindingType.SUPERSEDED_FACT,
        citation(
            first.event_id,
            role=CitationRole.CONTEXT,
            temporal=TemporalStatus.SUPERSEDED,
        ),
        citation(
            second.event_id,
            role=CitationRole.CONTRADICTION,
            temporal=TemporalStatus.HISTORICAL_CONTEXT,
        ),
    )])[0]
    rows = list(db_session.scalars(select(IntelligenceFindingCitation).where(
        IntelligenceFindingCitation.finding_id == created.finding_id
    ).order_by(IntelligenceFindingCitation.event_id)))
    assert {(row.citation_role, row.temporal_status) for row in rows} == {
        ("CONTEXT", "SUPERSEDED"),
        ("CONTRADICTION", "HISTORICAL_CONTEXT"),
    }


def test_case_verdict_is_derived_from_evidence_not_caller_selected(
    db_session: Session, tmp_path: Path
) -> None:
    assert "verdict" not in inspect.signature(CaseEngine.conclude_case).parameters
    evidence = add_evidence(db_session, tmp_path)
    engine = case_engine(db_session)
    case = open_case(engine)
    engine.add_findings(case.case_id, [finding(
        FindingType.SUPPORTING, citation(evidence.event_id)
    )])
    assert conclude(engine, case.case_id).verdict == InvestigationVerdict.CONFIRMED.value


def test_case_manifest_changes_if_any_citation_or_evidence_hash_changes(
    db_session: Session, tmp_path: Path
) -> None:
    evidence = add_evidence(db_session, tmp_path)
    engine = case_engine(db_session)
    case = open_case(engine)
    engine.add_findings(case.case_id, [finding(
        FindingType.SUPPORTING, citation(evidence.event_id)
    )])
    conclusion = conclude(engine, case.case_id)
    findings = list(db_session.scalars(select(IntelligenceCaseFinding).where(
        IntelligenceCaseFinding.case_id == case.case_id
    )))
    citations = list(db_session.scalars(select(IntelligenceFindingCitation)))
    hashes = {evidence.event_id: evidence.raw_content_sha256}
    assert compute_case_manifest_sha256(
        case, findings, citations, conclusion, hashes
    ) == conclusion.case_manifest_sha256
    changed_hash = compute_case_manifest_sha256(
        case, findings, citations, conclusion, {evidence.event_id: "0" * 64}
    )
    changed_citation = SimpleNamespace(
        finding_id=citations[0].finding_id,
        event_id=citations[0].event_id,
        citation_role="CONTEXT",
        temporal_status=citations[0].temporal_status,
        citation_relevance=citations[0].citation_relevance,
    )
    changed_role_hash = compute_case_manifest_sha256(
        case, findings, [changed_citation], conclusion, hashes
    )
    assert len({conclusion.case_manifest_sha256, changed_hash, changed_role_hash}) == 3


def test_case_rejects_citations_referencing_nonexistent_evidence_event_id(
    db_session: Session,
) -> None:
    engine = case_engine(db_session)
    case = open_case(engine)
    with pytest.raises(ValueError, match="nonexistent evidence"):
        engine.add_findings(case.case_id, [finding(
            FindingType.CONTRADICTORY,
            citation(uuid4(), role=CitationRole.CONTRADICTION),
        )])


def test_case_engine_evaluates_confirmed_verdict_with_primary_sec_filing(
    db_session: Session, tmp_path: Path
) -> None:
    sec_filing = add_evidence(
        db_session, tmp_path, source_name="SEC_EDGAR", raw=b"official SEC 10-K"
    )
    engine = case_engine(db_session)
    case = open_case(engine)
    engine.add_findings(case.case_id, [finding(
        FindingType.SUPPORTING, citation(sec_filing.event_id)
    )])
    result = conclude(engine, case.case_id)
    assert result.verdict == "CONFIRMED"
    assert db_session.get(
        IntelligenceEvidenceLedger, sec_filing.event_id
    ).source_name == "SEC_EDGAR"


def test_case_engine_evaluates_contradictory_findings_and_adjusts_verdict(
    db_session: Session, tmp_path: Path
) -> None:
    support = add_evidence(db_session, tmp_path)
    contradiction = add_evidence(db_session, tmp_path)
    engine = case_engine(db_session)
    case = open_case(engine)
    engine.add_findings(case.case_id, [
        finding(FindingType.SUPPORTING, citation(support.event_id)),
        finding(
            FindingType.CONTRADICTORY,
            citation(
                contradiction.event_id, role=CitationRole.CONTRADICTION
            ),
        ),
    ])
    assert conclude(engine, case.case_id).verdict == "PARTIALLY_SUPPORTED"


def test_superseded_fact_requires_at_least_two_citations(
    db_session: Session, tmp_path: Path
) -> None:
    evidence = add_evidence(db_session, tmp_path)
    with pytest.raises(ValidationError, match="at least 2 citations"):
        finding(FindingType.SUPERSEDED_FACT, citation(evidence.event_id))

    engine = case_engine(db_session)
    case = open_case(engine)
    direct = IntelligenceCaseFinding(
        finding_id=uuid4(), case_id=case.case_id,
        finding_type="SUPERSEDED_FACT", claim_assertion="old fact",
        finding_narrative="old fact was superseded", search_scope_json=None,
        sequence_num=1, created_at=NOW,
    )
    db_session.add(direct)
    db_session.flush()
    db_session.add(IntelligenceFindingCitation(
        citation_id=uuid4(), finding_id=direct.finding_id,
        event_id=evidence.event_id, citation_role="CONTEXT",
        temporal_status="SUPERSEDED", citation_relevance=Decimal("100.00"),
        created_at=NOW,
    ))
    db_session.flush()
    with pytest.raises(DBAPIError, match="at least 2 citations"), db_session.begin_nested():
        db_session.add(IntelligenceCaseConclusion(
            conclusion_id=uuid4(), case_id=case.case_id, verdict="INSUFFICIENT_EVIDENCE",
            confidence_score=Decimal("0.00"), materiality_scope="COMPANY",
            time_horizon="MONTHS", synthesis_summary="Insufficient evidence.",
            case_manifest_sha256="a" * 64, closed_at=NOW,
        ))
        db_session.flush()


def test_empty_evidence_library_returns_insufficient_evidence_verdict(
    db_session: Session,
) -> None:
    engine = case_engine(db_session)
    case = open_case(engine)
    result = conclude(engine, case.case_id)
    assert result.verdict == "INSUFFICIENT_EVIDENCE"
    assert len(result.case_manifest_sha256) == 64


def test_database_immutability_rejects_update_or_delete_on_cases_and_conclusions(
    db_session: Session,
) -> None:
    engine = case_engine(db_session)
    case = open_case(engine)
    conclusion = conclude(engine, case.case_id)
    statements = (
        ("UPDATE intelligence_investigation_cases SET target_symbol='X' WHERE case_id=:id", case.case_id),
        ("DELETE FROM intelligence_investigation_cases WHERE case_id=:id", case.case_id),
        ("UPDATE intelligence_case_conclusions SET confidence_score=1 WHERE conclusion_id=:id", conclusion.conclusion_id),
        ("DELETE FROM intelligence_case_conclusions WHERE conclusion_id=:id", conclusion.conclusion_id),
    )
    for statement, identity in statements:
        with pytest.raises(DBAPIError, match="immutable"), db_session.begin_nested():
            db_session.execute(text(statement), {"id": identity})


def test_zero_trade_authority_or_governor_leakage_from_investigation_cases() -> None:
    from engine.intelligence.cases import case_engine as module

    source = inspect.getsource(module).lower()
    assert CaseEngine.authority_mode == "OBSERVE_ONLY"
    assert "engine.risk" not in source
    assert "engine.execution" not in source
    assert "orderintent" not in source
    assert "veto" not in source


def _alembic_config() -> Config:
    backend_root = Path(__file__).resolve().parents[3]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    return config


def test_migration_0018_upgrade_and_downgrade_are_clean_and_data_safe(
    migrated_database: tuple[str, str],
) -> None:
    admin_url, _ = migrated_database
    config = _alembic_config()
    engine = create_engine(admin_url)
    table_names = {
        "intelligence_investigation_cases",
        "intelligence_case_findings",
        "intelligence_finding_citations",
        "intelligence_case_conclusions",
    }
    try:
        command.downgrade(config, "0017")
        assert table_names.isdisjoint(sa_inspect(engine).get_table_names())
        command.upgrade(config, "0018")
        assert table_names <= set(sa_inspect(engine).get_table_names())
        with engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO intelligence_investigation_cases
                  (case_id, case_number, query_prompt, hypothesis_claim, opened_at)
                VALUES (:id, 'CASE-MIGRATION', 'query', 'hypothesis', :opened)
            """), {"id": uuid4(), "opened": NOW})
        with pytest.raises(Exception, match="immutable investigation records"):
            command.downgrade(config, "0017")
        with engine.begin() as connection:
            connection.execute(text(
                "TRUNCATE intelligence_investigation_cases CASCADE"
            ))
        command.downgrade(config, "0017")
        assert table_names.isdisjoint(sa_inspect(engine).get_table_names())
        command.upgrade(config, "head")
    finally:
        engine.dispose()
