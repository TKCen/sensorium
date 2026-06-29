"""Hot-reloadable actuator registry and trusted script runner.

Actuators are Conscious-only prepare lanes. They may prepare artifacts or outbox
records, but they must not deliver messages directly from raw pressure or
Subconscious output. The registry is loaded fresh from disk for each run so
operators/agents can tune or replace local scripts without restarting the Hermes
gateway.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .artifacts import store_artifact
from .schemas import truncate_text, utc_now_iso
from .script_sensor import run_script_sensor
from .store import SensoriumStore, atomic_write_json

_ACTUATOR_STATUSES = {"active", "paused", "deprecated"}
_ACTUATOR_KINDS = {"prepare_artifact"}
_ALLOWED_IMPL_TYPES = {"script"}
_DEFAULT_TIMEOUT_SECONDS = 30.0
_DEFAULT_MAX_STDOUT_BYTES = 65536
_DEFAULT_MAX_STDERR_BYTES = 65536
_DEFAULT_MAX_MESSAGE_CHARS = 1200
_VALID_REQUEST_TYPES = {"PRIVATE_EXPRESSION", "REACH_OUT", "PREPARE_ARTIFACT", "THINK"}


def _hash_obj(value: Any) -> str:
    try:
        payload = json.dumps(value, sort_keys=True, separators=(",", ":"))
    except TypeError:
        payload = repr(value)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _safe_name(name: str) -> str:
    text = "".join(ch if ch.isalnum() or ch in "_.:-" else "_" for ch in str(name or "").strip()).strip("_")
    return text[:80]


def _string_list(value: Any, *, limit: int = 32) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str):
            text = item.strip()
            if text and text not in out:
                out.append(text)
    return out[:limit]


def _positive_float(value: Any, default: float, *, lo: float = 0.001, hi: float = 600.0) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        num = default
    return max(lo, min(hi, num))


def _positive_int(value: Any, default: int, *, lo: int = 1, hi: int = 1_000_000) -> int:
    try:
        num = int(value)
    except (TypeError, ValueError):
        num = default
    return max(lo, min(hi, num))


def _registry_path(store: SensoriumStore) -> Path:
    return store.root / "actuators" / "registry.json"


def read_actuator_registry(store: SensoriumStore) -> dict:
    path = _registry_path(store)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_actuator_registry(store: SensoriumStore, registry: dict) -> None:
    store.ensure_dirs()
    atomic_write_json(_registry_path(store), registry)


def _sanitize_entry(name: str, raw: dict[str, Any]) -> dict[str, Any] | None:
    safe = _safe_name(name)
    if not safe or not isinstance(raw, dict):
        return None
    status = str(raw.get("status") or ("active" if raw.get("enabled", True) else "paused")).strip()
    if status not in _ACTUATOR_STATUSES:
        status = "paused"
    kind = str(raw.get("kind") or "prepare_artifact").strip()
    if kind not in _ACTUATOR_KINDS:
        status = "paused"
        kind = "prepare_artifact"
    impl_value = raw.get("impl")
    impl_raw: dict[str, Any] = impl_value if isinstance(impl_value, dict) else {}
    impl_type = str(impl_raw.get("type") or "").strip()
    command = _string_list(impl_raw.get("command"), limit=16)
    impl = {"type": impl_type, "command": command}
    if impl_type not in _ALLOWED_IMPL_TYPES or not command:
        status = "paused"
    schedule_value = raw.get("schedule")
    schedule_raw: dict[str, Any] = schedule_value if isinstance(schedule_value, dict) else {}
    caps_value = raw.get("caps")
    caps_raw: dict[str, Any] = caps_value if isinstance(caps_value, dict) else {}
    input_value = raw.get("input_contract")
    input_raw: dict[str, Any] = input_value if isinstance(input_value, dict) else {}
    output_value = raw.get("output_contract")
    output_raw: dict[str, Any] = output_value if isinstance(output_value, dict) else {}
    if "allowed_request_types" in input_raw:
        allowed_request_types = [
            t for t in _string_list(input_raw.get("allowed_request_types"), limit=16)
            if t in _VALID_REQUEST_TYPES
        ]
        if not allowed_request_types:
            status = "paused"
    else:
        allowed_request_types = ["PRIVATE_EXPRESSION", "REACH_OUT", "PREPARE_ARTIFACT"]
    requires_conscious_decision = input_raw.get("requires_conscious_decision", True)
    if not isinstance(requires_conscious_decision, bool):
        requires_conscious_decision = True
    return {
        "name": safe,
        "enabled": status == "active",
        "status": status,
        "kind": kind,
        "capability": truncate_text(str(raw.get("capability") or safe), 120),
        "impl": impl,
        "schedule": {
            "timeout_seconds": _positive_float(schedule_raw.get("timeout_seconds"), _DEFAULT_TIMEOUT_SECONDS),
        },
        "caps": {
            "max_stdout_bytes": _positive_int(caps_raw.get("max_stdout_bytes"), _DEFAULT_MAX_STDOUT_BYTES),
            "max_stderr_bytes": _positive_int(caps_raw.get("max_stderr_bytes"), _DEFAULT_MAX_STDERR_BYTES),
        },
        "input_contract": {
            "allowed_request_types": allowed_request_types,
            "max_message_chars": _positive_int(
                input_raw.get("max_message_chars"), _DEFAULT_MAX_MESSAGE_CHARS, hi=4000
            ),
            "requires_conscious_decision": requires_conscious_decision,
        },
        "output_contract": {
            "artifact_kinds": _string_list(output_raw.get("artifact_kinds"), limit=8) or ["audio", "text", "image", "video"],
            "max_media_refs": _positive_int(output_raw.get("max_media_refs"), 1, hi=8),
            "delivery_authorized": bool(output_raw.get("delivery_authorized", False)),
        },
        "script_roots": _string_list(raw.get("script_roots"), limit=16),
    }


def load_actuator_registry(store: SensoriumStore) -> dict[str, dict[str, Any]]:
    """Load and sanitize the actuator registry fresh from disk."""
    raw = read_actuator_registry(store)
    if isinstance(raw.get("actuators"), dict):
        raw_entries = raw.get("actuators") or {}
    else:
        raw_entries = raw
    out: dict[str, dict[str, Any]] = {}
    for name, entry in sorted(raw_entries.items()):
        sanitized = _sanitize_entry(str(name), entry if isinstance(entry, dict) else {})
        if sanitized:
            out[sanitized["name"]] = sanitized
    return out


def _merge_entry(existing: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = dict(existing)
    for key, value in patch.items():
        if (
            key in {"impl", "schedule", "caps", "input_contract", "output_contract"}
            and isinstance(value, dict)
            and isinstance(merged.get(key), dict)
        ):
            nested = dict(merged[key])
            nested.update(value)
            merged[key] = nested
        else:
            merged[key] = value
    return merged


def register_actuator(
    store: SensoriumStore,
    name: str,
    *,
    entry: dict[str, Any] | None = None,
    status: str = "active",
) -> dict[str, dict[str, Any]]:
    safe = _safe_name(name)
    if not safe:
        raise ValueError("actuator name required")
    raw = read_actuator_registry(store)
    raw_actuators = raw.get("actuators")
    entries: dict[str, Any] = dict(raw_actuators if isinstance(raw_actuators, dict) else raw)
    existing = entries.get(safe)
    merged: dict[str, Any] = dict(existing if isinstance(existing, dict) else {})
    if entry:
        merged = _merge_entry(merged, entry)
    merged["status"] = {"pause": "paused", "deprecate": "deprecated"}.get(status, status)
    sanitized = _sanitize_entry(safe, merged)
    if sanitized is None:
        raise ValueError("invalid actuator entry")
    entries[safe] = sanitized
    write_actuator_registry(store, {"version": 1, "actuators": entries})
    return load_actuator_registry(store)


def _allowed_script_roots(store: SensoriumStore, entry: dict[str, Any]) -> list[Path]:
    roots = [store.root / "actuators" / "scripts", Path.home() / ".hermes" / "scripts"]
    for raw in entry.get("script_roots") or []:
        roots.append(Path(raw).expanduser())
    resolved: list[Path] = []
    for root in roots:
        try:
            resolved.append(root.resolve())
        except OSError:
            continue
    return resolved


def _script_command_index(command: list[str]) -> int | None:
    if not command:
        return None
    first = Path(command[0]).name.lower()
    return 1 if (first.startswith("python") and len(command) > 1) else 0


def _script_path_from_command(command: list[str]) -> Path | None:
    idx = _script_command_index(command)
    if idx is None:
        return None
    try:
        return Path(command[idx]).expanduser().resolve()
    except OSError:
        return None


def _execution_command(command: list[str], script_path: Path) -> list[str]:
    expanded = [os.path.expanduser(arg) if isinstance(arg, str) else arg for arg in command]
    idx = _script_command_index(command)
    if idx is not None:
        expanded[idx] = str(script_path)
    return expanded


def _script_content_hash(script_path: Path | None) -> str:
    if script_path is None:
        return ""
    try:
        return hashlib.sha256(script_path.read_bytes()).hexdigest()[:16]
    except OSError:
        return "unreadable"


def _safe_ref(value: Any, *, prefix: str = "ref", max_len: int = 120) -> str:
    text = str(value or "").strip()
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:/#@+-")
    if text and len(text) <= max_len and all(ch in allowed for ch in text):
        return text
    if not text:
        return ""
    return f"{prefix}_{_hash_obj(text)}"


def _script_allowed(store: SensoriumStore, entry: dict[str, Any]) -> bool:
    command = entry.get("impl", {}).get("command") or []
    script_path = _script_path_from_command(command)
    if script_path is None:
        return False
    for root in _allowed_script_roots(store, entry):
        try:
            script_path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def run_actuator_prepare_artifact(
    store: SensoriumStore,
    *,
    name: str,
    request: dict[str, Any],
    config: dict | None = None,
) -> dict:
    """Run one Conscious-gated hotloaded actuator and record its artifact.

    The registry is loaded fresh on every call. The script receives only compact
    request JSON. Direct delivery is rejected; output may prepare an artifact and
    optionally an outbox in a later phase.
    """
    store.ensure_dirs()
    registry = load_actuator_registry(store)
    safe = _safe_name(name)
    entry = registry.get(safe)
    now = utc_now_iso()
    if not entry:
        return _actuator_denied(store, now, safe, "actuator_not_found")
    if entry.get("status") != "active" or not entry.get("enabled"):
        return _actuator_denied(store, now, safe, "actuator_not_active")
    if entry.get("kind") != "prepare_artifact":
        return _actuator_denied(store, now, safe, "unsupported_actuator_kind")
    if not _script_allowed(store, entry):
        return _actuator_denied(store, now, safe, "script_path_not_allowed")
    script_path = _script_path_from_command(entry["impl"]["command"])
    if script_path is None:
        return _actuator_denied(store, now, safe, "script_path_not_allowed")

    request = request if isinstance(request, dict) else {}
    request_type = str(request.get("request_type") or "").strip()
    contract = entry["input_contract"]
    if request_type not in contract["allowed_request_types"]:
        return _actuator_denied(store, now, safe, "request_type_not_allowed")
    decision_ref = _safe_ref(request.get("conscious_decision_ref") or request.get("decision_ref"), prefix="decision")
    if contract.get("requires_conscious_decision") and not decision_ref:
        return _actuator_denied(store, now, safe, "missing_conscious_decision_ref")

    compact_request = {
        "request_type": request_type,
        "message": truncate_text(str(request.get("message") or ""), int(contract["max_message_chars"])),
        "title": truncate_text(str(request.get("title") or ""), 200),
        "thread_id": truncate_text(str(request.get("thread_id") or ""), 120),
        "candidate_id": truncate_text(str(request.get("candidate_id") or ""), 120),
        "conscious_decision_ref": decision_ref,
        "surface": truncate_text(str(request.get("surface") or "local"), 40),
    }
    script_payload = {
        "schema_version": 1,
        "actuator": safe,
        "capability": entry.get("capability", safe),
        "request": compact_request,
    }
    env: dict[str, str] = dict(os.environ)
    env.update({
        "SENSORIUM_INSTANCE": store.instance,
        "SENSORIUM_STATE_DIR": str(store.root),
        "SENSORIUM_ACTUATOR_NAME": safe,
        "SENSORIUM_NOW": now,
    })
    run = run_script_sensor(
        _execution_command(entry["impl"]["command"], script_path),
        env=env,
        timeout_seconds=float(entry["schedule"]["timeout_seconds"]),
        max_stdout_bytes=int(entry["caps"]["max_stdout_bytes"]),
        max_stderr_bytes=int(entry["caps"].get("max_stderr_bytes", _DEFAULT_MAX_STDERR_BYTES)),
        stdin_text=json.dumps(script_payload, sort_keys=True),
    )
    if not run.get("ok"):
        return _actuator_denied(store, now, safe, str(run.get("error") or "script_failed"), extra={"duration_seconds": run.get("duration_seconds")})

    raw_signals = run.get("signals")
    signals: list[Any] = raw_signals if isinstance(raw_signals, list) else []
    if any(isinstance(item, dict) and (item.get("delivery_authorized") or item.get("outbound_delivery")) for item in signals):
        return _actuator_denied(store, now, safe, "direct_delivery_not_allowed")
    if len(signals) != 1 or not isinstance(signals[0], dict):
        return _actuator_denied(store, now, safe, "invalid_script_result_count")
    result: dict[str, Any] = dict(signals[0])
    artifact = result.get("artifact") if isinstance(result.get("artifact"), dict) else {}
    artifact_kind = str(artifact.get("kind") or "").strip()
    if artifact_kind not in set(entry["output_contract"].get("artifact_kinds") or []):
        return _actuator_denied(store, now, safe, "artifact_kind_not_allowed")
    ref_path = str(artifact.get("ref_path") or "").strip()
    if not ref_path:
        return _actuator_denied(store, now, safe, "missing_artifact_ref")

    script_hash = _script_content_hash(script_path)
    artifact_result = store_artifact(
        store,
        kind=artifact_kind,
        ref_path=ref_path,
        provenance={
            "source": "actuator_script",
            "actuator": safe,
            "capability": entry.get("capability", safe),
            "config_hash": _hash_obj(entry),
            "script_hash": script_hash,
        },
        why_created=truncate_text(str(result.get("summary") or artifact.get("why_created") or "Actuator prepared artifact."), 300),
        intended_handoff_mode=str(artifact.get("intended_handoff_mode") or "present_thread"),
        delivery_state=str(artifact.get("delivery_state") or "prepared"),
        source_thread_id=compact_request["thread_id"],
        source_candidate_id=compact_request["candidate_id"],
        sensitivity=str(artifact.get("sensitivity") or "private"),
        allowed_surfaces=artifact.get("allowed_surfaces") if isinstance(artifact.get("allowed_surfaces"), list) else ["local"],
        config=config,
    )
    if not artifact_result.get("success"):
        return _actuator_denied(store, now, safe, str(artifact_result.get("error") or "artifact_record_failed"))
    artifact_data = artifact_result.get("data") if isinstance(artifact_result.get("data"), dict) else {}
    receipt = {
        "ts": now,
        "type": "actuator.prepared",
        "actuator": safe,
        "capability": entry.get("capability", safe),
        "request_type": request_type,
        "conscious_decision_ref": decision_ref,
        "thread_id": compact_request["thread_id"],
        "candidate_id": compact_request["candidate_id"],
        "artifact_id": artifact_data.get("id", ""),
        "artifact_kind": artifact_kind,
        "delivery_authorized": False,
        "outbound_delivery": False,
        "config_hash": _hash_obj(entry),
        "script_hash": script_hash,
        "duration_seconds": run.get("duration_seconds"),
    }
    store.append_jsonl("decisions", receipt)
    return {"success": True, "data": {"artifact": artifact_data, "receipt": receipt}, "receipt": receipt}


def _actuator_denied(store: SensoriumStore, now: str, name: str, reason: str, *, extra: dict | None = None) -> dict:
    receipt = {
        "ts": now,
        "type": "actuator.denied",
        "actuator": name,
        "blocked_reason": truncate_text(reason, 120),
        "delivery_authorized": False,
        "outbound_delivery": False,
    }
    if extra:
        receipt.update({k: v for k, v in extra.items() if k in {"duration_seconds", "exit_code"}})
    store.append_jsonl("decisions", receipt)
    return {"success": False, "error": reason, "receipt": receipt}
