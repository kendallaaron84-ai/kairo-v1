import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from html.parser import HTMLParser
from xml.etree import ElementTree

from engine.intelligence.feeds.base import BaseFeedAdapter
from engine.intelligence.feeds.parsing import aware_datetime, require_list
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


class CorporateIrFormat(StrEnum):
    RSS = "RSS"
    ATOM = "ATOM"
    JSON = "JSON"
    HTML = "HTML"


@dataclass(frozen=True)
class CorporateIrProfile:
    symbol: str
    url: str
    feed_format: CorporateIrFormat
    event_type: EventType = EventType.PRODUCT
    html_item_class: str = "release"


class _ConfiguredHtmlParser(HTMLParser):
    def __init__(self, item_class: str) -> None:
        super().__init__()
        self.item_class = item_class
        self.items: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = (values.get("class") or "").split()
        if tag == "article" and self.item_class in classes:
            self._current = {
                "title": values.get("data-title") or "",
                "published_at": values.get("data-published-at") or "",
                "url": values.get("data-url") or "",
            }
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._current is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "article" and self._current is not None:
            self._current["summary"] = " ".join("".join(self._text).split())
            self.items.append(self._current)
            self._current = None
            self._text = []


class CorporateIrAdapter(BaseFeedAdapter):
    def __init__(
        self,
        http_client: object,
        *,
        profiles: tuple[CorporateIrProfile, ...],
        observed_clock=lambda: datetime.now(UTC),
    ) -> None:
        super().__init__(http_client)
        if not profiles:
            raise ValueError("at least one corporate IR profile is required")
        self.profiles = profiles
        self.observed_clock = observed_clock

    @property
    def provider_name(self) -> str:
        return "CORPORATE_IR"

    def poll_feed(self) -> list[IntelligenceIngestPayload]:
        payloads: list[IntelligenceIngestPayload] = []
        for profile in self.profiles:
            response = self.http.get(profile.url)
            if response.status_code == 304:
                continue
            rows = self._parse(profile, response.content)
            for row in rows:
                title = str(row.get("title") or "").strip()
                summary = str(row.get("summary") or title).strip()
                if not title or not summary or not row.get("published_at"):
                    raise ValueError(f"{profile.symbol} IR item lacks canonical fields")
                payloads.append(
                    IntelligenceIngestPayload(
                        source_type=SourceType.PRIMARY,
                        source_name=self.provider_name,
                        source_uri=str(row.get("url") or profile.url),
                        event_type=profile.event_type,
                        title=title,
                        summary=summary,
                        published_at=aware_datetime(str(row["published_at"])),
                        effective_at=aware_datetime(str(row["published_at"])),
                        observed_at=self.observed_clock(),
                        impact_scope=ImpactScope.COMPANY,
                        urgency=(
                            UrgencyLevel.HIGH
                            if profile.event_type is EventType.EARNINGS
                            else UrgencyLevel.MEDIUM
                        ),
                        confidence_score=Decimal("100.00"),
                        time_horizon=TimeHorizon.DAYS,
                        release_status=ReleaseStatus.RELEASED,
                        raw_content_bytes=response.content,
                        mime_type=self._mime(profile.feed_format),
                        entity_links=(EntityLinkPayload(
                            entity_type=EntityType.TICKER,
                            entity_symbol=profile.symbol,
                            relevance_score=Decimal("100.00"),
                        ),),
                    )
                )
        return payloads

    def _parse(self, profile: CorporateIrProfile, content: bytes) -> list[dict]:
        if profile.feed_format is CorporateIrFormat.JSON:
            try:
                root = json.loads(content)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("corporate IR JSON is malformed") from exc
            if not isinstance(root, dict):
                raise ValueError("corporate IR JSON root must be an object")
            return require_list(root.get("items"), "items")
        if profile.feed_format in (CorporateIrFormat.RSS, CorporateIrFormat.ATOM):
            return self._parse_xml(profile.feed_format, content)
        parser = _ConfiguredHtmlParser(profile.html_item_class)
        try:
            parser.feed(content.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise ValueError("corporate IR HTML is not UTF-8") from exc
        if not parser.items:
            raise ValueError("corporate IR HTML profile matched no items")
        return parser.items

    @staticmethod
    def _parse_xml(feed_format: CorporateIrFormat, content: bytes) -> list[dict]:
        try:
            root = ElementTree.fromstring(content)
        except ElementTree.ParseError as exc:
            raise ValueError("corporate IR XML is malformed") from exc
        if feed_format is CorporateIrFormat.RSS:
            return [
                {
                    "title": item.findtext("title", ""),
                    "summary": item.findtext("description", ""),
                    "published_at": item.findtext("pubDate", ""),
                    "url": item.findtext("link", ""),
                }
                for item in root.findall("./channel/item")
            ]
        namespace = {"atom": "http://www.w3.org/2005/Atom"}
        rows = []
        for entry in root.findall("atom:entry", namespace):
            link = entry.find("atom:link", namespace)
            rows.append({
                "title": entry.findtext("atom:title", "", namespace),
                "summary": entry.findtext("atom:summary", "", namespace),
                "published_at": entry.findtext("atom:updated", "", namespace),
                "url": link.get("href", "") if link is not None else "",
            })
        return rows

    @staticmethod
    def _mime(feed_format: CorporateIrFormat) -> str:
        return {
            CorporateIrFormat.JSON: "application/json",
            CorporateIrFormat.HTML: "text/html",
            CorporateIrFormat.RSS: "application/rss+xml",
            CorporateIrFormat.ATOM: "application/atom+xml",
        }[feed_format]
