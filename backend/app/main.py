from fastapi import Depends, FastAPI
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.session import get_db


class HealthResponse(BaseModel):
    application: str
    database: str
    status: str


settings = get_settings()
app = FastAPI(title=settings.app_name, version="0.1.0")


@app.get("/health", response_model=HealthResponse)
def health(db: Session = Depends(get_db)) -> HealthResponse:
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        return HealthResponse(application="healthy", database="unhealthy", status="degraded")
    return HealthResponse(application="healthy", database="healthy", status="healthy")
