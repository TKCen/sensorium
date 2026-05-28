#!/usr/bin/env python3
"""Record a conscious Sensorium attention-policy-review decision.

This is intentionally a receipt writer, not a policy applier. It marks/holds the
attention_policy_review candidate and records future_tendency_delta plus
verification/rollback conditions so later ticks can learn from the decision.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_sensorium.config import default_instance_name  # noqa: E402
from agent_sensorium.improvement import record_attention_policy_decision  # noqa: E402
from agent_sensorium.store import SensoriumStore  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record an attention-policy-review decision receipt")
    parser.add_argument("--instance", default=default_instance_name())
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument(
        "--decision",
        required=True,
        choices=[
            "NO_CHANGE",
            "THRESHOLD_COALESCING_TWEAK_PROPOSAL",
            "SENSOR_ADDITION_TASK",
            "PRIORITY_MAP_CHANGE",
            "MEMORY_SKILL_PATCH",
            "HOLD",
        ],
    )
    parser.add_argument("--reason", required=True)
    parser.add_argument("--future-tendency-delta", required=True)
    parser.add_argument("--verification-condition", required=True)
    parser.add_argument("--rollback-condition", required=True)
    parser.add_argument("--decided-by", default="conscious")
    parser.add_argument("--decision-ref", default="")
    parser.add_argument("--implementation-ref", default="")
    parser.add_argument("--json", action="store_true", dest="print_json")
    args = parser.parse_args(argv)

    store = SensoriumStore(instance=args.instance, state_dir=args.state_dir)
    try:
        result = record_attention_policy_decision(
            store,
            candidate_id=args.candidate_id,
            decision=args.decision,
            reason=args.reason,
            future_tendency_delta=args.future_tendency_delta,
            verification_condition=args.verification_condition,
            rollback_condition=args.rollback_condition,
            decided_by=args.decided_by,
            decision_ref=args.decision_ref,
            implementation_ref=args.implementation_ref,
        )
    except Exception as exc:
        if args.print_json:
            print(json.dumps({"success": False, "error": str(exc)}, indent=2))
        else:
            print(f"sensorium_attention_policy_decide: {exc}", file=sys.stderr)
        return 1

    payload = {"success": True, "instance": args.instance, "data": result}
    if args.print_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
