# Kairo Phase 2C — Trust Evaluation Policy Engine

Backend-only implementation of the Kairo persistence motor and deterministic risk governor.
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
current positions.

Migration `0003` adds explicit market sessions, an immutable risk-state event ledger, the
singleton restart-safe governor projection, and complete decision evidence. The governor starts
each session disarmed, classifies projected exposure rather than trusting intent labels, enforces
the frozen loss/profit/market-data/capability/capital gates, and emits deterministic cancellation
and emergency-exit requests. Those requests are commands only; this phase does not claim broker
submission, acknowledgement, execution, or fills. Phase 2C remains explicitly out of scope.

Migration `0004` persists each session's latest mark per instrument. Every mark update reloads
the canonical open-position portfolio and recomputes aggregate unrealized P&L across all
instruments, preventing one instrument's update from erasing every other position's contribution.

Migration `0005` extends the immutable trust-evaluation ledger with canonical capital-cell
lineage, evidence-window boundaries, Tier 1 eligibility, autonomy recommendations, and a SHA-256
evidence manifest. The Trust engine evaluates six policy-weighted process factors over W_20/W_50
closed-trade windows, recomputes from underlying facts, and records recommendations without
mutating the cell tier. Missing reconciliation, settlement, planned-risk, regime, reference-price,
or MFE/MAE evidence remains explicitly insufficient and cannot manufacture a score or promotion.

Phase 2C does not include broker integration, execution, Strategy 001 runtime, WebSockets, or UI
visualization work.
