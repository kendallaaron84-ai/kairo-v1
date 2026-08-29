"""Separate lifecycle state from autonomy and freeze TRUST-v0.1 tiers.

Revision ID: 0006
Revises: 0005
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "capital_cells",
        sa.Column(
            "autonomy_tier",
            sa.String(32),
            nullable=False,
            server_default="APPRENTICE",
        ),
    )

    # Phase 2C initially overloaded lifecycle status with autonomy. Preserve the
    # governance value in its own axis before restoring canonical lifecycle state.
    op.execute(
        "UPDATE capital_cells SET autonomy_tier = CASE "
        "WHEN status = 'GUARDED' THEN 'GUARDED' "
        "WHEN status IN ('AUTONOMOUS', 'CAPITAL_BUILDER') THEN 'CAPITAL_BUILDER' "
        "ELSE 'APPRENTICE' END"
    )
    op.execute(
        "UPDATE capital_cells SET status = CASE "
        "WHEN status = 'HALTED' THEN 'HALTED_FOR_DAY' "
        "WHEN status = 'RETIRED' THEN 'DECOMMISSIONED' "
        "WHEN status IN ('APPRENTICE', 'GUARDED', 'AUTONOMOUS', "
        "'CAPITAL_BUILDER') THEN 'ACTIVE' ELSE status END"
    )
    op.create_check_constraint(
        op.f("ck_capital_cells_valid_lifecycle_status"),
        "capital_cells",
        "status IN ('INITIALIZING', 'ACTIVE', 'PAUSED', 'HALTED_FOR_DAY', "
        "'REPLICATION_READY', 'DECOMMISSIONED')",
    )
    op.create_check_constraint(
        op.f("ck_capital_cells_valid_autonomy_tier"),
        "capital_cells",
        "autonomy_tier IN ('APPRENTICE', 'GUARDED', 'CAPITAL_BUILDER')",
    )

    op.execute(
        "UPDATE trust_evaluations te SET current_autonomy_tier = CASE "
        "WHEN te.current_autonomy_tier = 'AUTONOMOUS' THEN 'CAPITAL_BUILDER' "
        "WHEN te.current_autonomy_tier IN ('APPRENTICE', 'GUARDED', "
        "'CAPITAL_BUILDER') THEN te.current_autonomy_tier "
        "ELSE cc.autonomy_tier END FROM capital_cells cc WHERE cc.cell_id = te.cell_id"
    )
    op.execute(
        "UPDATE trust_evaluations SET recommended_autonomy_tier = CASE "
        "WHEN recommended_autonomy_tier = 'AUTONOMOUS' THEN 'CAPITAL_BUILDER' "
        "WHEN recommended_autonomy_tier IN ('APPRENTICE', 'GUARDED', "
        "'CAPITAL_BUILDER') THEN recommended_autonomy_tier "
        "ELSE 'APPRENTICE' END"
    )
    op.create_check_constraint(
        op.f("ck_trust_evaluations_valid_current_autonomy_tier"),
        "trust_evaluations",
        "current_autonomy_tier IN ('APPRENTICE', 'GUARDED', 'CAPITAL_BUILDER')",
    )
    op.create_check_constraint(
        op.f("ck_trust_evaluations_valid_recommended_autonomy_tier"),
        "trust_evaluations",
        "recommended_autonomy_tier IN "
        "('APPRENTICE', 'GUARDED', 'CAPITAL_BUILDER')",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_trust_evaluations_valid_recommended_autonomy_tier"),
        "trust_evaluations",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_trust_evaluations_valid_current_autonomy_tier"),
        "trust_evaluations",
        type_="check",
    )
    op.execute(
        "UPDATE trust_evaluations SET current_autonomy_tier = 'AUTONOMOUS' "
        "WHERE current_autonomy_tier = 'CAPITAL_BUILDER'"
    )
    op.execute(
        "UPDATE trust_evaluations SET recommended_autonomy_tier = 'AUTONOMOUS' "
        "WHERE recommended_autonomy_tier = 'CAPITAL_BUILDER'"
    )

    op.drop_constraint(
        op.f("ck_capital_cells_valid_autonomy_tier"),
        "capital_cells",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_capital_cells_valid_lifecycle_status"),
        "capital_cells",
        type_="check",
    )
    op.execute(
        "UPDATE capital_cells SET status = CASE "
        "WHEN autonomy_tier = 'CAPITAL_BUILDER' THEN 'AUTONOMOUS' "
        "ELSE autonomy_tier END"
    )
    op.drop_column("capital_cells", "autonomy_tier")
