from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


MONEY = Numeric(28, 10)
VALID_STATES = (
    "'DISARMED', 'ARMED', 'LOCKED_FOR_DAY', 'HALTED_HARD', "
    "'FLAT_LOCKED', 'MANUAL_PAUSE'"
)


class RiskSession(Base):
    __tablename__ = "risk_sessions"
    __table_args__ = (
        CheckConstraint("session_close > session_open", name="valid_window"),
        UniqueConstraint("cell_id", "session_id", name="uq_risk_sessions_cell_session"),
        Index("ix_risk_sessions_trading_date_window", "trading_date", "session_open", "session_close"),
    )

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    cell_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("capital_cells.cell_id"), nullable=False
    )
    trading_date: Mapped[date] = mapped_column(Date, nullable=False)
    market_timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default="America/New_York"
    )
    session_open: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    session_close: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RiskStateEvent(Base):
    __tablename__ = "risk_state_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["cell_id", "session_id"], ["risk_sessions.cell_id", "risk_sessions.session_id"],
            name="fk_risk_state_events_cell_session",
        ),
        CheckConstraint(f"previous_state IN ({VALID_STATES})", name="valid_previous_state"),
        CheckConstraint(f"new_state IN ({VALID_STATES})", name="valid_new_state"),
        CheckConstraint("authorized_cash_usd >= 0", name="authorized_cash_nonnegative"),
        Index("ix_risk_state_events_session_recorded", "session_id", "recorded_at"),
    )

    event_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("uuid_generate_v4()"),
    )
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("risk_sessions.session_id"), nullable=False
    )
    cell_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("capital_cells.cell_id"), nullable=False
    )
    previous_state: Mapped[str] = mapped_column(String(32), nullable=False)
    new_state: Mapped[str] = mapped_column(String(32), nullable=False)
    trigger_reason: Mapped[str] = mapped_column(String(256), nullable=False)
    current_session_net_pnl: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    authorized_cash_usd: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RiskGovernorState(Base):
    __tablename__ = "risk_governor_state"
    __table_args__ = (
        ForeignKeyConstraint(
            ["cell_id", "current_session_id"],
            ["risk_sessions.cell_id", "risk_sessions.session_id"],
            name="fk_risk_governor_state_cell_session",
        ),
        CheckConstraint(f"operational_state IN ({VALID_STATES})", name="valid_state"),
        CheckConstraint(
            "session_fees_usd >= 0 AND session_slippage_usd >= 0",
            name="costs_nonnegative",
        ),
        CheckConstraint(
            "session_net_pnl = session_realized_pnl + session_unrealized_pnl "
            "- session_fees_usd",
            name="net_pnl_consistent",
        ),
    )

    cell_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("capital_cells.cell_id"), primary_key=True
    )
    current_session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("risk_sessions.session_id"), nullable=False
    )
    operational_state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="DISARMED"
    )
    session_realized_pnl: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    session_unrealized_pnl: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    session_fees_usd: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    session_slippage_usd: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    session_net_pnl: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    last_state_change_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class RiskInstrumentMark(Base):
    __tablename__ = "risk_instrument_marks"
    __table_args__ = (
        CheckConstraint("mark_price > 0", name="positive_mark_price"),
        CheckConstraint(
            "received_at >= source_timestamp", name="valid_mark_provenance"
        ),
        Index("ix_risk_instrument_marks_session_received", "session_id", "received_at"),
    )

    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("risk_sessions.session_id"), primary_key=True
    )
    instrument_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("instruments.instrument_id"), primary_key=True
    )
    mark_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    source_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
