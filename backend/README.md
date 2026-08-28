# Kairo Phase 2A — Persistence Motor

Backend-only implementation of the frozen Kairo Ledger & Execution Domain Standard v0.1.
It contains no broker adapters, trading execution, live feeds, AI agents, or strategy runtime.

## Local PostgreSQL stack

1. Copy `../.env.example` to `../.env` and replace both local passwords.
2. From the repository root, run `docker compose up --build`.
3. Verify `GET http://localhost:8000/health`.

Run the complete PostgreSQL suite with:

```bash
docker compose run --rm kairo-api pytest
```

The API container creates the credentialed `kairo_runtime` role from environment variables,
then applies Alembic migrations through `head`. Migration `0001` grants that role append/read
access to immutable ledger facts and controlled update access to configuration and projections.

Migration `0002` is the final Standard v0.1 conformance patch. It restores canonical option
identity, broker execution capabilities, intent purpose and sizing semantics, broker cash and
capital-authorization provenance, zero-evidence trust metadata, and capital-cell ownership of
current positions. Phase 2B remains explicitly out of scope.
