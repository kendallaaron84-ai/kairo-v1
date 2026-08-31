from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from engine.risk.models import FillAccountingEvent, MarketMark, PnLSnapshot
from engine.risk.pnl_tracker import apply_fill


def test_pnl_formula_uses_exact_decimal_components() -> None:
    snapshot = PnLSnapshot(
        realized_pnl=Decimal("1.10"),
        unrealized_pnl=Decimal("2.20"),
        fees_usd=Decimal("0.10"),
        slippage_usd=Decimal("0.20"),
        net_pnl=Decimal("3.00"),
    )
    result = apply_fill(
        snapshot,
        FillAccountingEvent(
            fill_id=uuid4(),
            kairo_order_id=uuid4(),
            broker_account_id=uuid4(),
            instrument_id=uuid4(),
            realized_pnl_delta_usd=Decimal("0.3333333333"),
            commission_fees_usd=Decimal("0.03"),
            slippage_usd=Decimal("0.04"),
            fill_price=Decimal("10"),
            filled_qty=Decimal("1"),
            timestamp=datetime.now(UTC),
        ),
    )
    assert result.net_pnl == Decimal("3.5033333333")


def test_market_mark_derives_age_from_provenance_timestamps() -> None:
    received = datetime.now(UTC)
    mark = MarketMark(
        instrument_id=uuid4(),
        mark_price=Decimal("10"),
        source_timestamp=received - timedelta(seconds=1.25),
        received_at=received,
    )
    assert mark.quote_age() == timedelta(seconds=1.25)

    future = mark.model_copy(
        update={"source_timestamp": received + timedelta(milliseconds=1)}
    )
    assert future.quote_age() is None
