from copy import deepcopy

from sqlalchemy.orm import Session

from app.db.models.configuration import StrategyRegistry


STRATEGY_ID = "EMA-CROSS-001"
STRATEGY_VERSION = "1.0.0"
DISPLAY_NAME = "TQQQ/SQQQ Price-to-EMA-9 Prototype"
SOURCE_SHA256 = "75993b9fd059b145fcfb2ac313e6ffe277f78591ab75b35884f14072398f9c6d"

_PARAMETERS = {
    "symbols": ["TQQQ", "SQQQ"],
    "signal_model": "PRICE_EMA_CROSS",
    "ema_period": 9,
    "ema_seed": "SMA_FIRST_N_CLOSES",
    "ema_smoothing_alpha": "2/(N+1)",
    "minimum_completed_closes": 10,
    "bar_interval_seconds": 60,
    "quote_poll_interval_seconds": 15,
    "quote_include_extended_hours": True,
    "bullish_option_right": "CALL",
    "bearish_option_right": "PUT",
    "expiration_policy": "TODAY_ELSE_NEAREST_UPCOMING",
    "premium_cap_per_share_usd": "0.50",
    "max_bid_ask_spread_per_share_usd": "0.03",
    "minimum_volume": 10,
    "minimum_open_interest": 50,
    "liquidity_threshold_logic": "VOLUME_OR_OPEN_INTEREST",
    "strike_selection": "NEAREST_SPOT_AFTER_FILTERS",
    "daily_budget_fraction_of_settled_cash": "0.50",
    "budget_slot_count": 3,
    "maximum_positions_per_underlying": 1,
    "entry_limit_reference": "ASK",
    "exit_limit_reference": "BID",
    "time_in_force": "GFD",
    "take_profit_fraction": "0.10",
    "stop_loss_fraction": "0.05",
    "trend_reversal_exit": True,
    "maximum_consecutive_losses": 2,
    "loss_streak_action": "HALT_NEW_ENTRIES_FOR_SESSION",
    "market_timezone": "America/New_York",
    "market_open_time": "09:30:00",
    "forced_flatten_time": "15:45:00",
    "hard_stop_time": "16:00:00",
    "balance_milestone_usd": "2000.00",
}

_PARAMETER_PROVENANCE = {
    "clearance": "RESEARCH_VARIANT",
    **{name: "INHERITED_PROTOTYPE" for name in _PARAMETERS},
}

_REPLAY_FIDELITY = {
    "legacy": {
        "mode": "LEGACY_REPLAY_MODE",
        "input": "ORDERED_TIMESTAMPED_SAMPLED_PRICES",
        "output": "CLOSE_ONLY_COMPLETED_MINUTES",
        "exact_claim_requires": "EXACT_OBSERVED_SAMPLES",
        "allowed_provenance": [
            "EXACT_OBSERVED_SAMPLES",
            "RECONSTRUCTED_SAMPLES",
        ],
        "reconstructed_exact_prototype_replay": False,
        "ohlcv_fabricated": False,
    },
    "research": {
        "mode": "RESEARCH_REPLAY_MODE",
        "input": "VENDOR_NEUTRAL_TICKS_QUOTES_TRADES_OR_BARS",
        "exact_prototype_replay": False,
    },
}


class StrategySeedConflictError(RuntimeError):
    pass


def ema_cross_v100_configuration() -> dict:
    return deepcopy(
        {
            "source_sha256": SOURCE_SHA256,
            "strategy_version": STRATEGY_VERSION,
            "clearance": "PAPER_ONLY",
            "parameters": _PARAMETERS,
            "parameter_provenance": _PARAMETER_PROVENANCE,
            "replay_fidelity": _REPLAY_FIDELITY,
        }
    )


def seed_ema_cross_v100(session: Session) -> StrategyRegistry:
    expected = ema_cross_v100_configuration()
    existing = session.get(StrategyRegistry, (STRATEGY_ID, STRATEGY_VERSION))
    if existing is not None:
        if existing.display_name != DISPLAY_NAME or existing.configuration != expected:
            raise StrategySeedConflictError(
                "EMA-CROSS-001 v1.0.0 exists with conflicting immutable content"
            )
        return existing

    strategy = StrategyRegistry(
        strategy_id=STRATEGY_ID,
        version_tag=STRATEGY_VERSION,
        display_name=DISPLAY_NAME,
        status="ACTIVE",
        configuration=expected,
    )
    session.add(strategy)
    session.flush()
    return strategy
