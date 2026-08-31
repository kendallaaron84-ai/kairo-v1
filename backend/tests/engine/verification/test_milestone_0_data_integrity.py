from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.ledger import Fill, KairoOrder, MarketSnapshot
from app.db.models.projections import CurrentPosition
from engine.execution.replay_orchestrator import LegacyReplayInput, ReplayOrchestrator
from engine.risk.models import DecisionVerdict, DisqualificationReason
from engine.strategy.market_data import SampledPriceObservation


pytestmark = pytest.mark.integration


class VerificationTelemetryFault(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_timestamp: datetime
    received_at: datetime
    fault_type: str

    @property
    def quote_age(self) -> timedelta:
        return self.received_at - self.source_timestamp


def test_m0_stale_quote_blocks_entry_allows_emergency_exit(
    db_session: Session, verification_replay
) -> None:
    governor = verification_replay.initialize_governor()
    now = verification_replay.session_open() + timedelta(hours=1)
    fault = VerificationTelemetryFault(
        source_timestamp=now - timedelta(seconds=2),
        received_at=now,
        fault_type="STALE_DELIVERY",
    )
    entry = governor.evaluate(
        verification_replay.risk_request(
            age_seconds=Decimal(str(fault.quote_age.total_seconds()))
        )
    )
    assert entry.verdict is DecisionVerdict.REJECTED
    assert entry.reason is DisqualificationReason.MARKET_DATA_STALE

    position = CurrentPosition(
        position_id=verification_replay.call.instrument_id,
        cell_id=verification_replay.cell.cell_id,
        broker_account_id=verification_replay.broker.broker_account_id,
        instrument_id=verification_replay.call.instrument_id,
        quantity=Decimal("1"),
        average_price=Decimal("0.50"),
        updated_at=now,
    )
    db_session.add(position)
    db_session.flush()
    emergency_exit = governor.evaluate(
        verification_replay.risk_request(
            purpose="EMERGENCY_EXIT",
            side="SELL",
            age_seconds=Decimal("20"),
            position=position,
        )
    )
    assert emergency_exit.verdict is DecisionVerdict.AUTHORIZED


def test_m0_inverted_quote_persists_evidence_without_economic_mutation(
    db_session: Session, verification_replay
) -> None:
    observation = verification_replay.observations()[:1]
    timestamp = observation[0].timestamp
    inverted = verification_replay.chain(
        timestamp,
        candidates=(
            verification_replay.candidate(
                verification_replay.call, bid="0.50", ask="0.49"
            ),
        ),
    )
    stream = LegacyReplayInput(
        provider=verification_replay.provider(),
        observations=observation,
        option_chains=(inverted,),
    )
    orchestrator = ReplayOrchestrator(
        db_session, verification_replay.config(session_id="M0-INVERTED")
    )
    with pytest.raises(ValueError, match="bid cannot exceed ask"):
        orchestrator.replay_legacy((stream,))

    audit = db_session.scalar(
        select(MarketSnapshot).where(
            MarketSnapshot.instrument_id == verification_replay.call.instrument_id
        )
    )
    assert audit is not None
    assert (audit.bid, audit.ask) == (Decimal("0.50"), Decimal("0.49"))
    assert db_session.scalar(select(func.count()).select_from(KairoOrder)) == 0
    assert db_session.scalar(select(func.count()).select_from(Fill)) == 0
    assert db_session.scalar(select(func.count()).select_from(CurrentPosition)) == 0
    state = orchestrator.governor.current_state()
    assert state.session_net_pnl == Decimal("0")


def test_m0_missing_sample_gap_does_not_interpolate_legacy_close(
    db_session: Session, verification_replay
) -> None:
    start = verification_replay.session_open()
    observations = tuple(
        SampledPriceObservation(
            timestamp=start + timedelta(seconds=offset),
            price=Decimal(price),
            instrument_id=verification_replay.underlying.instrument_id,
            symbol="TQQQ",
        )
        for offset, price in ((0, "10"), (15, "10.25"), (65, "10.50"))
    )
    result = verification_replay.provider().replay(observations)
    assert result.observations == observations
    assert len(result.completed_minutes) == 1
    assert result.completed_minutes[0].close == Decimal("10.25")
    assert result.completed_minutes[0].completed_at == observations[-1].timestamp
    assert not hasattr(result.completed_minutes[0], "open")


def test_m0_backward_timestamp_fails_closed(
    db_session: Session, verification_replay
) -> None:
    observations = verification_replay.observations()[:2]
    with pytest.raises(ValueError, match="strictly chronological"):
        ReplayOrchestrator(
            db_session,
            verification_replay.config(session_id="M0-BACKWARD"),
        ).replay_legacy(
            (
                LegacyReplayInput(
                    provider=verification_replay.provider(),
                    observations=tuple(reversed(observations)),
                ),
            )
        )
    assert db_session.scalar(select(func.count()).select_from(Fill)) == 0


def test_m0_replay_manifest_hash_is_identical_across_fresh_databases(
    db_session: Session, verification_replay
) -> None:
    _, first = verification_replay.run_entry()
    verification_replay.clean_replay_facts()
    _, second = verification_replay.run_entry()
    assert first.financial_ids == second.financial_ids
    assert first.manifest_hash == second.manifest_hash
