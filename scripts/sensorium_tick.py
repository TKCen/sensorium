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
from agent_sensorium.sensors import (
    classify_hindsight_pressure,
    classify_kanban_pressure,
    classify_machine_body_pressure,
    classify_machine_network_pressure,
    classify_machine_process_pressure,
    hindsight_pressure_sample,
    kanban_pressure_sample,
    machine_body_pressure_sample,
    machine_network_pressure_sample,
    machine_process_pressure_sample,
)
from agent_sensorium.store import SensoriumStore
from agent_sensorium.tools import (
    handle_sensorium_compact,
    handle_sensorium_dispatch_once,
    handle_sensorium_ingest_signal,
    handle_sensorium_service_threads,
    handle_sensorium_status,
    handle_sensorium_subconscious_advisory,
)


def _sensor_state_path(store: SensoriumStore, name: str) -> Path:
    return store.root / f"{name}_state.json"


def _body_state_path(store: SensoriumStore) -> Path:
    return _sensor_state_path(store, "body_pressure")


def _read_sensor_state(path: Path) -> dict:
    try:
        if path.exists():
            return json.loads(path.read_text(errors="ignore"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {}


def _write_sensor_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, separators=(",", ":")))


_read_body_state = _read_sensor_state
_write_body_state = _write_sensor_state


def _run_transition_sensor(
    *,
    name: str,
    store: SensoriumStore,
    dry_run: bool,
    kw: dict,
    sample_fn,
    classify_fn,
) -> tuple[dict, str | None]:
    path = _sensor_state_path(store, name)
    state = _read_sensor_state(path)
    sample = sample_fn()
    signal, next_state = classify_fn(sample, state=state)
    step = {
        "sampled": True,
        "emitted": signal is not None,
        "level": next_state.get("level", "healthy"),
    }
    if signal is not None:
        step["transition"] = signal.get("transition")
        if not dry_run:
            raw = json.loads(handle_sensorium_ingest_signal(signal=signal, **kw))
            if raw.get("success"):
                step["ingest"] = raw["data"]
            else:
                return step, f"{name}_ingest: {raw.get('error', 'unknown')}"
    if not dry_run:
        store.ensure_dirs()
        _write_sensor_state(path, next_state)
    return step, None


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
    parser.add_argument(
        "--body-pressure", action="store_true",
        help="Sample machine body pressure and ingest only transition signals",
    )
    parser.add_argument("--network-pressure", action="store_true", help="Sample network pressure transition signals")
    parser.add_argument("--process-pressure", action="store_true", help="Sample process/zombie pressure transition signals")
    parser.add_argument("--hindsight-pressure", action="store_true", help="Sample Hindsight queue/API pressure transition signals")
    parser.add_argument("--kanban-pressure", action="store_true", help="Sample Kanban board pressure transition signals")
    parser.add_argument("--all-sensors", action="store_true", help="Run all currently wired deterministic sensors")
    parser.add_argument(
        "--subconscious-advisory", action="store_true",
        help="Run bounded Subconscious advisory dry-run over Events/Candidates",
    )
    parser.add_argument(
        "--enable-subconscious-advisory", action="store_true",
        help="Explicitly enable advisory output handling; still no live model call in core tick",
    )
    args = parser.parse_args(argv)

    kw: dict = {"instance": args.instance, "state_dir": args.state_dir}
    steps: dict = {}
    errors: list[str] = []

    try:
        if args.body_pressure or args.all_sensors:
            store = SensoriumStore(instance=args.instance, state_dir=args.state_dir)
            body_path = _body_state_path(store)
            body_state = _read_body_state(body_path)
            sample = machine_body_pressure_sample()
            signal, next_state = classify_machine_body_pressure(sample, state=body_state)
            body_step = {
                "sampled": True,
                "emitted": signal is not None,
                "level": next_state.get("level", "healthy"),
                "observed_level": next_state.get("last_observed_level", "healthy"),
            }
            if signal is not None:
                body_step["transition"] = signal.get("transition")
                if not args.dry_run:
                    raw = json.loads(handle_sensorium_ingest_signal(signal=signal, **kw))
                    if raw.get("success"):
                        body_step["ingest"] = raw["data"]
                    else:
                        errors.append(f"body_pressure_ingest: {raw.get('error', 'unknown')}")
            if not args.dry_run:
                store.ensure_dirs()
                _write_body_state(body_path, next_state)
            steps["body_pressure"] = body_step

        transition_specs = [
            ("network_pressure", args.network_pressure, machine_network_pressure_sample, classify_machine_network_pressure),
            ("process_pressure", args.process_pressure, machine_process_pressure_sample, classify_machine_process_pressure),
            ("hindsight_pressure", args.hindsight_pressure, hindsight_pressure_sample, classify_hindsight_pressure),
            ("kanban_pressure", args.kanban_pressure, kanban_pressure_sample, classify_kanban_pressure),
        ]
        if any(enabled or args.all_sensors for _, enabled, _, _ in transition_specs):
            store = SensoriumStore(instance=args.instance, state_dir=args.state_dir)
            for name, enabled, sample_fn, classify_fn in transition_specs:
                if not (enabled or args.all_sensors):
                    continue
                step, err = _run_transition_sensor(
                    name=name,
                    store=store,
                    dry_run=args.dry_run,
                    kw=kw,
                    sample_fn=sample_fn,
                    classify_fn=classify_fn,
                )
                steps[name] = step
                if err:
                    errors.append(err)

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

        if args.subconscious_advisory:
            advisory_dry_run = args.dry_run or not args.enable_subconscious_advisory
            raw = json.loads(handle_sensorium_subconscious_advisory(
                dry_run=advisory_dry_run,
                enabled=args.enable_subconscious_advisory,
                record_receipt=not args.dry_run,
                **kw,
            ))
            if raw.get("success"):
                steps["subconscious_advisory"] = raw["data"]
            else:
                errors.append(f"subconscious_advisory: {raw.get('error', 'unknown')}")

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
