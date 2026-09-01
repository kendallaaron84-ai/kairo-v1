import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP, localcontext
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.historical import HistoricalMarketDataset
from app.db.models.projections import CapitalCell
from app.db.models.scorecards import (
    HistoricalRunAnalogVector,
    HistoricalSessionDistributionFact,
    HistoricalValidationPerformanceBand,
    HistoricalValidationRegimeSlice,
    HistoricalValidationRun,
)
from engine.validation.regime_policy import RegimeObservation, RegimePolicyV1
from engine.validation.vector_normalizer import VectorNormalizer


CENT = Decimal("0.01")
FOUR = Decimal("0.0001")
MINIMUM_BENCHMARK = 10


def _decimal_json(value: Decimal) -> str:
    return format(value, "f")


def _canonical(value: Any) -> Any:
    if isinstance(value, Decimal):
        return _decimal_json(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _canonical(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


class SessionPerformanceFact(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_date: date
    session_pnl_usd: Decimal
    max_drawdown_usd: Decimal = Field(ge=0)
    trade_count: int = Field(ge=0)
    winning_trades_count: int = Field(ge=0)
    losing_trades_count: int = Field(ge=0)
    breakeven_trades_count: int = Field(default=0, ge=0)
    hard_halt: bool = False
    siphoned_safety_usd: Decimal = Field(default=Decimal("0.00"), ge=0)
    siphoned_treasury_usd: Decimal = Field(default=Decimal("0.00"), ge=0)
    siphoned_replication_usd: Decimal = Field(default=Decimal("0.00"), ge=0)
    cells_spawned_count: int = Field(default=0, ge=0)
    market_return_pct: Decimal
    vix_level: Decimal | None = None
    rate_change_bps: Decimal | None = None
    event_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_counts_and_cents(self) -> "SessionPerformanceFact":
        classified = self.winning_trades_count + self.losing_trades_count + self.breakeven_trades_count
        if classified != self.trade_count:
            raise ValueError("trade classifications must reconcile exactly to trade_count")
        for name in ("session_pnl_usd", "max_drawdown_usd", "siphoned_safety_usd", "siphoned_treasury_usd", "siphoned_replication_usd"):
            if getattr(self, name).quantize(CENT) != getattr(self, name):
                raise ValueError(f"{name} must have exact cent precision")
        return self

    def raw_vector(self) -> dict[str, Decimal]:
        win_rate = Decimal("0") if self.trade_count == 0 else Decimal(self.winning_trades_count) * Decimal("100") / Decimal(self.trade_count)
        return {
            "event_count": Decimal(self.event_count),
            "market_return_pct": self.market_return_pct,
            "rate_change_bps": self.rate_change_bps or Decimal("0"),
            "trade_count": Decimal(self.trade_count),
            "vix_level": self.vix_level or Decimal("0"),
            "win_rate_pct": win_rate,
        }


class ScorecardEvidenceManifest(BaseModel):
    model_config = ConfigDict(frozen=True)

    manifest_version: str = "SCORECARD-MANIFEST-v1"
    payload: dict[str, Any]
    scorecard_manifest_sha256: str

    @classmethod
    def build(cls, payload: dict[str, Any]) -> "ScorecardEvidenceManifest":
        canonical = json.dumps(_canonical(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return cls(payload=_canonical(payload), scorecard_manifest_sha256=hashlib.sha256(canonical).hexdigest())


class ScorecardResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    validation_run_id: UUID
    manifest: ScorecardEvidenceManifest


class ValidationScorecardEngine:
    def __init__(self, session: Session, *, regime_policy: RegimePolicyV1 | None = None, normalizer: VectorNormalizer | None = None) -> None:
        self.session = session
        self.regime_policy = regime_policy or RegimePolicyV1()
        self.normalizer = normalizer or VectorNormalizer()

    def evaluate(
        self,
        *,
        cell_id: UUID,
        dataset_id: UUID,
        validation_scope: str,
        session_facts: tuple[SessionPerformanceFact, ...],
        multi_year_manifest_sha256: str,
        executed_at: datetime,
    ) -> ScorecardResult:
        if self.session.get(CapitalCell, cell_id) is None or self.session.get(HistoricalMarketDataset, dataset_id) is None:
            raise ValueError("cell and historical dataset identities must resolve canonically")
        if len(multi_year_manifest_sha256) != 64 or any(char not in "0123456789abcdef" for char in multi_year_manifest_sha256):
            raise ValueError("multi-year manifest must be lowercase SHA-256")
        if not session_facts:
            raise ValueError("validation scorecard requires at least one canonical session fact")
        ordered = tuple(sorted(session_facts, key=lambda item: item.session_date))
        if ordered != session_facts or len({item.session_date for item in ordered}) != len(ordered):
            raise ValueError("session facts must be unique and supplied in chronological order")
        if executed_at.tzinfo is None:
            raise ValueError("executed_at must be timezone-aware")

        run_id = uuid5(NAMESPACE_URL, f"kairo:scorecard:{cell_id}:{dataset_id}:{validation_scope}:{multi_year_manifest_sha256}")
        labels = {fact.session_date: self.regime_policy.classify(RegimeObservation(**fact.model_dump(include={"session_date", "market_return_pct", "vix_level", "rate_change_bps", "event_count"}))) for fact in ordered}
        normalized, parameters = self.normalizer.fit_transform([fact.raw_vector() for fact in ordered])
        distributions = self._distributions(run_id, ordered)
        bands = self._bands(run_id, ordered, labels)
        slices = self._slices(run_id, ordered, labels)
        vectors = self._vectors(run_id, ordered, normalized, parameters)
        summary = self._summary(ordered)
        payload = {
            "validation_run_id": run_id,
            "cell_id": cell_id,
            "dataset_id": dataset_id,
            "validation_scope": validation_scope,
            "regime_policy_version": self.regime_policy.version,
            "normalization_policy_version": self.normalizer.version,
            "multi_year_manifest_sha256": multi_year_manifest_sha256,
            "executed_at": executed_at.astimezone(timezone.utc),
            "summary": summary,
            "regime_slices": [self._row_payload(row, "_sa_instance_state") for row in slices],
            "distribution_facts": [self._row_payload(row, "_sa_instance_state") for row in distributions],
            "performance_bands": [self._row_payload(row, "_sa_instance_state") for row in bands],
            "analog_vectors": [self._row_payload(row, "_sa_instance_state") for row in vectors],
        }
        manifest = ScorecardEvidenceManifest.build(payload)
        existing = self.session.get(HistoricalValidationRun, run_id)
        if existing is not None:
            if existing.scorecard_manifest_sha256 != manifest.scorecard_manifest_sha256:
                raise ValueError("conflicting immutable scorecard run identity")
            return ScorecardResult(validation_run_id=run_id, manifest=manifest)
        run = HistoricalValidationRun(
            validation_run_id=run_id, cell_id=cell_id, dataset_id=dataset_id,
            validation_scope=validation_scope, regime_policy_version=self.regime_policy.version,
            normalization_policy_version=self.normalizer.version,
            sample_start_time=datetime.combine(ordered[0].session_date, datetime.min.time(), tzinfo=timezone.utc),
            sample_end_time=datetime.combine(ordered[-1].session_date, datetime.max.time(), tzinfo=timezone.utc),
            multi_year_manifest_sha256=multi_year_manifest_sha256,
            scorecard_manifest_sha256=manifest.scorecard_manifest_sha256,
            executed_at=executed_at, **summary,
        )
        self.session.add_all([run, *slices, *distributions, *bands, *vectors])
        self.session.flush()
        return ScorecardResult(validation_run_id=run_id, manifest=manifest)

    @staticmethod
    def _row_payload(row: Any, *excluded: str) -> dict[str, Any]:
        excluded_names = set(excluded) | {name for name in vars(row) if name.endswith("_id") and name != "validation_run_id"}
        return {name: value for name, value in vars(row).items() if name not in excluded_names}

    @staticmethod
    def distribution(values: tuple[Decimal, ...]) -> dict[str, Any]:
        if len(values) < MINIMUM_BENCHMARK:
            return {"sample_count": len(values), "distribution_status": "INSUFFICIENT_EVIDENCE", **{name: None for name in ("p10_value", "p25_value", "p50_value", "p75_value", "p90_value", "p99_value", "mean_value", "std_dev_value")}}
        ordered = sorted(values)
        with localcontext() as context:
            context.prec = 34
            def percentile(percent: int) -> Decimal:
                position = Decimal(percent) / Decimal("100") * Decimal(len(ordered) - 1)
                lower = int(position)
                upper = min(lower + 1, len(ordered) - 1)
                fraction = position - Decimal(lower)
                return (ordered[lower] + (ordered[upper] - ordered[lower]) * fraction).quantize(FOUR)
            mean = sum(ordered, Decimal("0")) / Decimal(len(ordered))
            std = (sum(((value - mean) ** 2 for value in ordered), Decimal("0")) / Decimal(len(ordered))).sqrt()
        return {"sample_count": len(values), "distribution_status": "SUFFICIENT", **{f"p{percent}_value": percentile(percent) for percent in (10, 25, 50, 75, 90, 99)}, "mean_value": mean.quantize(FOUR), "std_dev_value": std.quantize(FOUR)}

    @staticmethod
    def percentile_rank(value: Decimal, benchmark: tuple[Decimal, ...]) -> Decimal | None:
        if len(benchmark) < MINIMUM_BENCHMARK:
            return None
        count = sum(1 for item in benchmark if item <= value)
        return (Decimal(count) * Decimal("100") / Decimal(len(benchmark))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @staticmethod
    def performance_band(percentile: Decimal | None) -> str | None:
        if percentile is None: return None
        if percentile >= 90: return "EXCEPTIONAL"
        if percentile >= 75: return "STRONG"
        if percentile >= 40: return "NOMINAL"
        if percentile >= 10: return "COMPROMISED"
        return "CRITICAL"

    def _distributions(self, run_id: UUID, facts: tuple[SessionPerformanceFact, ...]) -> list[HistoricalSessionDistributionFact]:
        all_values = tuple(fact.session_pnl_usd for fact in facts)
        rows = [self._distribution_row(run_id, "RETROSPECTIVE", None, all_values)]
        for index, fact in enumerate(facts):
            rows.append(self._distribution_row(run_id, "AS_OF", fact.session_date, tuple(item.session_pnl_usd for item in facts[:index])))
        return rows

    def _distribution_row(self, run_id: UUID, perspective: str, benchmark_date: date | None, values: tuple[Decimal, ...]) -> HistoricalSessionDistributionFact:
        stable = benchmark_date.isoformat() if benchmark_date else "FULL"
        return HistoricalSessionDistributionFact(distribution_id=uuid5(run_id, f"distribution:{perspective}:{stable}:SESSION_PNL_USD"), validation_run_id=run_id, percentile_perspective=perspective, benchmark_as_of_date=benchmark_date, regime_code=None, metric_name="SESSION_PNL_USD", **self.distribution(values))

    def _bands(self, run_id: UUID, facts: tuple[SessionPerformanceFact, ...], labels: dict[date, tuple[str, ...]]) -> list[HistoricalValidationPerformanceBand]:
        full = tuple(fact.session_pnl_usd for fact in facts)
        rows = []
        for index, fact in enumerate(facts):
            retrospective = self.percentile_rank(fact.session_pnl_usd, full)
            as_of = self.percentile_rank(fact.session_pnl_usd, tuple(item.session_pnl_usd for item in facts[:index]))
            rows.append(HistoricalValidationPerformanceBand(
                band_entry_id=uuid5(run_id, f"band:{fact.session_date.isoformat()}"), validation_run_id=run_id,
                session_date=fact.session_date, session_pnl_usd=fact.session_pnl_usd,
                retrospective_percentile=retrospective, retrospective_band=self.performance_band(retrospective),
                as_of_percentile=as_of, as_of_band=self.performance_band(as_of),
                as_of_evidence_status="SUFFICIENT" if as_of is not None else "INSUFFICIENT_EVIDENCE",
                regime_labels=list(labels[fact.session_date]),
            ))
        return rows

    def _slices(self, run_id: UUID, facts: tuple[SessionPerformanceFact, ...], labels: dict[date, tuple[str, ...]]) -> list[HistoricalValidationRegimeSlice]:
        rows = []
        for label in self.regime_policy.label_order:
            selected = tuple(fact for fact in facts if label in labels[fact.session_date])
            if not selected: continue
            summary = self._summary(selected)
            rows.append(HistoricalValidationRegimeSlice(
                slice_id=uuid5(run_id, f"regime:{label}"), validation_run_id=run_id, regime_code=label,
                sessions_count=len(selected), trades_count=summary["total_trades_count"], winning_trades_count=summary["winning_trades_count"], losing_trades_count=summary["losing_trades_count"], net_pnl_usd=summary["net_realized_pnl_usd"], win_rate_pct=summary["win_rate_pct"], profit_factor=summary["profit_factor"], max_drawdown_usd=summary["max_drawdown_usd"], expectancy_per_trade_usd=summary["expectancy_per_trade_usd"],
            ))
        return rows

    def _vectors(self, run_id: UUID, facts: tuple[SessionPerformanceFact, ...], normalized: list[dict[str, Decimal]], parameters: dict[str, Any]) -> list[HistoricalRunAnalogVector]:
        params = _canonical(parameters)
        rows = []
        for fact, vector in zip(facts, normalized, strict=True):
            win_rate = Decimal("0") if fact.trade_count == 0 else Decimal(fact.winning_trades_count) * Decimal("100") / Decimal(fact.trade_count)
            cohort = "WINNING" if fact.session_pnl_usd > 0 else "LOSING" if fact.session_pnl_usd < 0 else "NEUTRAL"
            rows.append(HistoricalRunAnalogVector(vector_id=uuid5(run_id, f"vector:{fact.session_date.isoformat()}"), validation_run_id=run_id, session_date=fact.session_date, cohort_type=cohort, raw_feature_vector_json=_canonical(fact.raw_vector()), normalized_z_vector_json=_canonical(vector), normalization_parameters_json=params, daily_pnl_usd=fact.session_pnl_usd, max_drawdown_usd=fact.max_drawdown_usd, trade_count=fact.trade_count, win_rate_pct=win_rate.quantize(Decimal("0.01"))))
        return rows

    @staticmethod
    def _summary(facts: tuple[SessionPerformanceFact, ...]) -> dict[str, Any]:
        total_trades = sum(fact.trade_count for fact in facts); wins = sum(fact.winning_trades_count for fact in facts); losses = sum(fact.losing_trades_count for fact in facts); breakeven = sum(fact.breakeven_trades_count for fact in facts)
        gross_profit = sum((fact.session_pnl_usd for fact in facts if fact.session_pnl_usd > 0), Decimal("0.00")); gross_loss = -sum((fact.session_pnl_usd for fact in facts if fact.session_pnl_usd < 0), Decimal("0.00")); net = gross_profit - gross_loss
        losing_streak = longest = 0
        for fact in facts:
            losing_streak = losing_streak + 1 if fact.session_pnl_usd < 0 else 0; longest = max(longest, losing_streak)
        return {
            "total_sessions_count": len(facts), "total_trades_count": total_trades, "winning_trades_count": wins, "losing_trades_count": losses, "breakeven_trades_count": breakeven,
            "win_rate_pct": (Decimal("0") if total_trades == 0 else Decimal(wins) * Decimal("100") / Decimal(total_trades)).quantize(Decimal("0.01")),
            "gross_profit_usd": gross_profit, "gross_loss_usd": gross_loss, "net_realized_pnl_usd": net,
            "profit_factor": (Decimal("0") if gross_loss == 0 else gross_profit / gross_loss).quantize(FOUR),
            "expectancy_per_trade_usd": (Decimal("0") if total_trades == 0 else net / Decimal(total_trades)).quantize(FOUR),
            "max_drawdown_usd": max((fact.max_drawdown_usd for fact in facts), default=Decimal("0.00")), "hard_halt_count": sum(fact.hard_halt for fact in facts), "longest_losing_streak": longest,
            "siphoned_safety_usd": sum((fact.siphoned_safety_usd for fact in facts), Decimal("0.00")), "siphoned_treasury_usd": sum((fact.siphoned_treasury_usd for fact in facts), Decimal("0.00")), "siphoned_replication_usd": sum((fact.siphoned_replication_usd for fact in facts), Decimal("0.00")), "cells_spawned_count": sum(fact.cells_spawned_count for fact in facts),
        }
