FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml alembic.ini ./
COPY app ./app
COPY alembic ./alembic
COPY tests ./tests

RUN pip install --no-cache-dir ".[test]"

EXPOSE 8000
CMD ["sh", "-c", "python -m app.db.bootstrap && alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
