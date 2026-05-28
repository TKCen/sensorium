#!/usr/bin/env python3
"""Apply Kanban Subconscious settlement records back to Sensorium truth.

The cheap `serasubconscious` Kanban reviewer decides DROP/SAVE/PROMOTE_CONSCIOUS
for each consumed intake. Without this CLI those decisions only land on the
Kanban board: the source Sensorium candidate stays `candidate`, and the next
`sensorium_status` dry-run reports `would_promote` for it, splitting truth.

Run this script after a Subconscious review completes (or directly from the
review completion summary) with one structured settlement record per consumed
intake. The CLI is deterministic and idempotent so it is safe to re-run.

Input shape (single record or list-of-records, JSON):

    {
        "decision": "DROP" | "SAVE" | "PROMOTE_CONSCIOUS",
        "candidate_id": "cand_...",        # OR
        "event_id":     "evt_...",         # OR
        "fingerprint":  "fp_...",          # OR
        "correlation_keys": ["..."],
        "intake_task_id": "kanban_task_id",
        "review_task_id": "kanban_task_id",
        "conscious_task_ref": {            # required only on PROMOTE_CONSCIOUS
            "task_id":   "kanban_task_id",
            "thread_id": "sth_...",
            "board":     "sensorium"
        },
        "reason": "short evidence string"
    }
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_sensorium.settlement import apply_settlement_record
from agent_sensorium.store import SensoriumStore


def _load_records(args: argparse.Namespace) -> list[dict]:
    if args.record:
        raw = json.loads(args.record)
    elif args.file:
        raw = json.loads(Path(args.file).read_text(encoding="utf-8"))
    else:
        raw = json.loads(sys.stdin.read())
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)]
    raise SystemExit("settlement record must be a JSON object or list of objects")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--instance", default="sera")
    ap.add_argument("--state-dir", default=None,
                    help="Override Sensorium state dir (defaults to ~/.hermes/agent-sensorium/<instance>)")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--record", help="Inline JSON settlement record")
    src.add_argument("--file", help="Path to a JSON file with one record or a list of records")
    ap.add_argument("--json", action="store_true", help="Print the per-record results as JSON")
    args = ap.parse_args()

    records = _load_records(args)
    store = SensoriumStore(instance=args.instance, state_dir=args.state_dir)
    store.ensure_dirs()

    results = [apply_settlement_record(store, rec) for rec in records]
    summary = {
        "instance": args.instance,
        "record_count": len(records),
        "settled": sum(1 for r in results if r["action"] == "settled"),
        "already_settled": sum(1 for r in results if r["action"] == "already_settled"),
        "no_candidate_match": sum(1 for r in results if r["action"] == "no_candidate_match"),
        "invalid": sum(1 for r in results if r["action"] in {"invalid_decision", "invalid_record"}),
        "results": results,
    }

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["invalid"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
