from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from engine.data.corpus_qualifier import CorpusQualificationManifest


class ApplicationStatus(BaseModel):
    status: Literal["HEALTHY", "DEGRADED"]
    version: str


class DatabaseStatus(BaseModel):
    status: Literal["HEALTHY", "DEGRADED"]
    migration_head: str | None


class ResearchStatus(BaseModel):
    latest_dataset_id: UUID | None
    latest_dataset_ingested_at: datetime | None
    qualification_manifest_available: bool


class SystemStatusResponse(BaseModel):
    schema_version: Literal["kairo.system-status.v1"] = "kairo.system-status.v1"
    as_of: datetime
    overall_status: Literal["HEALTHY", "DEGRADED"]
    application: ApplicationStatus
    database: DatabaseStatus
    research: ResearchStatus


class ArtifactIdentity(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    artifact_id: UUID
    content_sha256: str
    byte_size: int
    created_at: datetime


class QualificationLatestResponse(BaseModel):
    schema_version: Literal["kairo.corpus-qualification-response.v1"] = (
        "kairo.corpus-qualification-response.v1"
    )
    dataset_id: UUID
    dataset_name: str
    artifact: ArtifactIdentity
    qualification: CorpusQualificationManifest


class ResearchMatrixStream(BaseModel):
    instrument_id: UUID
    symbol: str
    stream_role: str
    stream_ordinal: int
    observation_count: int
    first_interval_start_at: datetime
    last_completed_at: datetime
    raw_content_sha256: str
    normalized_content_sha256: str


class ResearchMatrixResponse(BaseModel):
    schema_version: Literal["kairo.research-matrix.v1"] = "kairo.research-matrix.v1"
    dataset_id: UUID
    dataset_name: str
    provider_name: str
    start_session: str
    end_session: str
    symbols: tuple[str, ...]
    streams: tuple[ResearchMatrixStream, ...]


class CapitalLedgerEntry(BaseModel):
    authorization_id: UUID
    cell_id: UUID
    cell_code: str
    economic_domain: str
    settled_cash: Decimal
    safety_reserve: Decimal
    ownership_treasury_reserved: Decimal
    replication_reserve: Decimal
    committed_obligations: Decimal
    authorized_trading_cash: Decimal
    computed_at: datetime
    broker_snapshot_id: UUID | None
    synthetic_provenance_id: UUID | None


class CapitalLedgersResponse(BaseModel):
    schema_version: Literal["kairo.capital-ledger-list.v1"] = (
        "kairo.capital-ledger-list.v1"
    )
    items: tuple[CapitalLedgerEntry, ...]
