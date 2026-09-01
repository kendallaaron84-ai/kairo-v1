import hashlib
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SourceType(StrEnum):
    PRIMARY = "PRIMARY"
    SECONDARY = "SECONDARY"
    SOCIAL = "SOCIAL"
    USER_SUBMITTED = "USER_SUBMITTED"


class EventType(StrEnum):
    EARNINGS = "EARNINGS"
    MACRO = "MACRO"
    REGULATORY = "REGULATORY"
    GEOPOLITICAL = "GEOPOLITICAL"
    M_AND_A = "M_AND_A"
    LEGAL = "LEGAL"
    MANAGEMENT = "MANAGEMENT"
    PRODUCT = "PRODUCT"
    CAPITAL_RAISE = "CAPITAL_RAISE"
    LEGISLATION = "LEGISLATION"
    INSIDER_ACTIVITY = "INSIDER_ACTIVITY"
    CUSTOM_CLAIM = "CUSTOM_CLAIM"


class ImpactScope(StrEnum):
    MARKET = "MARKET"
    SECTOR = "SECTOR"
    THEME = "THEME"
    COMPANY = "COMPANY"


class UrgencyLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class TimeHorizon(StrEnum):
    INTRADAY = "INTRADAY"
    DAYS = "DAYS"
    MONTHS = "MONTHS"
    STRUCTURAL = "STRUCTURAL"


class ReleaseStatus(StrEnum):
    SCHEDULED = "SCHEDULED"
    RELEASED = "RELEASED"
    REVISED = "REVISED"


class EntityType(StrEnum):
    TICKER = "TICKER"
    THEME = "THEME"
    SECTOR = "SECTOR"
    MACRO_FACTOR = "MACRO_FACTOR"


class EntityLinkPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    entity_type: EntityType
    entity_symbol: str = Field(min_length=1, max_length=32)
    relevance_score: Decimal = Field(ge=0, le=100, max_digits=5, decimal_places=2)

    @field_validator("entity_symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("entity_symbol cannot be blank")
        return normalized


class IntelligenceIngestPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_type: SourceType
    source_name: str = Field(min_length=1, max_length=128)
    source_uri: str | None = Field(default=None, max_length=1024)
    event_type: EventType
    title: str = Field(min_length=1, max_length=256)
    summary: str = Field(min_length=1)
    published_at: datetime
    observed_at: datetime
    impact_scope: ImpactScope
    urgency: UrgencyLevel
    confidence_score: Decimal = Field(ge=0, le=100, max_digits=5, decimal_places=2)
    time_horizon: TimeHorizon
    release_status: ReleaseStatus = ReleaseStatus.RELEASED
    referenced_event_id: UUID | None = None
    raw_content_bytes: bytes = Field(min_length=1)
    mime_type: str = Field(default="text/plain", min_length=1, max_length=64)
    entity_links: tuple[EntityLinkPayload, ...] = ()

    @field_validator("published_at", "observed_at")
    @classmethod
    def validate_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("datetime fields must be timezone-aware")
        return value

    @field_validator("source_name", "title", "summary", "mime_type")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text fields cannot be blank")
        return value.strip()

    def compute_raw_content_sha256(self) -> str:
        return hashlib.sha256(self.raw_content_bytes).hexdigest()
