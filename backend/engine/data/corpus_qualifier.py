import hashlib
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from enum import StrEnum
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.db.models.configuration import Instrument
from engine.validation.feed_loader import HistoricalDatasetRegistry, canonical_json_bytes
from engine.validation.models import CanonicalMarketBar, CanonicalOptionChainSnapshot
from engine.validation.session_calendar import SessionCalendarResolver


class QualificationStatus(StrEnum):
    PASS = "PASS"
    REVIEW = "REVIEW"
    FAIL = "FAIL"


class PilotDecisionPoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    underlying_instrument_id: UUID
    symbol: str
    signal_at: datetime
    underlying_spot: Decimal = Field(gt=0)


class CorpusQualificationInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider_code: str
    start_session: date
    end_session: date
    symbols: tuple[str, ...]
    bars: tuple[CanonicalMarketBar, ...]
    option_snapshots: tuple[CanonicalOptionChainSnapshot, ...]
    decision_points: tuple[PilotDecisionPoint, ...]
    raw_artifact_sha256s: tuple[str, ...]
    normalized_dataset_manifest_sha256: str
    target_dtes: tuple[int, ...] = (0, 1, 7, 14, 30)
    strikes_each_side: int = 10


class QualificationMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    underlying_bar_completeness_pct: Decimal
    underlying_status: QualificationStatus
    strategy_signal_count: int
    decision_point_complete_evidence_count: int
    decision_point_evidence_pct: Decimal
    decision_evidence_status: QualificationStatus
    causal_timestamp_violations_count: int
    causal_status: QualificationStatus
    canonical_contract_resolution_pct: Decimal
    resolution_status: QualificationStatus
    assigned_fidelity_tier: str
    fidelity_status: QualificationStatus


class PilotWindow(BaseModel):
    model_config = ConfigDict(frozen=True)

    start_session: date
    end_session: date
    total_calendar_sessions: int
    rth_expected_minutes: int


class CorpusQualificationManifest(BaseModel):
    model_config = ConfigDict(frozen=True)

    qualification_manifest_id: UUID
    qualification_manifest_sha256: str
    qualification_policy_version: str
    provider_code: str
    pilot_window: PilotWindow
    metrics: QualificationMetrics
    overall_qualification_verdict: QualificationStatus
    raw_artifacts_manifest_sha256: str
    normalized_dataset_manifest_sha256: str

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.model_dump(mode="json"))


class CorpusQualificationEngine:
    POLICY_VERSION = "CORPUS-QUALIFICATION-v1"
    REQUIRED_TARGET_DTES = (0, 1, 7, 14, 30)
    REQUIRED_STRIKES_EACH_SIDE = 10

    def __init__(
        self, session: Session, calendar: SessionCalendarResolver | None = None
    ) -> None:
        self.session = session
        self.calendar = calendar or SessionCalendarResolver()

    def qualify(self, evidence: CorpusQualificationInput) -> CorpusQualificationManifest:
        if evidence.end_session < evidence.start_session:
            raise ValueError("pilot end session cannot precede start session")
        if not evidence.symbols or len(set(evidence.symbols)) != len(evidence.symbols):
            raise ValueError("pilot symbols must be non-empty and unique")
        if evidence.target_dtes != self.REQUIRED_TARGET_DTES:
            raise ValueError("Stage 1 requires the frozen 0/1/7/14/30 DTE envelope")
        if evidence.strikes_each_side != self.REQUIRED_STRIKES_EACH_SIDE:
            raise ValueError("Stage 1 requires ten strikes on each side of spot")
        self._validate_hashes(evidence)

        sessions = self.calendar.sessions(evidence.start_session, evidence.end_session)
        if not sessions:
            raise ValueError("pilot window contains no canonical sessions")
        expected_minutes = sum(int((closed - opened).total_seconds() // 60) for _, opened, closed in sessions)
        completeness = self._bar_completeness(evidence, expected_minutes)
        causal_violations = self._causal_violations(evidence)
        complete_decisions = self._complete_decisions(evidence)
        decision_pct = self._percentage(complete_decisions, len(evidence.decision_points))
        resolution_pct = self._canonical_resolution_percentage(evidence.option_snapshots)
        fidelity_tier = self._fidelity_tier(evidence)

        metrics = QualificationMetrics(
            underlying_bar_completeness_pct=completeness,
            underlying_status=self._threshold(completeness, Decimal("99.80"), Decimal("99.00")),
            strategy_signal_count=len(evidence.decision_points),
            decision_point_complete_evidence_count=complete_decisions,
            decision_point_evidence_pct=decision_pct,
            decision_evidence_status=self._threshold(decision_pct, Decimal("95.00"), Decimal("90.00")),
            causal_timestamp_violations_count=causal_violations,
            causal_status=QualificationStatus.PASS if causal_violations == 0 else QualificationStatus.FAIL,
            canonical_contract_resolution_pct=resolution_pct,
            resolution_status=QualificationStatus.PASS if resolution_pct == Decimal("100.00") else QualificationStatus.FAIL,
            assigned_fidelity_tier=fidelity_tier,
            fidelity_status={
                "TIER_1_QUOTE_DEPTH": QualificationStatus.PASS,
                "TIER_2_TRADE_HISTORY": QualificationStatus.REVIEW,
                "TIER_3_BAR_ONLY": QualificationStatus.FAIL,
            }[fidelity_tier],
        )
        verdict = self._overall(metrics)
        raw_manifest_hash = hashlib.sha256(
            canonical_json_bytes(sorted(evidence.raw_artifact_sha256s))
        ).hexdigest()
        window = PilotWindow(
            start_session=evidence.start_session,
            end_session=evidence.end_session,
            total_calendar_sessions=len(sessions),
            rth_expected_minutes=expected_minutes,
        )
        body = {
            "qualification_policy_version": self.POLICY_VERSION,
            "provider_code": evidence.provider_code,
            "pilot_window": window.model_dump(mode="json"),
            "metrics": metrics.model_dump(mode="json"),
            "overall_qualification_verdict": verdict,
            "raw_artifacts_manifest_sha256": raw_manifest_hash,
            "normalized_dataset_manifest_sha256": evidence.normalized_dataset_manifest_sha256,
        }
        digest = hashlib.sha256(canonical_json_bytes(body)).hexdigest()
        return CorpusQualificationManifest(
            qualification_manifest_id=uuid5(NAMESPACE_URL, f"kairo:corpus-qualification:{digest}"),
            qualification_manifest_sha256=digest,
            qualification_policy_version=self.POLICY_VERSION,
            provider_code=evidence.provider_code,
            pilot_window=window,
            metrics=metrics,
            overall_qualification_verdict=verdict,
            raw_artifacts_manifest_sha256=raw_manifest_hash,
            normalized_dataset_manifest_sha256=evidence.normalized_dataset_manifest_sha256,
        )

    def persist_manifest(
        self,
        registry: HistoricalDatasetRegistry,
        manifest: CorpusQualificationManifest,
        *,
        created_at: datetime,
    ):
        return registry.persist_artifact(
            manifest.canonical_bytes(),
            role="NORMALIZED_RESEARCH_STREAM",
            mime_type="application/vnd.kairo.corpus-qualification+json",
            created_at=created_at,
        )

    def _bar_completeness(
        self, evidence: CorpusQualificationInput, expected_minutes: int
    ) -> Decimal:
        by_symbol: dict[str, set[datetime]] = defaultdict(set)
        for bar in evidence.bars:
            if evidence.start_session <= bar.completed_at.astimezone(self.calendar.eastern).date() <= evidence.end_session:
                by_symbol[bar.symbol].add(bar.completed_at)
        rates = [self._percentage(min(len(by_symbol[symbol]), expected_minutes), expected_minutes) for symbol in evidence.symbols]
        return min(rates)

    @staticmethod
    def _causal_violations(evidence: CorpusQualificationInput) -> int:
        violations = 0
        bars_by_symbol: dict[str, list[CanonicalMarketBar]] = defaultdict(list)
        for bar in evidence.bars:
            bars_by_symbol[bar.symbol].append(bar)
            if bar.completed_at <= bar.interval_start_at:
                violations += 1
        for values in bars_by_symbol.values():
            violations += sum(current.completed_at <= previous.completed_at for previous, current in zip(values, values[1:]))
        snapshots_by_symbol: dict[str, list[CanonicalOptionChainSnapshot]] = defaultdict(list)
        for snapshot in evidence.option_snapshots:
            snapshots_by_symbol[snapshot.underlying_symbol].append(snapshot)
        for values in snapshots_by_symbol.values():
            violations += sum(
                current.canonical_completed_at <= previous.canonical_completed_at
                for previous, current in zip(values, values[1:])
            )
        decision_keys = {(row.underlying_instrument_id, row.signal_at) for row in evidence.decision_points}
        for snapshot in evidence.option_snapshots:
            key = (snapshot.underlying_instrument_id, snapshot.canonical_completed_at)
            if key not in decision_keys:
                # An asynchronous snapshot is valid evidence, but never evidence for
                # a preceding decision. It is excluded rather than time-travel joined.
                continue
        for decision in evidence.decision_points:
            exact = any(
                snapshot.underlying_instrument_id == decision.underlying_instrument_id
                and snapshot.canonical_completed_at == decision.signal_at
                for snapshot in evidence.option_snapshots
            )
            if not exact and any(
                snapshot.underlying_instrument_id == decision.underlying_instrument_id
                and snapshot.canonical_completed_at > decision.signal_at
                for snapshot in evidence.option_snapshots
            ):
                violations += 1
        return violations

    def _complete_decisions(self, evidence: CorpusQualificationInput) -> int:
        snapshots = {
            (item.underlying_instrument_id, item.canonical_completed_at): item
            for item in evidence.option_snapshots
        }
        total = 0
        for decision in evidence.decision_points:
            snapshot = snapshots.get((decision.underlying_instrument_id, decision.signal_at))
            if snapshot is None or snapshot.underlying_symbol != decision.symbol:
                continue
            if self._has_full_neighborhood(snapshot, decision):
                total += 1
        return total

    def _has_full_neighborhood(
        self, snapshot: CanonicalOptionChainSnapshot, decision: PilotDecisionPoint
    ) -> bool:
        groups: dict[tuple[int, str], set[Decimal]] = defaultdict(set)
        for contract in snapshot.contracts:
            signal_date = decision.signal_at.astimezone(self.calendar.eastern).date()
            dte = (contract.expiration_date - signal_date).days
            if dte not in self.REQUIRED_TARGET_DTES:
                continue
            if not (
                contract.bid_price > 0
                and contract.ask_price > contract.bid_price
                and contract.bid_size > 0
                and contract.ask_size > 0
            ):
                return False
            groups[(dte, str(contract.option_right))].add(contract.strike_price)
        for dte in self.REQUIRED_TARGET_DTES:
            for right in ("CALL", "PUT"):
                strikes = groups.get((dte, right), set())
                if len(strikes) < 21:
                    return False
                if sum(value < decision.underlying_spot for value in strikes) < 10:
                    return False
                if sum(value > decision.underlying_spot for value in strikes) < 10:
                    return False
        return True

    def _canonical_resolution_percentage(
        self, snapshots: tuple[CanonicalOptionChainSnapshot, ...]
    ) -> Decimal:
        contracts = [contract for snapshot in snapshots for contract in snapshot.contracts]
        if not contracts:
            return Decimal("0.00")
        resolved = 0
        for contract in contracts:
            row = self.session.get(Instrument, contract.contract_instrument_id)
            if row is not None and row.retired_at is None and self._matches_canonical(row, contract):
                resolved += 1
        return self._percentage(resolved, len(contracts))

    @staticmethod
    def _matches_canonical(row: Instrument, contract) -> bool:
        return all(
            (
                row.asset_class == "OPTION",
                row.symbol == contract.canonical_contract_symbol,
                row.contract_symbol == contract.canonical_contract_symbol,
                row.underlying_symbol == contract.underlying_symbol,
                row.expiration_date == contract.expiration_date,
                row.strike_price == contract.strike_price,
                row.option_right == str(contract.option_right),
                row.contract_multiplier == contract.contract_multiplier,
                row.listing_type == contract.listing_type,
            )
        )

    def _fidelity_tier(self, evidence: CorpusQualificationInput) -> str:
        relevant = [
            contract
            for snapshot in evidence.option_snapshots
            for contract in snapshot.contracts
        ]
        if relevant and all(
            row.bid_price > 0 and row.ask_price > row.bid_price and row.bid_size > 0 and row.ask_size > 0
            for row in relevant
        ):
            return "TIER_1_QUOTE_DEPTH"
        if relevant and any(
            (row.volume is not None and row.volume > 0)
            or (row.open_interest is not None and row.open_interest > 0)
            for row in relevant
        ):
            return "TIER_2_TRADE_HISTORY"
        return "TIER_3_BAR_ONLY"

    @staticmethod
    def _percentage(numerator: int, denominator: int) -> Decimal:
        if denominator <= 0:
            return Decimal("0.00")
        return (Decimal(numerator) * Decimal("100") / Decimal(denominator)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

    @staticmethod
    def _threshold(value: Decimal, passing: Decimal, review: Decimal) -> QualificationStatus:
        if value >= passing:
            return QualificationStatus.PASS
        if value >= review:
            return QualificationStatus.REVIEW
        return QualificationStatus.FAIL

    @staticmethod
    def _overall(metrics: QualificationMetrics) -> QualificationStatus:
        values = (
            metrics.underlying_status,
            metrics.decision_evidence_status,
            metrics.causal_status,
            metrics.resolution_status,
            metrics.fidelity_status,
        )
        if QualificationStatus.FAIL in values:
            return QualificationStatus.FAIL
        if QualificationStatus.REVIEW in values:
            return QualificationStatus.REVIEW
        return QualificationStatus.PASS

    @staticmethod
    def _validate_hashes(evidence: CorpusQualificationInput) -> None:
        values = (*evidence.raw_artifact_sha256s, evidence.normalized_dataset_manifest_sha256)
        if not evidence.raw_artifact_sha256s or any(
            len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
            for value in values
        ):
            raise ValueError("qualification evidence hashes must be lowercase SHA-256 values")
