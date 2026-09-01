"""Phase 3.5 Step 1: Market Intelligence Evidence Library & Storage.

Revision ID: 0016
Revises: 0015
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "intelligence_raw_artifacts",
        sa.Column("artifact_id", UUID, primary_key=True),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("mime_type", sa.String(64), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("storage_uri", sa.String(512), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("byte_size > 0", name="ck_raw_artifact_size_positive"),
        sa.CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'", name="ck_raw_artifact_sha256"
        ),
        sa.UniqueConstraint("content_sha256", name="uq_raw_artifact_content_sha256"),
        sa.UniqueConstraint(
            "artifact_id", "content_sha256", name="uq_raw_artifact_identity_hash"
        ),
    )
    op.create_index(
        "idx_raw_artifacts_hash", "intelligence_raw_artifacts", ["content_sha256"]
    )

    op.create_table(
        "intelligence_evidence_ledger",
        sa.Column("event_id", UUID, primary_key=True),
        sa.Column("artifact_id", UUID, nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_name", sa.String(128), nullable=False),
        sa.Column("source_uri", sa.String(1024)),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("impact_scope", sa.String(32), nullable=False),
        sa.Column("urgency", sa.String(16), nullable=False),
        sa.Column("confidence_score", sa.Numeric(5, 2), nullable=False),
        sa.Column("time_horizon", sa.String(32), nullable=False),
        sa.Column("raw_content_sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["artifact_id", "raw_content_sha256"],
            [
                "intelligence_raw_artifacts.artifact_id",
                "intelligence_raw_artifacts.content_sha256",
            ],
            name="fk_intelligence_evidence_artifact_hash",
        ),
        sa.CheckConstraint(
            "source_type IN ('PRIMARY', 'SECONDARY', 'SOCIAL', 'USER_SUBMITTED')",
            name="ck_evidence_source_type",
        ),
        sa.CheckConstraint(
            "event_type IN ('EARNINGS', 'MACRO', 'REGULATORY', 'GEOPOLITICAL', "
            "'M_AND_A', 'LEGAL', 'MANAGEMENT', 'PRODUCT', 'CAPITAL_RAISE', "
            "'LEGISLATION', 'INSIDER_ACTIVITY', 'CUSTOM_CLAIM')",
            name="ck_evidence_event_type",
        ),
        sa.CheckConstraint(
            "impact_scope IN ('MARKET', 'SECTOR', 'THEME', 'COMPANY')",
            name="ck_evidence_impact_scope",
        ),
        sa.CheckConstraint(
            "urgency IN ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')",
            name="ck_evidence_urgency",
        ),
        sa.CheckConstraint(
            "time_horizon IN ('INTRADAY', 'DAYS', 'MONTHS', 'STRUCTURAL')",
            name="ck_evidence_time_horizon",
        ),
        sa.CheckConstraint(
            "confidence_score >= 0.00 AND confidence_score <= 100.00",
            name="ck_evidence_confidence_range",
        ),
        sa.CheckConstraint(
            "raw_content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_evidence_raw_content_sha256",
        ),
    )
    op.create_index(
        "idx_evidence_published_at", "intelligence_evidence_ledger", ["published_at"]
    )
    op.create_index(
        "idx_evidence_event_type", "intelligence_evidence_ledger", ["event_type"]
    )

    op.create_table(
        "intelligence_entity_links",
        sa.Column("link_id", UUID, primary_key=True),
        sa.Column(
            "event_id",
            UUID,
            sa.ForeignKey("intelligence_evidence_ledger.event_id"),
            nullable=False,
        ),
        sa.Column("entity_type", sa.String(32), nullable=False),
        sa.Column("entity_symbol", sa.String(32), nullable=False),
        sa.Column("relevance_score", sa.Numeric(5, 2), nullable=False),
        sa.CheckConstraint(
            "entity_type IN ('TICKER', 'THEME', 'SECTOR', 'MACRO_FACTOR')",
            name="ck_entity_link_type",
        ),
        sa.CheckConstraint(
            "relevance_score >= 0.00 AND relevance_score <= 100.00",
            name="ck_entity_link_relevance_range",
        ),
        sa.CheckConstraint(
            "entity_symbol = upper(entity_symbol)", name="ck_entity_symbol_uppercase"
        ),
        sa.UniqueConstraint(
            "event_id", "entity_type", "entity_symbol", name="uq_evidence_entity_link"
        ),
    )
    op.create_index(
        "idx_entity_links_symbol", "intelligence_entity_links", ["entity_symbol"]
    )

    op.execute(
        """
        CREATE FUNCTION reject_intelligence_fact_mutation()
        RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION 'Intelligence facts and raw artifacts are immutable: %',
                TG_TABLE_NAME;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_intelligence_raw_artifacts_immutable
        BEFORE UPDATE OR DELETE ON intelligence_raw_artifacts
        FOR EACH ROW EXECUTE FUNCTION reject_intelligence_fact_mutation();

        CREATE TRIGGER trg_intelligence_evidence_ledger_immutable
        BEFORE UPDATE OR DELETE ON intelligence_evidence_ledger
        FOR EACH ROW EXECUTE FUNCTION reject_intelligence_fact_mutation();

        CREATE TRIGGER trg_intelligence_entity_links_immutable
        BEFORE UPDATE OR DELETE ON intelligence_entity_links
        FOR EACH ROW EXECUTE FUNCTION reject_intelligence_fact_mutation();
        """
    )
    op.execute(
        """
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kairo_runtime') THEN
            REVOKE ALL ON intelligence_raw_artifacts FROM kairo_runtime;
            GRANT SELECT, INSERT ON intelligence_raw_artifacts TO kairo_runtime;
            REVOKE ALL ON intelligence_evidence_ledger FROM kairo_runtime;
            GRANT SELECT, INSERT ON intelligence_evidence_ledger TO kairo_runtime;
            REVOKE ALL ON intelligence_entity_links FROM kairo_runtime;
            GRANT SELECT, INSERT ON intelligence_entity_links TO kairo_runtime;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM intelligence_raw_artifacts)
             OR EXISTS (SELECT 1 FROM intelligence_evidence_ledger)
             OR EXISTS (SELECT 1 FROM intelligence_entity_links) THEN
            RAISE EXCEPTION 'Refusing 0016 downgrade: immutable intelligence facts exist';
          END IF;
        END $$;
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_intelligence_entity_links_immutable "
        "ON intelligence_entity_links"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_intelligence_evidence_ledger_immutable "
        "ON intelligence_evidence_ledger"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_intelligence_raw_artifacts_immutable "
        "ON intelligence_raw_artifacts"
    )
    op.execute("DROP FUNCTION IF EXISTS reject_intelligence_fact_mutation")
    op.drop_table("intelligence_entity_links")
    op.drop_table("intelligence_evidence_ledger")
    op.drop_table("intelligence_raw_artifacts")
