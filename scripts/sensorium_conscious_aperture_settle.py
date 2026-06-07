#!/usr/bin/env python3
"""Settle Conscious aperture items.

Input shape (single object or list via --record, --file, or stdin):

    {
      "candidate_id": "cand_...",
      "aperture_id": "cap_...",          # optional; checked when supplied
      "decision": "REVIEWED | HELD | SETTLED | PREPARED_EXTERNAL_WORK",
      "reason": "short Conscious decision rationale",
      "external_work": {                 # optional, recorded only; no dispatch
        "title": "...",
        "summary": "...",
        "worker_type": "kanban_task",
        "profile": {},
        "target": {}
      }
    }
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_sensorium.conscious_aperture import settle_conscious_aperture_item  # noqa: E402
from agent_sensorium.store import SensoriumStore  # noqa: E402


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
    raise SystemExit("settlement input must be a JSON object or list of objects")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--instance", default="default")
    ap.add_argument(
        "--state-dir",
        default=None,
        help="Override Sensorium state dir (defaults to ~/.hermes/agent-sensorium/<instance>)",
    )
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--record", help="Inline JSON settlement record")
    src.add_argument("--file", help="Path to JSON settlement record or list")
    ap.add_argument("--apply", action="store_true", help="Persist settlements; default is dry-run preview")
    ap.add_argument("--now", default=None, help="Testing override timestamp")
    ap.add_argument("--json", action="store_true", help="Print JSON; default also prints JSON for now")
    args = ap.parse_args()

    store = SensoriumStore(instance=args.instance, state_dir=args.state_dir)
    results = [
        settle_conscious_aperture_item(
            store,
            candidate_id=record.get("candidate_id", ""),
            aperture_id=record.get("aperture_id"),
            decision=record.get("decision", ""),
            reason=record.get("reason", ""),
            external_work=record.get("external_work"),
            dry_run=not args.apply,
            now=args.now,
        )
        for record in _load_records(args)
    ]
    summary = {
        "success": all(r.get("success") for r in results),
        "dry_run": not args.apply,
        "record_count": len(results),
        "settled": sum(1 for r in results if r.get("action") == "settled_aperture_item"),
        "would_settle": sum(1 for r in results if r.get("action") == "would_settle_aperture_item"),
        "already_settled": sum(1 for r in results if r.get("action") == "already_settled"),
        "errors": [r for r in results if not r.get("success")],
        "results": results,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["success"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
