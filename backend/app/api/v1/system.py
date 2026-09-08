from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.schemas import (
    ApplicationStatus,
    DatabaseStatus,
    ResearchStatus,
    SystemStatusResponse,
)
from app.db.models.historical import HistoricalMarketArtifact, HistoricalMarketDataset
from app.db.session import get_db


router = APIRouter()
QUALIFICATION_MIME_TYPE = "application/vnd.kairo.corpus-qualification+json"


@router.get("/status", response_model=SystemStatusResponse)
def system_status(db: Session = Depends(get_db)) -> SystemStatusResponse:
    try:
        migration_head = db.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one_or_none()
        latest_dataset = db.scalar(
            select(HistoricalMarketDataset)
            .order_by(HistoricalMarketDataset.ingested_at.desc())
            .limit(1)
        )
        manifest_available = (
            db.scalar(
                select(HistoricalMarketArtifact.artifact_id)
                .where(HistoricalMarketArtifact.mime_type == QUALIFICATION_MIME_TYPE)
                .limit(1)
            )
            is not None
        )
    except SQLAlchemyError:
        migration_head = None
        latest_dataset = None
        manifest_available = False
    database_status = "HEALTHY" if migration_head == "0027" else "DEGRADED"
    return SystemStatusResponse(
        as_of=datetime.now(timezone.utc),
        overall_status=database_status,
        application=ApplicationStatus(status="HEALTHY", version="0.1.0"),
        database=DatabaseStatus(status=database_status, migration_head=migration_head),
        research=ResearchStatus(
            latest_dataset_id=latest_dataset.dataset_id if latest_dataset else None,
            latest_dataset_ingested_at=latest_dataset.ingested_at if latest_dataset else None,
            qualification_manifest_available=manifest_available,
        ),
    )
