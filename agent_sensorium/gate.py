"""Deterministic signal-to-event and event-to-candidate promotion gate."""

import hashlib
import json
import math
from datetime import datetime, timezone

from .schemas import (
    new_id,
    utc_now_iso,
)

DEFAULT_CONFIG: dict = {
    "thresholds": {
        "single_signal_strength": 0.75,
        "important_kind_strength": 0.6,
        "candidate_pressure": 0.65,
    },
    "decay": {
        "half_life_hours": 72.0,
    },
    "inhibition": {
        "ttl_hours": 168.0,
        "max_entries": 128,
    },
    "promote_kinds": [
        "design_decision",
        "user_correction",
        "explicit_correction",
        "design_insight",
        "relational_salience",
        "creative_pull",
        "durable_importance",
        "artifact_created",
        "unresolved_question",
        "task_result",
        "body_pressure",
        "network_pressure",
        "process_pressure",
        "hindsight_pressure",
        "kanban_pressure",
        "tts_sidecar_pressure",
    ],
}


def signal_fingerprint(signal: dict) -> str:
    key_material = json.dumps(
        {
            "sensor": signal.get("sensor", ""),
            "kind": signal.get("kind", ""),
            "correlation_keys": sorted(signal.get("correlation_keys", [])),
            "summary": signal.get("summary", ""),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(key_material.encode()).hexdigest()[:16]


def event_fingerprint(event: dict) -> str:
    key_material = json.dumps(
        {
            "type": event.get("type", ""),
            "kind": event.get("kind", ""),
            "source_signal_ids": sorted(event.get("source_signal_ids", [])),
            "correlation_keys": sorted(event.get("correlation_keys", [])),
            "summary": event.get("summary", ""),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(key_material.encode()).hexdigest()[:16]


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        numeric = 0.0
    return max(lo, min(hi, numeric))


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def decayed_candidate(candidate: dict, *, now: str | None = None, config: dict | None = None) -> dict:
    """Apply exponential pressure decay/habituation without mutating input."""
    cfg = dict(DEFAULT_CONFIG)
    if isinstance(config, dict):
        cfg.update(config)
    raw_decay = cfg.get("decay")
    decay_cfg: dict = raw_decay if isinstance(raw_decay, dict) else {}
    try:
        half_life = float(decay_cfg.get("half_life_hours", 72.0) or 72.0)
    except (TypeError, ValueError):
        half_life = 72.0
    half_life = max(0.1, half_life)
    reference = _parse_iso(candidate.get("last_decay_at") or candidate.get("updated_at") or candidate.get("created_at"))
    current = _parse_iso(now) if now else _utcnow()
    if current is None:
        current = _utcnow()
    updated = dict(candidate)
    if reference is None:
        return updated
    elapsed_hours = max(0.0, (current - reference).total_seconds() / 3600.0)
    if elapsed_hours <= 0:
        return updated
    pressure = _clamp(candidate.get("pressure", 0.0))
    decayed = round(pressure * math.pow(0.5, elapsed_hours / half_life), 3)
    updated["pressure"] = decayed
    updated["last_decay_at"] = current.isoformat().replace("+00:00", "Z")
    meta = dict(updated.get("pressure_meta") or {})
    meta.update({
        "decay_applied": True,
        "elapsed_hours": round(elapsed_hours, 3),
        "half_life_hours": half_life,
        "pre_decay_pressure": pressure,
    })
    updated["pressure_meta"] = meta
    return updated


def build_pressure_pitch(candidate: dict, *, events: list[dict] | None = None, threshold: float = 0.65) -> dict:
    """Build the compact traceable pitch payload for Conscious/Operator review."""
    candidate_event_ids = set(candidate.get("event_ids", []) or [])
    if candidate_event_ids:
        related = [e for e in (events or []) if e.get("id") in candidate_event_ids]
    else:
        related = events or []
    related = sorted(related, key=lambda e: e.get("ts", ""))
    times = [e.get("ts", "") for e in related if e.get("ts")]
    timeframe = f"{times[0]}..{times[-1]}" if times else ""
    sample = ""
    if related:
        sample = str(related[-1].get("summary", ""))[:200]
    kind = candidate.get("kind", "")
    return {
        "candidate_id": candidate.get("id", ""),
        "kind": kind,
        "summary": str(candidate.get("summary", ""))[:200],
        "pressure": _clamp(candidate.get("pressure", 0.0)),
        "threshold": threshold,
        "event_count": len(related) or len(candidate.get("event_ids", []) or []),
        "timeframe": timeframe,
        "sample": sample,
        "correlation_keys": list(candidate.get("correlation_keys", []) or [])[:8],
        "recommended_prompt": f"I noticed repeated pressure around {kind}. Do we want to invest in this?",
    }


def _inhibition_config(config: dict | None = None) -> dict:
    cfg = dict(DEFAULT_CONFIG["inhibition"])
    raw = (config or {}).get("inhibition") if isinstance(config, dict) else None
    if isinstance(raw, dict):
        cfg.update(raw)
    try:
        cfg["ttl_hours"] = max(0.1, float(cfg.get("ttl_hours", 168.0)))
    except (TypeError, ValueError):
        cfg["ttl_hours"] = 168.0
    try:
        cfg["max_entries"] = max(1, int(cfg.get("max_entries", 128)))
    except (TypeError, ValueError):
        cfg["max_entries"] = 128
    return cfg


def prune_sensor_policy(policy: dict | None, *, now: str | None = None, config: dict | None = None) -> dict:
    """Return sensor policy with expired or overflowing inhibitions removed."""
    current = _parse_iso(now) if now else _utcnow()
    if current is None:
        current = _utcnow()
    cfg = _inhibition_config(config)
    ttl_hours = cfg["ttl_hours"]
    max_entries = cfg["max_entries"]
    result = dict(policy or {})
    entries = result.get("inhibitions") if isinstance(result.get("inhibitions"), list) else []
    active: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        expires_at = _parse_iso(entry.get("expires_at"))
        created_at = _parse_iso(entry.get("created_at"))
        if expires_at is not None and expires_at <= current:
            continue
        if expires_at is None and created_at is not None:
            age_hours = (current - created_at).total_seconds() / 3600.0
            if age_hours > ttl_hours:
                continue
        active.append(entry)
    active.sort(key=lambda e: e.get("created_at", ""))
    if len(active) > max_entries:
        active = active[-max_entries:]
    result["inhibitions"] = active
    return result


def inhibited_by_sensor_policy(signal: dict, policy: dict | None, *, config: dict | None = None, now: str | None = None) -> tuple[bool, str]:
    policy = prune_sensor_policy(policy, config=config, now=now)
    entries = policy.get("inhibitions") if isinstance(policy.get("inhibitions"), list) else []
    kind = signal.get("kind", "")
    keys = set(signal.get("correlation_keys") or [])
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("kind") != kind:
            continue
        entry_keys = set(entry.get("correlation_keys", []) or [])
        if entry_keys and not (keys & entry_keys):
            continue
        return True, entry.get("reason", "sensor pathway inhibited")
    return False, ""


def should_promote_signal(signal: dict, config: dict | None = None) -> tuple[bool, str]:
    cfg = config or DEFAULT_CONFIG

    if signal.get("source") == "feedback":
        promote, reason = should_promote_feedback(signal, cfg)
        if not promote:
            return False, reason

    thresholds = dict(DEFAULT_CONFIG["thresholds"])
    if isinstance(cfg.get("thresholds"), dict):
        thresholds.update(cfg["thresholds"])
    promote_kinds = cfg.get("promote_kinds", DEFAULT_CONFIG["promote_kinds"])
    strength = signal.get("strength_hint", 0.0)
    kind = signal.get("kind", "")

    if strength >= thresholds["single_signal_strength"]:
        return True, f"strength {strength} >= {thresholds['single_signal_strength']}"

    if kind in promote_kinds and strength >= thresholds["important_kind_strength"]:
        return True, f"kind '{kind}' with strength {strength} >= {thresholds['important_kind_strength']}"

    return False, f"below threshold (strength={strength}, kind='{kind}')"


def promote_signal_to_event(signal: dict, config: dict | None = None) -> dict:
    now = utc_now_iso()
    event = {
        "id": new_id("evt"),
        "ts": now,
        "type": "sensor.event.promoted",
        "source_signal_ids": [signal.get("id", "")],
        "kind": signal.get("kind", ""),
        "summary": signal.get("summary", ""),
        "signal_count": 1,
        "strength": signal.get("strength_hint", 0.5),
        "correlation_keys": signal.get("correlation_keys", []),
        "sensitivity": signal.get("sensitivity", "private"),
        "allowed_surfaces": signal.get("allowed_surfaces", ["local"]),
        "expires_at": "",
    }
    if signal.get("source") == "feedback":
        event["feedback_meta"] = {
            "caused_by": signal.get("caused_by"),
            "outcome": signal.get("outcome"),
            "feedback_scope": signal.get("feedback_scope"),
        }
    event["fingerprint"] = event_fingerprint(event)
    return event


def event_to_candidate(event: dict, config: dict | None = None) -> dict:
    now = utc_now_iso()
    strength = event.get("strength", 0.5)
    pressure = round(strength * 0.6 + 0.2 * 0.5 + 0.2 * 0.5, 3)

    candidate = {
        "id": new_id("cand"),
        "status": "candidate",
        "kind": event.get("kind", ""),
        "pressure": pressure,
        "novelty": 0.5,
        "repetition": 0.0,
        "identity_relevance": 0.5,
        "relationship_relevance": 0.0,
        "actionability": 0.5,
        "time_sensitivity": 0.0,
        "summary": event.get("summary", ""),
        "event_ids": [event.get("id", "")],
        "correlation_keys": event.get("correlation_keys", []),
        "sensitivity": event.get("sensitivity", "private"),
        "allowed_surfaces": event.get("allowed_surfaces", ["local"]),
        "created_at": now,
        "updated_at": now,
        "expires_at": "",
    }
    if event.get("feedback_meta"):
        candidate["feedback_meta"] = event["feedback_meta"]
    candidate["fingerprint"] = candidate_fingerprint(candidate)
    return candidate


_SENSORIUM_ID_PREFIXES = ("sth_", "cand_", "ctask_", "evt_", "sig_", "dispatch_", "tact_", "wreq_")


def is_feedback_self_loop(candidate: dict) -> bool:
    meta = candidate.get("feedback_meta")
    if not meta:
        return False
    if meta.get("feedback_scope") == "operator_evaluation":
        return False
    caused_by = meta.get("caused_by") or {}
    if not isinstance(caused_by, dict):
        return False
    for val in caused_by.values():
        if isinstance(val, str) and any(val.startswith(p) for p in _SENSORIUM_ID_PREFIXES):
            return True
    return False


_SENSORIUM_FEEDBACK_SENSORS = frozenset({
    "sensorium.action_result",
    "sensorium.thread_update",
    "sensorium.worker_result",
})

_PROMOTABLE_FEEDBACK_OUTCOMES = frozenset({
    "failed", "timeout", "operator_rejected",
})

_REGRESSION_KEYWORDS = ("regression", "reopen", "blocker", "escalat")


def is_settled_feedback_signal(signal: dict) -> bool:
    if signal.get("source") != "feedback":
        return False
    if signal.get("sensor", "") not in _SENSORIUM_FEEDBACK_SENSORS:
        return False
    outcome = signal.get("outcome", "")
    if outcome in _PROMOTABLE_FEEDBACK_OUTCOMES:
        return False
    if signal.get("promote_feedback"):
        return False
    summary_lower = signal.get("summary", "").lower()
    if any(kw in summary_lower for kw in _REGRESSION_KEYWORDS):
        return False
    caused_by = signal.get("caused_by")
    if not isinstance(caused_by, dict):
        return False
    return any(
        isinstance(v, str) and any(v.startswith(p) for p in _SENSORIUM_ID_PREFIXES)
        for v in caused_by.values()
    )


def should_promote_feedback(signal: dict, config: dict | None = None) -> tuple[bool, str]:
    if not is_settled_feedback_signal(signal):
        return True, ""
    cfg = config or {}
    if cfg.get("promote_all_feedback"):
        return True, "config: promote_all_feedback override"
    return False, "settled_feedback: ordinary internal completion feedback suppressed"


def candidate_fingerprint(candidate: dict) -> str:
    key_material = json.dumps(
        {
            "kind": candidate.get("kind", ""),
            "correlation_keys": sorted(candidate.get("correlation_keys", [])),
            "summary": candidate.get("summary", ""),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(key_material.encode()).hexdigest()[:16]
