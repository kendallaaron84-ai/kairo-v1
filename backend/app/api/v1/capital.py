from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import require_capital_api_token
from app.api.schemas import CapitalLedgerEntry, CapitalLedgersResponse
from app.db.models.ledger import KairoCapitalAuthorizationRecord
from app.db.models.projections import CapitalCell
from app.db.session import get_db


router = APIRouter()


@router.get(
    "/ledgers",
    response_model=CapitalLedgersResponse,
    dependencies=[Depends(require_capital_api_token)],
)
def capital_ledgers(
    cell_id: UUID | None = None,
    economic_domain: str | None = Query(default=None, pattern="^(LIVE|SYNTHETIC)$"),
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
) -> CapitalLedgersResponse:
    statement = (
        select(KairoCapitalAuthorizationRecord, CapitalCell.cell_code)
        .join(CapitalCell, CapitalCell.cell_id == KairoCapitalAuthorizationRecord.cell_id)
        .order_by(
            KairoCapitalAuthorizationRecord.computed_at.desc(),
            KairoCapitalAuthorizationRecord.authorization_id,
        )
        .limit(limit)
    )
    if cell_id is not None:
        statement = statement.where(KairoCapitalAuthorizationRecord.cell_id == cell_id)
    if economic_domain is not None:
        statement = statement.where(
            KairoCapitalAuthorizationRecord.economic_domain == economic_domain
        )
    rows = db.execute(statement).all()
    return CapitalLedgersResponse(
        items=tuple(
            CapitalLedgerEntry(
                authorization_id=record.authorization_id,
                cell_id=record.cell_id,
                cell_code=cell_code,
                economic_domain=record.economic_domain,
                settled_cash=record.settled_cash,
                safety_reserve=record.safety_reserve,
                ownership_treasury_reserved=record.ownership_treasury_reserved,
                replication_reserve=record.replication_reserve,
                committed_obligations=record.committed_obligations,
                authorized_trading_cash=record.authorized_trading_cash,
                computed_at=record.computed_at,
                broker_snapshot_id=record.broker_snapshot_id,
                synthetic_provenance_id=record.synthetic_provenance_id,
            )
            for record, cell_code in rows
        )
    )
