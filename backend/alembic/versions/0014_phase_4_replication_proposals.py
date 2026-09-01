"""Phase 4 Step 3A: replication proposals and append-only reservations.

Revision ID: 0014
Revises: 0013
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
CENT_MONEY = sa.Numeric(12, 2)
TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "cell_replication_proposals",
        sa.Column("proposal_id", UUID, primary_key=True),
        sa.Column("parent_cell_id", UUID, sa.ForeignKey("capital_cells.cell_id"), nullable=False),
        sa.Column("proposed_child_code", sa.String(16), nullable=False),
        sa.Column("capital_class", sa.String(32), server_default="MICRO-100-v1", nullable=False),
        sa.Column("proposed_seed_capital_usd", CENT_MONEY, nullable=False),
        sa.Column("strategy_identifier", sa.String(100), nullable=False),
        sa.Column("strategy_version", sa.String(50), nullable=False),
        sa.Column("risk_policy_identifier", sa.String(64), nullable=False),
        sa.Column("target_config_id", UUID, sa.ForeignKey("cell_treasury_configs.config_id"), nullable=False),
        sa.Column("proposed_autonomy_tier", sa.String(32), server_default="APPRENTICE", nullable=False),
        sa.Column("is_synthetic", sa.Boolean, server_default=sa.true(), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("created_at", TZ, nullable=False),
        sa.ForeignKeyConstraint(
            ["strategy_identifier", "strategy_version"],
            ["strategy_registry.strategy_id", "strategy_registry.version_tag"],
            name="fk_replication_proposal_strategy_version",
        ),
        sa.ForeignKeyConstraint(
            ["risk_policy_identifier"], ["risk_policies.policy_identifier"],
            name="fk_replication_proposal_risk_policy_identifier",
        ),
        sa.CheckConstraint(
            "proposed_seed_capital_usd > 0",
            name=op.f("ck_proposal_seed_positive"),
        ),
        sa.CheckConstraint(
            "is_synthetic = true",
            name=op.f("ck_proposals_phase4_synthetic_only"),
        ),
        sa.CheckConstraint(
            "manifest_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_replication_proposal_manifest_sha256"),
        ),
        sa.UniqueConstraint("proposed_child_code", name="uq_replication_proposed_child_code"),
        sa.UniqueConstraint("manifest_hash", name="uq_replication_proposal_manifest_hash"),
    )
    op.create_index(
        "idx_replication_proposals_parent", "cell_replication_proposals", ["parent_cell_id"]
    )

    op.create_table(
        "replication_proposal_events",
        sa.Column("event_id", UUID, primary_key=True),
        sa.Column(
            "proposal_id", UUID,
            sa.ForeignKey("cell_replication_proposals.proposal_id"), nullable=False,
        ),
        sa.Column("state_from", sa.String(32), nullable=False),
        sa.Column("state_to", sa.String(32), nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.Column("occurred_at", TZ, nullable=False),
        sa.CheckConstraint(
            "state_from IN ('INITIAL', 'PENDING_AUTHORIZATION', 'AUTHORIZED', "
            "'REJECTED', 'EXPIRED', 'EXECUTED', 'CANCELLED')",
            name=op.f("ck_proposal_events_state_from"),
        ),
        sa.CheckConstraint(
            "state_to IN ('PENDING_AUTHORIZATION', 'AUTHORIZED', 'REJECTED', "
            "'EXPIRED', 'EXECUTED', 'CANCELLED')",
            name=op.f("ck_proposal_events_state_to"),
        ),
    )
    op.create_index(
        "idx_proposal_events_proposal", "replication_proposal_events",
        ["proposal_id", "occurred_at"],
    )

    op.create_table(
        "replication_proposal_reservations",
        sa.Column("reservation_id", UUID, primary_key=True),
        sa.Column(
            "proposal_id", UUID,
            sa.ForeignKey("cell_replication_proposals.proposal_id"), nullable=False,
        ),
        sa.Column(
            "allocation_id", UUID, sa.ForeignKey("siphon_allocations.allocation_id"),
            nullable=False,
        ),
        sa.Column("reserved_usd", CENT_MONEY, nullable=False),
        sa.Column("occurred_at", TZ, nullable=False),
        sa.CheckConstraint(
            "reserved_usd > 0", name=op.f("ck_replication_reserved_positive")
        ),
        sa.UniqueConstraint(
            "proposal_id", "allocation_id", name="uq_replication_proposal_allocation"
        ),
    )
    op.create_index(
        "idx_replication_reservations_alloc", "replication_proposal_reservations",
        ["allocation_id"],
    )
    op.create_index(
        "idx_replication_reservations_prop", "replication_proposal_reservations",
        ["proposal_id"],
    )

    op.create_table(
        "replication_reservation_events",
        sa.Column("event_id", UUID, primary_key=True),
        sa.Column(
            "reservation_id", UUID,
            sa.ForeignKey("replication_proposal_reservations.reservation_id"), nullable=False,
        ),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.Column("occurred_at", TZ, nullable=False),
        sa.CheckConstraint(
            "event_type IN ('RESERVED', 'RELEASED', 'CONSUMED')",
            name=op.f("ck_reservation_events_type"),
        ),
    )
    op.create_index(
        "idx_reservation_events_res", "replication_reservation_events",
        ["reservation_id", "occurred_at"],
    )

    op.execute(
        """
        CREATE FUNCTION check_proposal_event_transition() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE v_prev_state VARCHAR(32);
        BEGIN
          PERFORM 1 FROM cell_replication_proposals
            WHERE proposal_id = NEW.proposal_id FOR UPDATE;
          IF NOT FOUND THEN
            RAISE EXCEPTION 'Proposal % does not resolve', NEW.proposal_id;
          END IF;

          SELECT state_to INTO v_prev_state
          FROM replication_proposal_events
          WHERE proposal_id = NEW.proposal_id
          ORDER BY occurred_at DESC, event_id DESC LIMIT 1;

          IF v_prev_state IS NULL THEN
            IF NEW.state_from <> 'INITIAL' OR NEW.state_to <> 'PENDING_AUTHORIZATION' THEN
              RAISE EXCEPTION 'First proposal event must transition from INITIAL to PENDING_AUTHORIZATION. Received % -> %',
                NEW.state_from, NEW.state_to;
            END IF;
          ELSE
            IF NEW.state_from <> v_prev_state THEN
              RAISE EXCEPTION 'Invalid transition: state_from (%) does not match current state (%)',
                NEW.state_from, v_prev_state;
            END IF;
            IF v_prev_state = 'PENDING_AUTHORIZATION'
               AND NEW.state_to NOT IN ('CANCELLED', 'EXPIRED') THEN
              RAISE EXCEPTION 'Unauthorized proposal transition from PENDING_AUTHORIZATION to % in Step 3A',
                NEW.state_to;
            END IF;
            IF v_prev_state IN ('CANCELLED', 'EXPIRED', 'REJECTED', 'EXECUTED') THEN
              RAISE EXCEPTION 'Proposal is in terminal state %. No further transitions allowed',
                v_prev_state;
            END IF;
          END IF;
          IF NEW.state_to IN ('AUTHORIZED', 'EXECUTED') THEN
            RAISE EXCEPTION 'State % is not authorized in Step 3A', NEW.state_to;
          END IF;
          RETURN NEW;
        END $$;

        CREATE TRIGGER trg_check_proposal_event_transition
        BEFORE INSERT ON replication_proposal_events
        FOR EACH ROW EXECUTE FUNCTION check_proposal_event_transition();

        CREATE FUNCTION check_replication_reservation_ceiling() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
          v_allocated_usd NUMERIC(12,2);
          v_bucket_type VARCHAR(32);
          v_total_consumed_usd NUMERIC(12,2) := 0.00;
          v_active_reserved_usd NUMERIC(12,2);
          v_allocation_id UUID;
          v_prev_event_type VARCHAR(32);
          v_prev_occurred_at TIMESTAMPTZ;
        BEGIN
          SELECT allocation_id INTO v_allocation_id
          FROM replication_proposal_reservations
          WHERE reservation_id = NEW.reservation_id FOR UPDATE;
          IF v_allocation_id IS NULL THEN
            RAISE EXCEPTION 'Reservation % does not resolve', NEW.reservation_id;
          END IF;

          SELECT event_type, occurred_at INTO v_prev_event_type, v_prev_occurred_at
          FROM replication_reservation_events
          WHERE reservation_id = NEW.reservation_id
            AND event_id <> NEW.event_id
          ORDER BY occurred_at DESC, event_id DESC LIMIT 1;

          IF v_prev_event_type IS NULL THEN
            IF NEW.event_type <> 'RESERVED' THEN
              RAISE EXCEPTION 'First reservation event must be RESERVED. Found % for reservation_id %',
                NEW.event_type, NEW.reservation_id;
            END IF;
          ELSE
            IF NEW.occurred_at < v_prev_occurred_at THEN
              RAISE EXCEPTION 'Reservation event timestamp cannot move backward. Current: %, Previous: %',
                NEW.occurred_at, v_prev_occurred_at;
            END IF;
            IF v_prev_event_type IN ('RELEASED', 'CONSUMED') THEN
              RAISE EXCEPTION 'Reservation % is in terminal state %. Cannot transition to %',
                NEW.reservation_id, v_prev_event_type, NEW.event_type;
            END IF;
            IF NEW.event_type = 'RESERVED' THEN
              RAISE EXCEPTION 'Reservation % already initialized. Duplicate RESERVED event prohibited',
                NEW.reservation_id;
            END IF;
          END IF;

          IF NEW.event_type = 'RESERVED' THEN
            SELECT allocated_usd, bucket_type INTO v_allocated_usd, v_bucket_type
            FROM siphon_allocations WHERE allocation_id = v_allocation_id FOR UPDATE;
            IF v_allocated_usd IS NULL THEN
              RAISE EXCEPTION 'Allocation % does not resolve', v_allocation_id;
            END IF;
            IF v_bucket_type <> 'REPLICATION_POOL' THEN
              RAISE EXCEPTION 'Replication reservations permitted strictly on REPLICATION_POOL buckets. Found % on allocation_id %',
                v_bucket_type, v_allocation_id;
            END IF;

            IF to_regclass('public.replication_cash_consumptions') IS NOT NULL THEN
              EXECUTE 'SELECT COALESCE(SUM(consumed_usd), 0.00) FROM replication_cash_consumptions WHERE allocation_id = $1'
                INTO v_total_consumed_usd USING v_allocation_id;
            END IF;

            SELECT COALESCE(SUM(r.reserved_usd), 0.00) INTO v_active_reserved_usd
            FROM replication_proposal_reservations r
            WHERE r.allocation_id = v_allocation_id
              AND (
                SELECT e.event_type FROM replication_reservation_events e
                WHERE e.reservation_id = r.reservation_id
                ORDER BY e.occurred_at DESC, e.event_id DESC LIMIT 1
              ) = 'RESERVED';

            IF v_total_consumed_usd + v_active_reserved_usd > v_allocated_usd THEN
              RAISE EXCEPTION 'Total commitment (Consumed % + Reserved %) exceeds allocation % on allocation_id %',
                v_total_consumed_usd, v_active_reserved_usd, v_allocated_usd, v_allocation_id;
            END IF;
          END IF;
          RETURN NEW;
        END $$;

        CREATE TRIGGER trg_check_replication_reservation_ceiling
        AFTER INSERT ON replication_reservation_events
        FOR EACH ROW EXECUTE FUNCTION check_replication_reservation_ceiling();

        CREATE FUNCTION reject_replication_fact_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
          RAISE EXCEPTION 'Immutable fact and event tables cannot be updated or deleted: %',
            TG_TABLE_NAME;
        END $$;
        """
    )
    for table in (
        "cell_replication_proposals",
        "replication_proposal_events",
        "replication_proposal_reservations",
        "replication_reservation_events",
    ):
        op.execute(
            f"CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_replication_fact_mutation()"
        )

    op.execute(
        """
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kairo_runtime') THEN
            REVOKE ALL ON cell_replication_proposals FROM kairo_runtime;
            GRANT SELECT, INSERT ON cell_replication_proposals TO kairo_runtime;
            REVOKE ALL ON replication_proposal_events FROM kairo_runtime;
            GRANT SELECT, INSERT ON replication_proposal_events TO kairo_runtime;
            REVOKE ALL ON replication_proposal_reservations FROM kairo_runtime;
            GRANT SELECT, INSERT ON replication_proposal_reservations TO kairo_runtime;
            REVOKE ALL ON replication_reservation_events FROM kairo_runtime;
            GRANT SELECT, INSERT ON replication_reservation_events TO kairo_runtime;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    for table in (
        "replication_reservation_events",
        "replication_proposal_reservations",
        "replication_proposal_events",
        "cell_replication_proposals",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_immutable ON {table}")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_check_replication_reservation_ceiling "
        "ON replication_reservation_events"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_check_proposal_event_transition "
        "ON replication_proposal_events"
    )
    op.execute("DROP FUNCTION IF EXISTS reject_replication_fact_mutation()")
    op.execute("DROP FUNCTION IF EXISTS check_replication_reservation_ceiling()")
    op.execute("DROP FUNCTION IF EXISTS check_proposal_event_transition()")
    op.drop_table("replication_reservation_events")
    op.drop_table("replication_proposal_reservations")
    op.drop_table("replication_proposal_events")
    op.drop_table("cell_replication_proposals")
