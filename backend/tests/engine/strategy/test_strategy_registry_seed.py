from copy import deepcopy

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.configuration import StrategyRegistry
from engine.strategy.registry_seed import (
    SOURCE_SHA256,
    STRATEGY_ID,
    STRATEGY_VERSION,
    StrategySeedConflictError,
    seed_ema_cross_v100,
)


pytestmark = pytest.mark.integration


def test_registry_seed_contains_all_35_provenance_values(db_session: Session) -> None:
    strategy = db_session.get(StrategyRegistry, (STRATEGY_ID, STRATEGY_VERSION))
    assert strategy is not None
    configuration = strategy.configuration
    classified_values = {"clearance", *configuration["parameters"]}
    assert len(classified_values) == 35
    assert set(configuration["parameter_provenance"]) == classified_values
    assert set(configuration["parameter_provenance"].values()) == {
        "INHERITED_PROTOTYPE",
        "RESEARCH_VARIANT",
    }
    assert configuration["source_sha256"] == SOURCE_SHA256
    assert configuration["strategy_version"] == STRATEGY_VERSION
    assert configuration["replay_fidelity"]["legacy"]["ohlcv_fabricated"] is False
    assert (
        configuration["replay_fidelity"]["research"]["exact_prototype_replay"]
        is False
    )


def test_registry_seed_is_idempotent(db_session: Session) -> None:
    first = seed_ema_cross_v100(db_session)
    second = seed_ema_cross_v100(db_session)
    count = db_session.scalar(
        select(func.count())
        .select_from(StrategyRegistry)
        .where(
            StrategyRegistry.strategy_id == STRATEGY_ID,
            StrategyRegistry.version_tag == STRATEGY_VERSION,
        )
    )
    assert first is second
    assert count == 1


def test_registry_seed_rejects_conflicting_v100(db_session: Session) -> None:
    strategy = db_session.get(StrategyRegistry, (STRATEGY_ID, STRATEGY_VERSION))
    assert strategy is not None
    conflicting = deepcopy(strategy.configuration)
    conflicting["parameters"]["ema_period"] = 8
    strategy.configuration = conflicting
    db_session.flush()

    with pytest.raises(StrategySeedConflictError, match="conflicting immutable content"):
        seed_ema_cross_v100(db_session)
