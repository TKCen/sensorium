"""Tool handlers for Agent Sensorium — callable without live Hermes runtime."""

import json

from .gate import (
    DEFAULT_CONFIG,
    event_to_candidate,
    promote_signal_to_event,
    should_promote_signal,
)
from .schemas import normalize_signal, utc_now_iso, validate_signal
from .store import SensoriumStore


def _ok(instance: str, data: dict) -> str:
    return json.dumps({"success": True, "instance": instance, "data": data, "error": None})


def _err(instance: str, error: str) -> str:
    return json.dumps({"success": False, "instance": instance, "data": None, "error": error})


def handle_sensorium_status(
    *, instance: str = "default", state_dir: str | None = None
) -> str:
    store = SensoriumStore(instance=instance, state_dir=state_dir)
    store.ensure_dirs()

    signals = store.read_jsonl("signals")
    events = store.read_jsonl("events")
    candidates = store.read_jsonl("candidates")
    threads = store.read_jsonl("threads")

    active_candidates = [c for c in candidates if c.get("status") == "candidate"]
    active_candidates.sort(key=lambda c: c.get("pressure", 0), reverse=True)

    data = {
        "instance": instance,
        "state_dir": str(store.root),
        "counts": {
            "signals": len(signals),
            "events": len(events),
            "candidates": len(candidates),
            "active_candidates": len(active_candidates),
            "threads": len(threads),
        },
        "top_candidates": [
            {
                "id": c["id"],
                "kind": c.get("kind"),
                "pressure": c.get("pressure"),
                "summary": c.get("summary", "")[:120],
            }
            for c in active_candidates[:5]
        ],
        "ts": utc_now_iso(),
    }
    return _ok(instance, data)


def handle_sensorium_ingest_signal(
    *,
    signal: dict,
    instance: str = "default",
    state_dir: str | None = None,
    config: dict | None = None,
) -> str:
    try:
        validate_signal(signal)
    except ValueError as e:
        return _err(instance, str(e))

    store = SensoriumStore(instance=instance, state_dir=state_dir)
    store.ensure_dirs()

    normalized = normalize_signal(signal)
    store.append_jsonl("signals", normalized)

    promoted, reason = should_promote_signal(normalized, config)
    result: dict = {
        "signal_id": normalized["id"],
        "promoted": promoted,
        "reason": reason,
    }

    if promoted:
        event = promote_signal_to_event(normalized, config)
        store.append_jsonl("events", event)
        result["event_id"] = event["id"]

        candidate = event_to_candidate(event, config)
        store.append_jsonl("candidates", candidate)
        result["candidate_id"] = candidate["id"]

    return _ok(instance, result)
