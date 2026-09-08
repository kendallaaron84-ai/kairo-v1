"""Deterministic, read-only Pass 1 capital matrix for certified Q1 evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import urllib.request
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator


ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = ROOT / "backend"
for import_root in (ROOT, BACKEND_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from app.domain.enums import OptionRight  # noqa: E402
from engine.data.corpus_qualifier import (  # noqa: E402
    CorpusQualificationManifest,
    QualificationStatus,
)
from engine.intelligence.storage_driver import GCSReadOnlyArtifactStorage  # noqa: E402
from engine.strategy.ema_cross_strategy import (  # noqa: E402
    EASTERN,
    EMACrossStrategy,
    StrategySignalReason,
)


Q1_START = date(2024, 1, 2)
Q1_END = date(2024, 3, 28)
Q1_SESSION_COUNT = 61
Q1_RTH_MINUTES = Decimal("23790")
DEFAULT_CAPITALS = (Decimal("100"), Decimal("250"), Decimal("500"), Decimal("1000"))
CENT = Decimal("0.01")
PCT = Decimal("0.01")
FOUR = Decimal("0.0001")
FORBIDDEN_SCRATCH = ".attempt-4-staging-v1"
PASS_2_BLOCK = (
    "Pass 2 flywheel execution is blocked until cell-population ceilings, shared-liquidity "
    "allocation, multi-cell collision handling, and capital-conservation proofs are certified"
)
PASS_2_PREREQUISITES = (
    "PARAMETERIZED_CELL_POPULATION_CEILING",
    "DETERMINISTIC_SHARED_LIQUIDITY_ALLOCATION",
    "MULTI_CELL_ORDER_COLLISION_HANDLING",
    "STRICT_MULTI_CELL_CAPITAL_CONSERVATION_PROOF",
)


def _q(value: Decimal, quantum: Decimal = CENT) -> Decimal:
    return value.quantize(quantum, rounding=ROUND_HALF_EVEN)


def _pct(numerator: Decimal | int, denominator: Decimal | int) -> Decimal:
    denominator = Decimal(denominator)
    if denominator == 0:
        return Decimal("0.00")
    return _q(Decimal(numerator) / denominator * Decimal("100"), PCT)


class ArtifactReference(BaseModel):
    model_config = ConfigDict(frozen=True)

    uri: str = Field(min_length=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_size: int = Field(gt=0)

    @model_validator(mode="after")
    def canonical_only(self) -> "ArtifactReference":
        if FORBIDDEN_SCRATCH in self.uri:
            raise ValueError("scratch/staging artifacts cannot authorize research replay")
        return self


class IntraTradeQuote(BaseModel):
    model_config = ConfigDict(frozen=True)

    timestamp: datetime
    bid: Decimal = Field(ge=0)
    ask: Decimal = Field(ge=0)

    @model_validator(mode="after")
    def causal_quote(self) -> "IntraTradeQuote":
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("intra-trade quote timestamp must be timezone-aware")
        if self.ask < self.bid:
            raise ValueError("intra-trade option quote market cannot be crossed")
        return self


class EmpiricalSignal(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True, serialize_by_alias=True)

    signal_id: str = Field(min_length=1)
    contract_instrument_id: UUID = Field(
        validation_alias=AliasChoices("contract_instrument_id", "contract_id"),
        serialization_alias="contract_id",
    )
    symbol: str = Field(
        pattern=r"^(TQQQ|SQQQ)$",
        validation_alias=AliasChoices("symbol", "underlying"),
        serialization_alias="underlying",
    )
    option_right: OptionRight = Field(
        validation_alias=AliasChoices("option_right", "right"),
        serialization_alias="right",
    )
    session: date
    signal_at: datetime = Field(
        validation_alias=AliasChoices("signal_at", "entry_timestamp"),
        serialization_alias="entry_timestamp",
    )
    exit_at: datetime = Field(
        validation_alias=AliasChoices("exit_at", "exit_timestamp"),
        serialization_alias="exit_timestamp",
    )
    entry_bid: Decimal = Field(gt=0)
    entry_ask: Decimal = Field(gt=0)
    exit_bid: Decimal = Field(gt=0)
    exit_ask: Decimal = Field(gt=0)
    contract_multiplier: Decimal = Field(default=Decimal("100"), gt=0)
    exit_reason: StrategySignalReason
    intra_trade_path: tuple[IntraTradeQuote, ...]

    @model_validator(mode="after")
    def valid_observation(self) -> "EmpiricalSignal":
        if self.signal_at.tzinfo is None or self.signal_at.utcoffset() is None:
            raise ValueError("signal timestamp must be timezone-aware")
        if self.exit_at.tzinfo is None or self.exit_at.utcoffset() is None:
            raise ValueError("exit timestamp must be timezone-aware")
        if self.exit_at <= self.signal_at:
            raise ValueError("exit must follow the signal")
        if self.entry_ask < self.entry_bid or self.exit_ask < self.exit_bid:
            raise ValueError("option quote markets cannot be crossed")
        if not self.intra_trade_path:
            raise ValueError("empirical signal requires a complete intra-trade quote path")
        timestamps = [item.timestamp for item in self.intra_trade_path]
        if timestamps != sorted(set(timestamps)):
            raise ValueError("intra-trade quote path must be unique and chronological")
        if any(
            current - previous != timedelta(minutes=1)
            for previous, current in zip(timestamps, timestamps[1:])
        ):
            raise ValueError("intra-trade quote path must contain every causal minute")
        if timestamps[0] != self.signal_at or timestamps[-1] != self.exit_at:
            raise ValueError("intra-trade quote path endpoints do not match execution")
        if (
            self.intra_trade_path[0].bid != self.entry_bid
            or self.intra_trade_path[0].ask != self.entry_ask
            or self.intra_trade_path[-1].bid != self.exit_bid
            or self.intra_trade_path[-1].ask != self.exit_ask
        ):
            raise ValueError("intra-trade quote path prices do not match execution")
        if not Q1_START <= self.session <= Q1_END:
            raise ValueError("signal session is outside certified Q1")
        if self.exit_reason not in {
            StrategySignalReason.TAKE_PROFIT,
            StrategySignalReason.STOP_LOSS,
            StrategySignalReason.TREND_REVERSAL,
            StrategySignalReason.FORCED_FLATTEN,
        }:
            raise ValueError("empirical exits must use a frozen Strategy 001 exit reason")
        option_return = (self.exit_bid - self.entry_ask) / self.entry_ask
        if (
            self.exit_reason is StrategySignalReason.TAKE_PROFIT
            and option_return < Decimal("0.10")
        ):
            raise ValueError("take-profit evidence does not satisfy frozen 10% threshold")
        if (
            self.exit_reason is StrategySignalReason.STOP_LOSS
            and option_return > Decimal("-0.05")
        ):
            raise ValueError("stop-loss evidence does not satisfy frozen -5% threshold")
        if (
            self.exit_reason is StrategySignalReason.FORCED_FLATTEN
            and self.exit_at.astimezone(EASTERN).time() < time(15, 45)
        ):
            raise ValueError("forced-flatten evidence precedes the frozen 15:45 ET boundary")
        return self


class Q1CapitalMatrixEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: str = Field(pattern=r"^KAIRO-Q1-CAPITAL-EVIDENCE-v1$")
    evidence_payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    qualification_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    qualification_manifest_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    normalized_dataset_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    strategy_id: str = Field(pattern=r"^EMA-CROSS-001$")
    strategy_version: str = Field(pattern=r"^1\.0\.0$")
    start_session: date
    end_session: date
    artifacts: tuple[ArtifactReference, ...]
    signals: tuple[EmpiricalSignal, ...]

    @model_validator(mode="after")
    def certified_shape(self) -> "Q1CapitalMatrixEvidence":
        if (self.start_session, self.end_session) != (Q1_START, Q1_END):
            raise ValueError("capital evidence does not cover the exact certified Q1 window")
        if not self.artifacts:
            raise ValueError("capital evidence must bind canonical artifacts")
        identifiers = [item.signal_id for item in self.signals]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("empirical signal identifiers must be unique")
        ordering = [(item.signal_at, item.signal_id) for item in self.signals]
        if ordering != sorted(ordering):
            raise ValueError("empirical signals must be deterministically ordered")
        return self

    def canonical_payload_bytes(self) -> bytes:
        return json.dumps(
            self.model_dump(mode="json", exclude={"evidence_payload_sha256"}),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def verify_self_seal(self) -> None:
        digest = hashlib.sha256(self.canonical_payload_bytes()).hexdigest()
        if digest != self.evidence_payload_sha256:
            raise ValueError("capital evidence self-seal is invalid")


class FrictionPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    commission_per_contract_side: Decimal = Field(ge=0)
    spread_capture_pct: Decimal = Field(ge=0, le=100)
    slippage_bps: Decimal = Field(ge=0)


class ShadowSignalOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    signal_id: str
    required_cell_capital: Decimal
    gross_outcome: Decimal
    commissions: Decimal
    modeled_spread_slippage: Decimal
    net_outcome: Decimal


class CapitalTierResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    starting_capital: Decimal
    ending_capital: Decimal
    signals_generated: int
    affordability_rejections: int
    affordability_rate_pct: Decimal
    shadow_rejected_signals: int
    shadow_gross_profit: Decimal
    shadow_gross_loss: Decimal
    shadow_commissions_paid: Decimal
    shadow_modeled_spread_slippage: Decimal
    shadow_net_profit: Decimal
    shadow_rejected_outcomes: tuple[ShadowSignalOutcome, ...]
    risk_halt_rejections: int
    trades_entered: int
    wins: int
    losses: int
    win_rate_pct: Decimal
    gross_profit: Decimal
    gross_loss: Decimal
    commissions_paid: Decimal
    modeled_spread_slippage: Decimal
    net_profit: Decimal
    avg_winner: Decimal
    avg_loser: Decimal
    expectancy: Decimal
    profit_factor: Decimal | None
    max_drawdown: Decimal
    worst_session_pnl: Decimal
    hard_halt_sessions: int
    return_pct: Decimal
    capital_utilization_pct: Decimal
    time_in_market_pct: Decimal
    avg_capital_deployed: Decimal
    peak_capital_deployed: Decimal
    trades_per_session: Decimal
    no_trade_sessions: int


class AffordabilityPoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    capital: Decimal
    executable_signals: int
    execution_pct: Decimal


class EconomicViabilityPoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    capital: Decimal
    trades_entered: int
    net_profit: Decimal


class AffordabilityAnalysis(BaseModel):
    model_config = ConfigDict(frozen=True)

    minimum_capital_for_participation: Decimal | None
    capital_for_50_pct: Decimal | None
    capital_for_80_pct: Decimal | None
    capital_for_95_pct: Decimal | None
    curve: tuple[AffordabilityPoint, ...]
    minimum_economically_viable_capital: Decimal | None
    economic_viability_curve: tuple[EconomicViabilityPoint, ...]


class CapitalMatrixSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: str = "KAIRO-Q1-CAPITAL-MATRIX-v1"
    mode: str = "PASS_1_SINGLE_CELL"
    qualification_manifest_sha256: str
    evidence_sha256: str
    normalized_dataset_manifest_sha256: str
    strategy_id: str
    strategy_version: str
    start_session: date
    end_session: date
    cell_count: int = 1
    flywheel: str = "off"
    siphon: str = "off"
    safety_sweep: str = "off"
    treasury_sweep: str = "off"
    replication: str = "off"
    pass_2_status: str = "BLOCKED_PENDING_CERTIFICATION"
    pass_2_prerequisites: tuple[str, ...] = PASS_2_PREREQUISITES
    friction_policy: FrictionPolicy
    tiers: tuple[CapitalTierResult, ...]
    affordability: AffordabilityAnalysis


def _local_path(uri: str) -> Path | None:
    candidate = Path(uri)
    if candidate.is_absolute():
        return candidate
    parsed = urlparse(uri)
    if parsed.scheme == "":
        return Path(uri)
    if parsed.scheme == "file" and parsed.netloc in ("", "localhost"):
        return Path(unquote(parsed.path.lstrip("/") if sys.platform == "win32" else parsed.path))
    return None


def read_uri(uri: str) -> bytes:
    if FORBIDDEN_SCRATCH in uri:
        raise ValueError("active scratch/staging paths are forbidden")
    local = _local_path(uri)
    if local is not None:
        if not local.is_file():
            raise FileNotFoundError(f"required evidence does not exist: {uri}")
        return local.read_bytes()
    parsed = urlparse(uri)
    if parsed.scheme == "gs":
        return GCSReadOnlyArtifactStorage().read_bytes(uri)
    if parsed.scheme == "https":
        with urllib.request.urlopen(uri, timeout=60) as response:  # noqa: S310
            return response.read()
    raise ValueError("evidence URI must use a local path, file://, gs://, or https://")


def verified_bytes(uri: str, expected_sha256: str, expected_size: int | None = None) -> bytes:
    content = read_uri(uri)
    if expected_size is not None and len(content) != expected_size:
        raise ValueError(f"evidence byte-size mismatch: {uri}")
    if hashlib.sha256(content).hexdigest() != expected_sha256:
        raise ValueError(f"evidence SHA-256 mismatch: {uri}")
    return content


def verify_artifact_reference(artifact: ArtifactReference) -> None:
    local = _local_path(artifact.uri)
    if local is None:
        verified_bytes(artifact.uri, artifact.content_sha256, artifact.byte_size)
        return
    if not local.is_file():
        raise FileNotFoundError(f"required evidence does not exist: {artifact.uri}")
    digest = hashlib.sha256()
    size = 0
    with local.open("rb") as stream:
        while chunk := stream.read(8 * 1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    if size != artifact.byte_size:
        raise ValueError(f"evidence byte-size mismatch: {artifact.uri}")
    if digest.hexdigest() != artifact.content_sha256:
        raise ValueError(f"evidence SHA-256 mismatch: {artifact.uri}")


def verify_qualification_manifest_identity(
    manifest: CorpusQualificationManifest,
) -> None:
    body = manifest.model_dump(
        mode="json",
        exclude={"qualification_manifest_id", "qualification_manifest_sha256"},
    )
    digest = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if digest != manifest.qualification_manifest_sha256:
        raise ValueError("qualification manifest internal SHA-256 is invalid")
    expected_id = uuid5(NAMESPACE_URL, f"kairo:corpus-qualification:{digest}")
    if expected_id != manifest.qualification_manifest_id:
        raise ValueError("qualification manifest deterministic identity is invalid")


def load_certified_evidence(
    *,
    manifest_uri: str,
    manifest_sha256: str,
    evidence_uri: str,
    evidence_sha256: str,
) -> tuple[CorpusQualificationManifest, Q1CapitalMatrixEvidence]:
    manifest_content = verified_bytes(manifest_uri, manifest_sha256)
    manifest = CorpusQualificationManifest.model_validate_json(manifest_content)
    verify_qualification_manifest_identity(manifest)
    window = manifest.pilot_window
    if (window.start_session, window.end_session) != (Q1_START, Q1_END):
        raise ValueError("qualification manifest is not the certified Q1 2024 window")
    if manifest.overall_qualification_verdict is not QualificationStatus.PASS:
        raise ValueError("qualification manifest verdict must be PASS")

    evidence_content = verified_bytes(evidence_uri, evidence_sha256)
    evidence = Q1CapitalMatrixEvidence.model_validate_json(evidence_content)
    evidence.verify_self_seal()
    if evidence.qualification_manifest_content_sha256 != manifest_sha256:
        raise ValueError("empirical evidence is not bound to the supplied manifest artifact")
    if evidence.qualification_manifest_sha256 != manifest.qualification_manifest_sha256:
        raise ValueError("empirical evidence is not bound to the qualification identity")
    if (
        evidence.normalized_dataset_manifest_sha256
        != manifest.normalized_dataset_manifest_sha256
    ):
        raise ValueError("empirical evidence dataset identity does not match qualification")
    if len(evidence.signals) != manifest.metrics.strategy_signal_count:
        raise ValueError("empirical signal population does not match qualification manifest")
    for artifact in evidence.artifacts:
        verify_artifact_reference(artifact)
    return manifest, evidence


def required_cell_capital(signal: EmpiricalSignal) -> Decimal:
    # Frozen Strategy 001: daily budget=50%; three slots; at least one contract.
    return (signal.entry_ask * signal.contract_multiplier * Decimal("6")).quantize(
        CENT, rounding=ROUND_CEILING
    )


def affordability_analysis(
    signals: tuple[EmpiricalSignal, ...], policy: FrictionPolicy
) -> AffordabilityAnalysis:
    requirements = sorted(required_cell_capital(item) for item in signals)
    if not requirements:
        return AffordabilityAnalysis(
            minimum_capital_for_participation=None,
            capital_for_50_pct=None,
            capital_for_80_pct=None,
            capital_for_95_pct=None,
            curve=(),
            minimum_economically_viable_capital=None,
            economic_viability_curve=(),
        )
    curve = tuple(
        AffordabilityPoint(
            capital=capital,
            executable_signals=sum(required <= capital for required in requirements),
            execution_pct=_pct(
                sum(required <= capital for required in requirements), len(requirements)
            ),
        )
        for capital in sorted(set(requirements))
    )

    def threshold(percent: int) -> Decimal:
        rank = math.ceil(len(requirements) * percent / 100)
        return requirements[max(rank - 1, 0)]

    economic_curve = tuple(
        EconomicViabilityPoint(
            capital=capital,
            trades_entered=result.trades_entered,
            net_profit=result.net_profit,
        )
        for capital in sorted(set(requirements))
        for result in (run_tier(capital, signals, policy),)
    )
    viable = next(
        (
            point.capital
            for point in economic_curve
            if point.trades_entered > 0 and point.net_profit > 0
        ),
        None,
    )
    return AffordabilityAnalysis(
        minimum_capital_for_participation=requirements[0],
        capital_for_50_pct=threshold(50),
        capital_for_80_pct=threshold(80),
        capital_for_95_pct=threshold(95),
        curve=curve,
        minimum_economically_viable_capital=viable,
        economic_viability_curve=economic_curve,
    )


def _trade_economics(
    signal: EmpiricalSignal,
    quantity: int,
    policy: FrictionPolicy,
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    multiplier_quantity = signal.contract_multiplier * Decimal(quantity)
    entry_mid = (signal.entry_bid + signal.entry_ask) / Decimal("2")
    exit_mid = (signal.exit_bid + signal.exit_ask) / Decimal("2")
    gross = (exit_mid - entry_mid) * multiplier_quantity
    spread_fraction = policy.spread_capture_pct / Decimal("100")
    spread = (
        (signal.entry_ask - signal.entry_bid) / Decimal("2")
        + (signal.exit_ask - signal.exit_bid) / Decimal("2")
    ) * spread_fraction * multiplier_quantity
    slippage = (
        (entry_mid + exit_mid)
        * (policy.slippage_bps / Decimal("10000"))
        * multiplier_quantity
    )
    commission = policy.commission_per_contract_side * Decimal(quantity) * Decimal("2")
    friction = spread + slippage
    return _q(gross), _q(commission), _q(friction), _q(gross - commission - friction)


def _exposure_metrics(
    positions: list[tuple[datetime, datetime, Decimal]],
) -> tuple[Decimal, Decimal, Decimal]:
    events: dict[datetime, Decimal] = defaultdict(lambda: Decimal("0"))
    for opened_at, closed_at, deployed in positions:
        events[opened_at] += deployed
        events[closed_at] -= deployed
    active_minutes = exposure_minutes = peak = current = Decimal("0")
    previous: datetime | None = None
    for timestamp in sorted(events):
        if previous is not None:
            minutes = Decimal(str((timestamp - previous).total_seconds())) / Decimal("60")
            if current > 0:
                active_minutes += minutes
                exposure_minutes += current * minutes
        current += events[timestamp]
        if current < 0:
            raise ValueError("empirical position intervals produce negative exposure")
        peak = max(peak, current)
        previous = timestamp
    average = exposure_minutes / active_minutes if active_minutes else Decimal("0")
    return _pct(active_minutes, Q1_RTH_MINUTES), _q(average), _q(peak)


def run_tier(
    capital: Decimal,
    signals: tuple[EmpiricalSignal, ...],
    policy: FrictionPolicy,
) -> CapitalTierResult:
    if capital <= 0:
        raise ValueError("starting capital must be positive")
    equity = capital
    peak = capital
    maximum_drawdown = Decimal("0")
    affordability_rejections = risk_rejections = 0
    gross_profit = gross_loss = commissions = friction = net = Decimal("0")
    winners: list[Decimal] = []
    losers: list[Decimal] = []
    utilization = Decimal("0")
    shadow_gross_profit = shadow_gross_loss = Decimal("0")
    shadow_commissions = shadow_friction = shadow_net = Decimal("0")
    shadow_outcomes: list[ShadowSignalOutcome] = []
    session_pnl: dict[date, Decimal] = defaultdict(lambda: Decimal("0"))
    halted_sessions: set[date] = set()
    traded_sessions: set[date] = set()
    positions: list[tuple[datetime, datetime, Decimal]] = []

    by_session: dict[date, list[EmpiricalSignal]] = defaultdict(list)
    for signal in signals:
        by_session[signal.session].append(signal)
    for session_date in sorted(by_session):
        strategy = EMACrossStrategy(settled_cash=equity)
        for signal in by_session[session_date]:
            if strategy.entries_halted:
                risk_rejections += 1
                continue
            premium = signal.entry_ask * signal.contract_multiplier
            quantity = int((strategy.slot_size / premium).to_integral_value(rounding=ROUND_FLOOR))
            if quantity < 1:
                affordability_rejections += 1
                shadow_gross, shadow_commission, shadow_modeled, shadow_result = (
                    _trade_economics(signal, 1, policy)
                )
                shadow_gross_profit += max(shadow_gross, Decimal("0"))
                shadow_gross_loss += abs(min(shadow_gross, Decimal("0")))
                shadow_commissions += shadow_commission
                shadow_friction += shadow_modeled
                shadow_net += shadow_result
                shadow_outcomes.append(ShadowSignalOutcome(
                    signal_id=signal.signal_id,
                    required_cell_capital=required_cell_capital(signal),
                    gross_outcome=shadow_gross,
                    commissions=shadow_commission,
                    modeled_spread_slippage=shadow_modeled,
                    net_outcome=shadow_result,
                ))
                continue
            deployed = premium * Decimal(quantity)
            positions.append((signal.signal_at, signal.exit_at, deployed))
            traded_sessions.add(session_date)
            gross, commission, modeled_friction, trade_net = _trade_economics(
                signal, quantity, policy
            )
            gross_profit += max(gross, Decimal("0"))
            gross_loss += abs(min(gross, Decimal("0")))
            commissions += commission
            friction += modeled_friction
            net += trade_net
            session_pnl[session_date] += trade_net
            equity += trade_net
            peak = max(peak, equity)
            maximum_drawdown = max(maximum_drawdown, peak - equity)
            (winners if trade_net > 0 else losers).append(trade_net)
            strategy.record_close(signal.symbol, realized_pnl=trade_net)
            if strategy.entries_halted:
                halted_sessions.add(session_date)

    trades = len(winners) + len(losers)
    time_in_market, average_deployed, peak_deployed = _exposure_metrics(positions)
    utilization = peak_deployed / capital * Decimal("100") if capital else Decimal("0")
    return CapitalTierResult(
        starting_capital=_q(capital),
        ending_capital=_q(equity),
        signals_generated=len(signals),
        affordability_rejections=affordability_rejections,
        affordability_rate_pct=_pct(len(signals) - affordability_rejections, len(signals)),
        shadow_rejected_signals=affordability_rejections,
        shadow_gross_profit=_q(shadow_gross_profit),
        shadow_gross_loss=_q(shadow_gross_loss),
        shadow_commissions_paid=_q(shadow_commissions),
        shadow_modeled_spread_slippage=_q(shadow_friction),
        shadow_net_profit=_q(shadow_net),
        shadow_rejected_outcomes=tuple(shadow_outcomes),
        risk_halt_rejections=risk_rejections,
        trades_entered=trades,
        wins=len(winners),
        losses=len(losers),
        win_rate_pct=_pct(len(winners), trades),
        gross_profit=_q(gross_profit),
        gross_loss=_q(gross_loss),
        commissions_paid=_q(commissions),
        modeled_spread_slippage=_q(friction),
        net_profit=_q(net),
        avg_winner=_q(sum(winners, Decimal("0")) / len(winners)) if winners else Decimal("0.00"),
        avg_loser=_q(sum(losers, Decimal("0")) / len(losers)) if losers else Decimal("0.00"),
        expectancy=_q(net / trades, FOUR) if trades else Decimal("0.0000"),
        profit_factor=(
            _q(sum(winners, Decimal("0")) / abs(sum(losers, Decimal("0"))), FOUR)
            if losers else None
        ),
        max_drawdown=_q(maximum_drawdown),
        worst_session_pnl=_q(min(session_pnl.values(), default=Decimal("0"))),
        hard_halt_sessions=len(halted_sessions),
        return_pct=_pct(net, capital),
        capital_utilization_pct=_q(utilization, PCT),
        time_in_market_pct=time_in_market,
        avg_capital_deployed=average_deployed,
        peak_capital_deployed=peak_deployed,
        trades_per_session=_q(Decimal(trades) / Decimal(Q1_SESSION_COUNT), FOUR),
        no_trade_sessions=Q1_SESSION_COUNT - len(traded_sessions),
    )


def build_summary(
    evidence: Q1CapitalMatrixEvidence,
    *,
    manifest_sha256: str,
    evidence_sha256: str,
    capitals: tuple[Decimal, ...],
    policy: FrictionPolicy,
) -> CapitalMatrixSummary:
    if tuple(sorted(set(capitals))) != capitals:
        raise ValueError("capital tiers must be unique and strictly ascending")
    return CapitalMatrixSummary(
        qualification_manifest_sha256=manifest_sha256,
        evidence_sha256=evidence_sha256,
        normalized_dataset_manifest_sha256=evidence.normalized_dataset_manifest_sha256,
        strategy_id=evidence.strategy_id,
        strategy_version=evidence.strategy_version,
        start_session=evidence.start_session,
        end_session=evidence.end_session,
        friction_policy=policy,
        tiers=tuple(run_tier(capital, evidence.signals, policy) for capital in capitals),
        affordability=affordability_analysis(evidence.signals, policy),
    )


def canonical_summary_bytes(summary: CapitalMatrixSummary) -> bytes:
    return json.dumps(
        summary.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def markdown_summary(summary: CapitalMatrixSummary) -> str:
    headers = (
        "Capital", "Signals", "Affordable %", "Trades", "W/L", "Net", "Return %",
        "Max DD", "Utilization %", "Time in market %", "Avg deployed", "Peak deployed",
        "Trades/session", "No-trade sessions", "Halts",
    )
    lines = [
        "# Q1 2024 Capital Matrix — Pass 1",
        "",
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for tier in summary.tiers:
        lines.append(
            "| " + " | ".join((
                f"${tier.starting_capital}", str(tier.signals_generated),
                str(tier.affordability_rate_pct), str(tier.trades_entered),
                f"{tier.wins}/{tier.losses}", f"${tier.net_profit}", str(tier.return_pct),
                f"${tier.max_drawdown}", str(tier.capital_utilization_pct),
                str(tier.time_in_market_pct), f"${tier.avg_capital_deployed}",
                f"${tier.peak_capital_deployed}", str(tier.trades_per_session),
                str(tier.no_trade_sessions),
                str(tier.hard_halt_sessions),
            )) + " |"
        )
    affordability = summary.affordability
    lines.extend((
        "", "## Empirical affordability", "",
        f"- Minimum capital for participation: {affordability.minimum_capital_for_participation}",
        f"- Capital for 50% execution: {affordability.capital_for_50_pct}",
        f"- Capital for 80% execution: {affordability.capital_for_80_pct}",
        f"- Capital for 95% execution: {affordability.capital_for_95_pct}",
        "- Minimum economically viable capital: "
        f"{affordability.minimum_economically_viable_capital}",
        "", "Flywheel and replication: **disabled**.", "",
    ))
    return "\n".join(lines)


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


def arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest-uri", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--evidence-uri", required=True)
    parser.add_argument("--evidence-sha256", required=True)
    parser.add_argument("--capital", type=Decimal, action="append")
    parser.add_argument("--commission-per-contract-side", type=Decimal, default=Decimal("0"))
    parser.add_argument("--spread-capture-pct", type=Decimal, default=Decimal("100"))
    parser.add_argument("--slippage-bps", type=Decimal, default=Decimal("0"))
    parser.add_argument("--flywheel", choices=("off", "on"), default="off")
    parser.add_argument("--output-json", type=Path, default=Path("q1_capital_matrix_summary.json"))
    parser.add_argument(
        "--output-markdown", type=Path, default=Path("q1_capital_matrix_summary.md")
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = arguments(argv)
    if args.flywheel == "on":
        raise RuntimeError(PASS_2_BLOCK)
    _manifest, evidence = load_certified_evidence(
        manifest_uri=args.manifest_uri,
        manifest_sha256=args.manifest_sha256,
        evidence_uri=args.evidence_uri,
        evidence_sha256=args.evidence_sha256,
    )
    policy = FrictionPolicy(
        commission_per_contract_side=args.commission_per_contract_side,
        spread_capture_pct=args.spread_capture_pct,
        slippage_bps=args.slippage_bps,
    )
    capitals = tuple(args.capital) if args.capital else DEFAULT_CAPITALS
    summary = build_summary(
        evidence,
        manifest_sha256=args.manifest_sha256,
        evidence_sha256=args.evidence_sha256,
        capitals=capitals,
        policy=policy,
    )
    _atomic_write(args.output_json, canonical_summary_bytes(summary))
    _atomic_write(args.output_markdown, markdown_summary(summary).encode("utf-8"))
    print(canonical_summary_bytes(summary).decode("utf-8"), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
