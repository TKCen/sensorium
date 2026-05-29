#!/usr/bin/env python3
"""Manual salience-review harness — no model calls.

Two modes:
- --json-only-context: print bounded review context for the top open
  attention_policy_review candidate, so a human/conscious reviewer can decide.
- --decision-file PATH: apply a JSON decision payload by writing exactly one
  attention_policy_review.decision receipt.

This harness performs no model calls, no subprocess, and no external network.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_sensorium.config import default_instance_name  # noqa: E402
from agent_sensorium.improvement import (  # noqa: E402
    ATTENTION_POLICY_KIND,
    apply_salience_review_decision,
    build_salience_review_context,
)
from agent_sensorium.store import SensoriumStore  # noqa: E402


def _top_open_candidate(store: SensoriumStore) -> dict | None:
    open_candidates = [
        c
        for c in store.read_jsonl("candidates")
        if c.get("kind") == ATTENTION_POLICY_KIND and c.get("status") == "candidate"
    ]
    if not open_candidates:
        return None
    return open_candidates[-1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manual Sensorium salience-review harness (no model calls)")
    parser.add_argument("--instance", default=default_instance_name())
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--json-only-context", action="store_true", dest="json_only_context")
    parser.add_argument("--decision-file", default=None)
    args = parser.parse_args(argv)

    if args.json_only_context == bool(args.decision_file):
        parser.error("provide exactly one of --json-only-context or --decision-file")

    store = SensoriumStore(instance=args.instance, state_dir=args.state_dir)

    if args.json_only_context:
        candidate = _top_open_candidate(store)
        if candidate is None:
            print(json.dumps({
                "status": "no_candidate",
                "message": "no open attention_policy_review candidate to review",
            }, indent=2, sort_keys=True))
            return 0
        evidence = (candidate.get("improvement_meta") or {}).get("evidence") or {}
        context = build_salience_review_context(store, evidence)
        context["candidate_id"] = candidate.get("id")
        print(json.dumps(context, indent=2, sort_keys=True))
        return 0

    decision_path = Path(args.decision_file)
    payload = json.loads(decision_path.read_text())
    candidate_id = payload.get("candidate_id")
    if not candidate_id:
        print(json.dumps({"success": False, "error": "decision file missing candidate_id"}, indent=2))
        return 1
    result = apply_salience_review_decision(store, candidate_id, payload)
    print(json.dumps({"success": True, "instance": args.instance, "data": result}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
