"""Phase 3.5 Step 2: immutable release lifecycle lineage.

Revision ID: 0017
Revises: 0016
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "intelligence_evidence_ledger",
        sa.Column(
            "release_status",
            sa.String(16),
            server_default="RELEASED",
            nullable=False,
        ),
    )
    op.add_column(
        "intelligence_evidence_ledger",
        sa.Column("referenced_event_id", postgresql.UUID(as_uuid=True)),
    )
    op.create_foreign_key(
        "fk_intelligence_evidence_ledger_referenced_event_id",
        "intelligence_evidence_ledger",
        "intelligence_evidence_ledger",
        ["referenced_event_id"],
        ["event_id"],
    )
    op.create_check_constraint(
        "ck_evidence_release_status",
        "intelligence_evidence_ledger",
        "release_status IN ('SCHEDULED', 'RELEASED', 'REVISED')",
    )
    op.create_check_constraint(
        "ck_evidence_release_reference_semantics",
        "intelligence_evidence_ledger",
        "referenced_event_id IS NULL OR referenced_event_id <> event_id",
    )
    op.create_index(
        "idx_evidence_release_status",
        "intelligence_evidence_ledger",
        ["release_status"],
    )
    op.create_index(
        "idx_evidence_referenced_event",
        "intelligence_evidence_ledger",
        ["referenced_event_id"],
    )


def downgrade() -> None:
    # This check deliberately precedes every destructive DDL statement. Rows
    # backfilled as ordinary RELEASED facts lose no lifecycle distinction, but
    # a scheduled/revised fact or reference edge is canonical lineage that 0016
    # cannot represent and therefore must never be silently discarded.
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1
            FROM intelligence_evidence_ledger
            WHERE release_status IN ('SCHEDULED', 'REVISED')
               OR referenced_event_id IS NOT NULL
          ) THEN
            RAISE EXCEPTION
              'Refusing 0017 downgrade: immutable release lifecycle lineage exists';
          END IF;
        END $$;
        """
    )
    op.drop_index(
        "idx_evidence_referenced_event", table_name="intelligence_evidence_ledger"
    )
    op.drop_index(
        "idx_evidence_release_status", table_name="intelligence_evidence_ledger"
    )
    op.drop_constraint(
        "ck_evidence_release_reference_semantics",
        "intelligence_evidence_ledger",
        type_="check",
    )
    op.drop_constraint(
        "ck_evidence_release_status",
        "intelligence_evidence_ledger",
        type_="check",
    )
    op.drop_constraint(
        "fk_intelligence_evidence_ledger_referenced_event_id",
        "intelligence_evidence_ledger",
        type_="foreignkey",
    )
    op.drop_column("intelligence_evidence_ledger", "referenced_event_id")
    op.drop_column("intelligence_evidence_ledger", "release_status")
