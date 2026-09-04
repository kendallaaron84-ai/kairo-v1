FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/backend

WORKDIR /app

COPY backend/pyproject.toml ./backend/pyproject.toml
COPY backend/app ./backend/app
COPY backend/engine ./backend/engine
COPY backend/alembic ./backend/alembic
COPY backend/alembic.ini ./backend/alembic.ini
COPY scripts ./scripts

RUN pip install --no-cache-dir "./backend" "google-cloud-storage>=2.18,<3"

CMD ["python", "scripts/data/cloud_smoke_test.py"]
