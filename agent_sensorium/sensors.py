"""Deterministic compact sensor helpers — stdlib-only, no model calls.

Each helper returns a compact signal dict suitable for sensorium_ingest_signal.
Signals contain only metadata, summaries, refs, hashes, and sizes — never raw
file contents, full transcripts, or unbounded data.
"""

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .schemas import VALID_SENSITIVITIES, truncate_text

MAX_SUMMARY_CHARS = 200
MAX_REF_CHARS = 256


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        numeric = 0.5
    return max(lo, min(hi, numeric))


def _safe_list(values: list[str] | None, *, default: list[str], max_chars: int = MAX_REF_CHARS) -> list[str]:
    if not values:
        return list(default)
    sanitized = [truncate_text(v.strip(), max_chars) for v in values if isinstance(v, str) and v.strip()]
    return sanitized or list(default)


def _safe_sensitivity(value: str) -> str:
    return value if value in VALID_SENSITIVITIES else "private"


_SENSOR_STATUSES = {"active", "paused", "deprecated"}
_sensor_registry: dict[str, dict] = {}


def _safe_sensor_name(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.:-]+", "_", (name or "").strip()).strip("_")
    return cleaned[:80]


def sensor_registry_snapshot() -> dict:
    """Return deterministic runtime sensor registry state."""
    return {name: _sensor_registry[name] for name in sorted(_sensor_registry)}


def register_sensor_kind(
    name: str,
    *,
    defaults: dict | None = None,
    status: str = "active",
    store=None,
) -> dict:
    """Register/modify/pause/deprecate a deterministic sensor kind at runtime."""
    safe_name = _safe_sensor_name(name)
    if not safe_name:
        raise ValueError("sensor name required")
    if status not in _SENSOR_STATUSES:
        raise ValueError(f"invalid sensor status: {status}")
    existing = dict(_sensor_registry.get(safe_name, {}))
    merged_defaults = dict(existing.get("defaults") or {})
    if defaults:
        for key, value in defaults.items():
            if key in {"strength_hint", "sensitivity", "allowed_surfaces", "correlation_keys"}:
                merged_defaults[key] = value
    entry = {
        "name": safe_name,
        "status": status,
        "defaults": merged_defaults,
    }
    _sensor_registry[safe_name] = entry
    if store is not None:
        save_sensor_registry(store)
    return dict(entry)


def save_sensor_registry(store) -> dict:
    snapshot = sensor_registry_snapshot()
    store.write_sensor_registry(snapshot)
    return snapshot


def load_sensor_registry(store) -> dict:
    from .inner_life import BlockKind, InnerLifeValidationError, load_block_registry_v2

    _sensor_registry.clear()
    raw_registry = store.read_sensor_registry()
    try:
        registry = load_block_registry_v2(raw_registry)
    except InnerLifeValidationError:
        registry = {"blocks": {}}
    for name, entry in sorted(registry.get("blocks", {}).items()):
        if not isinstance(entry, dict) or entry.get("type") != BlockKind.SENSOR.value:
            continue
        safe_name = _safe_sensor_name(name)
        if safe_name:
            status = entry.get("status")
            if status not in _SENSOR_STATUSES:
                status = "active" if entry.get("enabled", True) else "paused"
            _sensor_registry[safe_name] = {
                "name": safe_name,
                "status": status,
                "defaults": dict(entry.get("defaults") or {}),
            }
    return sensor_registry_snapshot()


def _active_sensor_defaults(kind: str) -> dict:
    entry = _sensor_registry.get(kind)
    if not entry or entry.get("status") != "active":
        return {}
    defaults = entry.get("defaults")
    return dict(defaults) if isinstance(defaults, dict) else {}


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def apply_pressure_delta(candidate: dict, delta: float, *, reason: str = "") -> dict:
    updated = dict(candidate)
    updated["pressure"] = round(_clamp(float(updated.get("pressure", 0.0)) + float(delta)), 3)
    meta = dict(updated.get("pressure_meta") or {})
    meta["last_delta"] = round(float(delta), 3)
    meta["last_delta_reason"] = truncate_text(reason, 120)
    updated["pressure_meta"] = meta
    return updated


def mark_extinct(candidate: dict, *, reason: str = "") -> dict:
    updated = apply_pressure_delta(candidate, -1.0, reason=reason or "extinction")
    updated["extinct"] = True
    updated["extinct_at"] = _utc_now().isoformat().replace("+00:00", "Z")
    meta = dict(updated.get("pressure_meta") or {})
    meta["extinction_reason"] = truncate_text(reason, 160)
    updated["pressure_meta"] = meta
    return updated


def is_candidate_extinct(candidate: dict, *, now: str | None = None, silence_ttl_hours: float = 168.0) -> bool:
    if candidate.get("extinct"):
        return True
    outcome = (candidate.get("feedback_meta") or {}).get("outcome")
    if outcome in {"operator_rejected", "silence", "rejected"}:
        return True
    reference = _parse_iso(candidate.get("updated_at") or candidate.get("created_at"))
    current = _parse_iso(now) if now else _utc_now()
    if reference is None or current is None:
        return False
    return (current - reference).total_seconds() >= float(silence_ttl_hours) * 3600.0


def session_event_signal(
    *,
    kind: str,
    summary: str,
    session_ref: str = "",
    strength_hint: float | None = None,
    sensitivity: str = "private",
    allowed_surfaces: list[str] | None = None,
    correlation_keys: list[str] | None = None,
) -> dict:
    defaults = _active_sensor_defaults(kind)
    effective_strength = defaults.get("strength_hint", 0.5) if strength_hint is None else strength_hint
    effective_sensitivity = defaults.get("sensitivity", sensitivity)
    effective_surfaces = allowed_surfaces if allowed_surfaces is not None else defaults.get("allowed_surfaces")
    effective_keys = correlation_keys if correlation_keys is not None else defaults.get("correlation_keys")
    return {
        "sensor": "sensorium.session_event",
        "source": "hermes_session",
        "kind": kind,
        "summary": truncate_text(summary, MAX_SUMMARY_CHARS),
        "session_ref": truncate_text(session_ref, MAX_REF_CHARS),
        "strength_hint": _clamp(effective_strength),
        "sensitivity": _safe_sensitivity(effective_sensitivity),
        "allowed_surfaces": _safe_list(effective_surfaces, default=["local"]),
        "correlation_keys": _safe_list(effective_keys, default=[]),
    }


def artifact_signal(
    *,
    path: str,
    summary: str,
    size: int | None = None,
    content_hash: str = "",
    artifact_ref: str = "",
    kind: str = "artifact_created",
    strength_hint: float = 0.6,
    sensitivity: str = "private",
    allowed_surfaces: list[str] | None = None,
    correlation_keys: list[str] | None = None,
) -> dict:
    return {
        "sensor": "sensorium.artifact",
        "source": "artifact",
        "kind": kind,
        "summary": truncate_text(summary, MAX_SUMMARY_CHARS),
        "path": truncate_text(path, MAX_REF_CHARS),
        "artifact_ref": truncate_text(artifact_ref, MAX_REF_CHARS),
        "size": size,
        "content_hash": content_hash[:64] if content_hash else "",
        "strength_hint": _clamp(strength_hint),
        "sensitivity": _safe_sensitivity(sensitivity),
        "allowed_surfaces": _safe_list(allowed_surfaces, default=["local"]),
        "correlation_keys": _safe_list(correlation_keys, default=[]),
    }


def operator_signal(
    *,
    summary: str,
    kind: str = "user_correction",
    strength_hint: float = 0.8,
    sensitivity: str = "private",
    allowed_surfaces: list[str] | None = None,
    correlation_keys: list[str] | None = None,
    source_ref: str = "",
) -> dict:
    return {
        "sensor": "sensorium.explicit_operator",
        "source": "manual",
        "kind": kind,
        "summary": truncate_text(summary, MAX_SUMMARY_CHARS),
        "actor": "operator",
        "strength_hint": _clamp(strength_hint),
        "sensitivity": _safe_sensitivity(sensitivity),
        "allowed_surfaces": _safe_list(allowed_surfaces, default=["local"]),
        "correlation_keys": _safe_list(correlation_keys, default=[]),
        "source_ref": truncate_text(source_ref, MAX_REF_CHARS),
    }


def runtime_heartbeat_sample(*, store) -> dict:
    """Compact deterministic runtime / state-dir health snapshot.

    Counts and names only — never file contents, paths, or transcripts.
    Safe on a brand-new profile (all zeros). No model calls.
    """
    try:
        state_dir_exists = store.root.is_dir()
    except OSError:
        state_dir_exists = False

    counts: dict[str, int] = {}
    for name in ("signals", "events", "candidates", "threads"):
        try:
            counts[name] = len(store.read_jsonl(name))
        except Exception:
            counts[name] = 0

    try:
        threads = store.read_jsonl("threads")
        pending_threads = sum(1 for t in threads if isinstance(t, dict) and t.get("status") == "dormant")
    except Exception:
        pending_threads = 0

    active: list[str] = []
    paused: list[str] = []
    deprecated: list[str] = []
    snapshot: dict = {}
    try:
        load_sensor_registry(store)
        snapshot = sensor_registry_snapshot()
        for entry in snapshot.values():
            status = entry.get("status")
            if status == "paused":
                paused.append(entry["name"])
            elif status == "deprecated":
                deprecated.append(entry["name"])
            else:
                active.append(entry["name"])
    except Exception:
        snapshot = {}
    registry = {
        "sensor_count": len(snapshot),
        "active": sorted(active),
        "paused": sorted(paused),
        "deprecated": sorted(deprecated),
    }

    last_decision_age_seconds: float | None = None
    try:
        decisions = store.read_jsonl("decisions")
        if decisions:
            last_ts = _parse_iso(decisions[-1].get("ts"))
            if last_ts is not None:
                last_decision_age_seconds = round((_utc_now() - last_ts).total_seconds(), 1)
    except Exception:
        last_decision_age_seconds = None

    return {
        "state_dir_exists": state_dir_exists,
        "counts": counts,
        "pending_threads": pending_threads,
        "registry": registry,
        "last_decision_age_seconds": last_decision_age_seconds,
    }


def build_runtime_heartbeat_signal(sample: dict, *, instance: str = "default") -> dict:
    """Build a low-strength compact heartbeat signal from a sample.

    Always emits (it is a status beacon). Low strength so it is recorded but not
    promoted into candidates/threads. local_only, ["local"] surfaces.
    """
    counts = sample.get("counts") or {}
    registry = sample.get("registry") or {}
    sig = int(counts.get("signals", 0) or 0)
    n = int(registry.get("sensor_count", 0) or 0)
    pending = int(sample.get("pending_threads", 0) or 0)
    return {
        "sensor": "sensorium.runtime_heartbeat",
        "source": "machine",
        "kind": "runtime_heartbeat",
        "summary": truncate_text(
            f"runtime heartbeat: {sig} signals, {n} sensors, {pending} open threads",
            MAX_SUMMARY_CHARS,
        ),
        "actor": "tool",
        "strength_hint": 0.2,
        "sensitivity": "local_only",
        "allowed_surfaces": ["local"],
        "correlation_keys": ["runtime-heartbeat"],
        "scope": "global",
        "values": {
            "state_dir_exists": sample.get("state_dir_exists"),
            "counts": counts,
            "pending_threads": pending,
            "sensor_count": n,
            "active_sensors": registry.get("active", []),
            "last_decision_age_seconds": sample.get("last_decision_age_seconds"),
        },
    }


BODY_PRESSURE_DEFAULT_CONFIG = {
    "mem_degraded_pct": 10.0,
    "mem_critical_pct": 5.0,
    "swap_degraded_pct": 20.0,
    "swap_critical_pct": 50.0,
    "load_degraded_per_cpu": 1.5,
    "load_critical_per_cpu": 2.5,
    "psi_cpu_degraded_avg10": 50.0,
    "psi_cpu_critical_avg10": 80.0,
    "psi_memory_degraded_avg10": 5.0,
    "psi_memory_critical_avg10": 20.0,
    "psi_io_degraded_avg10": 10.0,
    "psi_io_critical_avg10": 30.0,
    "disk_used_degraded_pct": 90.0,
    "disk_used_critical_pct": 97.0,
    "degraded_samples": 3,
    "critical_samples": 2,
    "recovery_samples": 5,
    "sustained_samples": 60,
}

_LEVEL_RANK = {"healthy": 0, "degraded": 1, "critical": 2}


def _body_config(config: dict | None = None) -> dict:
    merged = dict(BODY_PRESSURE_DEFAULT_CONFIG)
    if config:
        for key, value in config.items():
            if key in merged and isinstance(value, (int, float)) and value > 0:
                merged[key] = value
    return merged


def _read_text(path: Path) -> str:
    try:
        return path.read_text(errors="ignore")
    except OSError:
        return ""


def _round_float(value: float | int | None, digits: int = 3) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _parse_meminfo(text: str) -> dict:
    data: dict[str, float] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, rest = line.split(":", 1)
        parts = rest.strip().split()
        if not parts:
            continue
        try:
            data[key] = float(parts[0])
        except ValueError:
            continue
    result: dict[str, float] = {}
    total = data.get("MemTotal")
    available = data.get("MemAvailable")
    if total and available is not None:
        result["mem_available_pct"] = round(available / total * 100.0, 3)
    swap_total = data.get("SwapTotal")
    swap_free = data.get("SwapFree")
    if swap_total and swap_free is not None:
        result["swap_used_pct"] = round((swap_total - swap_free) / swap_total * 100.0, 3)
    return result


def _parse_loadavg(text: str) -> dict:
    parts = text.strip().split()
    if not parts:
        return {}
    try:
        load1 = float(parts[0])
    except ValueError:
        return {}
    cpus = os.cpu_count() or 1
    return {"load1": round(load1, 3), "load_per_cpu": round(load1 / cpus, 3)}


def _parse_psi(text: str, prefix: str) -> dict:
    result: dict[str, float] = {}
    for line in text.splitlines():
        if not line.startswith("some "):
            continue
        for key in ("avg10", "avg60", "avg300"):
            m = re.search(rf"\b{key}=([0-9.]+)", line)
            if m:
                result[f"psi_{prefix}_some_{key}"] = round(float(m.group(1)), 3)
        break
    return result


def wsl_disk_paths(*, mount_root: str = "/mnt") -> list[str]:
    """Return cheap disk paths for WSL root plus mounted Windows drives.

    WSL's ext4 filesystem commonly lives inside a VHDX on the Windows C: drive.
    Sampling only `/` sees ext4 fullness, but not host-drive pressure around the
    VHDX. Mounted drive roots (`/mnt/c`, `/mnt/d`, ...) are cheap statvfs targets
    and expose Windows-side free-space pressure without native Windows probes.
    """
    paths = ["/"]
    root = Path(mount_root)
    try:
        children = sorted(root.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return paths
    for child in children:
        name = child.name.lower()
        if len(name) == 1 and "a" <= name <= "z" and child.is_dir():
            paths.append(str(child))
    return paths


def _disk_pressure(paths: list[str]) -> dict:
    worst_used: float | None = None
    worst_inode_used: float | None = None
    for raw in paths:
        try:
            st = os.statvfs(raw)
        except OSError:
            continue
        if st.f_blocks:
            used = (st.f_blocks - st.f_bavail) / st.f_blocks * 100.0
            worst_used = used if worst_used is None else max(worst_used, used)
        if st.f_files:
            inode_used = (st.f_files - st.f_favail) / st.f_files * 100.0
            worst_inode_used = inode_used if worst_inode_used is None else max(worst_inode_used, inode_used)
    result: dict[str, float] = {}
    if worst_used is not None:
        result["disk_used_pct"] = round(worst_used, 3)
    if worst_inode_used is not None:
        result["inode_used_pct"] = round(worst_inode_used, 3)
    return result


def machine_body_pressure_sample(
    *,
    proc_root: str = "/proc",
    disk_paths: list[str] | None = None,
) -> dict:
    """Collect a cheap present-tense body-pressure sample.

    Uses procfs and statvfs only. It deliberately avoids process lists,
    command lines, profilers, raw logs, and unbounded command output.
    """
    root = Path(proc_root)
    sample: dict = {}
    sample.update(_parse_loadavg(_read_text(root / "loadavg")))
    sample.update(_parse_meminfo(_read_text(root / "meminfo")))
    for family in ("cpu", "memory", "io"):
        sample.update(_parse_psi(_read_text(root / "pressure" / family), family))
    sample.update(_disk_pressure(disk_paths or wsl_disk_paths()))
    return sample


def _pressure_reasons(sample: dict, cfg: dict) -> list[dict]:
    reasons: list[dict] = []

    def add_if(metric: str, family: str, degraded_cmp, critical_cmp, degraded_threshold, critical_threshold):
        value = sample.get(metric)
        if value is None:
            return
        level = "healthy"
        threshold = None
        if critical_cmp(value, critical_threshold):
            level = "critical"
            threshold = critical_threshold
        elif degraded_cmp(value, degraded_threshold):
            level = "degraded"
            threshold = degraded_threshold
        if level != "healthy":
            reasons.append(
                {
                    "metric": metric,
                    "family": family,
                    "level": level,
                    "value": _round_float(value),
                    "threshold": threshold,
                }
            )

    def less(value, threshold):
        return value < threshold

    def greater(value, threshold):
        return value >= threshold
    add_if("mem_available_pct", "memory", less, less, cfg["mem_degraded_pct"], cfg["mem_critical_pct"])
    add_if("swap_used_pct", "memory", greater, greater, cfg["swap_degraded_pct"], cfg["swap_critical_pct"])
    add_if("load_per_cpu", "cpu", greater, greater, cfg["load_degraded_per_cpu"], cfg["load_critical_per_cpu"])
    add_if("psi_cpu_some_avg10", "cpu", greater, greater, cfg["psi_cpu_degraded_avg10"], cfg["psi_cpu_critical_avg10"])
    add_if("psi_memory_some_avg10", "memory", greater, greater, cfg["psi_memory_degraded_avg10"], cfg["psi_memory_critical_avg10"])
    add_if("psi_io_some_avg10", "io", greater, greater, cfg["psi_io_degraded_avg10"], cfg["psi_io_critical_avg10"])
    add_if("disk_used_pct", "disk", greater, greater, cfg["disk_used_degraded_pct"], cfg["disk_used_critical_pct"])
    add_if("inode_used_pct", "disk", greater, greater, cfg["disk_used_degraded_pct"], cfg["disk_used_critical_pct"])
    return reasons


def _worst_pressure(sample: dict, cfg: dict) -> tuple[str, dict | None]:
    reasons = _pressure_reasons(sample, cfg)
    if not reasons:
        return "healthy", None
    reasons.sort(key=lambda item: (_LEVEL_RANK[item["level"]], item["family"], item["metric"]), reverse=True)
    return reasons[0]["level"], reasons[0]


def _safe_values(sample: dict) -> dict:
    allowed_prefixes = ("mem_", "swap_", "load", "psi_", "disk_", "inode_")
    out: dict[str, float] = {}
    for key, value in sample.items():
        if key.startswith(allowed_prefixes):
            rounded = _round_float(value)
            if rounded is not None:
                out[key] = rounded
    return out


def _body_pressure_signal(
    *,
    transition: str,
    level: str,
    previous_level: str,
    reason: dict | None,
    sample: dict,
    window_samples: int,
) -> dict:
    metric = (reason or {}).get("metric", "body")
    family = (reason or {}).get("family", "machine")
    threshold = (reason or {}).get("threshold")
    value = (reason or {}).get("value")
    summary = f"Machine body pressure {transition}"
    if reason:
        summary += f": {metric}={value} threshold={threshold}"
    strength = 0.95 if level == "critical" else 0.8
    if level == "healthy":
        strength = 0.75
    if transition.startswith("sustained_"):
        strength = 0.78
    return {
        "sensor": "sensorium.machine_body_pressure",
        "source": "machine",
        "kind": "body_pressure",
        "summary": truncate_text(summary, MAX_SUMMARY_CHARS),
        "actor": "tool",
        "strength_hint": strength,
        "sensitivity": "local_only",
        "allowed_surfaces": ["local"],
        "correlation_keys": ["machine-body-pressure", f"body:{family}"],
        "scope": "global",
        "metric_family": family,
        "pressure_level": level,
        "previous_level": previous_level,
        "transition": transition,
        "values": _safe_values(sample),
        "threshold": f"{metric} threshold {threshold}" if threshold is not None else "",
        "window": {"samples": window_samples},
    }


def classify_machine_body_pressure(
    sample: dict,
    *,
    state: dict | None = None,
    config: dict | None = None,
) -> tuple[dict | None, dict]:
    """Classify one present-tense body sample and maybe emit one signal.

    The function keeps only caller-provided tiny rolling state. It does not read
    history, logs, transcripts, process command lines, or long-horizon stores.
    """
    cfg = _body_config(config)
    st = dict(state or {})
    st.setdefault("level", "healthy")
    st.setdefault("pending_level", None)
    st.setdefault("pending_count", 0)
    st.setdefault("healthy_count", 0)
    st.setdefault("samples_since_emit", 0)

    observed_level, reason = _worst_pressure(sample, cfg)
    current_level = st["level"]
    st["last_sample"] = _safe_values(sample)
    st["last_observed_level"] = observed_level
    st["samples_since_emit"] += 1

    signal = None
    if observed_level == current_level:
        st["pending_level"] = None
        st["pending_count"] = 0
        st["healthy_count"] = 0
        if current_level != "healthy" and st["samples_since_emit"] >= int(cfg["sustained_samples"]):
            signal = _body_pressure_signal(
                transition=f"sustained_{current_level}",
                level=current_level,
                previous_level=current_level,
                reason=reason,
                sample=sample,
                window_samples=int(st["samples_since_emit"]),
            )
            st["samples_since_emit"] = 0
        return signal, st

    if observed_level == "healthy":
        st["healthy_count"] += 1
        st["pending_level"] = None
        st["pending_count"] = 0
        if st["healthy_count"] >= int(cfg["recovery_samples"]):
            previous = current_level
            st["level"] = "healthy"
            st["healthy_count"] = 0
            st["samples_since_emit"] = 0
            signal = _body_pressure_signal(
                transition=f"{previous}_to_recovered",
                level="healthy",
                previous_level=previous,
                reason=None,
                sample=sample,
                window_samples=int(cfg["recovery_samples"]),
            )
        return signal, st

    if _LEVEL_RANK[observed_level] > _LEVEL_RANK[current_level]:
        if st.get("pending_level") == observed_level:
            st["pending_count"] += 1
        else:
            st["pending_level"] = observed_level
            st["pending_count"] = 1
        needed = int(cfg["critical_samples"] if observed_level == "critical" else cfg["degraded_samples"])
        if st["pending_count"] >= needed:
            previous = current_level
            st["level"] = observed_level
            st["pending_level"] = None
            st["pending_count"] = 0
            st["healthy_count"] = 0
            st["samples_since_emit"] = 0
            signal = _body_pressure_signal(
                transition=f"{previous}_to_{observed_level}",
                level=observed_level,
                previous_level=previous,
                reason=reason,
                sample=sample,
                window_samples=needed,
            )
        return signal, st

    # Improvement from critical to degraded is not full recovery; require the
    # degraded state to persist before emitting a downshift transition.
    if st.get("pending_level") == observed_level:
        st["pending_count"] += 1
    else:
        st["pending_level"] = observed_level
        st["pending_count"] = 1
    if st["pending_count"] >= int(cfg["degraded_samples"]):
        previous = current_level
        st["level"] = observed_level
        st["pending_level"] = None
        st["pending_count"] = 0
        st["samples_since_emit"] = 0
        signal = _body_pressure_signal(
            transition=f"{previous}_to_{observed_level}",
            level=observed_level,
            previous_level=previous,
            reason=reason,
            sample=sample,
            window_samples=int(cfg["degraded_samples"]),
        )
    return signal, st


def replay_machine_body_pressure(samples: list[dict], *, config: dict | None = None) -> list[dict]:
    """Replay sampled metric fixtures through the online classifier.

    This is for tests/audits only; runtime sensors should call the classifier
    with the current sample and tiny rolling state.
    """
    state: dict = {}
    signals: list[dict] = []
    for sample in samples:
        sig, state = classify_machine_body_pressure(sample, state=state, config=config)
        if sig is not None:
            signals.append(sig)
    return signals


def _transition_pressure_signal(
    *,
    sensor: str,
    source: str,
    kind: str,
    level: str,
    previous_level: str,
    metric_family: str,
    reason: str,
    values: dict,
    correlation_key: str,
) -> dict:
    transition = f"{previous_level}_to_{level}" if level != "healthy" else f"{previous_level}_to_recovered"
    summary = f"{kind.replace('_', ' ')} {transition}"
    if reason:
        summary += f": {reason}"
    strength = 0.95 if level == "critical" else 0.8
    if level == "healthy":
        strength = 0.72
    return {
        "sensor": sensor,
        "source": source,
        "kind": kind,
        "summary": truncate_text(summary, MAX_SUMMARY_CHARS),
        "actor": "tool",
        "strength_hint": strength,
        "sensitivity": "local_only",
        "allowed_surfaces": ["local"],
        "correlation_keys": [correlation_key, f"{correlation_key}:{metric_family}"],
        "scope": "global",
        "metric_family": metric_family,
        "pressure_level": level,
        "previous_level": previous_level,
        "transition": transition,
        "values": values,
    }


def _transition_classifier(
    sample: dict,
    *,
    state: dict | None,
    observed_level: str,
    metric_family: str,
    reason: str,
    values: dict,
    sensor: str,
    source: str,
    kind: str,
    correlation_key: str,
) -> tuple[dict | None, dict]:
    st = dict(state or {})
    previous = st.get("level", "healthy")
    st["level"] = observed_level
    st["last_sample"] = dict(values)
    signal = None
    if observed_level != previous:
        signal = _transition_pressure_signal(
            sensor=sensor,
            source=source,
            kind=kind,
            level=observed_level,
            previous_level=previous,
            metric_family=metric_family,
            reason=reason,
            values=values,
            correlation_key=correlation_key,
        )
    return signal, st


CODEX_USAGE_DEFAULT_CONFIG = {
    "primary_over_expected_degraded_pp": 10.0,
    "primary_over_expected_critical_pp": 25.0,
    "weekly_over_expected_degraded_pp": 5.0,
    "weekly_over_expected_critical_pp": 15.0,
    "reset_near_seconds": 3600,
}


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _window_pace_points(*, used_percent, reset_after_seconds, window_seconds) -> tuple[float, float, float] | None:
    window = _safe_float(window_seconds, -1.0)
    if window <= 0:
        return None
    reset_after = max(0.0, min(window, _safe_float(reset_after_seconds)))
    used = _safe_float(used_percent)
    elapsed_percent = ((window - reset_after) / window) * 100.0
    over_expected_pp = used - elapsed_percent
    return round(used, 3), round(elapsed_percent, 3), round(over_expected_pp, 3)


def codex_usage_sample(
    *,
    probe_path: str | None = None,
    timeout_seconds: int = 45,
) -> dict:
    """Read the existing Codex/OpenAI subscription usage probe.

    This is deliberately a tiny adapter around the already-audited read-only
    probe. It stores no tokens/account identifiers and returns only compact
    budget/window metadata for Sensorium classification.
    """
    path = Path(probe_path or (Path.home() / ".hermes" / "scripts" / "subscription_usage_probe.py"))
    if not path.exists():
        return {"available": False, "error": "usage_probe_missing", "probe_path": str(path)}
    try:
        proc = subprocess.run(
            [sys.executable, str(path), "--json"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return {"available": False, "error": "usage_probe_timeout", "probe_path": str(path)}
    except Exception as exc:
        return {"available": False, "error": f"usage_probe_error:{type(exc).__name__}", "probe_path": str(path)}
    if proc.returncode != 0:
        return {"available": False, "error": "usage_probe_failed", "probe_path": str(path)}
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"available": False, "error": "usage_probe_bad_json", "probe_path": str(path)}
    codex = data.get("codex_openai") if isinstance(data, dict) else None
    if not isinstance(codex, dict):
        return {"available": False, "error": "codex_payload_missing", "probe_path": str(path)}
    return codex_usage_compact_sample(codex, generated_at=data.get("generated_at"))


def codex_usage_compact_sample(codex: dict, *, generated_at: str | None = None) -> dict:
    """Sanitize a Codex usage payload down to pressure-relevant fields."""
    raw_main = codex.get("main_rate_limit")
    main = raw_main if isinstance(raw_main, dict) else {}
    raw_primary = main.get("primary")
    primary = raw_primary if isinstance(raw_primary, dict) else {}
    raw_secondary = main.get("secondary")
    secondary = raw_secondary if isinstance(raw_secondary, dict) else {}
    extras: list[dict] = []
    for item in codex.get("additional_rate_limits") or []:
        if not isinstance(item, dict):
            continue
        raw_rl = item.get("rate_limit")
        rl = raw_rl if isinstance(raw_rl, dict) else {}
        raw_p = rl.get("primary")
        p = raw_p if isinstance(raw_p, dict) else {}
        raw_s = rl.get("secondary")
        s = raw_s if isinstance(raw_s, dict) else {}
        extras.append({
            "limit_name": truncate_text(str(item.get("limit_name") or ""), 80),
            "metered_feature": truncate_text(str(item.get("metered_feature") or ""), 80),
            "allowed": rl.get("allowed"),
            "limit_reached": rl.get("limit_reached"),
            "primary_used_percent": p.get("used_percent"),
            "primary_reset_after_seconds": p.get("reset_after_seconds"),
            "primary_window_seconds": p.get("window_seconds"),
            "weekly_used_percent": s.get("used_percent"),
            "weekly_reset_after_seconds": s.get("reset_after_seconds"),
            "weekly_window_seconds": s.get("window_seconds"),
        })
    return {
        "available": bool(codex.get("available")),
        "generated_at": truncate_text(str(generated_at or ""), 64),
        "plan_type": truncate_text(str(codex.get("plan_type") or ""), 80),
        "allowed": main.get("allowed"),
        "limit_reached": main.get("limit_reached"),
        "primary_used_percent": primary.get("used_percent"),
        "primary_reset_after_seconds": primary.get("reset_after_seconds"),
        "primary_window_seconds": primary.get("window_seconds"),
        "weekly_used_percent": secondary.get("used_percent"),
        "weekly_reset_after_seconds": secondary.get("reset_after_seconds"),
        "weekly_window_seconds": secondary.get("window_seconds"),
        "additional_limits": extras[:8],
        "error": truncate_text(str(codex.get("error") or ""), 120),
    }


def classify_codex_usage_pressure(
    sample: dict,
    *,
    state: dict | None = None,
    config: dict | None = None,
) -> tuple[dict | None, dict]:
    """Classify Codex/OpenAI reservoir pressure from compact usage metadata."""
    cfg = dict(CODEX_USAGE_DEFAULT_CONFIG)
    if config:
        cfg.update({k: v for k, v in config.items() if k in cfg and isinstance(v, (int, float))})
    available = bool(sample.get("available"))
    allowed = sample.get("allowed")
    limit_reached = bool(sample.get("limit_reached"))
    primary_used = _safe_float(sample.get("primary_used_percent"))
    weekly_used = _safe_float(sample.get("weekly_used_percent"))
    reset_after = _safe_int(sample.get("primary_reset_after_seconds"))
    weekly_reset_after = _safe_int(sample.get("weekly_reset_after_seconds"))
    primary_window = _safe_int(sample.get("primary_window_seconds"))
    weekly_window = _safe_int(sample.get("weekly_window_seconds"))
    primary_pace = _window_pace_points(
        used_percent=primary_used,
        reset_after_seconds=reset_after,
        window_seconds=primary_window,
    )
    weekly_pace = _window_pace_points(
        used_percent=weekly_used,
        reset_after_seconds=weekly_reset_after,
        window_seconds=weekly_window,
    )
    primary_elapsed = primary_pace[1] if primary_pace else 0.0
    primary_over = primary_pace[2] if primary_pace else primary_used
    weekly_elapsed = weekly_pace[1] if weekly_pace else 0.0
    weekly_over = weekly_pace[2] if weekly_pace else weekly_used
    reset_near = bool(reset_after and reset_after <= int(cfg["reset_near_seconds"]))
    level = "healthy"
    family = "codex_usage"
    reason = ""
    if not available:
        level = "critical"
        family = "availability"
        reason = sample.get("error") or "codex usage unavailable"
    elif allowed is False or limit_reached:
        level = "critical"
        family = "quota"
        reason = "codex main limit reached"
    elif weekly_over >= float(cfg["weekly_over_expected_critical_pp"]):
        level = "critical"
        family = "weekly_pace"
        reason = f"weekly_over_expected={weekly_over:.0f}pp used={weekly_used:.0f}% elapsed={weekly_elapsed:.0f}%"
    elif primary_over >= float(cfg["primary_over_expected_critical_pp"]):
        level = "critical"
        family = "primary_pace"
        reason = f"primary_over_expected={primary_over:.0f}pp used={primary_used:.0f}% elapsed={primary_elapsed:.0f}%"
    elif weekly_over >= float(cfg["weekly_over_expected_degraded_pp"]):
        level = "degraded"
        family = "weekly_pace"
        reason = f"weekly_over_expected={weekly_over:.0f}pp used={weekly_used:.0f}% elapsed={weekly_elapsed:.0f}%"
    elif primary_over >= float(cfg["primary_over_expected_degraded_pp"]):
        level = "degraded"
        family = "primary_pace"
        reason = f"primary_over_expected={primary_over:.0f}pp used={primary_used:.0f}% elapsed={primary_elapsed:.0f}%"

    values = {
        "provider": "codex_openai",
        "plan_type": sample.get("plan_type") or "",
        "primary_used_percent": primary_used,
        "primary_reset_after_seconds": reset_after,
        "primary_window_seconds": primary_window,
        "primary_elapsed_percent": primary_elapsed,
        "primary_over_expected_pp": primary_over,
        "weekly_used_percent": weekly_used,
        "weekly_reset_after_seconds": weekly_reset_after,
        "weekly_window_seconds": weekly_window,
        "weekly_elapsed_percent": weekly_elapsed,
        "weekly_over_expected_pp": weekly_over,
        "reset_near": reset_near,
        "additional_limit_count": len(sample.get("additional_limits") or []),
    }
    sig, next_state = _transition_classifier(
        sample,
        state=state,
        observed_level=level,
        metric_family=family,
        reason=reason,
        values=values,
        sensor="sensorium.codex_usage_pressure",
        source="provider_budget",
        kind="inference_budget_pressure",
        correlation_key="codex-openai-energy",
    )
    next_state["last_generated_at"] = sample.get("generated_at") or ""
    next_state["last_values"] = values
    return sig, next_state


_TCP_STATE_NAMES = {
    "01": "ESTABLISHED",
    "02": "SYN_SENT",
    "03": "SYN_RECV",
    "04": "FIN_WAIT1",
    "05": "FIN_WAIT2",
    "06": "TIME_WAIT",
    "07": "CLOSE",
    "08": "CLOSE_WAIT",
    "09": "LAST_ACK",
    "0A": "LISTEN",
    "0B": "CLOSING",
}


def _parse_tcp_state_counts(text: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 4:
            continue
        state = _TCP_STATE_NAMES.get(parts[3], parts[3])
        counts[state] = counts.get(state, 0) + 1
    return counts


def machine_network_pressure_sample(*, proc_root: str = "/proc") -> dict:
    """Collect network counters without packet contents or endpoint addresses."""
    root = Path(proc_root)
    interfaces: list[str] = []
    rx_errors = tx_errors = rx_drops = tx_drops = 0
    for line in _read_text(root / "net" / "dev").splitlines()[2:]:
        if ":" not in line:
            continue
        iface, rest = line.split(":", 1)
        name = iface.strip()
        values = rest.split()
        if len(values) < 12:
            continue
        interfaces.append(name)
        try:
            rx_errors += int(values[2])
            rx_drops += int(values[3])
            tx_errors += int(values[10])
            tx_drops += int(values[11])
        except ValueError:
            continue
    tcp_states: dict[str, int] = {}
    for rel in ("tcp", "tcp6"):
        for state, count in _parse_tcp_state_counts(_read_text(root / "net" / rel)).items():
            tcp_states[state] = tcp_states.get(state, 0) + count
    non_loopback = [i for i in interfaces if i != "lo" and not i.startswith("loopback")]
    return {
        "interfaces": sorted(interfaces),
        "non_loopback_interfaces": len(non_loopback),
        "rx_errors": rx_errors,
        "tx_errors": tx_errors,
        "rx_drops": rx_drops,
        "tx_drops": tx_drops,
        "tcp_states": tcp_states,
    }


def classify_machine_network_pressure(sample: dict, *, state: dict | None = None, config: dict | None = None) -> tuple[dict | None, dict]:
    cfg = {"close_wait_degraded": 20, "syn_degraded": 20, "error_delta_degraded": 1}
    if config:
        cfg.update({k: v for k, v in config.items() if k in cfg and isinstance(v, (int, float))})
    st = dict(state or {})
    errors = int(sample.get("rx_errors", 0) or 0) + int(sample.get("tx_errors", 0) or 0)
    drops = int(sample.get("rx_drops", 0) or 0) + int(sample.get("tx_drops", 0) or 0)
    tcp = sample.get("tcp_states", {}) if isinstance(sample.get("tcp_states"), dict) else {}
    syn = int(tcp.get("SYN_SENT", 0) or 0) + int(tcp.get("SYN_RECV", 0) or 0)
    close_wait = int(tcp.get("CLOSE_WAIT", 0) or 0)
    level = "healthy"
    family = "network"
    reason = ""
    if int(sample.get("non_loopback_interfaces", 0) or 0) == 0:
        level = "critical"
        family = "interface"
        reason = "no non-loopback interfaces"
    elif close_wait >= cfg["close_wait_degraded"]:
        level = "degraded"
        family = "tcp"
        reason = f"CLOSE_WAIT={close_wait}"
    elif syn >= cfg["syn_degraded"]:
        level = "degraded"
        family = "tcp"
        reason = f"SYN backlog={syn}"
    elif "last_error_total" in st and (errors - int(st.get("last_error_total", 0))) >= cfg["error_delta_degraded"]:
        level = "degraded"
        family = "errors"
        reason = f"network errors increased to {errors}"
    elif "last_drop_total" in st and (drops - int(st.get("last_drop_total", 0))) >= cfg["error_delta_degraded"]:
        level = "degraded"
        family = "drops"
        reason = f"network drops increased to {drops}"
    values = {
        "interface_count": len(sample.get("interfaces", []) or []),
        "non_loopback_interfaces": int(sample.get("non_loopback_interfaces", 0) or 0),
        "error_total": errors,
        "drop_total": drops,
        "tcp_close_wait": close_wait,
        "tcp_syn": syn,
    }
    sig, next_state = _transition_classifier(sample, state=st, observed_level=level, metric_family=family, reason=reason, values=values, sensor="sensorium.machine_network_pressure", source="machine", kind="network_pressure", correlation_key="machine-network-pressure")
    next_state["last_error_total"] = errors
    next_state["last_drop_total"] = drops
    return sig, next_state


def machine_process_pressure_sample(*, proc_root: str = "/proc") -> dict:
    """Count process states from /proc/[pid]/stat without command lines."""
    root = Path(proc_root)
    counts: dict[str, int] = {}
    total = 0
    try:
        children = list(root.iterdir())
    except OSError:
        children = []
    for child in children:
        if not child.name.isdigit():
            continue
        text = _read_text(child / "stat")
        if not text:
            continue
        try:
            after = text.rsplit(")", 1)[1].strip().split()
            state = after[0]
        except (IndexError, ValueError):
            continue
        counts[state] = counts.get(state, 0) + 1
        total += 1
    return {
        "process_count": total,
        "zombie_count": counts.get("Z", 0),
        "uninterruptible_count": counts.get("D", 0),
        "state_counts": counts,
    }


def classify_machine_process_pressure(sample: dict, *, state: dict | None = None, config: dict | None = None) -> tuple[dict | None, dict]:
    cfg = {"zombie_degraded": 1, "zombie_critical": 10, "d_degraded": 1, "d_critical": 5}
    if config:
        cfg.update({k: v for k, v in config.items() if k in cfg and isinstance(v, (int, float))})
    zombies = int(sample.get("zombie_count", 0) or 0)
    d_count = int(sample.get("uninterruptible_count", 0) or 0)
    level = "healthy"
    family = "process"
    reason = ""
    if zombies >= cfg["zombie_critical"]:
        level = "critical"
        family = "zombie"
        reason = f"zombies={zombies}"
    elif d_count >= cfg["d_critical"]:
        level = "critical"
        family = "uninterruptible"
        reason = f"D-state={d_count}"
    elif zombies >= cfg["zombie_degraded"]:
        level = "degraded"
        family = "zombie"
        reason = f"zombies={zombies}"
    elif d_count >= cfg["d_degraded"]:
        level = "degraded"
        family = "uninterruptible"
        reason = f"D-state={d_count}"
    values = {"process_count": int(sample.get("process_count", 0) or 0), "zombie_count": zombies, "uninterruptible_count": d_count}
    return _transition_classifier(sample, state=state, observed_level=level, metric_family=family, reason=reason, values=values, sensor="sensorium.machine_process_pressure", source="machine", kind="process_pressure", correlation_key="machine-process-pressure")


# The local TTS sidecar is an optional, operator-configured extension. No
# private checkout path is baked in: the probe is dormant until a deployment
# supplies a control command / pid file via the instance config `tts` block
# (tts.control_command / tts.pid_file / tts.sidecar_base) or the matching env
# vars. When unconfigured, the recovery checklist degrades to a generic hint.
def _resolve_tts_sidecar(config: dict | None = None) -> dict:
    tts = config.get("tts") if isinstance(config, dict) else None
    tts = tts if isinstance(tts, dict) else {}
    control = tts.get("control_command") or os.environ.get("SENSORIUM_TTS_CONTROL_COMMAND")
    pid_file = tts.get("pid_file") or os.environ.get("SENSORIUM_TTS_PID_FILE")
    base = tts.get("sidecar_base") or os.environ.get("SENSORIUM_TTS_SIDECAR_BASE")
    if base:
        base_path = Path(base).expanduser()
        if not control:
            control = str(base_path / "chatterbox-turbo-sidecar")
        if not pid_file:
            pid_file = str(base_path / "artifacts" / "sidecar" / "chatterbox-turbo-sidecar.pid")
    return {"control_command": control, "pid_file": pid_file, "sidecar_base": base}


def _tts_sidecar_recovery_checklist(control_command: str | None = None) -> list[str]:
    ctl = control_command or "<configure tts.control_command for your local TTS sidecar>"
    return [
        f"{ctl} health",
        f"{ctl} stop || true",
        f"{ctl} start",
        f"{ctl} health",
        f"{ctl} test /tmp/sensorium-tts-probe.mp3",
        "retry the intended voice memo only if the probe succeeds",
    ]


TTS_SIDECAR_VERIFICATION_CRITERIA = [
    "health returns JSON from the configured TTS health URL",
    "short TTS probe writes playable non-empty audio",
    "no automatic repeated restart loop was started",
]

_TTS_TIMEOUT_PATTERNS = (
    ("auto_voice_tts_failed", re.compile(r"auto voice reply tts failed", re.IGNORECASE)),
    (
        "tts_timeout",
        re.compile(
            r"\b(?:text_to_speech|\bTTS\b|/audio/speech|chatterbox|127\.0\.0\.1:8892)\b"
            r".*\b(?:timeout|timed out|ConnectTimeout|APITimeout|ReadTimeout|TimeoutError)",
            re.IGNORECASE,
        ),
    ),
    ("chatterbox_connect", re.compile(r"(?:chatterbox|127\.0\.0\.1:8892|port 8892).*(?:failed to connect|couldn.?t connect|connection refused|ConnectTimeout|APITimeout|ReadTimeout|timeout)", re.IGNORECASE)),
)

def _tail_text(path: Path, *, max_bytes: int = 131_072) -> str:
    try:
        with path.open("rb") as f:
            try:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - max_bytes), os.SEEK_SET)
            except OSError:
                pass
            return f.read(max_bytes).decode("utf-8", errors="ignore")
    except OSError:
        return ""


def _line_pattern_tags(line: str) -> list[str]:
    return [name for name, pattern in _TTS_TIMEOUT_PATTERNS if pattern.search(line)]


def _pid_running(pid_file: Path) -> bool:
    try:
        pid = int(pid_file.read_text(errors="ignore").strip())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def tts_sidecar_pressure_sample(
    *,
    log_paths: list[str] | None = None,
    health_url: str | None = "http://127.0.0.1:8892/health",
    pid_file: str | None = None,
    timeout_seconds: float = 2.0,
    max_log_bytes: int = 131_072,
) -> dict:
    """Collect a compact TTS/Chatterbox maintenance sample.

    This deliberately does not restart anything and does not copy log lines into
    Sensorium. It records only counts, pattern labels, hashes, and local health.
    A stopped manual sidecar is healthy unless a recent TTS timeout/error is also
    visible; discretionary voice must not become forced auto-TTS.
    """
    default_logs = [
        str(Path.home() / ".hermes" / "logs" / "errors.log"),
        str(Path.home() / ".hermes" / "logs" / "gateway.log"),
        str(Path.home() / ".hermes" / "logs" / "agent.log"),
    ]
    paths = [Path(p) for p in (log_paths or default_logs)]
    matches: list[dict] = []
    pattern_counts: dict[str, int] = {}
    for path in paths:
        text = _tail_text(path, max_bytes=max_log_bytes)
        if not text:
            continue
        for line in text.splitlines():
            tags = _line_pattern_tags(line)
            if not tags:
                continue
            line_hash = hashlib.sha256(" ".join(line.split()).encode("utf-8", errors="ignore")).hexdigest()[:16]
            matches.append({"path": str(path), "line_hash": line_hash, "tags": tags})
            for tag in tags:
                pattern_counts[tag] = pattern_counts.get(tag, 0) + 1

    timeout_fingerprint = ""
    if matches:
        timeout_fingerprint = hashlib.sha256(json.dumps(matches, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]

    sidecar_running = _pid_running(Path(pid_file)) if pid_file else False
    health_available = False
    health_error = ""
    if health_url:
        try:
            req = urllib.request.Request(health_url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:  # noqa: S310 - fixed loopback health URL by default
                health_available = 200 <= int(resp.status) < 500
        except Exception as exc:
            health_error = type(exc).__name__
    return {
        "timeout_match_count": len(matches),
        "timeout_fingerprint": timeout_fingerprint,
        "pattern_counts": pattern_counts,
        "log_paths_checked": [str(p) for p in paths if p.exists()],
        "sidecar_running": sidecar_running,
        "health_available": health_available,
        "health_error": truncate_text(health_error, 80),
    }


def classify_tts_sidecar_pressure(sample: dict, *, state: dict | None = None, config: dict | None = None) -> tuple[dict | None, dict]:
    cfg = {"emit_on_unhealthy_running_sidecar": True}
    if config:
        cfg.update({k: v for k, v in config.items() if k in cfg})
    st = dict(state or {})
    timeout_count = int(sample.get("timeout_match_count", 0) or 0)
    timeout_fingerprint = str(sample.get("timeout_fingerprint") or "")
    running = bool(sample.get("sidecar_running"))
    health_available = bool(sample.get("health_available"))
    unhealthy_running = bool(cfg["emit_on_unhealthy_running_sidecar"] and running and not health_available)
    previous_level = str(st.get("level") or "healthy")
    new_unhealthy_running = unhealthy_running and previous_level != "degraded"
    new_timeout = bool(timeout_count and timeout_fingerprint and timeout_fingerprint != st.get("last_timeout_fingerprint"))
    level = "degraded" if new_timeout or unhealthy_running else "healthy"
    st["level"] = level
    st["last_sample"] = {
        "timeout_match_count": timeout_count,
        "timeout_fingerprint": timeout_fingerprint,
        "pattern_counts": dict(sample.get("pattern_counts", {}) or {}),
        "sidecar_running": running,
        "health_available": health_available,
        "health_error": sample.get("health_error", ""),
    }
    signal = None
    if new_timeout or new_unhealthy_running:
        if timeout_fingerprint:
            st["last_timeout_fingerprint"] = timeout_fingerprint
        reason_bits = []
        if new_timeout:
            reason_bits.append(f"new timeout/error signature count={timeout_count}")
        if new_unhealthy_running:
            reason_bits.append(f"sidecar pid present but health failed ({sample.get('health_error') or 'unknown'})")
        summary = "TTS/Chatterbox maintenance cue: " + "; ".join(reason_bits)
        signal = {
            "sensor": "sensorium.tts_sidecar_pressure",
            "source": "machine",
            "kind": "tts_sidecar_pressure",
            "summary": truncate_text(summary, MAX_SUMMARY_CHARS),
            "actor": "tool",
            "strength_hint": 0.86,
            "sensitivity": "local_only",
            "allowed_surfaces": ["local"],
            "correlation_keys": ["tts-sidecar-pressure", "chatterbox-turbo", "mediated-presence"],
            "scope": "local_tts",
            "pressure_level": "degraded",
            "metric_family": "tts_sidecar",
            "values": st["last_sample"],
            "recovery_checklist": _tts_sidecar_recovery_checklist(
                _resolve_tts_sidecar(config)["control_command"]
            ),
            "verification_criteria": TTS_SIDECAR_VERIFICATION_CRITERIA,
            "policy": "cue_only_no_auto_restart",
        }
    return signal, st


MEDIA_CAPACITY_DEFAULT_CONFIG = {
    "mem_available_min_pct": 25.0,
    "swap_used_max_pct": 20.0,
    "gpu_util_max_pct": 20.0,
    "vram_used_max_pct": 35.0,
}


def _safe_int(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _read_int_file(path: Path) -> int | None:
    return _safe_int(_read_text(path))


def _cheap_gpu_idle_sample(*, drm_root: str = "/sys/class/drm") -> dict:
    """Read cheap AMD/DRM GPU counters when exposed; never load a model."""
    root = Path(drm_root)
    cards: list[dict] = []
    try:
        candidates = sorted(root.glob("card*/device"))
    except OSError:
        candidates = []

    for device in candidates:
        util = _read_int_file(device / "gpu_busy_percent")
        vram_used = _read_int_file(device / "mem_info_vram_used")
        vram_total = _read_int_file(device / "mem_info_vram_total")
        vram_pct = None
        if vram_used is not None and vram_total and vram_total > 0:
            vram_pct = round(vram_used / vram_total * 100.0, 3)
        if util is None and vram_pct is None:
            continue
        cards.append({"gpu_util_pct": util, "vram_used_pct": vram_pct})

    if not cards:
        return {"gpu_sample_available": False}
    util_values = [c["gpu_util_pct"] for c in cards if c.get("gpu_util_pct") is not None]
    vram_values = [c["vram_used_pct"] for c in cards if c.get("vram_used_pct") is not None]
    return {
        "gpu_sample_available": True,
        "gpu_count": len(cards),
        "gpu_util_pct": max(util_values) if util_values else None,
        "vram_used_pct": max(vram_values) if vram_values else None,
    }


def _comfy_queue_sample(*, base_url: str, timeout_seconds: float) -> dict:
    base = base_url.rstrip("/")
    try:
        data = _http_json(f"{base}/queue", timeout_seconds=timeout_seconds)
    except Exception as exc:
        return {
            "comfy_available": False,
            "comfy_error": truncate_text(type(exc).__name__, 80),
            "comfy_queue_running": None,
            "comfy_queue_pending": None,
        }
    if not isinstance(data, dict):
        return {
            "comfy_available": False,
            "comfy_error": "unexpected_response",
            "comfy_queue_running": None,
            "comfy_queue_pending": None,
        }
    running = len(data.get("queue_running") or [])
    pending = len(data.get("queue_pending") or [])
    return {
        "comfy_available": True,
        "comfy_error": "",
        "comfy_queue_running": running,
        "comfy_queue_pending": pending,
    }


def _local_health_sample(*, url: str | None, timeout_seconds: float) -> dict:
    if not url:
        return {"available": False, "error": "disabled"}
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:  # noqa: S310 - fixed loopback health URL by default
            return {"available": 200 <= int(resp.status) < 500, "error": ""}
    except Exception as exc:
        return {"available": False, "error": truncate_text(type(exc).__name__, 80)}


def media_capacity_sample(
    *,
    comfy_base_url: str = "http://127.0.0.1:8188",
    tts_health_url: str | None = "http://127.0.0.1:8892/health",
    proc_root: str = "/proc",
    gpu_drm_root: str = "/sys/class/drm",
    timeout_seconds: float = 0.75,
) -> dict:
    """Collect an almost-idle media-capacity sample using cheap local probes only.

    The sample intentionally uses GET-style health/queue checks and procfs/sysfs
    counters. It does not inspect process command lines, load models, restart
    sidecars, submit Comfy prompts, or generate media.
    """
    sample: dict = {}
    sample.update(_parse_meminfo(_read_text(Path(proc_root) / "meminfo")))
    sample.update(_comfy_queue_sample(base_url=comfy_base_url, timeout_seconds=timeout_seconds))
    sample.update(_cheap_gpu_idle_sample(drm_root=gpu_drm_root))
    health = _local_health_sample(url=tts_health_url, timeout_seconds=timeout_seconds)
    sample["tts_health_available"] = bool(health["available"])
    sample["tts_health_error"] = health["error"]
    return sample


def _media_capacity_config(config: dict | None = None) -> dict:
    merged = dict(MEDIA_CAPACITY_DEFAULT_CONFIG)
    if config:
        for key, value in config.items():
            if key in merged and isinstance(value, (int, float)) and value >= 0:
                merged[key] = value
    return merged


def _media_capacity_values(sample: dict) -> dict:
    keys = (
        "comfy_available",
        "comfy_queue_running",
        "comfy_queue_pending",
        "gpu_sample_available",
        "gpu_count",
        "gpu_util_pct",
        "vram_used_pct",
        "mem_available_pct",
        "swap_used_pct",
        "tts_health_available",
    )
    values = {key: sample.get(key) for key in keys if key in sample}
    if sample.get("comfy_error"):
        values["comfy_error"] = truncate_text(str(sample.get("comfy_error")), 80)
    if sample.get("tts_health_error"):
        values["tts_health_error"] = truncate_text(str(sample.get("tts_health_error")), 80)
    return values


def classify_media_capacity(
    sample: dict,
    *,
    state: dict | None = None,
    config: dict | None = None,
) -> tuple[dict | None, dict]:
    """Classify media gift capacity as almost_idle, busy, or unknown.

    `almost_idle` is an enabling condition, not an actuator: the emitted signal
    is low-strength/local-only and carries a no-auto-generation policy. The
    current capacity record is always returned in state for policy readers.
    """
    cfg = _media_capacity_config(config)
    st = dict(state or {})
    values = _media_capacity_values(sample)
    reasons: list[str] = []
    unknown: list[str] = []

    comfy_available = bool(sample.get("comfy_available"))
    running = sample.get("comfy_queue_running")
    pending = sample.get("comfy_queue_pending")
    if not comfy_available:
        unknown.append("comfy_unavailable")
    elif int(running or 0) > 0 or int(pending or 0) > 0:
        reasons.append("comfy_queue_active")

    if sample.get("mem_available_pct") is None:
        unknown.append("mem_unknown")
    elif float(sample.get("mem_available_pct") or 0.0) < cfg["mem_available_min_pct"]:
        reasons.append("mem_available_low")
    if sample.get("swap_used_pct") is not None:
        if float(sample.get("swap_used_pct") or 0.0) > cfg["swap_used_max_pct"]:
            reasons.append("swap_used_high")

    if bool(sample.get("gpu_sample_available")):
        util = sample.get("gpu_util_pct")
        vram = sample.get("vram_used_pct")
        if util is not None and float(util) > cfg["gpu_util_max_pct"]:
            reasons.append("gpu_busy")
        if vram is not None and float(vram) > cfg["vram_used_max_pct"]:
            reasons.append("vram_used_high")
    else:
        values["gpu_note"] = "no cheap DRM GPU sample available"

    if not bool(sample.get("tts_health_available")):
        unknown.append("tts_health_unavailable")

    if reasons:
        status = "busy"
    elif unknown:
        status = "unknown"
    else:
        status = "almost_idle"

    record = {
        "kind": "media_capacity",
        "status": status,
        "reasons": reasons,
        "unknown": unknown,
        "values": values,
        "policy": "capacity_record_only_no_generation",
        "sensitivity": "local_only",
        "allowed_surfaces": ["local"],
    }
    previous = str(st.get("status") or st.get("level") or "unknown")
    st["level"] = status
    st["status"] = status
    st["capacity_record"] = record
    st["last_sample"] = values

    signal = None
    if status == "almost_idle" and previous != "almost_idle":
        signal = {
            "sensor": "sensorium.media_capacity",
            "source": "machine",
            "kind": "media_capacity",
            "summary": "Local media stack appears almost idle for mediated-presence gift work",
            "actor": "tool",
            "strength_hint": 0.58,
            "sensitivity": "local_only",
            "allowed_surfaces": ["local"],
            "correlation_keys": ["media-capacity", "mediated-presence", "media-gifts"],
            "scope": "local_media_stack",
            "capacity_status": status,
            "previous_status": previous,
            "values": values,
            "policy": "capacity_record_only_no_generation",
        }
    return signal, st


def replay_media_capacity(samples: list[dict], *, config: dict | None = None) -> list[dict]:
    """Replay capacity fixtures through the online classifier for tests/audits."""
    state: dict = {}
    signals: list[dict] = []
    for sample in samples:
        sig, state = classify_media_capacity(sample, state=state, config=config)
        if sig is not None:
            signals.append(sig)
    return signals


def _http_json(url: str, *, timeout_seconds: float) -> dict | list:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:  # noqa: S310 - localhost/admin API by caller config
        return json.loads(resp.read(200_000).decode("utf-8", errors="ignore") or "{}")


HINDSIGHT_MAINTENANCE_TASK_TYPES = {"refresh_mental_model"}


def _hindsight_operation_status_sample(
    *,
    base: str,
    bank_id: str,
    status: str,
    timeout_seconds: float,
    page_limit: int,
    max_pages: int,
) -> tuple[int, dict[str, int], bool]:
    """Return compact operation counts by task_type for one status.

    Hindsight's operations endpoint currently returns a total per status but does
    not filter by task_type. Page enough metadata to classify operation-family
    pressure without retaining operation IDs, documents, prompts, or memory text.
    """
    total = 0
    counts: dict[str, int] = {}
    fetched = 0
    safe_limit = max(1, min(int(page_limit or 100), 100))
    safe_pages = max(1, int(max_pages or 1))
    for page in range(safe_pages):
        offset = page * safe_limit
        url = (
            f"{base}/v1/default/banks/{urllib.parse.quote(bank_id)}/operations"
            f"?status={urllib.parse.quote(status)}&limit={safe_limit}&offset={offset}"
        )
        data = _http_json(url, timeout_seconds=timeout_seconds)
        if not isinstance(data, dict):
            break
        total = int(data.get("total") or data.get("total_count") or total or 0)
        ops = data.get("operations") or data.get("items") or []
        if not isinstance(ops, list) or not ops:
            break
        for op in ops:
            if not isinstance(op, dict):
                continue
            task_type = truncate_text(str(op.get("task_type") or op.get("operation_type") or "unknown"), 80)
            counts[task_type] = counts.get(task_type, 0) + 1
            fetched += 1
        if fetched >= total or len(ops) < safe_limit:
            break
    return total, counts, fetched < total


def hindsight_pressure_sample(
    *,
    base_url: str = "http://localhost:8888",
    bank_id: str = "hermes",
    timeout_seconds: float = 1.0,
    page_limit: int = 100,
    max_pages: int = 20,
) -> dict:
    """Sample Hindsight operation pressure via local API counts only."""
    base = base_url.rstrip("/")
    try:
        _http_json(f"{base}/health", timeout_seconds=timeout_seconds)
        totals = {"pending_total": 0, "processing_total": 0, "failed_total": 0}
        operation_counts: dict[str, dict[str, int]] = {}
        operation_counts_truncated = False
        for status, key in (("pending", "pending_total"), ("processing", "processing_total"), ("failed", "failed_total")):
            total, counts, truncated = _hindsight_operation_status_sample(
                base=base,
                bank_id=bank_id,
                status=status,
                timeout_seconds=timeout_seconds,
                page_limit=page_limit,
                max_pages=max_pages,
            )
            totals[key] = total
            operation_counts[status] = counts
            operation_counts_truncated = operation_counts_truncated or truncated
        return {"api_available": True, **totals, "operation_counts": operation_counts, "operation_counts_truncated": operation_counts_truncated}
    except Exception as exc:
        return {"api_available": False, "error": truncate_text(type(exc).__name__, 80), "pending_total": 0, "processing_total": 0, "failed_total": 0, "operation_counts": {}, "operation_counts_truncated": False}


def _hindsight_task_count(counts_by_status: dict, status: str, *, maintenance: bool) -> int:
    counts = counts_by_status.get(status) if isinstance(counts_by_status, dict) else None
    if not isinstance(counts, dict):
        return 0
    total = 0
    for task_type, count in counts.items():
        is_maintenance = str(task_type) in HINDSIGHT_MAINTENANCE_TASK_TYPES
        if is_maintenance == maintenance:
            total += int(count or 0)
    return total


def classify_hindsight_pressure(sample: dict, *, state: dict | None = None, config: dict | None = None) -> tuple[dict | None, dict]:
    cfg = {
        "pending_degraded": 50,
        "pending_critical": 200,
        "failed_degraded": 5,
        "processing_critical": 20,
        "refresh_pending_degraded": 500,
        "refresh_failed_degraded": 25,
        "refresh_processing_degraded": 5,
    }
    if config:
        cfg.update({k: v for k, v in config.items() if k in cfg and isinstance(v, (int, float))})
    pending = int(sample.get("pending_total", 0) or 0)
    processing = int(sample.get("processing_total", 0) or 0)
    failed = int(sample.get("failed_total", 0) or 0)
    counts_by_status = sample.get("operation_counts") if isinstance(sample.get("operation_counts"), dict) else {}
    has_family_counts = bool(counts_by_status)
    if has_family_counts:
        refresh_pending = _hindsight_task_count(counts_by_status, "pending", maintenance=True)
        refresh_processing = _hindsight_task_count(counts_by_status, "processing", maintenance=True)
        refresh_failed = _hindsight_task_count(counts_by_status, "failed", maintenance=True)
        core_pending = max(0, pending - refresh_pending)
        core_processing = max(0, processing - refresh_processing)
        core_failed = max(0, failed - refresh_failed)
    else:
        refresh_pending = refresh_processing = refresh_failed = 0
        core_pending, core_processing, core_failed = pending, processing, failed
    level = "healthy"
    family = "memory_queue"
    reason = ""
    if not sample.get("api_available", False):
        level = "degraded"
        family = "api"
        reason = "Hindsight API unavailable"
    elif core_pending >= cfg["pending_critical"]:
        level = "critical"
        family = "core_pending"
        reason = f"core_pending={core_pending}"
    elif core_processing >= cfg["processing_critical"]:
        level = "critical"
        family = "core_processing"
        reason = f"core_processing={core_processing}"
    elif core_pending >= cfg["pending_degraded"]:
        level = "degraded"
        family = "core_pending"
        reason = f"core_pending={core_pending}"
    elif core_failed >= cfg["failed_degraded"]:
        level = "degraded"
        family = "core_failed"
        reason = f"core_failed={core_failed}"
    elif refresh_pending >= cfg["refresh_pending_degraded"]:
        level = "degraded"
        family = "refresh_mental_model"
        reason = f"refresh_mental_model pending={refresh_pending}"
    elif refresh_processing >= cfg["refresh_processing_degraded"]:
        level = "degraded"
        family = "refresh_mental_model"
        reason = f"refresh_mental_model processing={refresh_processing}"
    elif refresh_failed >= cfg["refresh_failed_degraded"]:
        level = "degraded"
        family = "refresh_mental_model"
        reason = f"refresh_mental_model failed={refresh_failed}"
    values = {
        "api_available": bool(sample.get("api_available", False)),
        "pending_total": pending,
        "processing_total": processing,
        "failed_total": failed,
        "core_pending_total": core_pending,
        "core_processing_total": core_processing,
        "core_failed_total": core_failed,
        "refresh_pending_total": refresh_pending,
        "refresh_processing_total": refresh_processing,
        "refresh_failed_total": refresh_failed,
        "operation_counts": counts_by_status,
        "operation_counts_truncated": bool(sample.get("operation_counts_truncated", False)),
    }
    return _transition_classifier(sample, state=state, observed_level=level, metric_family=family, reason=reason, values=values, sensor="sensorium.hindsight_pressure", source="memory", kind="hindsight_pressure", correlation_key="hindsight-pressure")


def _default_kanban_paths() -> list[str]:
    root = Path.home() / ".hermes" / "kanban" / "boards"
    paths: list[str] = []
    try:
        for p in root.glob("*/kanban.db"):
            if "_archived" not in p.parts:
                paths.append(str(p))
    except OSError:
        pass
    legacy = Path.home() / ".hermes" / "kanban.db"
    if legacy.exists():
        paths.append(str(legacy))
    return sorted(set(paths))


def kanban_pressure_sample(*, board_paths: list[str] | None = None, now_epoch: int | None = None, stale_running_seconds: int = 3600) -> dict:
    """Read Kanban board counts only; never task titles/bodies/comments."""
    now = int(now_epoch if now_epoch is not None else time.time())
    status_counts: dict[str, int] = {}
    failed = blocked = stale = total = boards = 0
    for raw in board_paths if board_paths is not None else _default_kanban_paths():
        try:
            con = sqlite3.connect(f"file:{raw}?mode=ro", uri=True, timeout=1)
            con.row_factory = sqlite3.Row
            boards += 1
            for row in con.execute("SELECT status, consecutive_failures, last_heartbeat_at FROM tasks"):
                status = str(row["status"] or "unknown")
                status_counts[status] = status_counts.get(status, 0) + 1
                total += 1
                # Consecutive failures are historical on terminal rows.  A task
                # that is already done/archived should not keep paging kanban
                # pressure forever just because it crashed before eventual
                # settlement or archival.  Keep active blocked/ready/running
                # rows with failures visible, because those are live wedges.
                if status not in {"done", "archived"} and (
                    status in {"failed", "crashed", "timed_out"}
                    or int(row["consecutive_failures"] or 0) > 0
                ):
                    failed += 1
                if status in {"blocked", "stuck"}:
                    blocked += 1
                heartbeat = row["last_heartbeat_at"]
                if status == "running" and heartbeat and now - int(heartbeat) >= stale_running_seconds:
                    stale += 1
            con.close()
        except (OSError, sqlite3.Error, ValueError):
            continue
    return {"board_count": boards, "task_count": total, "status_counts": status_counts, "failed_tasks": failed, "blocked_tasks": blocked, "stale_running_tasks": stale}


def classify_kanban_pressure(sample: dict, *, state: dict | None = None, config: dict | None = None) -> tuple[dict | None, dict]:
    cfg = {"stale_degraded": 1, "stale_critical": 5, "failed_degraded": 3, "failed_critical": 10, "blocked_degraded": 10}
    if config:
        cfg.update({k: v for k, v in config.items() if k in cfg and isinstance(v, (int, float))})
    stale = int(sample.get("stale_running_tasks", 0) or 0)
    failed = int(sample.get("failed_tasks", 0) or 0)
    blocked = int(sample.get("blocked_tasks", 0) or 0)
    level = "healthy"
    family = "kanban"
    reason = ""
    if stale >= cfg["stale_critical"]:
        level = "critical"
        family = "stale_running"
        reason = f"stale_running={stale}"
    elif failed >= cfg["failed_critical"]:
        level = "critical"
        family = "failed"
        reason = f"failed={failed}"
    elif stale >= cfg["stale_degraded"]:
        level = "degraded"
        family = "stale_running"
        reason = f"stale_running={stale}"
    elif failed >= cfg["failed_degraded"]:
        level = "degraded"
        family = "failed"
        reason = f"failed={failed}"
    elif blocked >= cfg["blocked_degraded"]:
        level = "degraded"
        family = "blocked"
        reason = f"blocked={blocked}"
    values = {"board_count": int(sample.get("board_count", 0) or 0), "task_count": int(sample.get("task_count", 0) or 0), "failed_tasks": failed, "blocked_tasks": blocked, "stale_running_tasks": stale, "status_counts": dict(sample.get("status_counts", {}) or {})}
    return _transition_classifier(sample, state=state, observed_level=level, metric_family=family, reason=reason, values=values, sensor="sensorium.kanban_pressure", source="kanban", kind="kanban_pressure", correlation_key="kanban-pressure")


def file_content_hash(path: str) -> str:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()
    except (OSError, IOError):
        return ""
