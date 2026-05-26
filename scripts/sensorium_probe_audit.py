#!/usr/bin/env python3
"""Probe coverage audit and smoke validation for Agent Sensorium.

Subcommands:
  inventory  -- probe inventory (helpers, wired probes, blind spots)
  smoke      -- exercise 3 source classes end-to-end in temp state
  audit      -- full audit report on existing state

Uses temporary state by default for smoke. Use --state-dir for explicit state.
Subcommands print a human report by default; use --json for machine-readable output.
"""

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_sensorium.probe_audit import audit_store, probe_inventory, run_smoke_probes


def cmd_inventory(args) -> int:
    inv = probe_inventory()
    if args.print_json:
        json.dump(inv, sys.stdout, indent=2)
        print()
    else:
        print("=== Probe Inventory ===")
        print()
        print("Implemented helpers:")
        for h in inv["implemented_helpers"]:
            tag = "LIVE" if h["wired_live"] else "helper-only"
            print(f"  {h['function']:30s}  source={h['source']:20s}  [{tag}]")
        print()
        print("Wired live probes:")
        if inv["wired_live_probes"]:
            for p in inv["wired_live_probes"]:
                print(f"  {p}")
        else:
            print("  (none)")
        print()
        print("Blind spots:")
        for b in inv["blind_spots"]:
            print(f"  {b['name']:30s}  {b['description']}")
    return 0


def _print_smoke(result: dict, args, state_dir: str) -> None:
    if args.print_json:
        compact = {
            "probes": [
                {
                    "source_class": p["source_class"],
                    "promoted": p["result"].get("promoted"),
                    "signal_id": p["result"].get("signal_id"),
                    "event_id": p["result"].get("event_id"),
                    "candidate_id": p["result"].get("candidate_id"),
                }
                for p in result["probes"]
            ],
            "counts": result["counts"],
            "all_promoted": result["all_promoted"],
            "state_dir": state_dir,
        }
        json.dump(compact, sys.stdout, indent=2)
        print()
    else:
        print("=== Probe Smoke ===")
        print(f"State: {state_dir}")
        print()
        for p in result["probes"]:
            r = p["result"]
            status = "PROMOTED" if r.get("promoted") else "below-threshold"
            print(f"  {p['source_class']:20s}  {status:20s}  signal={r.get('signal_id', '-')}")
            if r.get("event_id"):
                print(f"  {'':20s}  {'':20s}  event={r['event_id']}  candidate={r.get('candidate_id', '-')}")
        print()
        c = result["counts"]
        print(f"Totals: signals={c['signals']}  events={c['events']}  candidates={c['candidates']}")
        print(f"All promoted: {result['all_promoted']}")


def cmd_smoke(args) -> int:
    state_dir = args.state_dir
    if state_dir:
        result = run_smoke_probes(state_dir=state_dir)
        _print_smoke(result, args, state_dir)
    else:
        with tempfile.TemporaryDirectory(prefix="sensorium_smoke_") as td:
            result = run_smoke_probes(state_dir=td)
            _print_smoke(result, args, td)
    return 0


def cmd_audit(args) -> int:
    report = audit_store(state_dir=args.state_dir, instance=args.instance)
    if args.print_json:
        json.dump(report, sys.stdout, indent=2)
        print()
    else:
        print("=== Probe Audit ===")
        print()
        c = report["counts"]
        print(f"Signals: {c['signals']}  Events: {c['events']}  Candidates: {c['candidates']}  Active: {c['active_candidates']}")
        if report["by_source"]:
            print()
            print("By source:")
            for src, count in sorted(report["by_source"].items()):
                fresh = report["freshness"].get(src, "-")
                print(f"  {src:20s}  count={count}  latest={fresh}")
        if report["by_kind"]:
            print()
            print("By kind:")
            for kind, count in sorted(report["by_kind"].items()):
                print(f"  {kind:30s}  count={count}")
        print()
        y = report["promotion_yield"]
        print(f"Promotion yield: {y['promoted_events']}/{y['total_signals']} ({y['yield_pct']}%)")
        if report["silent_sources"]:
            print()
            print("Silent sources (configured but no signals):")
            for s in report["silent_sources"]:
                print(f"  {s}")
        if report["blind_spots"]:
            print()
            print("Blind spots (not yet implemented):")
            for b in report["blind_spots"]:
                print(f"  {b}")
    return 0


def _add_common_args(p: argparse.ArgumentParser, *, suppress_defaults: bool = False) -> None:
    """Add common args before or after subcommand without clobbering earlier values."""
    if suppress_defaults:
        p.add_argument("--json", action="store_true", dest="print_json", default=argparse.SUPPRESS)
        p.add_argument("--state-dir", default=argparse.SUPPRESS)
        p.add_argument("--instance", default=argparse.SUPPRESS)
    else:
        p.add_argument("--json", action="store_true", dest="print_json")
        p.add_argument("--state-dir", default=None)
        p.add_argument("--instance", default="default")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Agent Sensorium probe audit")
    _add_common_args(parser)

    sub = parser.add_subparsers(dest="command")
    for name, help_text in [
        ("inventory", "Probe inventory"),
        ("smoke", "Smoke test probes in temp state"),
        ("audit", "Audit existing state"),
    ]:
        sp = sub.add_parser(name, help=help_text)
        _add_common_args(sp, suppress_defaults=True)

    args = parser.parse_args(argv)

    if args.command == "inventory":
        return cmd_inventory(args)
    elif args.command == "smoke":
        return cmd_smoke(args)
    elif args.command == "audit":
        return cmd_audit(args)
    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(main())
