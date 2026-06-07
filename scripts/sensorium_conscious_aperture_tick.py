#!/usr/bin/env python3
"""Open a bounded Conscious aperture over internal conscious_task candidates.

This is the Conscious-session entrypoint for the Sensorium thread pool. It opens
at most one bounded aperture over pending internal conscious_task candidates and
prints the packet a Conscious pass should inspect. It performs no dispatch.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_sensorium.conscious_aperture import open_conscious_aperture  # noqa: E402
from agent_sensorium.store import SensoriumStore  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--instance", default="default")
    ap.add_argument(
        "--state-dir",
        default=None,
        help="Override Sensorium state dir (defaults to ~/.hermes/agent-sensorium/<instance>)",
    )
    ap.add_argument("--aperture-size", type=int, default=5)
    ap.add_argument("--max-active-sessions", type=int, default=1)
    ap.add_argument("--stale-after-minutes", type=int, default=180)
    ap.add_argument("--now", default=None, help="Testing override for opened_at timestamp")
    ap.add_argument("--open", action="store_true", help="Persist the aperture state; default is dry-run preview")
    ap.add_argument("--json", action="store_true", help="Print JSON; default also prints JSON for now")
    args = ap.parse_args()

    store = SensoriumStore(instance=args.instance, state_dir=args.state_dir)
    result = open_conscious_aperture(
        store,
        aperture_size=args.aperture_size,
        max_active_sessions=args.max_active_sessions,
        stale_after_minutes=args.stale_after_minutes,
        dry_run=not args.open,
        now=args.now,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("success") else 2


if __name__ == "__main__":
    raise SystemExit(main())
