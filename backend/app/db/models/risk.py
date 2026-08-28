from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
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
        Index("ix_risk_sessions_trading_date_window", "trading_date", "session_open", "session_close"),
    )

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
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
        CheckConstraint("singleton_key = 1", name="singleton"),
        CheckConstraint(f"operational_state IN ({VALID_STATES})", name="valid_state"),
        CheckConstraint(
            "session_fees_usd >= 0 AND session_slippage_usd >= 0",
            name="costs_nonnegative",
        ),
        CheckConstraint(
            "session_net_pnl = session_realized_pnl + session_unrealized_pnl "
            "- session_fees_usd - session_slippage_usd",
            name="net_pnl_consistent",
        ),
    )

    singleton_key: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
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
