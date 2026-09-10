#!/usr/bin/env python3
"""Apply one strict, bounded Conscious choice to one advisory candidate.

The default is a read-only local canary. ``--apply`` persists the aperture,
choice, and (for REACH_OUT) a prepared local outbox record. It never invokes a
model, calls an adapter, creates a thread, or dispatches work.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_sensorium.conscious_consumer import consume_conscious_advisory  # noqa: E402
from agent_sensorium.store import SensoriumStore  # noqa: E402


def _load_decision(args: argparse.Namespace) -> dict:
    if bool(args.decision) == bool(args.decision_file):
        raise SystemExit("provide exactly one of --decision or --decision-file")
    raw = args.decision
    if args.decision_file:
        raw = Path(args.decision_file).read_text(encoding="utf-8")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"decision must be valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit("decision must be a JSON object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance", default="default")
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--decision")
    parser.add_argument("--decision-file")
    parser.add_argument("--apply", action="store_true", help="Persist the bounded choice; default is read-only")
    parser.add_argument("--stale-after-minutes", type=int, default=180)
    parser.add_argument("--now", default=None)
    parser.add_argument("--json", action="store_true", help="Print JSON output")
    args = parser.parse_args()

    decision = _load_decision(args)
    store = SensoriumStore(instance=args.instance, state_dir=args.state_dir)
    result = consume_conscious_advisory(
        store,
        decision=decision,
        dry_run=not args.apply,
        now=args.now,
        stale_after_minutes=args.stale_after_minutes,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("success") else 2


if __name__ == "__main__":
    raise SystemExit(main())