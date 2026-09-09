"""Durable restartable finalization for the immutable Q1 2024 Theta corpus."""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

from app.infrastructure.storage.gcs_checkpoint import GCSCheckpointStore
from kairo.pipeline.finalization_state import (
    BUCKET,
    DATASET,
    GCSDurableObjectStore,
    DurableObjectStore,
    Receipt,
    load_receipt,
)
from kairo.pipeline.finalization_stages import (
    Progress,
    run_stage_1,
    run_stage_2,
    run_stage_3,
    run_stage_4,
    validate_receipt,
)


def arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("auto", "1", "2", "3", "4"), required=True)
    parser.add_argument("--dataset", choices=(DATASET,), required=True)
    parser.add_argument("--bucket", default=BUCKET)
    parser.add_argument("--workspace", type=Path)
    return parser.parse_args(argv)


def receipt_chain(store: DurableObjectStore) -> dict[int, Receipt]:
    receipts: dict[int, Receipt] = {}
    missing_seen = False
    predecessor = None
    for stage in range(1, 5):
        receipt = load_receipt(store, stage)
        if receipt is None:
            missing_seen = True
            continue
        if missing_seen:
            raise ValueError("finalization receipt chain contains a stage gap")
        validate_receipt(
            store,
            receipt,
            expected_stage=stage,
            predecessor=predecessor,
        )
        receipts[stage] = receipt
        predecessor = receipt
    return receipts


def run(
    args: argparse.Namespace,
    *,
    store: DurableObjectStore | None = None,
    checkpoint_store: GCSCheckpointStore | None = None,
    progress: Progress | None = None,
) -> dict[int, Receipt]:
    durable = store or GCSDurableObjectStore(args.bucket)
    checkpoints = checkpoint_store or GCSCheckpointStore.from_default_credentials(args.bucket)
    events = progress or Progress()
    database_url = os.environ.get("KAIRO_RUNTIME_DATABASE_URL")
    workspace = args.workspace or Path(tempfile.mkdtemp(prefix="kairo-q1-finalize-"))
    workspace.mkdir(parents=True, exist_ok=True)
    receipts = receipt_chain(durable)

    if args.stage == "auto":
        selected = list(range(len(receipts) + 1, 5))
    else:
        requested = int(args.stage)
        if requested in receipts:
            events.event(
                "FINALIZATION_STAGE_SKIPPED",
                requested,
                events.started,
                receipt_sha256=receipts[requested].sha256,
            )
            return receipts
        if requested != len(receipts) + 1:
            raise ValueError("requested stage does not immediately follow valid durable state")
        selected = [requested]

    for stage in selected:
        if stage == 1:
            receipt = run_stage_1(durable, workspace, events)
        else:
            if not database_url:
                raise RuntimeError("KAIRO_RUNTIME_DATABASE_URL is required for stages 2 through 4")
            if stage == 2:
                receipt = run_stage_2(
                    durable, checkpoints, database_url, workspace, events, receipts[1]
                )
            elif stage == 3:
                receipt = run_stage_3(
                    durable, database_url, workspace, events, receipts[1], receipts[2]
                )
            else:
                receipt = run_stage_4(
                    durable, database_url, workspace, events, receipts[2], receipts[3]
                )
        receipts[stage] = receipt
    return receipts


def main(argv: list[str] | None = None) -> int:
    run(arguments(argv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
