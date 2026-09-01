"""Phase 4.5 Step 4: Evidence Confidence Engine & Hard Governance Gates.

Revision ID: 0025
Revises: 0024
Create Date: 2026-09-01
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "historical_validation_confidence_ledgers",
        sa.Column("confidence_ledger_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "validation_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("historical_validation_runs.validation_run_id"),
            nullable=False,
        ),
        sa.Column("confidence_policy_version", sa.String(64), nullable=False),
        sa.Column("liquidity_fidelity_tier", sa.String(32), nullable=False),
        *[
            column
            for factor in (
                "sample_size",
                "regime_coverage",
                "data_completeness",
                "execution_realism",
                "oos_stability",
                "profit_distribution",
                "context_alignment",
            )
            for column in (
                sa.Column(f"{factor}_score", sa.Numeric(5, 2), nullable=True),
                sa.Column(f"{factor}_status", sa.String(32), nullable=False),
                sa.Column(f"{factor}_count", sa.Integer(), nullable=False),
                sa.Column(
                    f"{factor}_reasons",
                    postgresql.ARRAY(sa.String(64)),
                    nullable=False,
                ),
            )
        ],
        sa.Column("composite_confidence_score", sa.Numeric(5, 2), nullable=False),
        sa.Column("confidence_tier", sa.String(32), nullable=False),
        sa.Column("hard_gate_passed", sa.Boolean(), nullable=False),
        sa.Column("gate_eligible", sa.Boolean(), nullable=False),
        sa.Column("hard_gate_evaluations_json", postgresql.JSONB(), nullable=False),
        sa.Column("confidence_manifest_sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "confidence_tier IN ('HIGH_CONFIDENCE','MODERATE_CONFIDENCE','LOW_CONFIDENCE')",
            name="confidence_tier_category",
        ),
        sa.CheckConstraint(
            "composite_confidence_score >= 0.00 AND composite_confidence_score <= 100.00",
            name="composite_score_range",
        ),
        sa.CheckConstraint(
            "(composite_confidence_score >= 80.00 AND confidence_tier = 'HIGH_CONFIDENCE') OR "
            "(composite_confidence_score >= 65.00 AND composite_confidence_score < 80.00 AND confidence_tier = 'MODERATE_CONFIDENCE') OR "
            "(composite_confidence_score < 65.00 AND confidence_tier = 'LOW_CONFIDENCE')",
            name="confidence_tier_score_parity",
        ),
        sa.CheckConstraint(
            "(gate_eligible = TRUE AND composite_confidence_score >= 80.00 AND hard_gate_passed = TRUE) OR "
            "(gate_eligible = FALSE AND (composite_confidence_score < 80.00 OR hard_gate_passed = FALSE))",
            name="gate_eligibility_dual_key_parity",
        ),
        sa.CheckConstraint(
            "confidence_manifest_sha256 ~ '^[a-f0-9]{64}$'",
            name="conf_manifest_sha256_format",
        ),
        sa.UniqueConstraint("validation_run_id", name="uq_conf_validation_run"),
    )
    op.create_index(
        "idx_confidence_gate_lookup",
        "historical_validation_confidence_ledgers",
        ["gate_eligible", "hard_gate_passed", "confidence_tier"],
    )
    op.execute(
        """
        CREATE TRIGGER trg_historical_validation_confidence_immutable
        BEFORE UPDATE OR DELETE ON historical_validation_confidence_ledgers
        FOR EACH ROW EXECUTE FUNCTION reject_intelligence_fact_mutation();
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kairo_runtime') THEN
                REVOKE ALL ON historical_validation_confidence_ledgers FROM kairo_runtime;
                GRANT SELECT, INSERT ON historical_validation_confidence_ledgers TO kairo_runtime;
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM historical_validation_confidence_ledgers) THEN
                RAISE EXCEPTION 'Downgrade failed closed: Immutable Step 4 confidence records exist.';
            END IF;
        END $$;
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_historical_validation_confidence_immutable "
        "ON historical_validation_confidence_ledgers"
    )
    op.drop_table("historical_validation_confidence_ledgers")
