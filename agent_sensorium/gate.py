"""Deterministic signal-to-event and event-to-candidate promotion gate."""

import hashlib
import json

from .schemas import (
    intersect_allowed_surfaces,
    merge_sensitivity,
    new_id,
    utc_now_iso,
)

DEFAULT_CONFIG: dict = {
    "thresholds": {
        "single_signal_strength": 0.75,
        "important_kind_strength": 0.6,
        "candidate_pressure": 0.65,
    },
    "promote_kinds": [
        "design_decision",
        "user_correction",
        "artifact_created",
        "unresolved_question",
        "task_result",
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


def should_promote_signal(signal: dict, config: dict | None = None) -> tuple[bool, str]:
    cfg = config or DEFAULT_CONFIG
    thresholds = cfg.get("thresholds", DEFAULT_CONFIG["thresholds"])
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


_SENSORIUM_ID_PREFIXES = ("sth_", "cand_", "ctask_", "evt_", "sig_", "dispatch")


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
