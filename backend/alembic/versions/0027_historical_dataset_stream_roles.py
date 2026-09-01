"""Phase 4.5 Stage 1: canonical historical dataset stream roles.

Revision ID: 0027
Revises: 0026
"""
from collections.abc import Sequence

from alembic import op


revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "uq_dataset_symbol",
        "historical_market_dataset_symbols",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_dataset_symbol_stream_role",
        "historical_market_dataset_symbols",
        ["dataset_id", "symbol", "stream_role"],
    )


def downgrade() -> None:
    # Restoring the 0026 invariant is safe only when no immutable facts would
    # collapse. PostgreSQL NULL semantics cannot hide a conflict because all
    # three lineage columns are NOT NULL.
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1
            FROM historical_market_dataset_symbols
            GROUP BY dataset_id, symbol
            HAVING COUNT(*) > 1
          ) THEN
            RAISE EXCEPTION
              'Downgrade failed closed: multiple stream roles exist for a dataset symbol';
          END IF;
        END
        $$;
        """
    )
    op.drop_constraint(
        "uq_dataset_symbol_stream_role",
        "historical_market_dataset_symbols",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_dataset_symbol",
        "historical_market_dataset_symbols",
        ["dataset_id", "symbol"],
    )
