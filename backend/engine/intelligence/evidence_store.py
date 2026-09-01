from collections.abc import Callable
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.intelligence import (
    IntelligenceEntityLink,
    IntelligenceEvidenceLedger,
    IntelligenceRawArtifact,
)
from engine.intelligence.models import IntelligenceIngestPayload, TimeHorizon


class EvidenceStore:
    """Append-only metadata authority over content-addressed intelligence bytes."""

    def __init__(
        self,
        db_session: Session,
        storage_driver: object,
        *,
        clock: Callable[[], datetime] | None = None,
        identity_factory: Callable[[], UUID] | None = None,
    ) -> None:
        self.db = db_session
        self.storage = storage_driver
        self.clock = clock or (lambda: datetime.now(UTC))
        self.identity_factory = identity_factory or uuid4

    def ingest_evidence(
        self,
        payload: IntelligenceIngestPayload,
        *,
        deduplicate_event: bool = False,
    ) -> IntelligenceEvidenceLedger:
        content_hash = payload.compute_raw_content_sha256()
        if deduplicate_event:
            existing = self.db.scalar(
                select(IntelligenceEvidenceLedger).where(
                    IntelligenceEvidenceLedger.raw_content_sha256 == content_hash,
                    IntelligenceEvidenceLedger.source_name == payload.source_name,
                    IntelligenceEvidenceLedger.source_uri == payload.source_uri,
                    IntelligenceEvidenceLedger.event_type == payload.event_type.value,
                    IntelligenceEvidenceLedger.title == payload.title,
                    IntelligenceEvidenceLedger.published_at == payload.published_at,
                    IntelligenceEvidenceLedger.release_status
                    == payload.release_status.value,
                    IntelligenceEvidenceLedger.referenced_event_id
                    == payload.referenced_event_id,
                )
            )
            if existing is not None:
                return existing
        artifact = self.db.scalar(
            select(IntelligenceRawArtifact).where(
                IntelligenceRawArtifact.content_sha256 == content_hash
            )
        )
        now = self.clock()
        self._require_aware(now)
        if artifact is None:
            storage_uri = self.storage.write_bytes(
                content_hash, payload.raw_content_bytes, payload.mime_type
            )
            artifact = IntelligenceRawArtifact(
                artifact_id=uuid5(
                    NAMESPACE_URL, f"kairo:intelligence-artifact:{content_hash}"
                ),
                content_sha256=content_hash,
                mime_type=payload.mime_type,
                byte_size=len(payload.raw_content_bytes),
                storage_uri=storage_uri,
                created_at=now,
            )
            self.db.add(artifact)
            self.db.flush()

        event_id = self.identity_factory()
        evidence = IntelligenceEvidenceLedger(
            event_id=event_id,
            artifact_id=artifact.artifact_id,
            source_type=payload.source_type.value,
            source_name=payload.source_name,
            source_uri=payload.source_uri,
            event_type=payload.event_type.value,
            title=payload.title,
            summary=payload.summary,
            published_at=payload.published_at,
            observed_at=payload.observed_at,
            impact_scope=payload.impact_scope.value,
            urgency=payload.urgency.value,
            confidence_score=payload.confidence_score,
            time_horizon=payload.time_horizon.value,
            raw_content_sha256=content_hash,
            release_status=payload.release_status.value,
            referenced_event_id=payload.referenced_event_id,
            created_at=now,
        )
        self.db.add(evidence)
        self.db.flush()
        for link in payload.entity_links:
            self.db.add(
                IntelligenceEntityLink(
                    link_id=self.identity_factory(),
                    event_id=event_id,
                    entity_type=link.entity_type.value,
                    entity_symbol=link.entity_symbol,
                    relevance_score=link.relevance_score,
                )
            )
        self.db.flush()
        return evidence

    def query_by_entity(
        self, *, entity_symbol: str, time_horizon: TimeHorizon | None = None
    ) -> list[IntelligenceEvidenceLedger]:
        statement = (
            select(IntelligenceEvidenceLedger)
            .join(
                IntelligenceEntityLink,
                IntelligenceEntityLink.event_id == IntelligenceEvidenceLedger.event_id,
            )
            .where(IntelligenceEntityLink.entity_symbol == entity_symbol.strip().upper())
            .order_by(
                IntelligenceEvidenceLedger.published_at,
                IntelligenceEvidenceLedger.event_id,
            )
        )
        if time_horizon is not None:
            statement = statement.where(
                IntelligenceEvidenceLedger.time_horizon == time_horizon.value
            )
        return list(self.db.scalars(statement))

    @staticmethod
    def _require_aware(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("evidence clock must return a timezone-aware datetime")
