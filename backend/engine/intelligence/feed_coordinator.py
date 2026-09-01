from sqlalchemy.orm import Session

from app.db.models.intelligence import IntelligenceEvidenceLedger
from engine.intelligence.evidence_store import EvidenceStore
from engine.intelligence.feeds.base import BaseFeedAdapter


class FeedCoordinator:
    """OBSERVE_ONLY transaction coordinator; it exposes no trading authority."""

    authority_mode = "OBSERVE_ONLY"

    def __init__(self, db_session: Session, evidence_store: EvidenceStore) -> None:
        self.db = db_session
        self.evidence_store = evidence_store

    def poll_adapter(
        self, adapter: BaseFeedAdapter
    ) -> list[IntelligenceEvidenceLedger]:
        with self.db.begin_nested():
            payloads = adapter.poll_feed()
            return [
                self.evidence_store.ingest_evidence(
                    payload, deduplicate_event=True
                )
                for payload in payloads
            ]

    def poll_all(
        self, adapters: tuple[BaseFeedAdapter, ...]
    ) -> list[IntelligenceEvidenceLedger]:
        rows: list[IntelligenceEvidenceLedger] = []
        for adapter in adapters:
            rows.extend(self.poll_adapter(adapter))
        return rows
