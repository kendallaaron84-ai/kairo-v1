import hmac
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import get_settings
from engine.intelligence.storage_driver import GCSReadOnlyArtifactStorage


capital_bearer = HTTPBearer(auto_error=False)


def require_capital_api_token(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(capital_bearer)
    ],
) -> None:
    settings = get_settings()
    configured = settings.capital_api_token
    if configured is None or not configured.get_secret_value():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="capital API authentication is not configured",
        )
    if (
        credentials is None
        or credentials.scheme.lower() != "bearer"
        or not hmac.compare_digest(
            credentials.credentials, configured.get_secret_value()
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid capital API credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )


@lru_cache
def get_artifact_storage() -> GCSReadOnlyArtifactStorage:
    settings = get_settings()
    return GCSReadOnlyArtifactStorage(expected_bucket=settings.artifact_gcs_bucket)
