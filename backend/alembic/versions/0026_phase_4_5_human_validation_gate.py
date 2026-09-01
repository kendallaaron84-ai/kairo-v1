"""Phase 4.5 Step 5: Human Validation Acceptance Gate.

Revision ID: 0026
Revises: 0025
Create Date: 2026-09-01
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "historical_validation_acceptance_facts",
        sa.Column("acceptance_fact_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("confidence_ledger_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("historical_validation_confidence_ledgers.confidence_ledger_id"), nullable=False),
        sa.Column("validation_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("historical_validation_runs.validation_run_id"), nullable=False),
        sa.Column("human_reviewer_identity", sa.String(128), nullable=False),
        sa.Column("acceptance_decision", sa.String(32), nullable=False),
        sa.Column("decision_rationale", sa.Text(), nullable=False),
        sa.Column("confidence_score_at_review", sa.Numeric(5, 2), nullable=False),
        sa.Column("hard_gates_passed_at_review", sa.Boolean(), nullable=False),
        sa.Column("gate_eligibility_at_review", sa.Boolean(), nullable=False),
        sa.Column("bound_confidence_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("bound_scorecard_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("bound_multi_year_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("decision_manifest_sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("acceptance_decision IN ('ACCEPTED_FOR_LIVE','REJECTED','CONDITIONAL_REVIEW')", name="acceptance_decision_type"),
        sa.CheckConstraint("(acceptance_decision = 'ACCEPTED_FOR_LIVE' AND gate_eligibility_at_review = TRUE AND hard_gates_passed_at_review = TRUE AND confidence_score_at_review >= 80.00) OR (acceptance_decision IN ('REJECTED','CONDITIONAL_REVIEW'))", name="human_acceptance_prerequisite_parity"),
        sa.CheckConstraint("bound_confidence_manifest_sha256 ~ '^[a-f0-9]{64}$'", name="bound_conf_sha_format"),
        sa.CheckConstraint("bound_scorecard_manifest_sha256 ~ '^[a-f0-9]{64}$'", name="bound_scorecard_sha_format"),
        sa.CheckConstraint("bound_multi_year_manifest_sha256 ~ '^[a-f0-9]{64}$'", name="bound_multi_year_sha_format"),
        sa.CheckConstraint("decision_manifest_sha256 ~ '^[a-f0-9]{64}$'", name="decision_manifest_sha_format"),
        sa.UniqueConstraint("validation_run_id", name="uq_run_human_acceptance"),
    )
    op.create_index("idx_acceptance_decision_lookup", "historical_validation_acceptance_facts", ["acceptance_decision", "decided_at"])
    op.execute(
        """
        CREATE OR REPLACE FUNCTION check_historical_validation_acceptance_lineage()
        RETURNS TRIGGER AS $$
        DECLARE
          confidence_record historical_validation_confidence_ledgers%ROWTYPE;
          validation_record historical_validation_runs%ROWTYPE;
        BEGIN
          SELECT * INTO confidence_record FROM historical_validation_confidence_ledgers
            WHERE confidence_ledger_id = NEW.confidence_ledger_id;
          IF NOT FOUND THEN RAISE EXCEPTION 'Confidence ledger % does not resolve', NEW.confidence_ledger_id; END IF;
          SELECT * INTO validation_record FROM historical_validation_runs
            WHERE validation_run_id = NEW.validation_run_id;
          IF NOT FOUND THEN RAISE EXCEPTION 'Validation run % does not resolve', NEW.validation_run_id; END IF;
          IF confidence_record.validation_run_id IS DISTINCT FROM NEW.validation_run_id THEN
            RAISE EXCEPTION 'Confidence ledger and validation run lineage mismatch';
          END IF;
          IF confidence_record.composite_confidence_score IS DISTINCT FROM NEW.confidence_score_at_review
             OR confidence_record.hard_gate_passed IS DISTINCT FROM NEW.hard_gates_passed_at_review
             OR confidence_record.gate_eligible IS DISTINCT FROM NEW.gate_eligibility_at_review
             OR confidence_record.confidence_manifest_sha256 IS DISTINCT FROM NEW.bound_confidence_manifest_sha256
             OR validation_record.scorecard_manifest_sha256 IS DISTINCT FROM NEW.bound_scorecard_manifest_sha256
             OR validation_record.multi_year_manifest_sha256 IS DISTINCT FROM NEW.bound_multi_year_manifest_sha256 THEN
            RAISE EXCEPTION 'Human acceptance snapshot does not match canonical validation evidence';
          END IF;
          RETURN NEW;
        END; $$ LANGUAGE plpgsql;
        CREATE TRIGGER trg_check_historical_validation_acceptance_lineage
        BEFORE INSERT ON historical_validation_acceptance_facts
        FOR EACH ROW EXECUTE FUNCTION check_historical_validation_acceptance_lineage();
        CREATE TRIGGER trg_historical_validation_acceptance_immutable
        BEFORE UPDATE OR DELETE ON historical_validation_acceptance_facts
        FOR EACH ROW EXECUTE FUNCTION reject_intelligence_fact_mutation();
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kairo_governance_authority') THEN
                CREATE ROLE kairo_governance_authority NOLOGIN;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kairo_runtime') THEN
                REVOKE ALL ON historical_validation_acceptance_facts FROM kairo_runtime;
                GRANT SELECT ON historical_validation_acceptance_facts TO kairo_runtime;
            END IF;
            REVOKE ALL ON historical_validation_acceptance_facts FROM kairo_governance_authority;
            GRANT SELECT, INSERT ON historical_validation_acceptance_facts TO kairo_governance_authority;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM historical_validation_acceptance_facts) THEN RAISE EXCEPTION 'Downgrade failed closed: Immutable Step 5 human acceptance records exist.'; END IF; END $$;""")
    op.execute("DROP TRIGGER IF EXISTS trg_historical_validation_acceptance_immutable ON historical_validation_acceptance_facts")
    op.execute("DROP TRIGGER IF EXISTS trg_check_historical_validation_acceptance_lineage ON historical_validation_acceptance_facts")
    op.drop_table("historical_validation_acceptance_facts")
    op.execute("DROP FUNCTION IF EXISTS check_historical_validation_acceptance_lineage()")
