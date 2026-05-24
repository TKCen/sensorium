#!/usr/bin/env python3
"""Deterministic sensorium tick — no model calls, no outbound delivery.

Runs compaction, dispatch, and status in sequence.
Prints compact JSON result to stdout.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_sensorium.tools import (
    handle_sensorium_compact,
    handle_sensorium_dispatch_once,
    handle_sensorium_status,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Agent Sensorium deterministic tick")
    parser.add_argument("--instance", default="default", help="Instance name")
    parser.add_argument("--state-dir", default=None, help="Override state directory")
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview only, skip compaction"
    )
    args = parser.parse_args()

    kw: dict = {"instance": args.instance, "state_dir": args.state_dir}
    steps: dict = {}

    if not args.dry_run:
        steps["compact"] = json.loads(handle_sensorium_compact(**kw))["data"]

    steps["dispatch"] = json.loads(
        handle_sensorium_dispatch_once(dry_run=args.dry_run, **kw)
    )["data"]
    steps["status"] = json.loads(handle_sensorium_status(**kw))["data"]

    result = {
        "success": True,
        "instance": args.instance,
        "dry_run": args.dry_run,
        **steps,
    }
    json.dump(result, sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
