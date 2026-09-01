"""Phase 4 Step 3B: human-authorized genesis engine.

Revision ID: 0015
Revises: 0014
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
CENT_MONEY = sa.Numeric(12, 2)
TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    # The referenced target config is mutable. Snapshot its prospective child
    # bindings onto the append-only proposal before any human authorization.
    op.add_column("cell_replication_proposals", sa.Column("target_type", sa.String(32)))
    op.add_column("cell_replication_proposals", sa.Column("target_instrument_id", UUID))
    op.add_column("cell_replication_proposals", sa.Column("target_symbol", sa.String(32)))
    op.add_column("cell_replication_proposals", sa.Column("target_treasury_code", sa.String(50)))
    op.execute(
        """
        UPDATE cell_replication_proposals p
        SET target_type = c.target_type,
            target_instrument_id = c.target_instrument_id,
            target_symbol = c.target_symbol,
            target_treasury_code = c.target_symbol
        FROM cell_treasury_configs c
        WHERE c.config_id = p.target_config_id
        """
    )
    op.execute(
        """
        DO $$ BEGIN
          IF EXISTS (
            SELECT 1 FROM cell_replication_proposals
            WHERE target_type IS NULL OR target_instrument_id IS NULL
               OR target_symbol IS NULL OR target_treasury_code IS NULL
          ) THEN
            RAISE EXCEPTION 'existing replication proposal target lineage cannot be resolved';
          END IF;
        END $$
        """
    )
    for column in ("target_type", "target_instrument_id", "target_symbol", "target_treasury_code"):
        op.alter_column("cell_replication_proposals", column, nullable=False)
    op.create_foreign_key(
        "fk_replication_proposal_target_instrument",
        "cell_replication_proposals", "instruments",
        ["target_instrument_id"], ["instrument_id"],
    )
    op.create_check_constraint(
        op.f("ck_replication_proposals_target_type"),
        "cell_replication_proposals",
        "target_type IN ('SINGLE_ASSET', 'BASKET', 'INDEX', 'CASH_GOAL')",
    )

    op.create_table(
        "replication_authorizations",
        sa.Column("authorization_id", UUID, primary_key=True),
        sa.Column(
            "proposal_id", UUID,
            sa.ForeignKey("cell_replication_proposals.proposal_id"),
            nullable=False, unique=True,
        ),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("authorized_by", sa.String(64), nullable=False),
        sa.Column("authorization_method", sa.String(32), nullable=False),
        sa.Column("authorized_at", TZ, nullable=False),
        sa.CheckConstraint(
            "decision IN ('APPROVE', 'REJECT')",
            name=op.f("ck_replication_authorizations_decision"),
        ),
        sa.CheckConstraint(
            "manifest_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_replication_authorizations_manifest_sha256"),
        ),
    )

    op.create_table(
        "replication_cash_consumptions",
        sa.Column("consumption_id", UUID, primary_key=True),
        sa.Column(
            "proposal_id", UUID,
            sa.ForeignKey("cell_replication_proposals.proposal_id"), nullable=False,
        ),
        sa.Column(
            "child_cell_id", UUID, sa.ForeignKey("capital_cells.cell_id"), nullable=False,
        ),
        sa.Column(
            "allocation_id", UUID,
            sa.ForeignKey("siphon_allocations.allocation_id"), nullable=False,
        ),
        sa.Column("consumed_usd", CENT_MONEY, nullable=False),
        sa.Column("is_synthetic", sa.Boolean, server_default=sa.true(), nullable=False),
        sa.Column("occurred_at", TZ, nullable=False),
        sa.CheckConstraint("consumed_usd > 0", name=op.f("ck_genesis_consumed_positive")),
        sa.CheckConstraint("is_synthetic = true", name=op.f("ck_genesis_consumed_synthetic_only")),
        sa.UniqueConstraint(
            "proposal_id", "allocation_id",
            name="uq_genesis_consumption_proposal_alloc",
        ),
    )
    op.create_index(
        "idx_replication_consumptions_child", "replication_cash_consumptions", ["child_cell_id"]
    )
    op.create_index(
        "idx_replication_consumptions_alloc", "replication_cash_consumptions", ["allocation_id"]
    )

    op.create_table(
        "cell_genesis_events",
        sa.Column("genesis_id", UUID, primary_key=True),
        sa.Column(
            "proposal_id", UUID,
            sa.ForeignKey("cell_replication_proposals.proposal_id"),
            nullable=False, unique=True,
        ),
        sa.Column(
            "parent_cell_id", UUID, sa.ForeignKey("capital_cells.cell_id"), nullable=False,
        ),
        sa.Column(
            "child_cell_id", UUID, sa.ForeignKey("capital_cells.cell_id"),
            nullable=False, unique=True,
        ),
        sa.Column("seed_capital_usd", CENT_MONEY, nullable=False),
        sa.Column("is_synthetic", sa.Boolean, server_default=sa.true(), nullable=False),
        sa.Column("occurred_at", TZ, nullable=False),
        sa.CheckConstraint("seed_capital_usd = 100.00", name=op.f("ck_genesis_seed_exact_micro100")),
        sa.CheckConstraint("is_synthetic = true", name=op.f("ck_genesis_event_synthetic_only")),
    )

    op.execute(
        """
        CREATE FUNCTION check_replication_consumption_validity() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
          v_reserved_usd NUMERIC(12,2);
          v_reservation_id UUID;
          v_latest_res_state VARCHAR(32);
          v_proposal_state VARCHAR(32);
          v_parent_cell_id UUID;
          v_allocation_cell_id UUID;
          v_allocation_synthetic BOOLEAN;
          v_child_domain VARCHAR(32);
          v_child_code VARCHAR(50);
          v_proposed_child_code VARCHAR(16);
          v_total_consumed_usd NUMERIC(12,2);
        BEGIN
          SELECT se.cell_id, se.is_synthetic
          INTO v_allocation_cell_id, v_allocation_synthetic
          FROM siphon_allocations sa
          JOIN siphon_events se ON se.siphon_id = sa.siphon_id
          WHERE sa.allocation_id = NEW.allocation_id
          FOR UPDATE OF sa;
          IF NOT FOUND THEN
            RAISE EXCEPTION 'Cannot consume cash: allocation % does not resolve', NEW.allocation_id;
          END IF;

          SELECT parent_cell_id, proposed_child_code
          INTO v_parent_cell_id, v_proposed_child_code
          FROM cell_replication_proposals
          WHERE proposal_id = NEW.proposal_id FOR UPDATE;
          IF NOT FOUND THEN
            RAISE EXCEPTION 'Cannot consume cash: proposal % does not resolve', NEW.proposal_id;
          END IF;
          IF v_allocation_cell_id <> v_parent_cell_id OR v_allocation_synthetic IS DISTINCT FROM TRUE THEN
            RAISE EXCEPTION 'Cannot consume cash: allocation lineage/domain does not match synthetic proposal';
          END IF;

          SELECT state_to INTO v_proposal_state
          FROM replication_proposal_events
          WHERE proposal_id = NEW.proposal_id
          ORDER BY occurred_at DESC, event_id DESC LIMIT 1;
          IF v_proposal_state IS NULL OR v_proposal_state <> 'AUTHORIZED' THEN
            RAISE EXCEPTION 'Cannot consume cash: Proposal % is in state %, expected AUTHORIZED',
              NEW.proposal_id, v_proposal_state;
          END IF;

          SELECT economic_domain, cell_code INTO v_child_domain, v_child_code
          FROM capital_cells WHERE cell_id = NEW.child_cell_id;
          IF NOT FOUND OR v_child_domain <> 'SYNTHETIC' OR v_child_code <> v_proposed_child_code THEN
            RAISE EXCEPTION 'Cannot consume cash: child cell lineage/domain does not match proposal';
          END IF;

          SELECT reservation_id, reserved_usd INTO v_reservation_id, v_reserved_usd
          FROM replication_proposal_reservations
          WHERE proposal_id = NEW.proposal_id AND allocation_id = NEW.allocation_id
          FOR UPDATE;
          IF NOT FOUND THEN
            RAISE EXCEPTION 'No reservation exists on proposal % for allocation %',
              NEW.proposal_id, NEW.allocation_id;
          END IF;

          SELECT event_type INTO v_latest_res_state
          FROM replication_reservation_events
          WHERE reservation_id = v_reservation_id
          ORDER BY occurred_at DESC, event_id DESC LIMIT 1;
          IF v_latest_res_state IS NULL OR v_latest_res_state <> 'RESERVED' THEN
            RAISE EXCEPTION 'Reservation % is in state %, expected RESERVED for consumption',
              v_reservation_id, v_latest_res_state;
          END IF;

          SELECT COALESCE(SUM(consumed_usd), 0.00) INTO v_total_consumed_usd
          FROM replication_cash_consumptions
          WHERE proposal_id = NEW.proposal_id AND allocation_id = NEW.allocation_id;
          IF v_total_consumed_usd + NEW.consumed_usd > v_reserved_usd THEN
            RAISE EXCEPTION 'Cumulative consumption (% + %) exceeds reserved amount (%) for reservation %',
              v_total_consumed_usd, NEW.consumed_usd, v_reserved_usd, v_reservation_id;
          END IF;
          RETURN NEW;
        END $$;

        CREATE TRIGGER trg_check_replication_consumption_validity
        BEFORE INSERT ON replication_cash_consumptions
        FOR EACH ROW EXECUTE FUNCTION check_replication_consumption_validity();

        DROP TRIGGER trg_check_proposal_event_transition ON replication_proposal_events;
        DROP FUNCTION check_proposal_event_transition();

        CREATE FUNCTION check_proposal_event_transition_v2() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
          v_prev_state VARCHAR(32);
          v_prev_time TIMESTAMPTZ;
          v_proposal_hash VARCHAR(64);
          v_seed_capital NUMERIC(12,2);
          v_auth RECORD;
          v_consumed_usd NUMERIC(12,2);
          v_child_cell_id UUID;
          v_manifest_id UUID;
          v_genesis_seed NUMERIC(12,2);
        BEGIN
          PERFORM 1 FROM cell_replication_proposals
          WHERE proposal_id = NEW.proposal_id FOR UPDATE;
          IF NOT FOUND THEN RAISE EXCEPTION 'Proposal % does not resolve', NEW.proposal_id; END IF;

          SELECT state_to, occurred_at INTO v_prev_state, v_prev_time
          FROM replication_proposal_events
          WHERE proposal_id = NEW.proposal_id
          ORDER BY occurred_at DESC, event_id DESC LIMIT 1;
          SELECT manifest_hash, proposed_seed_capital_usd
          INTO v_proposal_hash, v_seed_capital
          FROM cell_replication_proposals WHERE proposal_id = NEW.proposal_id;

          IF v_prev_state IS NULL THEN
            IF NEW.state_from <> 'INITIAL' OR NEW.state_to <> 'PENDING_AUTHORIZATION' THEN
              RAISE EXCEPTION 'First proposal event must be INITIAL -> PENDING_AUTHORIZATION. Received % -> %',
                NEW.state_from, NEW.state_to;
            END IF;
            RETURN NEW;
          END IF;
          IF NEW.occurred_at < v_prev_time THEN
            RAISE EXCEPTION 'Proposal event timestamp cannot move backward';
          END IF;
          IF NEW.state_from <> v_prev_state THEN
            RAISE EXCEPTION 'Invalid transition: state_from (%) does not match current state (%)',
              NEW.state_from, v_prev_state;
          END IF;

          IF v_prev_state = 'PENDING_AUTHORIZATION' THEN
            IF NEW.state_to = 'AUTHORIZED' THEN
              SELECT * INTO v_auth FROM replication_authorizations
              WHERE proposal_id = NEW.proposal_id;
              IF NOT FOUND OR v_auth.decision <> 'APPROVE' THEN
                RAISE EXCEPTION 'Cannot transition to AUTHORIZED without matching APPROVE authorization';
              END IF;
              IF v_auth.manifest_hash <> v_proposal_hash THEN
                RAISE EXCEPTION 'Cannot transition to AUTHORIZED: Manifest hash mismatch';
              END IF;
            ELSIF NEW.state_to = 'REJECTED' THEN
              SELECT * INTO v_auth FROM replication_authorizations
              WHERE proposal_id = NEW.proposal_id;
              IF NOT FOUND OR v_auth.decision <> 'REJECT' OR v_auth.manifest_hash <> v_proposal_hash THEN
                RAISE EXCEPTION 'Cannot transition to REJECTED without matching REJECT authorization';
              END IF;
            ELSIF NEW.state_to NOT IN ('CANCELLED', 'EXPIRED') THEN
              RAISE EXCEPTION 'Illegal transition from PENDING_AUTHORIZATION to %', NEW.state_to;
            END IF;
          ELSIF v_prev_state = 'AUTHORIZED' THEN
            IF NEW.state_to <> 'EXECUTED' THEN
              RAISE EXCEPTION 'Illegal transition from AUTHORIZED to %', NEW.state_to;
            END IF;
            IF v_seed_capital <> 100.00 THEN
              RAISE EXCEPTION 'Cannot transition to EXECUTED: seed capital must equal 100.00';
            END IF;
            SELECT child_cell_id, seed_capital_usd
            INTO v_child_cell_id, v_genesis_seed
            FROM cell_genesis_events WHERE proposal_id = NEW.proposal_id;
            IF NOT FOUND OR v_genesis_seed <> v_seed_capital THEN
              RAISE EXCEPTION 'Cannot transition to EXECUTED: valid cell_genesis_events fact missing';
            END IF;
            IF NOT EXISTS (
              SELECT 1 FROM capital_cells
              WHERE cell_id = v_child_cell_id AND economic_domain = 'SYNTHETIC'
            ) THEN
              RAISE EXCEPTION 'Cannot transition to EXECUTED: synthetic child cell missing';
            END IF;
            SELECT manifest_id INTO v_manifest_id
            FROM synthetic_evidence_manifests
            WHERE cell_id = v_child_cell_id AND manifest_type = 'GENESIS_SEED'
              AND manifest_algorithm = 'GENESIS-SEED-MANIFEST-v1'
              AND model_identifier = 'KAIRO-GENESIS' AND model_version = '1.0.0';
            IF NOT FOUND THEN
              RAISE EXCEPTION 'Cannot transition to EXECUTED: GENESIS_SEED manifest missing';
            END IF;
            IF NOT EXISTS (
              SELECT 1 FROM kairo_capital_authorizations
              WHERE cell_id = v_child_cell_id AND economic_domain = 'SYNTHETIC'
                AND synthetic_provenance_id = v_manifest_id
                AND settled_cash = 100.00 AND authorized_trading_cash = 100.00
                AND safety_reserve = 0.00 AND ownership_treasury_reserved = 0.00
                AND replication_reserve = 0.00 AND committed_obligations = 0.00
            ) THEN
              RAISE EXCEPTION 'Cannot transition to EXECUTED: canonical capital authorization missing';
            END IF;
            IF NOT EXISTS (
              SELECT 1 FROM cell_treasury_configs
              WHERE cell_id = v_child_cell_id AND is_active = true
            ) THEN
              RAISE EXCEPTION 'Cannot transition to EXECUTED: active treasury config missing';
            END IF;
            SELECT COALESCE(SUM(consumed_usd), 0.00) INTO v_consumed_usd
            FROM replication_cash_consumptions
            WHERE proposal_id = NEW.proposal_id AND child_cell_id = v_child_cell_id;
            IF v_consumed_usd <> v_seed_capital THEN
              RAISE EXCEPTION 'Cannot transition to EXECUTED: Consumed cash (%) does not equal seed capital (%)',
                v_consumed_usd, v_seed_capital;
            END IF;
            IF EXISTS (
              SELECT 1 FROM replication_proposal_reservations r
              WHERE r.proposal_id = NEW.proposal_id AND (
                SELECT e.event_type FROM replication_reservation_events e
                WHERE e.reservation_id = r.reservation_id
                ORDER BY e.occurred_at DESC, e.event_id DESC LIMIT 1
              ) IS DISTINCT FROM 'CONSUMED'
            ) THEN
              RAISE EXCEPTION 'Cannot transition to EXECUTED: all reservations must be CONSUMED';
            END IF;
          ELSIF v_prev_state IN ('REJECTED', 'CANCELLED', 'EXPIRED', 'EXECUTED') THEN
            RAISE EXCEPTION 'Proposal is in terminal state %. No further transitions allowed', v_prev_state;
          ELSE
            RAISE EXCEPTION 'Unknown previous state: %', v_prev_state;
          END IF;
          RETURN NEW;
        END $$;

        CREATE TRIGGER trg_check_proposal_event_transition
        BEFORE INSERT ON replication_proposal_events
        FOR EACH ROW EXECUTE FUNCTION check_proposal_event_transition_v2();

        CREATE TRIGGER trg_replication_authorizations_immutable
        BEFORE UPDATE OR DELETE ON replication_authorizations
        FOR EACH ROW EXECUTE FUNCTION reject_replication_fact_mutation();
        CREATE TRIGGER trg_replication_cash_consumptions_immutable
        BEFORE UPDATE OR DELETE ON replication_cash_consumptions
        FOR EACH ROW EXECUTE FUNCTION reject_replication_fact_mutation();
        CREATE TRIGGER trg_cell_genesis_events_immutable
        BEFORE UPDATE OR DELETE ON cell_genesis_events
        FOR EACH ROW EXECUTE FUNCTION reject_replication_fact_mutation();
        """
    )

    op.execute(
        """
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kairo_runtime') THEN
            REVOKE ALL ON replication_authorizations FROM kairo_runtime;
            GRANT SELECT ON replication_authorizations TO kairo_runtime;
            REVOKE ALL ON replication_cash_consumptions FROM kairo_runtime;
            GRANT SELECT, INSERT ON replication_cash_consumptions TO kairo_runtime;
            REVOKE ALL ON cell_genesis_events FROM kairo_runtime;
            GRANT SELECT, INSERT ON cell_genesis_events TO kairo_runtime;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_cell_genesis_events_immutable ON cell_genesis_events")
    op.execute("DROP TRIGGER IF EXISTS trg_replication_cash_consumptions_immutable ON replication_cash_consumptions")
    op.execute("DROP TRIGGER IF EXISTS trg_replication_authorizations_immutable ON replication_authorizations")
    op.execute("DROP TRIGGER IF EXISTS trg_check_replication_consumption_validity ON replication_cash_consumptions")
    op.execute("DROP FUNCTION IF EXISTS check_replication_consumption_validity()")
    op.execute("DROP TRIGGER IF EXISTS trg_check_proposal_event_transition ON replication_proposal_events")
    op.execute("DROP FUNCTION IF EXISTS check_proposal_event_transition_v2()")
    op.execute(
        """
        CREATE FUNCTION check_proposal_event_transition() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE v_prev_state VARCHAR(32);
        BEGIN
          PERFORM 1 FROM cell_replication_proposals
            WHERE proposal_id = NEW.proposal_id FOR UPDATE;
          IF NOT FOUND THEN RAISE EXCEPTION 'Proposal % does not resolve', NEW.proposal_id; END IF;
          SELECT state_to INTO v_prev_state FROM replication_proposal_events
          WHERE proposal_id = NEW.proposal_id
          ORDER BY occurred_at DESC, event_id DESC LIMIT 1;
          IF v_prev_state IS NULL THEN
            IF NEW.state_from <> 'INITIAL' OR NEW.state_to <> 'PENDING_AUTHORIZATION' THEN
              RAISE EXCEPTION 'First proposal event must transition from INITIAL to PENDING_AUTHORIZATION. Received % -> %', NEW.state_from, NEW.state_to;
            END IF;
          ELSE
            IF NEW.state_from <> v_prev_state THEN
              RAISE EXCEPTION 'Invalid transition: state_from (%) does not match current state (%)', NEW.state_from, v_prev_state;
            END IF;
            IF v_prev_state = 'PENDING_AUTHORIZATION' AND NEW.state_to NOT IN ('CANCELLED', 'EXPIRED') THEN
              RAISE EXCEPTION 'Unauthorized proposal transition from PENDING_AUTHORIZATION to % in Step 3A', NEW.state_to;
            END IF;
            IF v_prev_state IN ('CANCELLED', 'EXPIRED', 'REJECTED', 'EXECUTED') THEN
              RAISE EXCEPTION 'Proposal is in terminal state %. No further transitions allowed', v_prev_state;
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
        """
    )
    op.drop_index("idx_replication_consumptions_alloc", table_name="replication_cash_consumptions")
    op.drop_index("idx_replication_consumptions_child", table_name="replication_cash_consumptions")
    op.drop_table("cell_genesis_events")
    op.drop_table("replication_cash_consumptions")
    op.drop_table("replication_authorizations")
    op.drop_constraint("fk_replication_proposal_target_instrument", "cell_replication_proposals", type_="foreignkey")
    op.drop_constraint(op.f("ck_replication_proposals_target_type"), "cell_replication_proposals", type_="check")
    for column in ("target_treasury_code", "target_symbol", "target_instrument_id", "target_type"):
        op.drop_column("cell_replication_proposals", column)
