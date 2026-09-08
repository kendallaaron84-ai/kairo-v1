import hashlib
from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, status
from google.api_core.exceptions import GoogleAPIError
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_artifact_storage
from app.api.schemas import (
    ArtifactIdentity,
    QualificationLatestResponse,
    ResearchMatrixResponse,
    ResearchMatrixStream,
)
from app.config import get_settings
from app.db.models.historical import (
    HistoricalMarketArtifact,
    HistoricalMarketDataset,
    HistoricalMarketDatasetSymbol,
)
from app.db.session import get_db
from engine.data.corpus_qualifier import CorpusQualificationManifest
from engine.intelligence.storage_driver import GCSReadOnlyArtifactStorage


router = APIRouter()
QUALIFICATION_MIME_TYPE = "application/vnd.kairo.corpus-qualification+json"


@dataclass(frozen=True)
class QualificationContext:
    artifact: HistoricalMarketArtifact
    dataset: HistoricalMarketDataset
    manifest: CorpusQualificationManifest


def _load_latest_qualification(
    db: Session, storage: GCSReadOnlyArtifactStorage
) -> QualificationContext:
    artifact = db.scalar(
        select(HistoricalMarketArtifact)
        .where(
            HistoricalMarketArtifact.mime_type == QUALIFICATION_MIME_TYPE,
            HistoricalMarketArtifact.artifact_role == "NORMALIZED_RESEARCH_STREAM",
        )
        .order_by(HistoricalMarketArtifact.created_at.desc())
        .limit(1)
    )
    if artifact is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no qualification manifest",
        )

    uri = get_settings().qualification_manifest_gcs_uri or artifact.storage_uri
    if not uri.startswith("gs://"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="canonical GCS qualification manifest URI is not configured",
        )
    try:
        content = storage.read_bytes(uri)
    except (ValueError, OSError, GoogleAPIError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="qualification artifact could not be read",
        ) from exc
    if (
        len(content) != artifact.byte_size
        or hashlib.sha256(content).hexdigest() != artifact.content_sha256
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="qualification artifact identity verification failed",
        )
    try:
        manifest = CorpusQualificationManifest.model_validate_json(content)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="qualification artifact schema validation failed",
        ) from exc

    dataset = db.scalar(
        select(HistoricalMarketDataset).where(
            HistoricalMarketDataset.dataset_manifest_sha256
            == manifest.normalized_dataset_manifest_sha256
        )
    )
    if dataset is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="qualification manifest does not resolve to a canonical dataset",
        )
    return QualificationContext(artifact=artifact, dataset=dataset, manifest=manifest)


@router.get("/qualification/latest", response_model=QualificationLatestResponse)
def qualification_latest(
    db: Session = Depends(get_db),
    storage: GCSReadOnlyArtifactStorage = Depends(get_artifact_storage),
) -> QualificationLatestResponse:
    context = _load_latest_qualification(db, storage)
    return QualificationLatestResponse(
        dataset_id=context.dataset.dataset_id,
        dataset_name=context.dataset.dataset_name,
        artifact=ArtifactIdentity.model_validate(context.artifact),
        qualification=context.manifest,
    )


@router.get("/matrix/q1-2024", response_model=ResearchMatrixResponse)
def research_matrix_q1_2024(
    db: Session = Depends(get_db),
    storage: GCSReadOnlyArtifactStorage = Depends(get_artifact_storage),
) -> ResearchMatrixResponse:
    context = _load_latest_qualification(db, storage)
    window = context.manifest.pilot_window
    if str(window.start_session) != "2024-01-02" or str(window.end_session) != "2024-03-28":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="latest qualification manifest is not the canonical Q1 2024 pilot",
        )
    entries = tuple(
        db.scalars(
            select(HistoricalMarketDatasetSymbol)
            .where(
                HistoricalMarketDatasetSymbol.dataset_id == context.dataset.dataset_id,
                HistoricalMarketDatasetSymbol.symbol.in_(("TQQQ", "SQQQ")),
            )
            .order_by(HistoricalMarketDatasetSymbol.stream_ordinal)
        )
    )
    return ResearchMatrixResponse(
        dataset_id=context.dataset.dataset_id,
        dataset_name=context.dataset.dataset_name,
        provider_name=context.dataset.provider_name,
        start_session=str(window.start_session),
        end_session=str(window.end_session),
        symbols=("TQQQ", "SQQQ"),
        streams=tuple(
            ResearchMatrixStream(
                instrument_id=entry.instrument_id,
                symbol=entry.symbol,
                stream_role=entry.stream_role,
                stream_ordinal=entry.stream_ordinal,
                observation_count=entry.bar_count,
                first_interval_start_at=entry.first_bar_start_at,
                last_completed_at=entry.last_bar_completed_at,
                raw_content_sha256=entry.raw_content_sha256,
                normalized_content_sha256=entry.normalized_content_sha256,
            )
            for entry in entries
        ),
    )
