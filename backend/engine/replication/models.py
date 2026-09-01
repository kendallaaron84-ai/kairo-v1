import hashlib
import json
from datetime import datetime
from decimal import Decimal
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ProposalLifecycleState(str, Enum):
    INITIAL = "INITIAL"
    PENDING_AUTHORIZATION = "PENDING_AUTHORIZATION"
    AUTHORIZED = "AUTHORIZED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    EXECUTED = "EXECUTED"
    CANCELLED = "CANCELLED"


class ReservationEventType(str, Enum):
    RESERVED = "RESERVED"
    RELEASED = "RELEASED"
    CONSUMED = "CONSUMED"


class AuthorizationDecision(str, Enum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"


class ReplicationPolicyConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    threshold_usd: Decimal = Field(default=Decimal("100.00"), gt=0)
    capital_class: str = Field(default="MICRO-100-v1", min_length=1, max_length=32)
    proposed_autonomy_tier: str = Field(default="APPRENTICE", min_length=1, max_length=32)
    manifest_algorithm: str = "REPLICATION-PROPOSAL-v1"


class AllocationReservationRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    allocation_id: UUID
    reserved_usd: Decimal = Field(gt=0)


class ReplicationProposalManifest(BaseModel):
    model_config = ConfigDict(frozen=True)

    manifest_algorithm: str
    parent_cell_id: UUID
    proposed_child_code: str
    capital_class: str
    proposed_seed_capital_usd: Decimal
    strategy_identifier: str
    strategy_version: str
    risk_policy_identifier: str
    target_config_id: UUID
    target_type: str
    target_instrument_id: UUID
    target_symbol: str
    target_treasury_code: str
    proposed_autonomy_tier: str
    is_synthetic: bool
    created_at: datetime
    source_allocations: tuple[AllocationReservationRef, ...]

    @field_validator("created_at")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value

    def canonical_bytes(self) -> bytes:
        payload = self.model_dump(mode="json")
        return json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")

    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


class GenesisAllocationSource(BaseModel):
    model_config = ConfigDict(frozen=True)

    allocation_id: UUID
    reservation_id: UUID
    reserved_usd: Decimal = Field(gt=0)


class GenesisSeedManifest(BaseModel):
    model_config = ConfigDict(frozen=True)

    manifest_type: str = "GENESIS_SEED"
    manifest_algorithm: str = "GENESIS-SEED-MANIFEST-v1"
    model_identifier: str = "KAIRO-GENESIS"
    model_version: str = "1.0.0"
    proposal_id: UUID
    proposal_manifest_hash: str
    authorization_id: UUID
    parent_cell_id: UUID
    child_cell_id: UUID
    child_cell_code: str
    seed_capital_usd: Decimal
    strategy_identifier: str
    strategy_version: str
    risk_policy_identifier: str
    target_config_id: UUID
    target_type: str
    target_instrument_id: UUID
    target_symbol: str
    target_treasury_code: str
    created_at: datetime
    source_allocations: tuple[GenesisAllocationSource, ...]

    @field_validator("created_at")
    @classmethod
    def genesis_timestamp_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")

    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    def source_refs(self) -> dict:
        return {
            "proposal_id": str(self.proposal_id),
            "authorization_id": str(self.authorization_id),
            "allocations": [item.model_dump(mode="json") for item in self.source_allocations],
        }
