#!/usr/bin/env python3
"""Out-of-band admin/dry-run CLI for the Sensorium Memory Reflection Layer.

This is deliberately a script, NOT a model-visible tool: it must never be added
to the compact live `sensorium` tool schema. It lets an operator inspect, validate,
and dry-run hot-loaded memory-reflection probe config without a gateway/plugin
restart, and (optionally) run a real probe whose compact reduced signals flow
through the ordinary Sensorium ingest path.

Commands:
  list       List configured probes (compact JSON).
  validate   Validate config; report per-probe errors. Exit 1 if any error.
  dry-run    Run due (or forced) probes against the live Hindsight client WITHOUT
             persisting raw/history/ingest. Prints compact JSON.
  run        Run due (or forced) probes and ingest compact signals through the
             existing Sensorium ingest handler. Quiet unless --json.

Raw reflect output is never printed. Only compact summaries/metadata are shown.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_sensorium import memory_reflection as mr
from agent_sensorium.config import default_instance_name
from agent_sensorium.store import SensoriumStore
from agent_sensorium.tools import handle_sensorium_ingest_signal


def _probe_summary(probe: mr.ProbeConfig) -> dict:
    return {
        "id": probe.id,
        "enabled": probe.enabled,
        "mode": probe.mode,
        "cadence_hours": probe.cadence_hours,
        "cooldown_hours": probe.cooldown_hours,
        "timeout_s": probe.timeout_s,
        "max_signals": probe.max_signals,
        "max_summary_chars": probe.max_summary_chars,
        "sensitivity": probe.sensitivity,
        "allowed_surfaces": probe.allowed_surfaces,
        "require_delta": probe.require_delta,
        "event_gated": probe.event_gated,
    }


def _resolve_state_dir(args) -> str:
    if args.state_dir:
        return args.state_dir
    store = SensoriumStore(instance=args.instance, state_dir=None)
    return str(store.root)


def _emit(result: dict, *, print_json: bool) -> None:
    if print_json:
        json.dump(result, sys.stdout, indent=2, sort_keys=True)
        print()


def cmd_list(args) -> int:
    state_dir = _resolve_state_dir(args)
    config = mr.load_config(state_dir=state_dir, config_path=args.config_path)
    result = {
        "enabled": config.enabled,
        "config_source": config.source,
        "config_path": config.path,
        "probe_count": len(config.probes),
        "probes": [_probe_summary(p) for p in config.probes],
        "errors": config.errors,
    }
    _emit(result, print_json=True)
    return 0


def cmd_validate(args) -> int:
    state_dir = _resolve_state_dir(args)
    config = mr.load_config(state_dir=state_dir, config_path=args.config_path)
    valid = not config.errors and config.source in {"file", "missing"}
    result = {
        "valid": valid,
        "enabled": config.enabled,
        "config_source": config.source,
        "config_path": config.path,
        "probe_count": len(config.probes),
        "errors": config.errors,
    }
    _emit(result, print_json=True)
    return 0 if valid else 1


def cmd_run(args, *, dry_run: bool) -> int:
    state_dir = _resolve_state_dir(args)

    ingest_fn = None
    if not dry_run:
        def ingest_fn(signal: dict) -> dict:  # noqa: E306 - local closure by design
            return json.loads(
                handle_sensorium_ingest_signal(
                    signal=signal, instance=args.instance, state_dir=state_dir
                )
            )

    result = mr.run_due_probes(
        state_dir=state_dir,
        config_path=args.config_path,
        client=None,  # real HTTP client constructed lazily only if a probe runs
        dry_run=dry_run,
        only_probe=args.probe,
        force=args.force,
        ingest_fn=ingest_fn,
    )
    result["dry_run"] = dry_run
    # Never print raw bodies; run records already exclude them.
    _emit(result, print_json=args.print_json or dry_run)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sensorium memory reflection admin (out-of-band)")
    parser.add_argument("--instance", default=default_instance_name())
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--config-path", default=None, help="Explicit memory_reflection.json path")
    parser.add_argument("--json", action="store_true", dest="print_json", help="Print result JSON to stdout")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="List configured probes (JSON)")
    sub.add_parser("validate", help="Validate config; exit 1 on error")

    for name in ("dry-run", "run"):
        p = sub.add_parser(name, help=f"{name} due/forced probes")
        p.add_argument("--probe", default=None, help="Only this probe id")
        p.add_argument("--force", action="store_true", help="Ignore cadence/cooldown")

    args = parser.parse_args(argv)

    if args.command == "list":
        return cmd_list(args)
    if args.command == "validate":
        return cmd_validate(args)
    if args.command == "dry-run":
        return cmd_run(args, dry_run=True)
    if args.command == "run":
        return cmd_run(args, dry_run=False)
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
