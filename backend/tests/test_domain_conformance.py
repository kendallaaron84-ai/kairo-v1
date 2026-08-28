from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.domain.execution import OrderIntentFact
from app.domain.instruments import CanonicalInstrument
from app.domain.trust import TrustEvaluationFact


def test_option_domain_requires_complete_identity() -> None:
    with pytest.raises(ValidationError, match="complete canonical option identity"):
        CanonicalInstrument(symbol="TQQQ option", asset_class="OPTION")

    option = CanonicalInstrument(
        symbol="TQQQ 2026-08-28 75 CALL",
        asset_class="OPTION",
        underlying_symbol="TQQQ",
        contract_symbol="TQQQ260828C00075000",
        expiration_date=date(2026, 8, 28),
        strike_price=Decimal("75"),
        option_right="CALL",
        contract_multiplier=Decimal("100"),
        listing_type="STANDARD",
    )
    assert option.contract_symbol == "TQQQ260828C00075000"


def test_intent_domain_enforces_sizing_and_price_semantics() -> None:
    intent = OrderIntentFact(
        cell_id=uuid4(),
        strategy_id="STRATEGY-001",
        strategy_version="1.0.0",
        instrument_id=uuid4(),
        client_order_key="intent-1",
        order_purpose="TREASURY_PURCHASE",
        side="BUY",
        target_notional_usd=Decimal("1.33"),
        order_type="MARKET",
    )
    assert intent.target_quantity is None

    with pytest.raises(ValidationError, match="exactly one intent sizing mode"):
        OrderIntentFact(
            cell_id=uuid4(),
            strategy_id="STRATEGY-001",
            strategy_version="1.0.0",
            instrument_id=uuid4(),
            client_order_key="intent-2",
            order_purpose="ENTRY",
            side="BUY",
            order_type="MARKET",
        )


def test_zero_evidence_trust_domain_has_no_score_or_promotion() -> None:
    evaluation = TrustEvaluationFact(
        cell_id=uuid4(),
        policy_id=uuid4(),
        policy_version="1.0.0",
        score=None,
        outcome="PASS",
        eligible_for_promotion=False,
        evidence_trade_count=0,
    )
    assert evaluation.score is None

    with pytest.raises(ValidationError, match="cannot score or promote"):
        TrustEvaluationFact(
            cell_id=uuid4(),
            policy_id=uuid4(),
            policy_version="1.0.0",
            score=Decimal("50"),
            outcome="PASS",
            eligible_for_promotion=False,
            evidence_trade_count=0,
        )
