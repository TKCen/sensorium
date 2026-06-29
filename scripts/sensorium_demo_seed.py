#!/usr/bin/env python3
"""Bootstrap a generic ``demo`` Sensorium profile with safe sensors and actuators.

A fresh install runs this once to seed a profile config plus small generic
sensor/actuator registries (from examples/demo-*.json). It is dry-run by
default: it prints a plan and writes nothing unless ``--apply`` is given and
``--dry-run`` is not. Everything is deterministic and stdlib-only.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_sensorium.actuators import register_actuator
from agent_sensorium.config import init_profile_config, profile_state_dir
from agent_sensorium.sensors import load_sensor_registry, register_sensor_kind
from agent_sensorium.store import SensoriumStore
from agent_sensorium.tools import handle_sensorium_ingest_signal

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_registry(path: Path, errors: list[str]) -> dict:
    if not path.is_file():
        errors.append(f"registry_missing: {path}")
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"registry_unreadable: {type(exc).__name__}")
        return {}
    if not isinstance(data, dict):
        errors.append("registry_invalid: expected object")
        return {}
    return data


def _registry_entries(raw: dict, key: str | None = None) -> dict:
    if key and isinstance(raw.get(key), dict):
        return raw[key]
    return raw


def _first_seed_signal(path: Path, errors: list[str]) -> dict | None:
    if not path.is_file():
        errors.append(f"seed_signal_missing: {path}")
        return None
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    return json.loads(line)
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"seed_signal_unreadable: {type(exc).__name__}")
        return None
    errors.append("seed_signal_empty")
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed a generic demo Sensorium profile")
    parser.add_argument("--instance", default="demo")
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--registry", default=str(_REPO_ROOT / "examples" / "demo-sensor-registry.json"))
    parser.add_argument("--actuator-registry", default=str(_REPO_ROOT / "examples" / "demo-actuator-registry.json"))
    parser.add_argument("--seed-signal", default=str(_REPO_ROOT / "examples" / "seed-signal.jsonl"))
    parser.add_argument("--apply", action="store_true", help="Perform writes")
    parser.add_argument("--dry-run", action="store_true", help="Force no writes (overrides --apply)")
    parser.add_argument("--json", action="store_true", dest="print_json", help="Print result JSON to stdout")
    parser.add_argument("--ingest-seed", action="store_true", help="Also ingest the example seed signal when applying")
    args = parser.parse_args(argv)

    apply = bool(args.apply and not args.dry_run)
    errors: list[str] = []

    sensor_registry = _registry_entries(_load_registry(Path(args.registry), errors))
    sensor_names = sorted(sensor_registry)
    actuator_registry = _registry_entries(_load_registry(Path(args.actuator_registry), errors), "actuators")
    actuator_names = sorted(actuator_registry)

    profile: dict | str
    if args.state_dir:
        store = SensoriumStore(instance=args.instance, state_dir=args.state_dir)
        profile = {"skipped": True, "reason": "explicit state-dir"}
        if apply:
            store.ensure_dirs()
    else:
        store = SensoriumStore(instance=args.instance, state_dir=str(profile_state_dir(args.instance)))
        if apply:
            try:
                init = init_profile_config(args.instance)
                profile = {
                    "profile": init["profile"],
                    "state_dir": init["state_dir"],
                    "config_path": init["config_path"],
                    "created": init["created"],
                }
            except Exception as exc:
                errors.append(f"profile_init: {type(exc).__name__}")
                profile = "error"
        else:
            profile = {"profile": args.instance, "state_dir": str(profile_state_dir(args.instance)), "planned": True}

    if apply and sensor_registry:
        load_sensor_registry(store)  # seed from the target store, ignore process-global state
        for name in sensor_names:
            entry = sensor_registry[name]
            try:
                register_sensor_kind(
                    name,
                    defaults=entry.get("defaults"),
                    status=entry.get("status", "active"),
                    store=store,
                )
            except Exception as exc:
                errors.append(f"register_sensor[{name}]: {type(exc).__name__}")

    if apply and actuator_registry:
        for name in actuator_names:
            entry = actuator_registry[name]
            if not isinstance(entry, dict):
                errors.append(f"register_actuator[{name}]: invalid_entry")
                continue
            try:
                register_actuator(
                    store,
                    name,
                    entry=entry,
                    status=entry.get("status", "active"),
                )
            except Exception as exc:
                errors.append(f"register_actuator[{name}]: {type(exc).__name__}")

    seed_signal: dict | None = None
    if args.ingest_seed:
        signal = _first_seed_signal(Path(args.seed_signal), errors)
        if signal is not None:
            seed_signal = {"sensor": signal.get("sensor"), "kind": signal.get("kind"), "ingested": False}
            if apply:
                kw = {"instance": args.instance, "state_dir": args.state_dir}
                raw = json.loads(handle_sensorium_ingest_signal(signal=signal, **kw))
                if raw.get("success"):
                    seed_signal["ingested"] = True
                    seed_signal["data"] = raw["data"]
                else:
                    errors.append(f"seed_ingest: {raw.get('error', 'unknown')}")

    result = {
        "success": len(errors) == 0,
        "instance": args.instance,
        "applied": apply,
        "dry_run": not apply,
        "profile": profile,
        "sensors": sensor_names,
        "actuators": actuator_names,
        "seed_signal": seed_signal,
        "errors": errors,
    }

    if args.print_json:
        json.dump(result, sys.stdout, indent=2)
        print()

    if errors:
        for err in errors:
            print(f"sensorium_demo_seed: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
