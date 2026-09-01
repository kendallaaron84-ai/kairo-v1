"""Phase 3.5 Step 4: Context Gate Engine and OBSERVE integration.

Revision ID: 0019
Revises: 0018
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.add_column(
        "intelligence_evidence_ledger",
        sa.Column("effective_at", sa.DateTime(timezone=True)),
    )
    op.execute(
        "UPDATE intelligence_evidence_ledger "
        "SET effective_at = published_at WHERE effective_at IS NULL"
    )
    op.alter_column(
        "intelligence_evidence_ledger", "effective_at", nullable=False
    )
    op.create_index(
        "idx_evidence_effective_at",
        "intelligence_evidence_ledger",
        ["effective_at"],
    )

    op.create_table(
        "market_context_assessments",
        sa.Column("assessment_id", UUID, primary_key=True),
        sa.Column(
            "cell_id", UUID, sa.ForeignKey("capital_cells.cell_id"), nullable=False
        ),
        sa.Column("risk_posture", sa.String(32), nullable=False),
        sa.Column(
            "authority_mode",
            sa.String(32),
            server_default="OBSERVE_ONLY",
            nullable=False,
        ),
        sa.Column(
            "macro_window_active",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "primary_event_id",
            UUID,
            sa.ForeignKey("intelligence_evidence_ledger.event_id"),
        ),
        sa.Column(
            "active_case_id",
            UUID,
            sa.ForeignKey("intelligence_investigation_cases.case_id"),
        ),
        sa.Column("assessment_summary", sa.Text(), nullable=False),
        sa.Column("assessment_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "risk_posture IN ('NORMAL', 'ELEVATED', 'HIGH_EVENT_RISK', 'CRITICAL')",
            name="ck_context_risk_posture",
        ),
        sa.CheckConstraint(
            "authority_mode = 'OBSERVE_ONLY'",
            name="ck_context_authority_mode_observe_only",
        ),
        sa.CheckConstraint(
            "assessment_manifest_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_context_manifest_sha256",
        ),
        sa.CheckConstraint(
            "(macro_window_active = true AND primary_event_id IS NOT NULL) "
            "OR macro_window_active = false",
            name="ck_context_macro_event_lineage",
        ),
    )
    op.create_index(
        "idx_context_cell_evaluated",
        "market_context_assessments",
        ["cell_id", "evaluated_at"],
    )

    op.create_table(
        "order_context_evaluations",
        sa.Column("evaluation_id", UUID, primary_key=True),
        sa.Column(
            "intent_id",
            UUID,
            sa.ForeignKey("order_intents.intent_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "assessment_id",
            UUID,
            sa.ForeignKey("market_context_assessments.assessment_id"),
            nullable=False,
        ),
        sa.Column("counterfactual_opinion", sa.String(32), nullable=False),
        sa.Column("veto_reason_code", sa.String(64)),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "counterfactual_opinion IN "
            "('WOULD_HAVE_AUTHORIZED', 'WOULD_HAVE_VETOED', 'NO_OPINION')",
            name="ck_order_context_opinion",
        ),
        sa.CheckConstraint(
            "(counterfactual_opinion = 'WOULD_HAVE_VETOED' "
            "AND veto_reason_code IS NOT NULL) "
            "OR (counterfactual_opinion <> 'WOULD_HAVE_VETOED')",
            name="ck_veto_opinion_requires_reason",
        ),
    )
    op.create_index(
        "idx_evaluations_intent_id", "order_context_evaluations", ["intent_id"]
    )

    op.execute(
        """
        CREATE FUNCTION check_order_context_cell_isolation()
        RETURNS TRIGGER AS $$
        DECLARE
            v_intent_cell_id UUID;
            v_assessment_cell_id UUID;
            v_assessment_evaluated_at TIMESTAMPTZ;
        BEGIN
            SELECT cell_id INTO v_intent_cell_id
            FROM order_intents WHERE intent_id = NEW.intent_id;
            IF v_intent_cell_id IS NULL THEN
                RAISE EXCEPTION 'Order context binding references unresolved intent %',
                    NEW.intent_id;
            END IF;

            SELECT cell_id, evaluated_at
            INTO v_assessment_cell_id, v_assessment_evaluated_at
            FROM market_context_assessments
            WHERE assessment_id = NEW.assessment_id;
            IF v_assessment_cell_id IS NULL OR v_assessment_evaluated_at IS NULL THEN
                RAISE EXCEPTION 'Order context binding references unresolved assessment %',
                    NEW.assessment_id;
            END IF;

            IF v_intent_cell_id <> v_assessment_cell_id THEN
                RAISE EXCEPTION
                    'Cross-cell context binding rejected: intent cell does not match assessment cell';
            END IF;
            IF v_assessment_evaluated_at > NEW.evaluated_at THEN
                RAISE EXCEPTION
                    'Temporal causality violation: assessment is after evaluation timestamp';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_check_order_context_cell_isolation
        BEFORE INSERT ON order_context_evaluations
        FOR EACH ROW EXECUTE FUNCTION check_order_context_cell_isolation();

        CREATE TRIGGER trg_market_context_assessments_immutable
        BEFORE UPDATE OR DELETE ON market_context_assessments
        FOR EACH ROW EXECUTE FUNCTION reject_intelligence_fact_mutation();

        CREATE TRIGGER trg_order_context_evaluations_immutable
        BEFORE UPDATE OR DELETE ON order_context_evaluations
        FOR EACH ROW EXECUTE FUNCTION reject_intelligence_fact_mutation();
        """
    )
    op.execute(
        """
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kairo_runtime') THEN
            REVOKE ALL ON market_context_assessments FROM kairo_runtime;
            GRANT SELECT, INSERT ON market_context_assessments TO kairo_runtime;
            REVOKE ALL ON order_context_evaluations FROM kairo_runtime;
            GRANT SELECT, INSERT ON order_context_evaluations TO kairo_runtime;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM market_context_assessments)
             OR EXISTS (SELECT 1 FROM order_context_evaluations)
             OR EXISTS (
                SELECT 1 FROM intelligence_evidence_ledger
                WHERE effective_at IS DISTINCT FROM published_at
             ) THEN
            RAISE EXCEPTION
              'Refusing 0019 downgrade: immutable context or effective-time lineage exists';
          END IF;
        END $$;
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_order_context_evaluations_immutable "
        "ON order_context_evaluations"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_market_context_assessments_immutable "
        "ON market_context_assessments"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_check_order_context_cell_isolation "
        "ON order_context_evaluations"
    )
    op.execute("DROP FUNCTION IF EXISTS check_order_context_cell_isolation")
    op.drop_table("order_context_evaluations")
    op.drop_table("market_context_assessments")
    op.drop_index(
        "idx_evidence_effective_at", table_name="intelligence_evidence_ledger"
    )
    op.drop_column("intelligence_evidence_ledger", "effective_at")
