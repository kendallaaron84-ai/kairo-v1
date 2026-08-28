from decimal import Decimal

from uuid import UUID

from engine.risk.models import FillAccountingEvent, PnLSnapshot, PositionSnapshot


def net_pnl(
    realized: Decimal, unrealized: Decimal, fees: Decimal, slippage: Decimal
) -> Decimal:
    return realized + unrealized - fees - slippage


def apply_fill(snapshot: PnLSnapshot, event: FillAccountingEvent) -> PnLSnapshot:
    realized = snapshot.realized_pnl + event.realized_pnl_delta_usd
    fees = snapshot.fees_usd + event.commission_fees_usd
    slippage = snapshot.slippage_usd + event.slippage_usd
    return PnLSnapshot(
        realized_pnl=realized,
        unrealized_pnl=snapshot.unrealized_pnl,
        fees_usd=fees,
        slippage_usd=slippage,
        net_pnl=net_pnl(realized, snapshot.unrealized_pnl, fees, slippage),
    )


def mark_to_market(
    snapshot: PnLSnapshot,
    latest_marks: dict[UUID, Decimal],
    positions: list[PositionSnapshot],
) -> PnLSnapshot:
    missing = {
        position.instrument_id
        for position in positions
        if position.quantity != 0 and position.instrument_id not in latest_marks
    }
    if missing:
        raise ValueError(f"portfolio marks missing for instruments: {sorted(map(str, missing))}")
    unrealized = sum(
        (
            position.quantity
            * (latest_marks[position.instrument_id] - position.average_price)
            * position.contract_multiplier
            for position in positions
            if position.quantity != 0
        ),
        start=Decimal("0"),
    )
    return PnLSnapshot(
        realized_pnl=snapshot.realized_pnl,
        unrealized_pnl=unrealized,
        fees_usd=snapshot.fees_usd,
        slippage_usd=snapshot.slippage_usd,
        net_pnl=net_pnl(
            snapshot.realized_pnl, unrealized, snapshot.fees_usd, snapshot.slippage_usd
        ),
    )
