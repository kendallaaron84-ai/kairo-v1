from decimal import Decimal

from engine.risk.models import FillAccountingEvent, MarketMark, PnLSnapshot, PositionSnapshot


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
    mark: MarketMark,
    positions: list[PositionSnapshot],
) -> PnLSnapshot:
    marked = [position for position in positions if position.instrument_id == mark.instrument_id]
    unrealized = sum(
        (
            position.quantity
            * (mark.mark_price - position.average_price)
            * position.contract_multiplier
            for position in marked
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
