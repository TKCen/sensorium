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
import time
import urllib.error
import urllib.parse
import urllib.request
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


def session_event_signal(
    *,
    kind: str,
    summary: str,
    session_ref: str = "",
    strength_hint: float = 0.5,
    sensitivity: str = "private",
    allowed_surfaces: list[str] | None = None,
    correlation_keys: list[str] | None = None,
) -> dict:
    return {
        "sensor": "sensorium.session_event",
        "source": "hermes_session",
        "kind": kind,
        "summary": truncate_text(summary, MAX_SUMMARY_CHARS),
        "session_ref": truncate_text(session_ref, MAX_REF_CHARS),
        "strength_hint": _clamp(strength_hint),
        "sensitivity": _safe_sensitivity(sensitivity),
        "allowed_surfaces": _safe_list(allowed_surfaces, default=["local"]),
        "correlation_keys": _safe_list(correlation_keys, default=[]),
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

    less = lambda value, threshold: value < threshold
    greater = lambda value, threshold: value >= threshold
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
            rx_errors += int(values[2]); rx_drops += int(values[3])
            tx_errors += int(values[10]); tx_drops += int(values[11])
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
    level = "healthy"; family = "network"; reason = ""
    if int(sample.get("non_loopback_interfaces", 0) or 0) == 0:
        level = "critical"; family = "interface"; reason = "no non-loopback interfaces"
    elif close_wait >= cfg["close_wait_degraded"]:
        level = "degraded"; family = "tcp"; reason = f"CLOSE_WAIT={close_wait}"
    elif syn >= cfg["syn_degraded"]:
        level = "degraded"; family = "tcp"; reason = f"SYN backlog={syn}"
    elif "last_error_total" in st and (errors - int(st.get("last_error_total", 0))) >= cfg["error_delta_degraded"]:
        level = "degraded"; family = "errors"; reason = f"network errors increased to {errors}"
    elif "last_drop_total" in st and (drops - int(st.get("last_drop_total", 0))) >= cfg["error_delta_degraded"]:
        level = "degraded"; family = "drops"; reason = f"network drops increased to {drops}"
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
    level = "healthy"; family = "process"; reason = ""
    if zombies >= cfg["zombie_critical"]:
        level = "critical"; family = "zombie"; reason = f"zombies={zombies}"
    elif d_count >= cfg["d_critical"]:
        level = "critical"; family = "uninterruptible"; reason = f"D-state={d_count}"
    elif zombies >= cfg["zombie_degraded"]:
        level = "degraded"; family = "zombie"; reason = f"zombies={zombies}"
    elif d_count >= cfg["d_degraded"]:
        level = "degraded"; family = "uninterruptible"; reason = f"D-state={d_count}"
    values = {"process_count": int(sample.get("process_count", 0) or 0), "zombie_count": zombies, "uninterruptible_count": d_count}
    return _transition_classifier(sample, state=state, observed_level=level, metric_family=family, reason=reason, values=values, sensor="sensorium.machine_process_pressure", source="machine", kind="process_pressure", correlation_key="machine-process-pressure")


def _http_json(url: str, *, timeout_seconds: float) -> dict | list:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:  # noqa: S310 - localhost/admin API by caller config
        return json.loads(resp.read(200_000).decode("utf-8", errors="ignore") or "{}")


def hindsight_pressure_sample(*, base_url: str = "http://localhost:8888", bank_id: str = "hermes", timeout_seconds: float = 1.0) -> dict:
    """Sample Hindsight operation pressure via local API counts only."""
    base = base_url.rstrip("/")
    try:
        _http_json(f"{base}/health", timeout_seconds=timeout_seconds)
        totals = {"pending_total": 0, "processing_total": 0, "failed_total": 0}
        for status, key in (("pending", "pending_total"), ("processing", "processing_total"), ("failed", "failed_total")):
            url = f"{base}/v1/default/banks/{urllib.parse.quote(bank_id)}/operations?status={status}&limit=1"
            data = _http_json(url, timeout_seconds=timeout_seconds)
            if isinstance(data, dict):
                totals[key] = int(data.get("total") or data.get("total_count") or len(data.get("operations", []) or data.get("items", []) or []))
        return {"api_available": True, **totals}
    except Exception as exc:
        return {"api_available": False, "error": truncate_text(type(exc).__name__, 80), "pending_total": 0, "processing_total": 0, "failed_total": 0}


def classify_hindsight_pressure(sample: dict, *, state: dict | None = None, config: dict | None = None) -> tuple[dict | None, dict]:
    cfg = {"pending_degraded": 50, "pending_critical": 200, "failed_degraded": 5, "processing_critical": 20}
    if config:
        cfg.update({k: v for k, v in config.items() if k in cfg and isinstance(v, (int, float))})
    pending = int(sample.get("pending_total", 0) or 0); processing = int(sample.get("processing_total", 0) or 0); failed = int(sample.get("failed_total", 0) or 0)
    level = "healthy"; family = "memory_queue"; reason = ""
    if not sample.get("api_available", False):
        level = "degraded"; family = "api"; reason = "Hindsight API unavailable"
    elif pending >= cfg["pending_critical"]:
        level = "critical"; family = "pending"; reason = f"pending={pending}"
    elif processing >= cfg["processing_critical"]:
        level = "critical"; family = "processing"; reason = f"processing={processing}"
    elif pending >= cfg["pending_degraded"]:
        level = "degraded"; family = "pending"; reason = f"pending={pending}"
    elif failed >= cfg["failed_degraded"]:
        level = "degraded"; family = "failed"; reason = f"failed={failed}"
    values = {"api_available": bool(sample.get("api_available", False)), "pending_total": pending, "processing_total": processing, "failed_total": failed}
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
                if status in {"failed", "crashed", "timed_out"} or int(row["consecutive_failures"] or 0) > 0:
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
    stale = int(sample.get("stale_running_tasks", 0) or 0); failed = int(sample.get("failed_tasks", 0) or 0); blocked = int(sample.get("blocked_tasks", 0) or 0)
    level = "healthy"; family = "kanban"; reason = ""
    if stale >= cfg["stale_critical"]:
        level = "critical"; family = "stale_running"; reason = f"stale_running={stale}"
    elif failed >= cfg["failed_critical"]:
        level = "critical"; family = "failed"; reason = f"failed={failed}"
    elif stale >= cfg["stale_degraded"]:
        level = "degraded"; family = "stale_running"; reason = f"stale_running={stale}"
    elif failed >= cfg["failed_degraded"]:
        level = "degraded"; family = "failed"; reason = f"failed={failed}"
    elif blocked >= cfg["blocked_degraded"]:
        level = "degraded"; family = "blocked"; reason = f"blocked={blocked}"
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
