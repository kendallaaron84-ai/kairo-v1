from datetime import UTC, datetime
from decimal import Decimal

from engine.intelligence.feeds.base import BaseFeedAdapter
from engine.intelligence.feeds.parsing import aware_datetime, json_object, require_list
from engine.intelligence.models import (
    EntityLinkPayload,
    EntityType,
    EventType,
    ImpactScope,
    IntelligenceIngestPayload,
    ReleaseStatus,
    SourceType,
    TimeHorizon,
    UrgencyLevel,
)


class FederalReserveAdapter(BaseFeedAdapter):
    def __init__(
        self,
        http_client: object,
        *,
        calendar_url: str,
        statements_url: str,
        observed_clock=lambda: datetime.now(UTC),
    ) -> None:
        super().__init__(http_client)
        self.calendar_url = calendar_url
        self.statements_url = statements_url
        self.observed_clock = observed_clock

    @property
    def provider_name(self) -> str:
        return "FEDERAL_RESERVE"

    def poll_feed(self) -> list[IntelligenceIngestPayload]:
        payloads: list[IntelligenceIngestPayload] = []
        calendar = self.http.get(self.calendar_url)
        if calendar.status_code != 304:
            root = json_object(calendar.content)
            for row in require_list(root.get("meetings"), "meetings"):
                at = aware_datetime(str(row["scheduled_release_at"]))
                payloads.append(
                    self._payload(
                        title=str(row.get("title") or "Scheduled FOMC meeting"),
                        summary=f"Official FOMC meeting calendar; scheduled_release_at={at.isoformat()}",
                        published_at=aware_datetime(
                            str(row.get("published_at") or at.isoformat())
                        ),
                        status=ReleaseStatus.SCHEDULED,
                        raw=calendar.content,
                        source_uri=self.calendar_url,
                    )
                )
        statements = self.http.get(self.statements_url)
        if statements.status_code != 304:
            root = json_object(statements.content)
            for row in require_list(root.get("statements"), "statements"):
                payloads.append(
                    self._payload(
                        title=str(row.get("title") or "FOMC policy statement"),
                        summary=str(row["official_text"]),
                        published_at=aware_datetime(str(row["published_at"])),
                        status=ReleaseStatus.RELEASED,
                        raw=statements.content,
                        source_uri=self.statements_url,
                    )
                )
        return payloads

    def _payload(
        self,
        *,
        title: str,
        summary: str,
        published_at: datetime,
        status: ReleaseStatus,
        raw: bytes,
        source_uri: str,
    ) -> IntelligenceIngestPayload:
        return IntelligenceIngestPayload(
            source_type=SourceType.PRIMARY,
            source_name=self.provider_name,
            source_uri=source_uri,
            event_type=EventType.MACRO,
            title=title,
            summary=summary,
            published_at=published_at,
            observed_at=self.observed_clock(),
            impact_scope=ImpactScope.MARKET,
            urgency=UrgencyLevel.CRITICAL,
            confidence_score=Decimal("100.00"),
            time_horizon=TimeHorizon.INTRADAY,
            release_status=status,
            raw_content_bytes=raw,
            mime_type="application/json",
            entity_links=(
                EntityLinkPayload(entity_type=EntityType.MACRO_FACTOR, entity_symbol="FOMC", relevance_score=100),
                EntityLinkPayload(entity_type=EntityType.MACRO_FACTOR, entity_symbol="FED_RATES", relevance_score=100),
                EntityLinkPayload(entity_type=EntityType.TICKER, entity_symbol="QQQ", relevance_score=90),
                EntityLinkPayload(entity_type=EntityType.TICKER, entity_symbol="TQQQ", relevance_score=90),
            ),
        )
