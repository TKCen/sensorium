#!/usr/bin/env python3
"""Create an internal Sensorium conscious_task candidate.

This is the non-executable promotion seam for Subconscious review. It turns a
bounded advisory JSON object into Sensorium internal state via
``handle_sensorium_subconscious_advisory``. It does not dispatch workers, send
messages, create Kanban tasks, or open platform threads.

Input shape (single JSON object via --record, --file, or stdin):

    {
        "rationale": "short evidence string",
        "event_ids": ["evt_..."],
        "candidate_ids": ["cand_..."],
        "pressure": 0.72,
        "conscious_task": {
            "request_type": "THINK | SAVE | UPDATE_MEMORY_OR_SKILL | CREATE_FOLLOWUP | PRIVATE_EXPRESSION | DELEGATE_WORK",
            "title": "short title",
            "why": "why Conscious should see this",
            "expected_decision": "what Conscious should decide"
        }
    }

The script wraps the object with action=CREATE_CONSCIOUS_TASK and prints the
handler result. Use the returned ``candidate_id`` as the internal conscious
candidate reference in settlement metadata.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_sensorium.tools import handle_sensorium_subconscious_advisory  # noqa: E402


def _load_record(args: argparse.Namespace) -> dict:
    if args.record:
        raw = json.loads(args.record)
    elif args.file:
        raw = json.loads(Path(args.file).read_text(encoding="utf-8"))
    else:
        raw = json.loads(sys.stdin.read())
    if not isinstance(raw, dict):
        raise SystemExit("conscious-task record must be a JSON object")
    return raw


def _advisory_output(record: dict) -> dict:
    output = dict(record)
    output["action"] = "CREATE_CONSCIOUS_TASK"
    output.setdefault("event_ids", [])
    output.setdefault("candidate_ids", [])
    if not output.get("rationale"):
        output["rationale"] = str(output.get("reason") or "Promoted by Sensorium Subconscious review")
    return output


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--instance", default="default")
    ap.add_argument(
        "--state-dir",
        default=None,
        help="Override Sensorium state dir (defaults to ~/.hermes/agent-sensorium/<instance>)",
    )
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--record", help="Inline JSON conscious-task promotion record")
    src.add_argument("--file", help="Path to a JSON conscious-task promotion record")
    ap.add_argument("--json", action="store_true", help="Print handler output as JSON")
    args = ap.parse_args()

    record = _load_record(args)
    raw = json.loads(handle_sensorium_subconscious_advisory(
        instance=args.instance,
        state_dir=args.state_dir,
        dry_run=False,
        enabled=True,
        advisory_output=_advisory_output(record),
        config={"model_enabled": False},
        record_receipt=True,
    ))
    if args.json:
        print(json.dumps(raw, indent=2, sort_keys=True))
    else:
        data = raw.get("data") if raw.get("success") else None
        if data and data.get("candidate_id"):
            print(data["candidate_id"])
        else:
            print(json.dumps(raw, sort_keys=True))
    return 0 if raw.get("success") else 2


if __name__ == "__main__":
    raise SystemExit(main())
