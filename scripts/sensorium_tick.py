#!/usr/bin/env python3
"""Deterministic sensorium tick — no model calls, no outbound delivery.

Runs compaction, thread service, dispatch preview, and status.
Writes a local tick receipt. Silent on stdout by default for cron use.
Use --json to print result to stdout.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_sensorium.schemas import utc_now_iso
from agent_sensorium.store import SensoriumStore
from agent_sensorium.tools import (
    handle_sensorium_compact,
    handle_sensorium_dispatch_once,
    handle_sensorium_service_threads,
    handle_sensorium_status,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Agent Sensorium deterministic tick")
    parser.add_argument("--instance", default="default")
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--config-path", default=None)
    parser.add_argument(
        "--json", action="store_true", dest="print_json",
        help="Print result JSON to stdout",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Skip mutations (compact, service archival, receipt)",
    )
    args = parser.parse_args(argv)

    kw: dict = {"instance": args.instance, "state_dir": args.state_dir}
    steps: dict = {}
    errors: list[str] = []

    try:
        if not args.dry_run:
            raw = json.loads(handle_sensorium_compact(**kw))
            if raw.get("success"):
                steps["compact"] = raw["data"]
            else:
                errors.append(f"compact: {raw.get('error', 'unknown')}")

        if not args.dry_run:
            raw = json.loads(handle_sensorium_service_threads(**kw))
            if raw.get("success"):
                steps["service"] = raw["data"]
            else:
                errors.append(f"service: {raw.get('error', 'unknown')}")

        raw = json.loads(handle_sensorium_dispatch_once(dry_run=True, **kw))
        if raw.get("success"):
            steps["dispatch"] = raw["data"]
        else:
            errors.append(f"dispatch: {raw.get('error', 'unknown')}")

        status_kw = dict(kw)
        if args.config_path:
            status_kw["config_path"] = args.config_path
        raw = json.loads(handle_sensorium_status(**status_kw))
        if raw.get("success"):
            steps["status"] = raw["data"]
        else:
            errors.append(f"status: {raw.get('error', 'unknown')}")

    except Exception as exc:
        result = {
            "success": False,
            "instance": args.instance,
            "dry_run": args.dry_run,
            "errors": [str(exc)],
        }
        if args.print_json:
            json.dump(result, sys.stdout, indent=2)
            print()
        print(f"sensorium_tick: {exc}", file=sys.stderr)
        return 1

    if not args.dry_run:
        try:
            store = SensoriumStore(instance=args.instance, state_dir=args.state_dir)
            store.ensure_dirs()
            receipt = {
                "ts": utc_now_iso(),
                "type": "tick.completed",
                "instance": args.instance,
                "dry_run": False,
                "steps": list(steps.keys()),
            }
            if errors:
                receipt["errors"] = errors
            store.append_jsonl("decisions", receipt)
        except Exception as exc:
            errors.append(f"receipt: {exc}")

    result = {
        "success": len(errors) == 0,
        "instance": args.instance,
        "dry_run": args.dry_run,
        **steps,
    }
    if errors:
        result["errors"] = errors

    if args.print_json:
        json.dump(result, sys.stdout, indent=2)
        print()

    if errors:
        for err in errors:
            print(f"sensorium_tick: {err}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
