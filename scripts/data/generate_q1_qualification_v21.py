"""Generate the Q1 qualification v2.1 artifact from sealed Stage 2 files only."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2] / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from engine.data.corpus_qualifier import (  # noqa: E402
    CorpusQualificationManifest,
    PilotDecisionPoint,
)
from engine.data.corpus_qualifier_v21 import qualify_staged_v21  # noqa: E402
from engine.validation.feed_loader import StagedArtifact  # noqa: E402
from kairo.pipeline.finalization_state import (  # noqa: E402
    NORMALIZATION_STAGE_VERSION,
    Receipt,
    sha256_file,
)


def arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage-2-receipt", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--tqqq-options", type=Path, required=True)
    parser.add_argument("--sqqq-options", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def _descriptor(receipt: Receipt, *, artifact_kind: str) -> dict:
    values = [
        item for item in receipt.outputs if item.get("artifact_kind") == artifact_kind
    ]
    if len(values) != 1:
        raise ValueError(f"Stage 2 receipt has no unique {artifact_kind} output")
    return values[0]


def _verified_artifact(path: Path, identity: dict) -> StagedArtifact:
    digest, size = sha256_file(path)
    if (digest, size) != (identity["sha256"], identity["byte_count"]):
        raise ValueError(f"local Stage 2 evidence identity mismatch: {path}")
    return StagedArtifact(
        path=path,
        content_sha256=digest,
        byte_size=size,
        mime_type="application/json",
    )


def main(argv: list[str] | None = None) -> int:
    args = arguments(argv)
    receipt_bytes = args.stage_2_receipt.read_bytes()
    receipt = Receipt.parse(receipt_bytes)
    if receipt.stage != 2 or receipt.stage_version != NORMALIZATION_STAGE_VERSION:
        raise ValueError("offline v2.1 generation requires a Stage 2 receipt")
    plan_bytes = args.plan.read_bytes()
    plan_descriptor = _descriptor(receipt, artifact_kind="stage_2_plan")
    if (
        hashlib.sha256(plan_bytes).hexdigest(),
        len(plan_bytes),
    ) != (
        plan_descriptor["identity"]["sha256"],
        plan_descriptor["identity"]["byte_count"],
    ):
        raise ValueError("local Stage 2 plan identity mismatch")
    plan = json.loads(plan_bytes)
    if plan.get("plan_version") != NORMALIZATION_STAGE_VERSION:
        raise ValueError("Stage 2 plan version mismatch")

    paths = {"TQQQ": args.tqqq_options, "SQQQ": args.sqqq_options}
    option_artifacts = {}
    for stream in plan["streams"]:
        if stream["stream_role"] != "OPTION_CHAIN_QUOTES":
            continue
        symbol = stream["symbol"]
        option_artifacts[symbol] = _verified_artifact(
            paths[symbol], stream["normalized_object"]["identity"]
        )
    if set(option_artifacts) != {"TQQQ", "SQQQ"}:
        raise ValueError("Stage 2 plan does not contain both option streams")

    manifest = qualify_staged_v21(
        option_snapshot_artifacts=option_artifacts,
        decision_points=tuple(
            PilotDecisionPoint.model_validate(value) for value in plan["decisions"]
        ),
        v1_manifest=CorpusQualificationManifest.model_validate(
            plan["expected_qualification"]
        ),
        stage_2_receipt_sha256=receipt.sha256,
        stage_2_plan_identity=plan_descriptor["identity"],
    )
    content = manifest.canonical_bytes()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(content)
    print(json.dumps({
        "artifact_byte_count": len(content),
        "artifact_sha256": hashlib.sha256(content).hexdigest(),
        "output": str(args.output.resolve()),
        "policy": manifest.qualification_policy_version,
        "qualification_manifest_sha256": manifest.qualification_manifest_sha256,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
