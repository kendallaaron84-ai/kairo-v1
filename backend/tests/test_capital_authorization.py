from decimal import Decimal
from uuid import uuid4

import pytest

from app.domain.capital import KairoCapitalAuthorization


@pytest.mark.parametrize(
    ("settled", "safety", "ownership", "replication", "committed", "expected"),
    [
        ("100", "0", "0", "0", "0", "100"),
        ("100", "8.40", "7.93", "4.20", "0", "79.47"),
        ("100", "30", "20", "25", "25", "0"),
        ("100", "8", "4", "3", "110", "0"),
        ("0", "0", "0", "0", "0", "0"),
        ("100.0000000001", "0.1000000001", "0.2", "0.3", "0.4", "99.0000000000"),
    ],
)
def test_compute_authorized_trading_cash(
    settled: str, safety: str, ownership: str, replication: str,
    committed: str, expected: str,
) -> None:
    authorization = KairoCapitalAuthorization.compute(
        cell_id=uuid4(),
        settled_cash=Decimal(settled),
        safety_reserve=Decimal(safety),
        ownership_treasury_reserved=Decimal(ownership),
        replication_reserve=Decimal(replication),
        committed_obligations=Decimal(committed),
    )
    assert authorization.authorized_trading_cash == Decimal(expected)
    assert authorization.authorized_trading_cash >= Decimal("0")


def test_compute_rejects_negative_inputs() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        KairoCapitalAuthorization.compute(
            cell_id=uuid4(), settled_cash=Decimal("100"), safety_reserve=Decimal("-1")
        )
