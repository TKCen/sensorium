#!/usr/bin/env python3
"""Demo local script actuator — prepare-only artifact contract canary.

Reads one compact JSON request payload on stdin and emits one compact JSON
object on stdout. It does not send messages, call networks, create tasks, or
write files. The Sensorium actuator runner records the returned artifact ref as
prepared local state only; delivery remains a separate conscious/operator step.
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    payload = json.loads(sys.stdin.read() or "{}")
    request = payload.get("request") if isinstance(payload.get("request"), dict) else {}
    decision_ref = str(request.get("conscious_decision_ref") or "")
    if not decision_ref:
        # The runner should enforce this before execution; keep the demo honest
        # if someone runs the script directly.
        print(json.dumps({"error": "missing_conscious_decision_ref"}))
        return 2

    result = {
        "artifact": {
            "kind": "text",
            "ref_path": "artifact://demo/prepared-text-note",
            "delivery_state": "prepared",
            "intended_handoff_mode": "present_thread",
            "sensitivity": "private",
            "allowed_surfaces": ["local"],
        },
        "summary": "Demo actuator prepared a local text artifact reference.",
        "delivery_authorized": False,
        "outbound_delivery": False,
    }
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
