#!/usr/bin/env python3
"""Deterministic sensorium tick.

Runs compaction, thread service, deterministic pressure sensors, optional bounded
Subconscious advisory, and status. It deliberately does not run the legacy
Sensorium dispatcher: Kanban is the only activation/ticketing substrate.
Pressure sensors remain deterministic/no-model. Subconscious model advisory runs only
when explicitly requested with --subconscious-advisory --subconscious-model.
Writes a local tick receipt. Silent on stdout by default for cron use.
Use --json to print result to stdout.

Sensors run through the unified registry-driven runner (agent_sensorium.runner):
`--sensor NAME` (repeatable) and `--sensor all` cover both built-in deterministic
sensors and trusted local script sensors declared in sensors/registry.json.
`--list-sensors` prints the runnable set and exits. The original per-sensor
flags (--body-pressure, --network-pressure, etc.) remain as compatible aliases
that resolve to the same sensor names through the same runner path.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_sensorium import memory_reflection as memory_reflection_mod
from agent_sensorium import runner
from agent_sensorium.config import default_instance_name, load_instance_config
from agent_sensorium.schemas import utc_now_iso
from agent_sensorium.sensors import (
    build_runtime_heartbeat_signal,
    classify_codex_usage_pressure,
    classify_hindsight_pressure,
    classify_kanban_pressure,
    classify_machine_body_pressure,
    classify_machine_network_pressure,
    classify_machine_process_pressure,
    classify_media_capacity,
    classify_provider_budget_pressure,
    classify_tts_sidecar_pressure,
    codex_usage_sample,
    hindsight_pressure_sample,
    kanban_pressure_sample,
    machine_body_pressure_sample,
    machine_network_pressure_sample,
    machine_process_pressure_sample,
    media_capacity_sample,
    provider_budget_sample,
    runtime_heartbeat_sample,
    tts_sidecar_pressure_sample,
)
from agent_sensorium.store import SensoriumStore
from agent_sensorium.tools import (
    handle_sensorium_compact,
    handle_sensorium_ingest_signal,
    handle_sensorium_service_threads,
    handle_sensorium_status,
    handle_sensorium_subconscious_advisory,
)

# Sensors driven by --all-sensors. Preserves the exact pre-unification set:
# heartbeat and codex_usage were never part of --all-sensors and still aren't.
LEGACY_ALL_SENSORS_NAMES = (
    "body_pressure",
    "network_pressure",
    "process_pressure",
    "hindsight_pressure",
    "kanban_pressure",
    "tts_sidecar_pressure",
    "media_capacity",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Agent Sensorium deterministic tick")
    parser.add_argument("--instance", default=default_instance_name())
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
        "--enable-dispatch", action="store_true",
        help="Deprecated/no-op: internal Sensorium dispatch is disabled; Kanban bridge owns activation.",
    )
    parser.add_argument(
        "--body-pressure", action="store_true",
        help="Sample machine body pressure and ingest only transition signals",
    )
    parser.add_argument("--network-pressure", action="store_true", help="Sample network pressure transition signals")
    parser.add_argument("--process-pressure", action="store_true", help="Sample process/zombie pressure transition signals")
    parser.add_argument("--hindsight-pressure", action="store_true", help="Sample Hindsight queue/API pressure transition signals")
    parser.add_argument("--kanban-pressure", action="store_true", help="Sample Kanban board pressure transition signals")
    parser.add_argument("--tts-sidecar-pressure", action="store_true", help="Sample Chatterbox TTS timeout/liveness cue signals")
    parser.add_argument("--media-capacity", action="store_true", help="Sample almost-idle local media gift capacity")
    parser.add_argument("--codex-usage", action="store_true", help="Sample Codex/OpenAI subscription usage pressure")
    parser.add_argument("--provider-budget", action="store_true", help="Sample paid-stack provider subscription budget pressure")
    parser.add_argument(
        "--heartbeat", action="store_true",
        help="Emit a compact deterministic runtime heartbeat signal (state-dir/registry/pending health)",
    )
    parser.add_argument(
        "--memory-reflection", action="store_true",
        help=(
            "Run due Subconscious-owned Hindsight reflect/recall memory probes "
            "(hot-loaded from memory_reflection.json) and ingest compact reduced "
            "signals. Slow/model-backed; NOT part of --all-sensors."
        ),
    )
    parser.add_argument(
        "--memory-reflection-config", default=None,
        help="Explicit path to memory_reflection.json (defaults to <state_dir>/memory_reflection.json)",
    )
    parser.add_argument("--all-sensors", action="store_true", help="Run all currently wired deterministic sensors")
    parser.add_argument(
        "--sensor", action="append", default=None, metavar="NAME",
        help=(
            "Run one sensor by name through the unified runner (built-in or local "
            "script sensor from sensors/registry.json). Repeatable. "
            "Use 'all' to run every builtin sensor plus every enabled script sensor."
        ),
    )
    parser.add_argument(
        "--list-sensors", action="store_true",
        help="Print the runnable builtin and script sensor names as JSON and exit",
    )
    parser.add_argument(
        "--subconscious-advisory", action="store_true",
        help="Run bounded Subconscious advisory dry-run over Events/Candidates",
    )
    parser.add_argument(
        "--enable-subconscious-advisory", action="store_true",
        help="Allow advisory output handling to create internal conscious-task candidates; still no external side effects",
    )
    parser.add_argument(
        "--subconscious-model", action="store_true",
        help="Allow the cheap OpenAI-compatible Subconscious model lane to generate advisory output",
    )
    parser.add_argument("--subconscious-model-name", default=None, help="Override Subconscious model name")
    parser.add_argument("--subconscious-model-provider", default=None, help="Override Subconscious model provider label")
    parser.add_argument("--subconscious-model-base-url", default=None, help="Override OpenAI-compatible base URL")
    args = parser.parse_args(argv)

    if args.list_sensors:
        store_for_list = SensoriumStore(instance=args.instance, state_dir=args.state_dir)
        listing = runner.list_sensors(store_for_list)
        json.dump(listing, sys.stdout, indent=2)
        print()
        return 0

    store_for_config = SensoriumStore(instance=args.instance, state_dir=args.state_dir)
    instance_config, config_diag = load_instance_config(
        config_path=args.config_path,
        state_dir=str(store_for_config.root),
    )
    kw: dict = {"instance": args.instance, "state_dir": args.state_dir}
    steps: dict = {}
    errors: list[str] = []

    legacy_flag_names = {
        "body_pressure": args.body_pressure,
        "network_pressure": args.network_pressure,
        "process_pressure": args.process_pressure,
        "hindsight_pressure": args.hindsight_pressure,
        "kanban_pressure": args.kanban_pressure,
        "tts_sidecar_pressure": args.tts_sidecar_pressure,
        "media_capacity": args.media_capacity,
        "codex_usage": args.codex_usage,
        "provider_budget": args.provider_budget,
        "heartbeat": args.heartbeat,
    }
    selected_sensors: set[str] = {name for name, enabled in legacy_flag_names.items() if enabled}
    if args.all_sensors:
        selected_sensors.update(LEGACY_ALL_SENSORS_NAMES)

    run_all_unified = False
    for raw_name in args.sensor or []:
        if raw_name == "all":
            run_all_unified = True
        else:
            selected_sensors.add(raw_name)

    # Builtin sample/classify fns are referenced by name here (not imported
    # directly into the runner call) so existing tests that monkeypatch these
    # module-level names (e.g. sensorium_tick.machine_body_pressure_sample)
    # keep working unchanged through the unified runner dispatch path.
    builtin_override_table = {
        "body_pressure": (machine_body_pressure_sample, classify_machine_body_pressure),
        "network_pressure": (machine_network_pressure_sample, classify_machine_network_pressure),
        "process_pressure": (machine_process_pressure_sample, classify_machine_process_pressure),
        "hindsight_pressure": (hindsight_pressure_sample, classify_hindsight_pressure),
        "kanban_pressure": (kanban_pressure_sample, classify_kanban_pressure),
        "tts_sidecar_pressure": (tts_sidecar_pressure_sample, classify_tts_sidecar_pressure),
        "media_capacity": (media_capacity_sample, classify_media_capacity),
        "codex_usage": (codex_usage_sample, classify_codex_usage_pressure),
        "provider_budget": (provider_budget_sample, classify_provider_budget_pressure),
    }

    try:
        if run_all_unified or selected_sensors:
            store = SensoriumStore(instance=args.instance, state_dir=args.state_dir)
            if run_all_unified:
                listing = runner.list_sensors(store)
                selected_sensors.update(listing["builtin"])
                selected_sensors.update(
                    s["name"] for s in listing["script"] if s.get("enabled", True)
                )
            for name in sorted(selected_sensors):
                sample_fn, classify_fn = builtin_override_table.get(name, (None, None))
                heartbeat_kwargs = {}
                if name == runner.HEARTBEAT_SENSOR_NAME:
                    heartbeat_kwargs = {
                        "heartbeat_sample_fn": runtime_heartbeat_sample,
                        "heartbeat_signal_fn": build_runtime_heartbeat_signal,
                    }
                step, err = runner.run_sensor(
                    name,
                    store=store,
                    config=instance_config,
                    dry_run=args.dry_run,
                    kw=kw,
                    instance=args.instance,
                    sample_fn=sample_fn,
                    classify_fn=classify_fn,
                    **heartbeat_kwargs,
                )
                steps[name] = step
                if err:
                    errors.append(err)

        if args.memory_reflection:
            store = SensoriumStore(instance=args.instance, state_dir=args.state_dir)
            store.ensure_dirs()
            mr_state_dir = str(store.root)

            def _memory_reflection_ingest(signal: dict) -> dict:
                return json.loads(handle_sensorium_ingest_signal(
                    signal=signal, config=instance_config, **kw
                ))

            mr_result = memory_reflection_mod.run_due_probes(
                state_dir=mr_state_dir,
                config_path=args.memory_reflection_config,
                client=None,
                dry_run=args.dry_run,
                ingest_fn=None if args.dry_run else _memory_reflection_ingest,
            )
            steps["memory_reflection"] = {
                "enabled": mr_result.get("enabled", False),
                "config_source": mr_result.get("config_source"),
                "due": [d.get("probe_id") for d in mr_result.get("due", [])],
                "skipped": [s.get("probe_id") for s in mr_result.get("skipped", [])],
                "runs": [
                    {
                        "probe_id": r.get("probe_id"),
                        "status": r.get("status"),
                        "emit_reason": r.get("emit_reason"),
                        "emitted_count": r.get("emitted_count"),
                        "delta": r.get("delta"),
                    }
                    for r in mr_result.get("runs", [])
                ],
            }
            if mr_result.get("config_errors"):
                steps["memory_reflection"]["config_errors"] = mr_result["config_errors"]
            memory_reflection_errors = []
            for run in mr_result.get("runs", []):
                if run.get("status") == "error":
                    memory_reflection_errors.append(
                        f"memory_reflection[{run.get('probe_id')}]: {run.get('error', 'unknown')}"
                    )
            if memory_reflection_errors:
                # Memory reflection is a slow Subconscious/Hindsight probe, not a
                # live deterministic sensor. A single reflect timeout should be
                # recorded for later diagnosis but must not abort compaction,
                # thread service, Kanban mirroring, or the quiet no-agent clock.
                steps["memory_reflection"]["nonfatal_errors"] = memory_reflection_errors

        if not args.dry_run:
            raw = json.loads(handle_sensorium_compact(**kw))
            if raw.get("success"):
                steps["compact"] = raw["data"]
            else:
                errors.append(f"compact: {raw.get('error', 'unknown')}")

        if not args.dry_run:
            raw = json.loads(handle_sensorium_service_threads(config=instance_config, **kw))
            if raw.get("success"):
                steps["service"] = raw["data"]
            else:
                errors.append(f"service: {raw.get('error', 'unknown')}")

        if args.subconscious_advisory:
            advisory_dry_run = args.dry_run or not args.enable_subconscious_advisory
            advisory_config = {}
            if args.subconscious_model:
                advisory_config["model_enabled"] = True
            if args.subconscious_model_name:
                advisory_config["model"] = args.subconscious_model_name
            if args.subconscious_model_provider:
                advisory_config["model_provider"] = args.subconscious_model_provider
            if args.subconscious_model_base_url:
                advisory_config["model_base_url"] = args.subconscious_model_base_url
            raw = json.loads(handle_sensorium_subconscious_advisory(
                dry_run=advisory_dry_run,
                enabled=args.enable_subconscious_advisory or args.subconscious_model,
                config=advisory_config or None,
                record_receipt=not args.dry_run,
                **kw,
            ))
            if raw.get("success"):
                steps["subconscious_advisory"] = raw["data"]
            else:
                errors.append(f"subconscious_advisory: {raw.get('error', 'unknown')}")

        if args.enable_dispatch:
            steps["activation"] = {
                "action": "legacy_dispatch_disabled",
                "activation_substrate": "kanban",
                "reason": "--enable-dispatch is deprecated; use the Sensorium-on-Kanban bridge instead.",
            }

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
            "config": config_diag,
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
        "config": config_diag,
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
