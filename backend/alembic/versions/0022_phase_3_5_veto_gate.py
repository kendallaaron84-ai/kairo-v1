"""Phase 3.5 Step 6: VETO_ONLY Governance and Authority Gate.

Revision ID: 0022
Revises: 0021
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "intelligence_authority_proposals",
        sa.Column("proposal_id", UUID, primary_key=True),
        sa.Column("cell_id", UUID, sa.ForeignKey("capital_cells.cell_id"), nullable=False),
        sa.Column("target_authority", sa.String(32), server_default="VETO_ONLY", nullable=False),
        sa.Column("policy_version", sa.String(64), nullable=False),
        sa.Column("step5_run_id", UUID, sa.ForeignKey("intelligence_research_runs.run_id"), nullable=False),
        sa.Column("step5_5_run_id", UUID, sa.ForeignKey("intelligence_stateful_replay_runs.replay_run_id"), nullable=False),
        sa.Column("evaluated_veto_opportunities", sa.Integer(), nullable=False),
        sa.Column("distinct_trading_months", sa.Integer(), nullable=False),
        sa.Column("sample_start_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sample_end_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trade_removal_alpha_usd", sa.Numeric(12, 2), nullable=False),
        sa.Column("stateful_alpha_usd", sa.Numeric(12, 2), nullable=False),
        sa.Column("drawdown_reduction_usd", sa.Numeric(12, 2), nullable=False),
        sa.Column("veto_precision_pct", sa.Numeric(5, 2), nullable=False),
        sa.Column("criteria_passed", sa.Boolean(), nullable=False),
        sa.Column("proposal_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("proposed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("target_authority = 'VETO_ONLY'", name="ck_proposal_target_authority_veto"),
        sa.CheckConstraint("policy_version = 'INTEL-VETO-MACRO-v1'", name="ck_proposal_policy_version"),
        sa.CheckConstraint("evaluated_veto_opportunities >= 0", name="ck_proposal_veto_opps_pos"),
        sa.CheckConstraint("distinct_trading_months >= 0", name="ck_proposal_months_pos"),
        sa.CheckConstraint("sample_end_time >= sample_start_time", name="ck_proposal_window"),
        sa.CheckConstraint("veto_precision_pct >= 0.00 AND veto_precision_pct <= 100.00", name="ck_proposal_precision_range"),
        sa.CheckConstraint("proposal_manifest_sha256 ~ '^[0-9a-f]{64}$'", name="ck_proposal_manifest_sha256"),
        sa.UniqueConstraint("step5_run_id", "step5_5_run_id", "policy_version", name="uq_authority_proposal_evidence_policy"),
    )
    op.create_index("idx_auth_proposals_cell", "intelligence_authority_proposals", ["cell_id", "proposed_at"])

    op.create_table(
        "intelligence_authority_decisions",
        sa.Column("decision_id", UUID, primary_key=True),
        sa.Column("proposal_id", UUID, sa.ForeignKey("intelligence_authority_proposals.proposal_id"), nullable=False, unique=True),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("operator_identity", sa.String(128), nullable=False),
        sa.Column("approved_proposal_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("decision_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("decision IN ('APPROVED', 'REJECTED')", name="ck_authority_decision_valid"),
        sa.CheckConstraint("btrim(operator_identity) <> ''", name="ck_authority_decision_operator"),
        sa.CheckConstraint("approved_proposal_manifest_sha256 ~ '^[0-9a-f]{64}$'", name="ck_decision_proposal_manifest_sha256"),
        sa.CheckConstraint("decision_manifest_sha256 ~ '^[0-9a-f]{64}$'", name="ck_decision_manifest_sha256"),
    )
    op.create_index("idx_auth_decisions_proposal", "intelligence_authority_decisions", ["proposal_id"])

    op.create_table(
        "cell_intelligence_authority_events",
        sa.Column("event_id", UUID, primary_key=True),
        sa.Column("cell_id", UUID, sa.ForeignKey("capital_cells.cell_id"), nullable=False),
        sa.Column("decision_id", UUID, sa.ForeignKey("intelligence_authority_decisions.decision_id")),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("authority_mode", sa.String(32), nullable=False),
        sa.Column("policy_version", sa.String(64), nullable=False),
        sa.Column("operator_identity", sa.String(128), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("event_type IN ('GRANTED', 'REVOKED', 'EXPIRED')", name="ck_authority_event_type"),
        sa.CheckConstraint("authority_mode IN ('OBSERVE_ONLY', 'VETO_ONLY')", name="ck_authority_event_mode"),
        sa.CheckConstraint("policy_version = 'INTEL-VETO-MACRO-v1'", name="ck_authority_event_policy"),
        sa.CheckConstraint("btrim(operator_identity) <> ''", name="ck_authority_event_operator"),
        sa.CheckConstraint("event_manifest_sha256 ~ '^[0-9a-f]{64}$'", name="ck_authority_event_manifest_sha256"),
        sa.UniqueConstraint("decision_id", name="uq_authority_event_decision"),
    )
    op.create_index("idx_auth_events_cell_eff", "cell_intelligence_authority_events", ["cell_id", "effective_at", "created_at"])

    op.execute("""
    CREATE OR REPLACE FUNCTION check_authority_proposal_validity()
    RETURNS TRIGGER AS $$
    DECLARE
      v_step5 intelligence_research_runs%ROWTYPE;
      v_step55 intelligence_stateful_replay_runs%ROWTYPE;
      v_months INTEGER;
      v_allowed_vetoes INTEGER;
      v_expected BOOLEAN;
    BEGIN
      SELECT * INTO v_step5 FROM intelligence_research_runs WHERE run_id = NEW.step5_run_id;
      IF NOT FOUND THEN RAISE EXCEPTION 'Authority proposal rejected: Step 5 evidence does not resolve'; END IF;
      SELECT * INTO v_step55 FROM intelligence_stateful_replay_runs WHERE replay_run_id = NEW.step5_5_run_id;
      IF NOT FOUND THEN RAISE EXCEPTION 'Authority proposal rejected: Step 5.5 evidence does not resolve'; END IF;
      IF v_step5.cell_id IS NULL OR v_step5.cell_id <> NEW.cell_id OR v_step55.cell_id <> NEW.cell_id THEN
        RAISE EXCEPTION 'Authority proposal rejected: evidence cell mismatch';
      END IF;
      IF v_step5.sample_start_time <> v_step55.sample_start_time
         OR v_step5.sample_end_time <> v_step55.sample_end_time
         OR NEW.sample_start_time <> v_step5.sample_start_time
         OR NEW.sample_end_time <> v_step5.sample_end_time THEN
        RAISE EXCEPTION 'Authority proposal rejected: evidence sample window mismatch';
      END IF;
      IF NEW.evaluated_veto_opportunities <> v_step5.total_veto_opportunities
         OR NEW.trade_removal_alpha_usd <> v_step5.net_alpha_usd
         OR NEW.stateful_alpha_usd <> v_step55.stateful_net_alpha_usd
         OR NEW.drawdown_reduction_usd <> v_step55.drawdown_reduction_usd
         OR NEW.veto_precision_pct <> v_step5.veto_precision_pct THEN
        RAISE EXCEPTION 'Authority proposal rejected: metrics do not match canonical evidence';
      END IF;
      SELECT count(*)::INTEGER,
             count(DISTINCT date_trunc('month', oce.evaluated_at))::INTEGER
      INTO v_allowed_vetoes, v_months
      FROM order_context_evaluations oce
      JOIN order_intents oi ON oi.intent_id = oce.intent_id
      WHERE oi.cell_id = NEW.cell_id
        AND oce.counterfactual_opinion = 'WOULD_HAVE_VETOED'
        AND oce.veto_reason_code = 'CRITICAL_MACRO_WINDOW_ACTIVE'
        AND oce.evaluated_at BETWEEN NEW.sample_start_time AND NEW.sample_end_time;
      IF NEW.distinct_trading_months <> v_months THEN
        RAISE EXCEPTION 'Authority proposal rejected: distinct trading months do not match canonical evidence';
      END IF;
      v_expected := NEW.evaluated_veto_opportunities >= 30
        AND v_allowed_vetoes = NEW.evaluated_veto_opportunities
        AND NEW.distinct_trading_months >= 3
        AND NEW.trade_removal_alpha_usd > 0
        AND NEW.stateful_alpha_usd > 0
        AND NEW.drawdown_reduction_usd >= 0
        AND NEW.veto_precision_pct >= 60.00;
      IF NEW.criteria_passed IS DISTINCT FROM v_expected THEN
        RAISE EXCEPTION 'Authority proposal rejected: criteria_passed does not equal canonical policy result';
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;

    CREATE OR REPLACE FUNCTION check_authority_decision_validity()
    RETURNS TRIGGER AS $$
    DECLARE v_proposal intelligence_authority_proposals%ROWTYPE;
    BEGIN
      SELECT * INTO v_proposal FROM intelligence_authority_proposals WHERE proposal_id = NEW.proposal_id;
      IF NOT FOUND THEN RAISE EXCEPTION 'Authority decision rejected: proposal does not resolve'; END IF;
      IF NEW.approved_proposal_manifest_sha256 <> v_proposal.proposal_manifest_sha256 THEN
        RAISE EXCEPTION 'Authority decision rejected: proposal manifest hash mismatch';
      END IF;
      IF NEW.decided_at < v_proposal.proposed_at THEN
        RAISE EXCEPTION 'Authority decision rejected: decision predates proposal';
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;

    CREATE OR REPLACE FUNCTION check_authority_event_validity()
    RETURNS TRIGGER AS $$
    DECLARE
      v_decision intelligence_authority_decisions%ROWTYPE;
      v_proposal intelligence_authority_proposals%ROWTYPE;
      v_latest cell_intelligence_authority_events%ROWTYPE;
    BEGIN
      SELECT * INTO v_latest FROM cell_intelligence_authority_events
      WHERE cell_id = NEW.cell_id
      ORDER BY effective_at DESC, created_at DESC, event_id DESC LIMIT 1;
      IF FOUND AND NEW.effective_at < v_latest.effective_at THEN
        RAISE EXCEPTION 'Authority event rejected: event effective time is out of sequence';
      END IF;
      IF NEW.event_type = 'GRANTED' THEN
        IF NEW.decision_id IS NULL THEN RAISE EXCEPTION 'Authority grant rejected: decision_id is required'; END IF;
        SELECT * INTO v_decision FROM intelligence_authority_decisions WHERE decision_id = NEW.decision_id;
        IF NOT FOUND THEN RAISE EXCEPTION 'Authority grant rejected: decision does not resolve'; END IF;
        SELECT * INTO v_proposal FROM intelligence_authority_proposals WHERE proposal_id = v_decision.proposal_id;
        IF NOT FOUND THEN RAISE EXCEPTION 'Authority grant rejected: proposal does not resolve'; END IF;
        IF v_decision.decision <> 'APPROVED' THEN RAISE EXCEPTION 'Authority grant rejected: decision is not APPROVED'; END IF;
        IF v_proposal.cell_id <> NEW.cell_id THEN RAISE EXCEPTION 'Authority grant rejected: cell mismatch'; END IF;
        IF v_decision.approved_proposal_manifest_sha256 <> v_proposal.proposal_manifest_sha256 THEN RAISE EXCEPTION 'Authority grant rejected: proposal manifest hash mismatch'; END IF;
        IF v_proposal.criteria_passed IS NOT TRUE THEN RAISE EXCEPTION 'Authority grant rejected: promotion criteria failed'; END IF;
        IF NEW.authority_mode <> 'VETO_ONLY' OR NEW.policy_version <> v_proposal.policy_version THEN RAISE EXCEPTION 'Authority grant rejected: policy or mode mismatch'; END IF;
        IF NEW.operator_identity <> v_decision.operator_identity THEN RAISE EXCEPTION 'Authority grant rejected: operator mismatch'; END IF;
        IF NEW.effective_at < v_decision.decided_at THEN RAISE EXCEPTION 'Authority grant rejected: grant predates decision'; END IF;
        IF v_latest.event_id IS NOT NULL AND v_latest.authority_mode = 'VETO_ONLY' THEN RAISE EXCEPTION 'Authority grant rejected: VETO_ONLY is already active'; END IF;
      ELSE
        IF NEW.decision_id IS NOT NULL THEN RAISE EXCEPTION 'Authority revocation/expiry cannot claim an approval decision'; END IF;
        IF NEW.authority_mode <> 'OBSERVE_ONLY' THEN RAISE EXCEPTION 'Authority revocation/expiry must set OBSERVE_ONLY'; END IF;
        IF v_latest.event_id IS NULL OR v_latest.authority_mode <> 'VETO_ONLY' THEN RAISE EXCEPTION 'Authority revocation/expiry requires an active grant'; END IF;
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;

    CREATE TRIGGER trg_check_authority_proposal_validity BEFORE INSERT ON intelligence_authority_proposals FOR EACH ROW EXECUTE FUNCTION check_authority_proposal_validity();
    CREATE TRIGGER trg_check_authority_decision_validity BEFORE INSERT ON intelligence_authority_decisions FOR EACH ROW EXECUTE FUNCTION check_authority_decision_validity();
    CREATE TRIGGER trg_check_authority_event_validity BEFORE INSERT ON cell_intelligence_authority_events FOR EACH ROW EXECUTE FUNCTION check_authority_event_validity();
    CREATE TRIGGER trg_intelligence_authority_proposals_immutable BEFORE UPDATE OR DELETE ON intelligence_authority_proposals FOR EACH ROW EXECUTE FUNCTION reject_intelligence_fact_mutation();
    CREATE TRIGGER trg_intelligence_authority_decisions_immutable BEFORE UPDATE OR DELETE ON intelligence_authority_decisions FOR EACH ROW EXECUTE FUNCTION reject_intelligence_fact_mutation();
    CREATE TRIGGER trg_cell_intelligence_authority_events_immutable BEFORE UPDATE OR DELETE ON cell_intelligence_authority_events FOR EACH ROW EXECUTE FUNCTION reject_intelligence_fact_mutation();
    """)
    op.execute("""
    DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kairo_runtime') THEN
        REVOKE ALL ON intelligence_authority_proposals, intelligence_authority_decisions, cell_intelligence_authority_events FROM kairo_runtime;
        GRANT SELECT, INSERT ON intelligence_authority_proposals, intelligence_authority_decisions, cell_intelligence_authority_events TO kairo_runtime;
      END IF;
    END $$;
    """)


def downgrade() -> None:
    op.execute("""
    DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM cell_intelligence_authority_events)
         OR EXISTS (SELECT 1 FROM intelligence_authority_decisions)
         OR EXISTS (SELECT 1 FROM intelligence_authority_proposals) THEN
        RAISE EXCEPTION 'Refusing 0022 downgrade: immutable authority governance evidence exists';
      END IF;
    END $$;
    """)
    op.execute("DROP TRIGGER IF EXISTS trg_cell_intelligence_authority_events_immutable ON cell_intelligence_authority_events")
    op.execute("DROP TRIGGER IF EXISTS trg_intelligence_authority_decisions_immutable ON intelligence_authority_decisions")
    op.execute("DROP TRIGGER IF EXISTS trg_intelligence_authority_proposals_immutable ON intelligence_authority_proposals")
    op.execute("DROP TRIGGER IF EXISTS trg_check_authority_event_validity ON cell_intelligence_authority_events")
    op.execute("DROP TRIGGER IF EXISTS trg_check_authority_decision_validity ON intelligence_authority_decisions")
    op.execute("DROP TRIGGER IF EXISTS trg_check_authority_proposal_validity ON intelligence_authority_proposals")
    op.execute("DROP FUNCTION IF EXISTS check_authority_event_validity()")
    op.execute("DROP FUNCTION IF EXISTS check_authority_decision_validity()")
    op.execute("DROP FUNCTION IF EXISTS check_authority_proposal_validity()")
    op.drop_table("cell_intelligence_authority_events")
    op.drop_table("intelligence_authority_decisions")
    op.drop_table("intelligence_authority_proposals")
