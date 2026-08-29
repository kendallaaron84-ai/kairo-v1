"""Seed frozen EMA-CROSS-001 v1.0.0 configuration.

Revision ID: 0007
Revises: 0006
"""

from collections.abc import Sequence
import json

from alembic import op
import sqlalchemy as sa

from engine.strategy.registry_seed import (
    DISPLAY_NAME,
    STRATEGY_ID,
    STRATEGY_VERSION,
    ema_cross_v100_configuration,
)


revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _configuration_literal() -> str:
    serialized = json.dumps(
        ema_cross_v100_configuration(), sort_keys=True, separators=(",", ":")
    )
    return serialized.replace("'", "''")


def upgrade() -> None:
    configuration = _configuration_literal()
    op.execute(
        sa.DDL(
            "DO $$ DECLARE existing_name text; existing_configuration jsonb; "
            "BEGIN SELECT display_name, configuration "
            "INTO existing_name, existing_configuration FROM strategy_registry "
            f"WHERE strategy_id = '{STRATEGY_ID}' "
            f"AND version_tag = '{STRATEGY_VERSION}'; "
            "IF NOT FOUND THEN INSERT INTO strategy_registry "
            "(strategy_id, version_tag, display_name, status, configuration) VALUES "
            f"('{STRATEGY_ID}', '{STRATEGY_VERSION}', '{DISPLAY_NAME}', 'ACTIVE', "
            f"'{configuration}'::jsonb); "
            f"ELSIF existing_name IS DISTINCT FROM '{DISPLAY_NAME}' "
            f"OR existing_configuration IS DISTINCT FROM '{configuration}'::jsonb "
            "THEN RAISE EXCEPTION "
            "'EMA-CROSS-001 v1.0.0 exists with conflicting immutable content'; "
            "END IF; END $$;"
        )
    )


def downgrade() -> None:
    configuration = _configuration_literal()
    op.execute(
        sa.DDL(
            "DO $$ DECLARE existing_configuration jsonb; "
            "BEGIN SELECT configuration INTO existing_configuration "
            "FROM strategy_registry "
            f"WHERE strategy_id = '{STRATEGY_ID}' "
            f"AND version_tag = '{STRATEGY_VERSION}'; "
            "IF NOT FOUND THEN RETURN; "
            f"ELSIF existing_configuration IS DISTINCT FROM '{configuration}'::jsonb "
            "THEN RAISE EXCEPTION "
            "'refusing to remove a modified EMA-CROSS-001 v1.0.0 seed'; "
            "END IF; DELETE FROM strategy_registry "
            f"WHERE strategy_id = '{STRATEGY_ID}' "
            f"AND version_tag = '{STRATEGY_VERSION}'; END $$;"
        )
    )
