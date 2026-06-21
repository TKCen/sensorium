"""Unified sensor runner.

Dispatches both built-in deterministic pressure sensors and trusted local
script sensors (declared in `sensors/registry.json` block registry v2)
through a single `--sensor NAME` / `--sensor all` / `--list-sensors` path.
Legacy per-sensor CLI flags remain aliases that resolve to the same builtin
sensor names so there is exactly one execution path per sensor.

No model calls. No outbound. No shell strings — script sensors run via argv
list only.
"""

from __future__ import annotations

import json
from pathlib import Path

from .inner_life import BlockKind, InnerLifeValidationError, load_block_registry_v2
from .schemas import truncate_text, utc_now_iso
from .script_sensor import (
    DEFAULT_MAX_STDOUT_BYTES,
    DEFAULT_TIMEOUT_SECONDS,
    run_script_sensor,
)
from .temporal_sensor import evaluate_temporal_block
from .sensors import (
    _safe_sensor_name,
    build_runtime_heartbeat_signal,
    classify_codex_usage_pressure,
    classify_hindsight_pressure,
    classify_kanban_pressure,
    classify_machine_body_pressure,
    classify_machine_network_pressure,
    classify_machine_process_pressure,
    classify_media_capacity,
    classify_tts_sidecar_pressure,
    codex_usage_sample,
    hindsight_pressure_sample,
    kanban_pressure_sample,
    machine_body_pressure_sample,
    machine_network_pressure_sample,
    machine_process_pressure_sample,
    media_capacity_sample,
    runtime_heartbeat_sample,
    tts_sidecar_pressure_sample,
)
from .store import SensoriumStore
from .tools import handle_sensorium_ingest_signal

HEARTBEAT_SENSOR_NAME = "heartbeat"

# name -> (sample_fn, classify_fn, include_observed_level)
BUILTIN_TRANSITION_SENSORS: dict[str, tuple] = {
    "body_pressure": (machine_body_pressure_sample, classify_machine_body_pressure, True),
    "network_pressure": (machine_network_pressure_sample, classify_machine_network_pressure, False),
    "process_pressure": (machine_process_pressure_sample, classify_machine_process_pressure, False),
    "hindsight_pressure": (hindsight_pressure_sample, classify_hindsight_pressure, False),
    "kanban_pressure": (kanban_pressure_sample, classify_kanban_pressure, False),
    "tts_sidecar_pressure": (tts_sidecar_pressure_sample, classify_tts_sidecar_pressure, False),
    "media_capacity": (media_capacity_sample, classify_media_capacity, False),
    "codex_usage": (codex_usage_sample, classify_codex_usage_pressure, False),
}

BUILTIN_SENSOR_NAMES = frozenset(BUILTIN_TRANSITION_SENSORS) | {HEARTBEAT_SENSOR_NAME}


def _sensor_state_path(store: SensoriumStore, name: str) -> Path:
    return store.root / f"{name}_state.json"


def _read_state(path: Path) -> dict:
    try:
        if path.exists():
            return json.loads(path.read_text(errors="ignore"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {}


def _write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, separators=(",", ":")))


def _write_run_state(store: SensoriumStore, name: str, *, dry_run: bool, record: dict) -> None:
    """Write per-block run-state telemetry, including dashboard-facing aliases.

    Every caller passes `ok` (bool) and `error` (str | None) in `record`. The
    T3 dashboard projection reads `status`/`last_error`; we keep the original
    `ok`/`error`/`emitted` fields too since they're already covered by tests
    and are convenient for direct callers.
    """
    if dry_run:
        return
    safe_id = _safe_sensor_name(name) or name
    ok = bool(record.get("ok"))
    error = record.get("error")
    payload = {
        "id": safe_id,
        "last_run_at": utc_now_iso(),
        **record,
        "status": "ok" if ok else "error",
        "last_error": error if isinstance(error, str) else "",
    }
    store.write_block_run_state(safe_id, payload)


def list_sensors(store: SensoriumStore) -> dict:
    """Return the deterministic set of runnable sensor names by source.

    `builtin` sensors are always available. `script` sensors come from the
    enabled `sensor`-type blocks in the registry whose impl type is `script`.
    """
    builtin = sorted(BUILTIN_SENSOR_NAMES)
    script: list[dict] = []
    temporal: list[dict] = []
    try:
        registry = load_block_registry_v2(store.read_sensor_registry())
    except InnerLifeValidationError:
        registry = {"blocks": {}}
    for block_id, block in sorted(registry.get("blocks", {}).items()):
        if not isinstance(block, dict):
            continue
        if block.get("type") == BlockKind.TEMPORAL_SENSOR.value:
            predicate_cfg = block.get("predicate")
            predicate = predicate_cfg.get("kind") if isinstance(predicate_cfg, dict) else None
            temporal.append(
                {
                    "name": block_id,
                    "enabled": bool(block.get("enabled", True)),
                    "predicate": predicate,
                }
            )
            continue
        if block.get("type") != BlockKind.SENSOR.value:
            continue
        impl = block.get("impl")
        if not isinstance(impl, dict) or impl.get("type") != "script":
            continue
        script.append(
            {
                "name": block_id,
                "enabled": bool(block.get("enabled", True)),
                "command": impl.get("command"),
            }
        )
    return {"builtin": builtin, "script": script, "temporal": temporal}


def _run_builtin_transition_sensor(
    name: str,
    *,
    store: SensoriumStore,
    config: dict,
    dry_run: bool,
    kw: dict,
    sample_fn,
    classify_fn,
    include_observed_level: bool,
) -> tuple[dict, str | None]:
    path = _sensor_state_path(store, name)
    state = _read_state(path)
    sample = sample_fn()
    signal, next_state = classify_fn(sample, state=state)
    step = {
        "sampled": True,
        "emitted": signal is not None,
        "level": next_state.get("level", "healthy"),
    }
    if include_observed_level:
        step["observed_level"] = next_state.get("last_observed_level", "healthy")
    if isinstance(next_state.get("capacity_record"), dict):
        step["capacity_status"] = next_state["capacity_record"].get("status")

    error = None
    if signal is not None:
        step["transition"] = signal.get("transition")
        if not dry_run:
            raw = json.loads(handle_sensorium_ingest_signal(signal=signal, config=config, **kw))
            if raw.get("success"):
                step["ingest"] = raw["data"]
            else:
                error = f"{name}_ingest: {raw.get('error', 'unknown')}"

    if not dry_run:
        store.ensure_dirs()
        _write_state(path, next_state)
        if isinstance(next_state.get("capacity_record"), dict):
            _write_state(store.root / "media_capacity_record.json", next_state["capacity_record"])

    _write_run_state(
        store,
        name,
        dry_run=dry_run,
        record={"ok": error is None, "emitted": signal is not None, "error": error},
    )
    return step, error


def _run_heartbeat_sensor(
    *,
    store: SensoriumStore,
    config: dict,
    dry_run: bool,
    kw: dict,
    instance: str,
    sample_fn=runtime_heartbeat_sample,
    signal_fn=build_runtime_heartbeat_signal,
) -> tuple[dict, str | None]:
    sample = sample_fn(store=store)
    signal = signal_fn(sample, instance=instance)
    step = {"sampled": True, "emitted": True, "values": sample}
    error = None
    if not dry_run:
        raw = json.loads(handle_sensorium_ingest_signal(signal=signal, config=config, **kw))
        if raw.get("success"):
            step["ingest"] = raw["data"]
        else:
            error = f"heartbeat_ingest: {raw.get('error', 'unknown')}"
    _write_run_state(
        store, HEARTBEAT_SENSOR_NAME, dry_run=dry_run, record={"ok": error is None, "emitted": True, "error": error}
    )
    return step, error


def _script_stdout_cap(store: SensoriumStore) -> int:
    try:
        raw_policy = store.read_sensor_policy()
        caps = raw_policy.get("caps") if isinstance(raw_policy, dict) else None
        if isinstance(caps, dict):
            value = caps.get("max_script_stdout_bytes")
            if isinstance(value, (int, float)) and value > 0 and not isinstance(value, bool):
                return int(value)
    except Exception:
        pass
    return DEFAULT_MAX_STDOUT_BYTES


def run_script_block(
    block_id: str, block: dict, *, store: SensoriumStore, config: dict, dry_run: bool, kw: dict
) -> tuple[dict, str | None]:
    """Execute a trusted local script-sensor block and ingest emitted signals."""
    impl = block.get("impl") or {}
    if impl.get("type") != "script":
        error = f"block {block_id} is not a script sensor"
        _write_run_state(store, block_id, dry_run=dry_run, record={"ok": False, "emitted": False, "error": error})
        return {"sampled": False, "emitted": False, "error": error}, error

    command = impl.get("command")
    schedule = block.get("schedule") or {}
    timeout_seconds = schedule.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
        timeout_seconds = DEFAULT_TIMEOUT_SECONDS

    result = run_script_sensor(
        command,
        timeout_seconds=timeout_seconds,
        max_stdout_bytes=_script_stdout_cap(store),
    )

    step = {
        "sampled": True,
        "emitted": False,
        "exit_code": result.get("exit_code"),
        "stdout_truncated": result.get("stdout_truncated", False),
        "duration_seconds": result.get("duration_seconds"),
    }
    error = None

    if not result["ok"]:
        error = f"{block_id}: {result['error']}"
        step["error"] = result["error"]
    else:
        defaults = block.get("defaults") or {}
        ingested: list[dict] = []
        for raw_signal in result["signals"]:
            signal = dict(raw_signal)
            signal.setdefault("sensor", block_id)
            signal.setdefault("source", block.get("source", "machine"))
            for key in ("sensitivity", "allowed_surfaces", "correlation_keys"):
                if key not in signal and key in defaults:
                    signal[key] = defaults[key]
            if dry_run:
                continue
            raw = json.loads(handle_sensorium_ingest_signal(signal=signal, config=config, **kw))
            if raw.get("success"):
                ingested.append(raw["data"])
            else:
                error = f"{block_id}_ingest: {raw.get('error', 'unknown')}"
                break
        step["emitted"] = bool(result["signals"])
        step["signals_emitted"] = len(result["signals"])
        if ingested:
            step["ingest"] = ingested

    _write_run_state(
        store,
        block_id,
        dry_run=dry_run,
        record={
            "ok": error is None,
            "emitted": step["emitted"],
            "error": truncate_text(error, 500) if error else None,
            "exit_code": step.get("exit_code"),
            "duration_seconds": step.get("duration_seconds"),
            "stdout_truncated": step.get("stdout_truncated", False),
        },
    )
    return step, error


def run_temporal_block(
    block_id: str, block: dict, *, store: SensoriumStore, config: dict, dry_run: bool, kw: dict, now: str | None = None
) -> tuple[dict, str | None]:
    """Evaluate a temporal_sensor block and ingest its trend signal, if any."""
    result = evaluate_temporal_block(block_id, block, store=store, now=now)
    error = None
    signal = result.pop("signal", None)
    if result.get("emitted") and signal is not None and not dry_run:
        raw = json.loads(handle_sensorium_ingest_signal(signal=signal, config=config, **kw))
        if raw.get("success"):
            result["ingest"] = raw["data"]
        else:
            error = f"{block_id}_ingest: {raw.get('error', 'unknown')}"

    record = {
        "ok": error is None,
        "emitted": bool(result.get("emitted")),
        "error": error,
        "cooldown_active": bool(result.get("cooldown_active", False)),
    }
    if error is None and result.get("emitted"):
        record["last_emitted_at"] = result.get("window", {}).get("until")
    _write_run_state(store, block_id, dry_run=dry_run, record=record)
    return result, error


def run_sensor(
    name: str,
    *,
    store: SensoriumStore,
    config: dict,
    dry_run: bool,
    kw: dict,
    instance: str,
    script_blocks: dict | None = None,
    sample_fn=None,
    classify_fn=None,
    heartbeat_sample_fn=None,
    heartbeat_signal_fn=None,
    now: str | None = None,
) -> tuple[dict, str | None]:
    """Run one sensor by name, builtin or script, through the unified path.

    `sample_fn`/`classify_fn` and `heartbeat_sample_fn`/`heartbeat_signal_fn`
    are optional overrides so callers (e.g. the tick CLI) can dispatch through
    their own currently-bound module-level names — this keeps existing
    monkeypatch-based tests valid while still running every sensor through
    this single registry-driven path.
    """
    if name in BUILTIN_TRANSITION_SENSORS:
        default_sample_fn, default_classify_fn, include_observed_level = BUILTIN_TRANSITION_SENSORS[name]
        return _run_builtin_transition_sensor(
            name,
            store=store,
            config=config,
            dry_run=dry_run,
            kw=kw,
            sample_fn=sample_fn or default_sample_fn,
            classify_fn=classify_fn or default_classify_fn,
            include_observed_level=include_observed_level,
        )
    if name == HEARTBEAT_SENSOR_NAME:
        return _run_heartbeat_sensor(
            store=store,
            config=config,
            dry_run=dry_run,
            kw=kw,
            instance=instance,
            sample_fn=heartbeat_sample_fn or runtime_heartbeat_sample,
            signal_fn=heartbeat_signal_fn or build_runtime_heartbeat_signal,
        )
    blocks = script_blocks if script_blocks is not None else load_block_registry_v2(
        store.read_sensor_registry()
    ).get("blocks", {})
    block = blocks.get(name)
    if isinstance(block, dict) and block.get("type") == BlockKind.TEMPORAL_SENSOR.value:
        return run_temporal_block(name, block, store=store, config=config, dry_run=dry_run, kw=kw, now=now)
    if not isinstance(block, dict) or block.get("type") != BlockKind.SENSOR.value:
        error = f"unknown sensor: {name}"
        return {"sampled": False, "emitted": False, "error": error}, error
    return run_script_block(name, block, store=store, config=config, dry_run=dry_run, kw=kw)
