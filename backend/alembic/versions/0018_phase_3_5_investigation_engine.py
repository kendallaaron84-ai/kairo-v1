"""Phase 3.5 Step 3: Investigation Case Workflow Engine.

Revision ID: 0018
Revises: 0017
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "intelligence_investigation_cases",
        sa.Column("case_id", UUID, primary_key=True),
        sa.Column("case_number", sa.String(32), nullable=False, unique=True),
        sa.Column("query_prompt", sa.Text(), nullable=False),
        sa.Column("hypothesis_claim", sa.Text(), nullable=False),
        sa.Column("target_symbol", sa.String(32)),
        sa.Column("target_theme", sa.String(64)),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("btrim(query_prompt) <> ''", name="ck_case_query_not_blank"),
        sa.CheckConstraint(
            "btrim(hypothesis_claim) <> ''", name="ck_case_hypothesis_not_blank"
        ),
    )
    op.create_index(
        "idx_cases_symbol", "intelligence_investigation_cases", ["target_symbol"]
    )

    op.create_table(
        "intelligence_case_findings",
        sa.Column("finding_id", UUID, primary_key=True),
        sa.Column(
            "case_id",
            UUID,
            sa.ForeignKey("intelligence_investigation_cases.case_id"),
            nullable=False,
        ),
        sa.Column("finding_type", sa.String(32), nullable=False),
        sa.Column("claim_assertion", sa.Text(), nullable=False),
        sa.Column("finding_narrative", sa.Text(), nullable=False),
        sa.Column("search_scope_json", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column("sequence_num", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "finding_type IN ('SUPPORTING', 'CONTRADICTORY', "
            "'GAP_IDENTIFIED', 'SUPERSEDED_FACT')",
            name="ck_case_finding_type",
        ),
        sa.CheckConstraint(
            "(finding_type = 'GAP_IDENTIFIED' "
            "AND search_scope_json IS NOT NULL "
            "AND jsonb_typeof(search_scope_json) = 'object' "
            "AND search_scope_json <> '{}'::jsonb) "
            "OR (finding_type <> 'GAP_IDENTIFIED')",
            name="ck_gap_finding_requires_search_scope",
        ),
        sa.CheckConstraint("sequence_num > 0", name="ck_case_finding_sequence_positive"),
        sa.UniqueConstraint(
            "case_id", "sequence_num", name="uq_case_finding_sequence"
        ),
    )
    op.create_index(
        "idx_findings_case_id", "intelligence_case_findings", ["case_id"]
    )

    op.create_table(
        "intelligence_finding_citations",
        sa.Column("citation_id", UUID, primary_key=True),
        sa.Column(
            "finding_id",
            UUID,
            sa.ForeignKey("intelligence_case_findings.finding_id"),
            nullable=False,
        ),
        sa.Column(
            "event_id",
            UUID,
            sa.ForeignKey("intelligence_evidence_ledger.event_id"),
            nullable=False,
        ),
        sa.Column("citation_role", sa.String(32), nullable=False),
        sa.Column("temporal_status", sa.String(32), nullable=False),
        sa.Column("citation_relevance", sa.Numeric(5, 2), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "citation_role IN ('PRIMARY_PROOF', 'CONTRADICTION', 'CONTEXT')",
            name="ck_citation_role",
        ),
        sa.CheckConstraint(
            "temporal_status IN ('ACTIVE', 'SUPERSEDED', 'HISTORICAL_CONTEXT')",
            name="ck_citation_temporal_status",
        ),
        sa.CheckConstraint(
            "citation_relevance >= 0.00 AND citation_relevance <= 100.00",
            name="ck_citation_relevance_range",
        ),
        sa.UniqueConstraint(
            "finding_id", "event_id", name="uq_finding_event_citation"
        ),
    )
    op.create_index(
        "idx_citations_finding_id",
        "intelligence_finding_citations",
        ["finding_id"],
    )
    op.create_index(
        "idx_citations_event_id", "intelligence_finding_citations", ["event_id"]
    )

    op.create_table(
        "intelligence_case_conclusions",
        sa.Column("conclusion_id", UUID, primary_key=True),
        sa.Column(
            "case_id",
            UUID,
            sa.ForeignKey("intelligence_investigation_cases.case_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("verdict", sa.String(32), nullable=False),
        sa.Column("confidence_score", sa.Numeric(5, 2), nullable=False),
        sa.Column("materiality_scope", sa.String(32), nullable=False),
        sa.Column("time_horizon", sa.String(32), nullable=False),
        sa.Column("synthesis_summary", sa.Text(), nullable=False),
        sa.Column("case_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "verdict IN ('CONFIRMED', 'PARTIALLY_SUPPORTED', "
            "'UNSUPPORTED', 'INSUFFICIENT_EVIDENCE')",
            name="ck_conclusion_verdict_valid",
        ),
        sa.CheckConstraint(
            "materiality_scope IN ('MARKET', 'SECTOR', 'THEME', 'COMPANY')",
            name="ck_conclusion_materiality_scope",
        ),
        sa.CheckConstraint(
            "time_horizon IN ('INTRADAY', 'DAYS', 'MONTHS', 'STRUCTURAL')",
            name="ck_conclusion_time_horizon",
        ),
        sa.CheckConstraint(
            "confidence_score >= 0.00 AND confidence_score <= 100.00",
            name="ck_conclusion_confidence_range",
        ),
        sa.CheckConstraint(
            "case_manifest_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_conclusion_manifest_sha256",
        ),
        sa.CheckConstraint(
            "btrim(synthesis_summary) <> ''", name="ck_conclusion_summary_not_blank"
        ),
    )
    op.create_index(
        "idx_conclusions_verdict", "intelligence_case_conclusions", ["verdict"]
    )

    op.execute(
        """
        CREATE FUNCTION check_case_conclusion_validity()
        RETURNS TRIGGER AS $$
        DECLARE
            v_case_opened_at TIMESTAMPTZ;
            v_finding RECORD;
            v_citation_count INTEGER;
        BEGIN
            SELECT opened_at INTO v_case_opened_at
            FROM intelligence_investigation_cases
            WHERE case_id = NEW.case_id;

            IF v_case_opened_at IS NULL THEN
                RAISE EXCEPTION 'Conclusion references unresolved case %', NEW.case_id;
            END IF;
            IF NEW.closed_at < v_case_opened_at THEN
                RAISE EXCEPTION 'Case conclusion closed_at cannot precede opened_at';
            END IF;

            FOR v_finding IN
                SELECT finding_id, finding_type
                FROM intelligence_case_findings
                WHERE case_id = NEW.case_id
            LOOP
                SELECT COUNT(*) INTO v_citation_count
                FROM intelligence_finding_citations
                WHERE finding_id = v_finding.finding_id;

                IF v_finding.finding_type IN ('SUPPORTING', 'CONTRADICTORY')
                   AND v_citation_count < 1 THEN
                    RAISE EXCEPTION 'Finding % (%) requires at least 1 citation',
                        v_finding.finding_id, v_finding.finding_type;
                ELSIF v_finding.finding_type = 'SUPERSEDED_FACT'
                   AND v_citation_count < 2 THEN
                    RAISE EXCEPTION 'SUPERSEDED_FACT finding % requires at least 2 citations',
                        v_finding.finding_id;
                ELSIF v_finding.finding_type = 'GAP_IDENTIFIED'
                   AND v_citation_count > 0 THEN
                    RAISE EXCEPTION 'GAP_IDENTIFIED finding % cannot have citations',
                        v_finding.finding_id;
                END IF;
            END LOOP;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_check_case_conclusion_validity
        BEFORE INSERT ON intelligence_case_conclusions
        FOR EACH ROW EXECUTE FUNCTION check_case_conclusion_validity();

        CREATE TRIGGER trg_investigation_cases_immutable
        BEFORE UPDATE OR DELETE ON intelligence_investigation_cases
        FOR EACH ROW EXECUTE FUNCTION reject_intelligence_fact_mutation();

        CREATE TRIGGER trg_case_findings_immutable
        BEFORE UPDATE OR DELETE ON intelligence_case_findings
        FOR EACH ROW EXECUTE FUNCTION reject_intelligence_fact_mutation();

        CREATE TRIGGER trg_finding_citations_immutable
        BEFORE UPDATE OR DELETE ON intelligence_finding_citations
        FOR EACH ROW EXECUTE FUNCTION reject_intelligence_fact_mutation();

        CREATE TRIGGER trg_case_conclusions_immutable
        BEFORE UPDATE OR DELETE ON intelligence_case_conclusions
        FOR EACH ROW EXECUTE FUNCTION reject_intelligence_fact_mutation();
        """
    )
    op.execute(
        """
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kairo_runtime') THEN
            REVOKE ALL ON intelligence_investigation_cases FROM kairo_runtime;
            GRANT SELECT, INSERT ON intelligence_investigation_cases TO kairo_runtime;
            REVOKE ALL ON intelligence_case_findings FROM kairo_runtime;
            GRANT SELECT, INSERT ON intelligence_case_findings TO kairo_runtime;
            REVOKE ALL ON intelligence_finding_citations FROM kairo_runtime;
            GRANT SELECT, INSERT ON intelligence_finding_citations TO kairo_runtime;
            REVOKE ALL ON intelligence_case_conclusions FROM kairo_runtime;
            GRANT SELECT, INSERT ON intelligence_case_conclusions TO kairo_runtime;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    # A case row is the root of every possible Step 3 fact. This guard precedes
    # all destructive DDL and therefore makes downgrade lossless or impossible.
    op.execute(
        """
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM intelligence_investigation_cases) THEN
            RAISE EXCEPTION
              'Refusing 0018 downgrade: immutable investigation records exist';
          END IF;
        END $$;
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_case_conclusions_immutable "
        "ON intelligence_case_conclusions"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_finding_citations_immutable "
        "ON intelligence_finding_citations"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_case_findings_immutable "
        "ON intelligence_case_findings"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_investigation_cases_immutable "
        "ON intelligence_investigation_cases"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_check_case_conclusion_validity "
        "ON intelligence_case_conclusions"
    )
    op.execute("DROP FUNCTION IF EXISTS check_case_conclusion_validity")
    op.drop_table("intelligence_case_conclusions")
    op.drop_table("intelligence_finding_citations")
    op.drop_table("intelligence_case_findings")
    op.drop_table("intelligence_investigation_cases")
