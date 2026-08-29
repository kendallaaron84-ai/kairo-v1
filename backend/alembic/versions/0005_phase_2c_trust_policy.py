"""Extend trust evaluations with Phase 2C evidence and recommendation lineage.

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "trust_evaluations",
        sa.Column("window_trade_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "trust_evaluations", sa.Column("window_start", sa.DateTime(timezone=True))
    )
    op.add_column(
        "trust_evaluations", sa.Column("window_end", sa.DateTime(timezone=True))
    )
    op.add_column(
        "trust_evaluations",
        sa.Column(
            "evidence_manifest_hash", sa.String(64), nullable=False, server_default=""
        ),
    )
    op.add_column(
        "trust_evaluations",
        sa.Column(
            "eligibility_status",
            sa.String(32),
            nullable=False,
            server_default="INSUFFICIENT_EVIDENCE",
        ),
    )
    op.add_column(
        "trust_evaluations",
        sa.Column(
            "current_autonomy_tier",
            sa.String(32),
            nullable=False,
            server_default="APPRENTICE",
        ),
    )
    op.add_column(
        "trust_evaluations",
        sa.Column(
            "recommended_autonomy_tier",
            sa.String(32),
            nullable=False,
            server_default="APPRENTICE",
        ),
    )

    op.execute(
        "DO $$ BEGIN IF EXISTS ("
        "SELECT 1 FROM trust_evaluations te "
        "LEFT JOIN capital_cells cc ON cc.cell_id = te.cell_id "
        "WHERE cc.cell_id IS NULL) THEN "
        "RAISE EXCEPTION 'trust evaluations contain non-canonical cell lineage'; "
        "END IF; END $$;"
    )
    op.create_foreign_key(
        "fk_trust_evaluations_cell_id_capital_cells",
        "trust_evaluations",
        "capital_cells",
        ["cell_id"],
        ["cell_id"],
    )
    op.drop_constraint(
        op.f("ck_trust_evaluations_evidence_score_semantics"),
        "trust_evaluations",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_trust_evaluations_evidence_score_semantics"),
        "trust_evaluations",
        "evidence_trade_count >= 0 AND window_trade_count >= 0 "
        "AND (evidence_trade_count > 0 OR score IS NULL) "
        "AND (eligible_for_promotion = false OR "
        "(evidence_trade_count > 0 AND score IS NOT NULL "
        "AND eligibility_status = 'ELIGIBLE'))",
    )
    op.create_check_constraint(
        op.f("ck_trust_evaluations_valid_eligibility"),
        "trust_evaluations",
        "eligibility_status IN ('ELIGIBLE', 'DISQUALIFIED', 'INSUFFICIENT_EVIDENCE')",
    )
    op.create_check_constraint(
        op.f("ck_trust_evaluations_window_order"),
        "trust_evaluations",
        "window_start IS NULL OR window_end IS NULL OR window_end >= window_start",
    )
    op.create_check_constraint(
        op.f("ck_trust_evaluations_manifest_shape"),
        "trust_evaluations",
        "char_length(evidence_manifest_hash) IN (0, 64)",
    )
    op.execute("GRANT SELECT, INSERT ON trust_evaluations TO kairo_runtime")
    op.execute("REVOKE UPDATE, DELETE ON trust_evaluations FROM kairo_runtime")


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM trust_evaluations "
        "WHERE evidence_trade_count > 0 AND score IS NULL) THEN "
        "RAISE EXCEPTION 'Phase 2C insufficient-evidence evaluations require archival'; "
        "END IF; END $$;"
    )
    op.drop_constraint(
        op.f("ck_trust_evaluations_manifest_shape"),
        "trust_evaluations",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_trust_evaluations_window_order"),
        "trust_evaluations",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_trust_evaluations_valid_eligibility"),
        "trust_evaluations",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_trust_evaluations_evidence_score_semantics"),
        "trust_evaluations",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_trust_evaluations_evidence_score_semantics"),
        "trust_evaluations",
        "(evidence_trade_count = 0 AND score IS NULL "
        "AND eligible_for_promotion = false) OR "
        "(evidence_trade_count > 0 AND score IS NOT NULL)",
    )
    op.drop_constraint(
        "fk_trust_evaluations_cell_id_capital_cells",
        "trust_evaluations",
        type_="foreignkey",
    )
    for column in (
        "recommended_autonomy_tier",
        "current_autonomy_tier",
        "eligibility_status",
        "evidence_manifest_hash",
        "window_end",
        "window_start",
        "window_trade_count",
    ):
        op.drop_column("trust_evaluations", column)
