import hashlib
import json
from pathlib import Path

from engine.data.corpus_qualifier_v21 import CorpusQualificationV21Manifest


ARTIFACT = Path(__file__).resolve().parents[4] / "qualification-v2.1.json"


def test_q1_policy_v21_frozen_independent_regression():
    content = ARTIFACT.read_bytes()
    payload = json.loads(content)
    manifest = CorpusQualificationV21Manifest.model_validate(payload)
    acquisition = manifest.scored_acquisition_qualification["acquisition_envelope"]

    assert acquisition["by_symbol"]["TQQQ"]["complete_decision_count"] == 991
    assert acquisition["by_symbol"]["TQQQ"]["decision_count"] == 4516
    assert acquisition["by_symbol"]["TQQQ"]["completeness_percentage"] == "21.94"
    assert acquisition["by_symbol"]["SQQQ"]["complete_decision_count"] == 2940
    assert acquisition["by_symbol"]["SQQQ"]["decision_count"] == 4938
    assert acquisition["by_symbol"]["SQQQ"]["completeness_percentage"] == "59.54"
    assert acquisition["combined"]["complete_decision_count"] == 3931
    assert acquisition["combined"]["decision_count"] == 9454
    assert acquisition["combined"]["completeness_percentage"] == "41.58"
    assert acquisition["combined"]["failure_attribution"] == {
        "STRIKE_ENVELOPE_DEFICIT": 5523
    }
    assert manifest.overall_qualification_verdict == "FAIL"
    assert manifest.strategy_001_diagnostic["eligible_candidate_decision_count"] == 9453
    assert manifest.strategy_001_diagnostic["decision_count"] == 9454
    assert manifest.strategy_001_diagnostic["candidate_availability_percentage"] == "99.99"
    assert manifest.strategy_001_diagnostic["scoring_effect"] == "NONE"
    assert manifest.strategy_001_diagnostic["live_capital_authorization"] is False
    assert hashlib.sha256(content).hexdigest() == (
        "24cb3a9e1806b804cda9ca7f2a4e3ef1a0123c8912bf42933a0053b3d43e915a"
    )
