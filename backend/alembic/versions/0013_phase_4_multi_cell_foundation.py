"""Phase 4 Step 3F: canonical multi-cell and synthetic evidence foundation.

Revision ID: 0013
Revises: 0012
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
MONEY = sa.Numeric(28, 10)
DEFAULT_POLICY_ID = "a0000000-0000-0000-0000-000000000001"


def upgrade() -> None:
    op.create_table(
        "risk_policies",
        sa.Column("policy_id", UUID, primary_key=True),
        sa.Column("policy_identifier", sa.String(64), nullable=False, unique=True),
        sa.Column("daily_loss_floor_usd", MONEY, nullable=False),
        sa.Column("daily_profit_lock_usd", MONEY, nullable=False),
        sa.Column("market_stale_seconds", sa.Numeric(10, 4), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("daily_loss_floor_usd < 0", name="loss_floor_negative"),
        sa.CheckConstraint("daily_profit_lock_usd > 0", name="profit_lock_positive"),
        sa.CheckConstraint("market_stale_seconds > 0", name="stale_seconds_positive"),
    )
    op.execute(
        "INSERT INTO risk_policies "
        "(policy_id, policy_identifier, daily_loss_floor_usd, "
        "daily_profit_lock_usd, market_stale_seconds, created_at) VALUES "
        f"('{DEFAULT_POLICY_ID}', 'RISK-v0.1', -6.00, 20.00, 1.5, "
        "'2026-08-31T00:00:00+00:00')"
    )

    op.add_column("capital_cells", sa.Column("risk_policy_id", UUID))
    op.add_column("capital_cells", sa.Column("economic_domain", sa.String(32)))
    op.create_foreign_key(
        "fk_capital_cells_risk_policy",
        "capital_cells",
        "risk_policies",
        ["risk_policy_id"],
        ["policy_id"],
    )
    # Existing cell history must prove its domain through canonical economic facts.
    op.execute(
        f"""
        DO $$
        DECLARE r RECORD; v_live BOOLEAN; v_synthetic BOOLEAN;
        BEGIN
          FOR r IN SELECT cell_id, cell_code FROM capital_cells LOOP
            SELECT
              EXISTS (
                SELECT 1 FROM fills f
                JOIN kairo_orders ko ON ko.kairo_order_id = f.kairo_order_id
                JOIN order_intents oi ON oi.intent_id = ko.intent_id
                WHERE oi.cell_id = r.cell_id AND f.is_simulated = false
              ) OR EXISTS (
                SELECT 1 FROM siphon_events s
                WHERE s.cell_id = r.cell_id AND s.is_synthetic = false
              ),
              EXISTS (
                SELECT 1 FROM fills f
                JOIN kairo_orders ko ON ko.kairo_order_id = f.kairo_order_id
                JOIN order_intents oi ON oi.intent_id = ko.intent_id
                WHERE oi.cell_id = r.cell_id AND f.is_simulated = true
              ) OR EXISTS (
                SELECT 1 FROM siphon_events s
                WHERE s.cell_id = r.cell_id AND s.is_synthetic = true
              )
            INTO v_live, v_synthetic;
            IF v_live AND v_synthetic THEN
              UPDATE capital_cells SET economic_domain = 'LEGACY_MIXED',
                risk_policy_id = '{DEFAULT_POLICY_ID}' WHERE cell_id = r.cell_id;
            ELSIF v_live THEN
              UPDATE capital_cells SET economic_domain = 'LIVE',
                risk_policy_id = '{DEFAULT_POLICY_ID}' WHERE cell_id = r.cell_id;
            ELSIF v_synthetic THEN
              UPDATE capital_cells SET economic_domain = 'SYNTHETIC',
                risk_policy_id = '{DEFAULT_POLICY_ID}' WHERE cell_id = r.cell_id;
            ELSE
              RAISE EXCEPTION 'Cell % (%) has no verifiable economic-domain evidence',
                r.cell_code, r.cell_id;
            END IF;
          END LOOP;
        END $$;
        """
    )
    op.alter_column("capital_cells", "risk_policy_id", nullable=False)
    op.alter_column("capital_cells", "economic_domain", nullable=False)
    op.create_check_constraint(
        op.f("ck_capital_cells_economic_domain"),
        "capital_cells",
        "economic_domain IN ('LIVE', 'SYNTHETIC', 'LEGACY_MIXED')",
    )

    op.create_table(
        "synthetic_evidence_manifests",
        sa.Column("manifest_id", UUID, primary_key=True),
        sa.Column("manifest_type", sa.String(32), nullable=False),
        sa.Column("manifest_hash", sa.CHAR(64), nullable=False),
        sa.Column("manifest_algorithm", sa.String(64), nullable=False),
        sa.Column("cell_id", UUID, sa.ForeignKey("capital_cells.cell_id"), nullable=False),
        sa.Column("source_count", sa.Integer, nullable=False),
        sa.Column("source_refs", postgresql.JSONB, nullable=False),
        sa.Column("model_identifier", sa.String(64), nullable=False),
        sa.Column("model_version", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("source_count >= 0", name="source_count_nonnegative"),
        sa.CheckConstraint(
            "manifest_hash ~ '^[0-9a-f]{64}$'", name="manifest_hash_sha256"
        ),
        sa.UniqueConstraint(
            "manifest_type", "manifest_algorithm", "cell_id", "manifest_hash",
            "model_identifier", "model_version", name="uq_synthetic_manifest_identity"
        ),
    )
    op.execute(
        """
        CREATE FUNCTION reject_synthetic_manifest_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
          RAISE EXCEPTION 'synthetic_evidence_manifests is append-only';
        END $$;
        CREATE TRIGGER trg_synthetic_manifest_append_only
        BEFORE UPDATE OR DELETE ON synthetic_evidence_manifests
        FOR EACH ROW EXECUTE FUNCTION reject_synthetic_manifest_mutation();
        GRANT SELECT, INSERT ON synthetic_evidence_manifests TO kairo_runtime;
        REVOKE UPDATE, DELETE ON synthetic_evidence_manifests FROM kairo_runtime;
        """
    )

    op.alter_column("kairo_capital_authorizations", "broker_snapshot_id", nullable=True)
    op.alter_column("kairo_capital_authorizations", "broker_account_id", nullable=True)
    op.add_column("kairo_capital_authorizations", sa.Column("economic_domain", sa.String(32)))
    op.add_column("kairo_capital_authorizations", sa.Column("synthetic_provenance_id", UUID))
    op.execute(
        "UPDATE kairo_capital_authorizations SET economic_domain = 'LIVE' "
        "WHERE broker_snapshot_id IS NOT NULL AND broker_account_id IS NOT NULL"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM kairo_capital_authorizations "
        "WHERE economic_domain IS NULL) THEN RAISE EXCEPTION "
        "'capital authorization provenance cannot be classified'; END IF; END $$;"
    )
    op.alter_column("kairo_capital_authorizations", "economic_domain", nullable=False)
    op.create_foreign_key(
        "fk_capital_authorizations_cell",
        "kairo_capital_authorizations",
        "capital_cells",
        ["cell_id"],
        ["cell_id"],
    )
    op.create_foreign_key(
        "fk_capital_authorizations_synthetic_manifest",
        "kairo_capital_authorizations",
        "synthetic_evidence_manifests",
        ["synthetic_provenance_id"],
        ["manifest_id"],
    )
    op.create_check_constraint(
        op.f("ck_kairo_capital_authorizations_provenance_exclusive"),
        "kairo_capital_authorizations",
        "(economic_domain = 'LIVE' AND broker_account_id IS NOT NULL AND "
        "broker_snapshot_id IS NOT NULL AND synthetic_provenance_id IS NULL) OR "
        "(economic_domain = 'SYNTHETIC' AND broker_account_id IS NULL AND "
        "broker_snapshot_id IS NULL AND synthetic_provenance_id IS NOT NULL)",
    )
    op.execute(
        """
        CREATE FUNCTION enforce_capital_authorization_manifest_cell() RETURNS trigger
        LANGUAGE plpgsql AS $$ DECLARE v_cell UUID; BEGIN
          IF NEW.economic_domain = 'SYNTHETIC' THEN
            SELECT cell_id INTO v_cell FROM synthetic_evidence_manifests
              WHERE manifest_id = NEW.synthetic_provenance_id;
            IF v_cell IS NULL THEN
              RAISE EXCEPTION 'synthetic provenance manifest does not resolve';
            END IF;
            IF v_cell <> NEW.cell_id THEN
              RAISE EXCEPTION 'synthetic provenance manifest belongs to another cell';
            END IF;
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER trg_capital_authorization_manifest_cell
        BEFORE INSERT OR UPDATE ON kairo_capital_authorizations
        FOR EACH ROW EXECUTE FUNCTION enforce_capital_authorization_manifest_cell();
        """
    )

    for table in ("risk_sessions", "risk_state_events", "risk_governor_state"):
        op.add_column(table, sa.Column("cell_id", UUID))
    op.execute(
        """
        DO $$ DECLARE v_count INTEGER; v_a001 UUID; v_has_history BOOLEAN; BEGIN
          SELECT EXISTS(SELECT 1 FROM risk_sessions) OR
                 EXISTS(SELECT 1 FROM risk_state_events) OR
                 EXISTS(SELECT 1 FROM risk_governor_state) INTO v_has_history;
          IF v_has_history THEN
            SELECT COUNT(*), min(cell_id) FILTER (WHERE cell_code = 'A001')
              INTO v_count, v_a001 FROM capital_cells;
            IF v_count <> 1 OR v_a001 IS NULL THEN
              RAISE EXCEPTION 'legacy risk history requires unique A001 proof';
            END IF;
            UPDATE risk_sessions SET cell_id = v_a001 WHERE cell_id IS NULL;
            UPDATE risk_state_events SET cell_id = v_a001 WHERE cell_id IS NULL;
            UPDATE risk_governor_state SET cell_id = v_a001 WHERE cell_id IS NULL;
          END IF;
        END $$;
        """
    )
    for table in ("risk_sessions", "risk_state_events", "risk_governor_state"):
        op.alter_column(table, "cell_id", nullable=False)
        op.create_foreign_key(
            f"fk_{table}_cell", table, "capital_cells", ["cell_id"], ["cell_id"]
        )
    op.create_unique_constraint(
        "uq_risk_sessions_cell_session", "risk_sessions", ["cell_id", "session_id"]
    )
    op.create_foreign_key(
        "fk_risk_state_events_cell_session",
        "risk_state_events",
        "risk_sessions",
        ["cell_id", "session_id"],
        ["cell_id", "session_id"],
    )
    op.drop_constraint(op.f("ck_risk_governor_state_singleton"), "risk_governor_state", type_="check")
    op.drop_constraint("pk_risk_governor_state", "risk_governor_state", type_="primary")
    op.drop_column("risk_governor_state", "singleton_key")
    op.create_primary_key("pk_risk_governor_state", "risk_governor_state", ["cell_id"])
    op.create_foreign_key(
        "fk_risk_governor_state_cell_session",
        "risk_governor_state",
        "risk_sessions",
        ["cell_id", "current_session_id"],
        ["cell_id", "session_id"],
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF (SELECT COUNT(*) FROM risk_governor_state) > 1 THEN "
        "RAISE EXCEPTION 'cannot safely collapse multiple cell risk states'; "
        "END IF; END $$;"
    )
    op.drop_constraint("fk_risk_governor_state_cell_session", "risk_governor_state", type_="foreignkey")
    op.drop_constraint("pk_risk_governor_state", "risk_governor_state", type_="primary")
    op.add_column("risk_governor_state", sa.Column("singleton_key", sa.Integer, nullable=False, server_default="1"))
    op.create_primary_key("pk_risk_governor_state", "risk_governor_state", ["singleton_key"])
    op.create_check_constraint(op.f("ck_risk_governor_state_singleton"), "risk_governor_state", "singleton_key = 1")
    op.drop_constraint("fk_risk_state_events_cell_session", "risk_state_events", type_="foreignkey")
    op.drop_constraint("uq_risk_sessions_cell_session", "risk_sessions", type_="unique")
    for table in ("risk_governor_state", "risk_state_events", "risk_sessions"):
        op.drop_constraint(f"fk_{table}_cell", table, type_="foreignkey")
        op.drop_column(table, "cell_id")

    op.execute("DROP TRIGGER trg_capital_authorization_manifest_cell ON kairo_capital_authorizations")
    op.execute("DROP FUNCTION enforce_capital_authorization_manifest_cell()")
    op.drop_constraint(op.f("ck_kairo_capital_authorizations_provenance_exclusive"), "kairo_capital_authorizations", type_="check")
    op.drop_constraint("fk_capital_authorizations_synthetic_manifest", "kairo_capital_authorizations", type_="foreignkey")
    op.drop_constraint("fk_capital_authorizations_cell", "kairo_capital_authorizations", type_="foreignkey")
    op.drop_column("kairo_capital_authorizations", "synthetic_provenance_id")
    op.drop_column("kairo_capital_authorizations", "economic_domain")
    op.alter_column("kairo_capital_authorizations", "broker_account_id", nullable=False)
    op.alter_column("kairo_capital_authorizations", "broker_snapshot_id", nullable=False)

    op.execute("DROP TRIGGER trg_synthetic_manifest_append_only ON synthetic_evidence_manifests")
    op.execute("DROP FUNCTION reject_synthetic_manifest_mutation()")
    op.drop_table("synthetic_evidence_manifests")
    op.drop_constraint(op.f("ck_capital_cells_economic_domain"), "capital_cells", type_="check")
    op.drop_constraint("fk_capital_cells_risk_policy", "capital_cells", type_="foreignkey")
    op.drop_column("capital_cells", "economic_domain")
    op.drop_column("capital_cells", "risk_policy_id")
    op.drop_table("risk_policies")
