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


BLS_ENTITIES = {
    "CPI": ("CPI", "QQQ"),
    "PPI": ("PPI", "QQQ"),
    "PAYROLLS": ("PAYROLLS", "QQQ"),
    "UNEMPLOYMENT": ("PAYROLLS", "QQQ"),
}


class BlsAdapter(BaseFeedAdapter):
    def __init__(
        self,
        http_client: object,
        *,
        schedule_url: str,
        series_url: str,
        observed_clock=lambda: datetime.now(UTC),
    ) -> None:
        super().__init__(http_client)
        self.schedule_url = schedule_url
        self.series_url = series_url
        self.observed_clock = observed_clock

    @property
    def provider_name(self) -> str:
        return "BLS"

    def poll_feed(self) -> list[IntelligenceIngestPayload]:
        payloads: list[IntelligenceIngestPayload] = []
        schedule = self.http.get(self.schedule_url)
        if schedule.status_code != 304:
            data = json_object(schedule.content)
            for row in require_list(data.get("releases"), "releases"):
                payloads.append(self._scheduled(row, schedule.content))
        series = self.http.get(self.series_url)
        if series.status_code != 304:
            data = json_object(series.content)
            for row in require_list(data.get("series"), "series"):
                payloads.append(self._released(row, series.content))
        return payloads

    def _scheduled(self, row: dict, raw: bytes) -> IntelligenceIngestPayload:
        series_id = self._series(row)
        scheduled = aware_datetime(str(row["scheduled_release_at"]))
        return self._payload(
            series_id=series_id,
            title=str(row.get("title") or f"{series_id} scheduled release"),
            summary=f"Official BLS calendar notice; scheduled_release_at={scheduled.isoformat()}",
            published_at=aware_datetime(str(row.get("published_at") or scheduled.isoformat())),
            status=ReleaseStatus.SCHEDULED,
            raw=raw,
            source_uri=self.schedule_url,
        )

    def _released(self, row: dict, raw: bytes) -> IntelligenceIngestPayload:
        forbidden = {"consensus_value", "consensus", "surprise"} & set(row)
        if forbidden:
            raise ValueError("BLS primary facts cannot contain market consensus fields")
        series_id = self._series(row)
        if "actual_value" not in row:
            raise ValueError("BLS release requires official actual_value")
        summary = "; ".join(
            f"{key}={row[key]}"
            for key in ("actual_value", "previous_value", "official_revision")
            if key in row
        )
        return self._payload(
            series_id=series_id,
            title=str(row.get("title") or f"{series_id} official release"),
            summary=summary,
            published_at=aware_datetime(str(row["published_at"])),
            status=(
                ReleaseStatus.REVISED
                if row.get("official_revision") is not None
                else ReleaseStatus.RELEASED
            ),
            raw=raw,
            source_uri=self.series_url,
        )

    def _payload(
        self,
        *,
        series_id: str,
        title: str,
        summary: str,
        published_at: datetime,
        status: ReleaseStatus,
        raw: bytes,
        source_uri: str,
    ) -> IntelligenceIngestPayload:
        entities = BLS_ENTITIES[series_id]
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
            urgency=UrgencyLevel.CRITICAL if series_id == "CPI" else UrgencyLevel.HIGH,
            confidence_score=Decimal("100.00"),
            time_horizon=TimeHorizon.INTRADAY,
            release_status=status,
            raw_content_bytes=raw,
            mime_type="application/json",
            entity_links=tuple(
                EntityLinkPayload(
                    entity_type=(
                        EntityType.TICKER if symbol == "QQQ" else EntityType.MACRO_FACTOR
                    ),
                    entity_symbol=symbol,
                    relevance_score=Decimal("100.00"),
                )
                for symbol in entities
            ),
        )

    @staticmethod
    def _series(row: dict) -> str:
        series_id = str(row.get("series_id", "")).upper()
        if series_id not in BLS_ENTITIES:
            raise ValueError(f"unsupported BLS series {series_id}")
        return series_id
