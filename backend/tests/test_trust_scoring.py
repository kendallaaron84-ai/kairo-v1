from decimal import Decimal

from engine.trust.manifest import evidence_manifest_hash
from engine.trust.models import EvidenceStatus, FactorScore
from engine.trust.scoring_factors import weighted_score


def test_optional_not_applicable_factor_renormalizes_available_weights() -> None:
    factors = (
        FactorScore(
            factor="available",
            status=EvidenceStatus.AVAILABLE,
            score=Decimal("80"),
            weight=Decimal("0.25"),
        ),
        FactorScore(
            factor="optional",
            status=EvidenceStatus.NOT_APPLICABLE,
            score=None,
            weight=Decimal("0.75"),
        ),
    )
    assert weighted_score(factors, ("available",)) == Decimal("80")


def test_required_missing_factor_never_fabricates_aggregate_score() -> None:
    factors = (
        FactorScore(
            factor="available",
            status=EvidenceStatus.AVAILABLE,
            score=Decimal("80"),
            weight=Decimal("0.5"),
        ),
        FactorScore(
            factor="required_missing",
            status=EvidenceStatus.INSUFFICIENT_EVIDENCE,
            score=None,
            weight=Decimal("0.5"),
        ),
    )
    assert weighted_score(factors, ("available", "required_missing")) is None


def test_evidence_manifest_is_canonical_and_reproducible() -> None:
    left = {"policy": "TRUST-v0.1", "facts": {"b": 2, "a": Decimal("1.00")}}
    right = {"facts": {"a": Decimal("1.00"), "b": 2}, "policy": "TRUST-v0.1"}
    assert evidence_manifest_hash(left) == evidence_manifest_hash(right)
    assert len(evidence_manifest_hash(left)) == 64
