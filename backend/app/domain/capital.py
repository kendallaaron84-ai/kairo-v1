from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class KairoCapitalAuthorization(BaseModel):
    """Frozen v0.1 capital authorization fact.

    ATC = max(0, SC - SR - OT - RR - CO)
    """

    model_config = ConfigDict(frozen=True)

    authorization_id: UUID = Field(default_factory=uuid4)
    cell_id: UUID
    broker_snapshot_id: UUID
    broker_account_id: UUID
    settled_cash: Decimal
    safety_reserve: Decimal
    ownership_treasury_reserved: Decimal
    replication_reserve: Decimal
    committed_obligations: Decimal
    authorized_trading_cash: Decimal
    computed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def amounts_are_non_negative(self) -> "KairoCapitalAuthorization":
        amounts = (
            self.settled_cash,
            self.safety_reserve,
            self.ownership_treasury_reserved,
            self.replication_reserve,
            self.committed_obligations,
            self.authorized_trading_cash,
        )
        if any(amount < Decimal("0") for amount in amounts):
            raise ValueError("capital authorization inputs and result must be non-negative")
        return self

    @classmethod
    def compute(
        cls,
        *,
        cell_id: UUID,
        broker_snapshot_id: UUID,
        broker_account_id: UUID,
        settled_cash: Decimal,
        safety_reserve: Decimal = Decimal("0"),
        ownership_treasury_reserved: Decimal = Decimal("0"),
        replication_reserve: Decimal = Decimal("0"),
        committed_obligations: Decimal = Decimal("0"),
        computed_at: datetime | None = None,
    ) -> "KairoCapitalAuthorization":
        inputs = (
            settled_cash,
            safety_reserve,
            ownership_treasury_reserved,
            replication_reserve,
            committed_obligations,
        )
        if any(value < Decimal("0") for value in inputs):
            raise ValueError("capital authorization inputs must be non-negative")
        authorized = max(
            Decimal("0"),
            settled_cash
            - safety_reserve
            - ownership_treasury_reserved
            - replication_reserve
            - committed_obligations,
        )
        return cls(
            authorization_id=uuid4(),
            cell_id=cell_id,
            broker_snapshot_id=broker_snapshot_id,
            broker_account_id=broker_account_id,
            settled_cash=settled_cash,
            safety_reserve=safety_reserve,
            ownership_treasury_reserved=ownership_treasury_reserved,
            replication_reserve=replication_reserve,
            committed_obligations=committed_obligations,
            authorized_trading_cash=authorized,
            computed_at=computed_at or datetime.now(UTC),
        )


class BrokerCashSnapshotFact(BaseModel):
    model_config = ConfigDict(frozen=True)

    snapshot_id: UUID = Field(default_factory=uuid4)
    broker_account_id: UUID
    broker_cash: Decimal = Field(ge=0)
    settled_cash: Decimal = Field(ge=0)
    unsettled_cash: Decimal = Field(ge=0)
    buying_power: Decimal = Field(ge=0)
    currency: str = "USD"
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SiphonEventFact(BaseModel):
    model_config = ConfigDict(frozen=True)

    siphon_id: UUID = Field(default_factory=uuid4)
    cell_id: UUID
    treasury_code: str
    amount: Decimal = Field(gt=0)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    reason_code: str


class CapitalCellProjection(BaseModel):
    cell_id: UUID = Field(default_factory=uuid4)
    cell_code: str
    seed_capital: Decimal = Field(ge=0)
    status: str
    autonomy_tier: str = "APPRENTICE"
    strategy_id: str
    strategy_version: str
    target_treasury_code: str


class OwnershipTreasuryHoldingProjection(BaseModel):
    holding_id: UUID = Field(default_factory=uuid4)
    treasury_code: str
    cell_id: UUID
    instrument_id: UUID
    symbol: str
    is_synthetic: bool
    # Retained legacy semantics. These are synchronized only when equivalence
    # to the execution-derived fields has been established explicitly.
    dollars_contributed: Decimal = Field(default=Decimal("0"), ge=0)
    fractional_shares: Decimal = Field(default=Decimal("0"), ge=0)
    legacy_values_equivalent: bool = False
    total_shares: Decimal = Field(default=Decimal("0"), ge=0)
    cumulative_cost_basis_usd: Decimal = Field(default=Decimal("0"), ge=0)
    average_entry_price_usd: Decimal = Field(default=Decimal("0"), ge=0)
    last_marked_price_usd: Decimal | None = Field(default=None, ge=0)
    market_value_usd: Decimal = Field(default=Decimal("0"), ge=0)
    unrealized_pnl_usd: Decimal = Decimal("0")
