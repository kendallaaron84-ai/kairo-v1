import hashlib
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.db.models.configuration import Instrument
from app.domain.enums import OptionRight
from engine.data.corpus_qualifier import CorpusQualificationEngine, CorpusQualificationInput
from engine.data.option_enrollment import (
    CanonicalResolutionAccounting,
    HistoricalOptionEnrollmentGate,
    OptionEnrollmentFailure,
    OptionEnrollmentReasonCode,
    RejectedOptionContract,
    canonical_occ_symbol,
    deterministic_option_instrument_id,
)


NOW = datetime(2024, 1, 2, 15, 31, tzinfo=timezone.utc)
EXPIRATION = date(2024, 1, 5)
STRIKE = Decimal("50")


@pytest.fixture
def underlying(db_session: Session) -> Instrument:
    row = Instrument(
        instrument_id=uuid4(),
        symbol="TQQQ",
        asset_class="ETF",
        currency="USD",
        effective_from=NOW,
    )
    db_session.add(row)
    db_session.flush()
    return row


def discovery(**updates):
    values = {
        "symbol": "TQQQ",
        "expiration": EXPIRATION,
        "strike": STRIKE,
        "right": "CALL",
        "timestamp": NOW,
    }
    values.update(updates)
    return values


def test_uuid5_instrument_id_is_deterministic_and_independent_of_occ_string_formatting(
    db_session: Session, underlying: Instrument
):
    first = deterministic_option_instrument_id("TQQQ", EXPIRATION, STRIKE, OptionRight.CALL)
    display = canonical_occ_symbol("TQQQ", EXPIRATION, STRIKE, OptionRight.CALL)
    second = deterministic_option_instrument_id("TQQQ", EXPIRATION, STRIKE, "CALL")
    provider_row = discovery()
    del provider_row["symbol"]
    del provider_row["expiration"]
    section = SimpleNamespace(
        endpoint="option_history_quote",
        parameters={"symbol": "TQQQ", "expiration": EXPIRATION},
        records=(provider_row,),
    )
    outcome = HistoricalOptionEnrollmentGate(db_session).enroll_theta_sections(
        (section,),
        underlying_instrument_id=underlying.instrument_id,
        underlying_symbol="TQQQ",
        research_replay_mode=True,
    )

    assert first == second
    assert str(first) not in display
    assert outcome.accounting.newly_enrolled_contracts_count == 1
    assert db_session.get(Instrument, first).symbol == display


def test_option_enrollment_rejects_non_positive_strikes(
    db_session: Session, underlying: Instrument
):
    outcome = HistoricalOptionEnrollmentGate(db_session).enroll(
        (discovery(strike=Decimal("0")),),
        underlying_instrument_id=underlying.instrument_id,
        underlying_symbol="TQQQ",
        research_replay_mode=True,
    )

    assert outcome.accounting.discovered_contracts_count == 1
    assert outcome.accounting.rejected_contracts_count == 1
    assert outcome.accounting.rejected_contracts[0].reason_code is (
        OptionEnrollmentReasonCode.NON_POSITIVE_STRIKE
    )


def test_option_enrollment_fails_closed_when_underlying_instrument_missing(
    db_session: Session,
):
    with pytest.raises(OptionEnrollmentFailure) as failure:
        HistoricalOptionEnrollmentGate(db_session).enroll(
            (discovery(),),
            underlying_instrument_id=uuid4(),
            underlying_symbol="TQQQ",
            research_replay_mode=True,
        )
    assert failure.value.reason_code is OptionEnrollmentReasonCode.UNDERLYING_NOT_ENROLLED


def test_option_enrollment_strictly_prohibited_outside_research_replay_mode(
    db_session: Session, underlying: Instrument
):
    with pytest.raises(OptionEnrollmentFailure) as failure:
        HistoricalOptionEnrollmentGate(db_session).enroll(
            (discovery(),),
            underlying_instrument_id=underlying.instrument_id,
            underlying_symbol="TQQQ",
            research_replay_mode=False,
        )
    assert failure.value.reason_code is (
        OptionEnrollmentReasonCode.RESEARCH_REPLAY_MODE_REQUIRED
    )


def test_existing_canonical_contract_validates_field_parity_and_counts_as_resolved_existing(
    db_session: Session, underlying: Instrument
):
    instrument_id = deterministic_option_instrument_id(
        "TQQQ", EXPIRATION, STRIKE, OptionRight.CALL
    )
    symbol = canonical_occ_symbol("TQQQ", EXPIRATION, STRIKE, OptionRight.CALL)
    db_session.add(Instrument(
        instrument_id=instrument_id, symbol=symbol, asset_class="OPTION", currency="USD",
        underlying_symbol="TQQQ", contract_symbol=symbol, expiration_date=EXPIRATION,
        strike_price=STRIKE, option_right="CALL", contract_multiplier=Decimal("100"),
        listing_type="STANDARD", effective_from=NOW,
    ))
    db_session.flush()

    outcome = HistoricalOptionEnrollmentGate(db_session).enroll(
        (discovery(), discovery(timestamp=NOW)),
        underlying_instrument_id=underlying.instrument_id,
        underlying_symbol="TQQQ",
        research_replay_mode=True,
    )

    assert outcome.accounting.discovered_contracts_count == 1
    assert outcome.accounting.resolved_existing_contracts_count == 1
    assert outcome.accounting.newly_enrolled_contracts_count == 0


def test_existing_canonical_contract_fails_closed_on_attribute_conflict(
    db_session: Session, underlying: Instrument
):
    instrument_id = deterministic_option_instrument_id(
        "TQQQ", EXPIRATION, STRIKE, OptionRight.CALL
    )
    db_session.add(Instrument(
        instrument_id=instrument_id, symbol="CONFLICT", asset_class="OPTION", currency="USD",
        underlying_symbol="TQQQ", contract_symbol="CONFLICT", expiration_date=EXPIRATION,
        strike_price=STRIKE, option_right="CALL", contract_multiplier=Decimal("100"),
        listing_type="STANDARD", effective_from=NOW,
    ))
    db_session.flush()

    with pytest.raises(OptionEnrollmentFailure) as failure:
        HistoricalOptionEnrollmentGate(db_session).enroll(
            (discovery(),),
            underlying_instrument_id=underlying.instrument_id,
            underlying_symbol="TQQQ",
            research_replay_mode=True,
        )
    assert failure.value.reason_code is (
        OptionEnrollmentReasonCode.CANONICAL_ATTRIBUTE_CONFLICT
    )


def test_qualification_engine_records_rejected_contracts_and_computes_exact_resolution_pct(
    db_session: Session, underlying: Instrument
):
    rejected = RejectedOptionContract(
        discovery_key="TQQQ|2024-01-05|0|CALL|0",
        reason_code=OptionEnrollmentReasonCode.NON_POSITIVE_STRIKE,
        diagnostic="provider strike must be strictly positive",
    )
    accounting = CanonicalResolutionAccounting(
        discovered_contracts_count=2,
        resolved_existing_contracts_count=1,
        newly_enrolled_contracts_count=0,
        resolved_contracts_count=1,
        rejected_contracts_count=1,
        rejected_contracts=(rejected,),
    )
    evidence = CorpusQualificationInput(
        provider_code="THETA_DATA", start_session=date(2024, 1, 2),
        end_session=date(2024, 1, 2), symbols=(underlying.symbol,), bars=(),
        option_snapshots=(), decision_points=(),
        raw_artifact_sha256s=(hashlib.sha256(b"raw").hexdigest(),),
        normalized_dataset_manifest_sha256=hashlib.sha256(b"normalized").hexdigest(),
        resolution_accounting=accounting,
    )

    manifest = CorpusQualificationEngine(db_session).qualify(evidence)

    assert manifest.metrics.canonical_contract_resolution_pct == Decimal("50.00")
    assert manifest.metrics.resolution_status == "FAIL"
    assert manifest.metrics.resolution_accounting == accounting
    with pytest.raises(ValidationError, match="discovered population mismatch"):
        CanonicalResolutionAccounting(
            discovered_contracts_count=3,
            resolved_existing_contracts_count=1,
            newly_enrolled_contracts_count=0,
            resolved_contracts_count=1,
            rejected_contracts_count=1,
            rejected_contracts=(rejected,),
        )
