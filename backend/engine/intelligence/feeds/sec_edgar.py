from datetime import UTC, datetime
from decimal import Decimal

from engine.intelligence.feeds.base import BaseFeedAdapter
from engine.intelligence.feeds.parsing import aware_datetime, json_object
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


class SecEdgarAdapter(BaseFeedAdapter):
    FORMS = {"10-K", "10-Q", "8-K"}

    def __init__(
        self,
        http_client: object,
        *,
        cik: str,
        ticker: str,
        observed_clock=lambda: datetime.now(UTC),
    ) -> None:
        super().__init__(http_client)
        digits = "".join(character for character in cik if character.isdigit())
        if not digits:
            raise ValueError("SEC CIK must contain digits")
        self.cik = digits
        self.ticker = ticker.strip().upper()
        self.observed_clock = observed_clock
        policy = getattr(http_client, "policy", None)
        if policy is not None and "@" not in policy.user_agent:
            raise ValueError("SEC User-Agent must include operator contact information")

    @property
    def provider_name(self) -> str:
        return "SEC_EDGAR"

    @property
    def submissions_url(self) -> str:
        return f"https://data.sec.gov/submissions/CIK{int(self.cik):010d}.json"

    def poll_feed(self) -> list[IntelligenceIngestPayload]:
        metadata_response = self.http.get(
            self.submissions_url, headers={"Accept": "application/json"}
        )
        if metadata_response.status_code == 304:
            return []
        metadata = json_object(metadata_response.content)
        recent = metadata.get("filings", {}).get("recent")
        if not isinstance(recent, dict):
            raise ValueError("SEC submissions metadata lacks filings.recent")
        required = ("accessionNumber", "primaryDocument", "form", "filingDate")
        if any(not isinstance(recent.get(key), list) for key in required):
            raise ValueError("SEC submissions metadata arrays are incomplete")
        lengths = {len(recent[key]) for key in required}
        if len(lengths) != 1:
            raise ValueError("SEC submissions metadata arrays are misaligned")

        payloads: list[IntelligenceIngestPayload] = []
        for accession, document, form, filing_date in zip(
            recent["accessionNumber"],
            recent["primaryDocument"],
            recent["form"],
            recent["filingDate"],
            strict=True,
        ):
            if form not in self.FORMS:
                continue
            accession_compact = str(accession).replace("-", "")
            document_url = (
                "https://www.sec.gov/Archives/edgar/data/"
                f"{int(self.cik)}/{accession_compact}/{document}"
            )
            document_response = self.http.get(
                document_url,
                headers={
                    "Accept": "text/html,text/plain,application/pdf",
                    "If-Modified-Since": self._last_modified(metadata_response.headers),
                },
            )
            if document_response.status_code == 304:
                continue
            if not document_response.content:
                raise ValueError("SEC primary document response is empty")
            content_type = self._content_type(document_response.headers)
            payloads.append(
                IntelligenceIngestPayload(
                    source_type=SourceType.PRIMARY,
                    source_name=self.provider_name,
                    source_uri=document_url,
                    event_type=(
                        EventType.EARNINGS if form in {"10-K", "10-Q"} else EventType.REGULATORY
                    ),
                    title=f"{self.ticker} {form} {accession}",
                    summary=f"Official SEC {form} filing; accessionNumber={accession}",
                    published_at=aware_datetime(str(filing_date)),
                    observed_at=self.observed_clock(),
                    impact_scope=ImpactScope.COMPANY,
                    urgency=UrgencyLevel.HIGH if form == "8-K" else UrgencyLevel.MEDIUM,
                    confidence_score=Decimal("100.00"),
                    time_horizon=TimeHorizon.MONTHS,
                    release_status=ReleaseStatus.RELEASED,
                    raw_content_bytes=document_response.content,
                    mime_type=content_type,
                    entity_links=(EntityLinkPayload(
                        entity_type=EntityType.TICKER,
                        entity_symbol=self.ticker,
                        relevance_score=Decimal("100.00"),
                    ),),
                )
            )
        return payloads

    @staticmethod
    def _content_type(headers: object) -> str:
        value = next(
            (v for k, v in dict(headers).items() if k.lower() == "content-type"),
            "text/html",
        )
        return value.split(";", 1)[0].strip()

    @staticmethod
    def _last_modified(headers: object) -> str:
        return next(
            (v for k, v in dict(headers).items() if k.lower() == "last-modified"), ""
        )
