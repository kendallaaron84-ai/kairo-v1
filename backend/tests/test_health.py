import pytest
from fastapi.testclient import TestClient


pytestmark = pytest.mark.integration


def test_health_reports_database_health(migrated_database: tuple[str, str]) -> None:
    from app.main import app

    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "application": "healthy", "database": "healthy", "status": "healthy"
    }
