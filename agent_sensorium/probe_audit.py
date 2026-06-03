"""Probe coverage audit and smoke validation for Agent Sensorium.

Distinguishes implemented helpers, wired live probes, configured watched
sources, and blind spots. Exercises end-to-end signal paths in temporary
state without mutating the real default Sensorium.
"""

import json

from .memory_reflection import MEMORY_REFLECTION_PROBE_FAMILY
from .schemas import VALID_SOURCES
from .sensors import (
    artifact_signal,
    hindsight_pressure_sample,
    kanban_pressure_sample,
    machine_body_pressure_sample,
    machine_network_pressure_sample,
    machine_process_pressure_sample,
    operator_signal,
    session_event_signal,
)
from .store import SensoriumStore
from .tools import handle_sensorium_ingest_signal

KNOWN_HELPERS = {
    "session_event_signal": {"source": "hermes_session", "sensor": "sensorium.session_event", "wired_live": False},
    "artifact_signal": {"source": "artifact", "sensor": "sensorium.artifact", "wired_live": False},
    "operator_signal": {"source": "manual", "sensor": "sensorium.explicit_operator", "wired_live": False},
    machine_body_pressure_sample.__name__: {"source": "machine", "sensor": "sensorium.machine_body_pressure", "wired_live": True},
    machine_network_pressure_sample.__name__: {"source": "machine", "sensor": "sensorium.machine_network_pressure", "wired_live": True},
    machine_process_pressure_sample.__name__: {"source": "machine", "sensor": "sensorium.machine_process_pressure", "wired_live": True},
    hindsight_pressure_sample.__name__: {"source": "memory", "sensor": "sensorium.hindsight_pressure", "wired_live": True},
    kanban_pressure_sample.__name__: {"source": "kanban", "sensor": "sensorium.kanban_pressure", "wired_live": True},
}

BLIND_SPOTS = [
    {"name": "rss_feed_items", "description": "RSS/feed item sensor not implemented"},
    {"name": "file_crawl", "description": "File system crawl sensor not implemented"},
    {"name": "task_results", "description": "External task result sensor not implemented"},
    {"name": "active_session_summary", "description": "Active session summary sensor not implemented"},
]


def probe_inventory() -> dict:
    return {
        "implemented_helpers": [
            {
                "function": name,
                "source": info["source"],
                "sensor": info["sensor"],
                "wired_live": bool(info.get("wired_live", False)),
            }
            for name, info in KNOWN_HELPERS.items()
        ],
        "wired_live_probes": [info["sensor"] for info in KNOWN_HELPERS.values() if info.get("wired_live")],
        "configured_sources": sorted(VALID_SOURCES),
        "silent_sources": sorted(VALID_SOURCES - {"feedback"}),
        "blind_spots": list(BLIND_SPOTS),
        # Subconscious-owned, hot-loadable internal probe families. These are NOT
        # cheap deterministic sensors and NOT model-visible tools; they call
        # Hindsight reflect/recall and emit compact reduced signals only.
        "internal_probe_families": [dict(MEMORY_REFLECTION_PROBE_FAMILY)],
    }


def run_smoke_probes(*, state_dir: str) -> dict:
    """Exercise three source classes end-to-end through ingest/promotion."""
    instance = "probe_smoke"
    probes = [
        ("session_event", session_event_signal(
            kind="design_decision",
            summary="Probe smoke: session event validation",
            strength_hint=0.9,
            correlation_keys=["probe_smoke_session"],
        )),
        ("artifact", artifact_signal(
            path="/tmp/probe_smoke_artifact.py",
            summary="Probe smoke: artifact validation",
            kind="artifact_created",
            strength_hint=0.9,
            correlation_keys=["probe_smoke_artifact"],
        )),
        ("operator", operator_signal(
            summary="Probe smoke: explicit operator validation",
            kind="user_correction",
            strength_hint=0.9,
            correlation_keys=["probe_smoke_operator"],
        )),
    ]

    results = []
    for source_class, sig in probes:
        raw = handle_sensorium_ingest_signal(
            signal=sig, instance=instance, state_dir=state_dir,
        )
        result = json.loads(raw)
        results.append({
            "source_class": source_class,
            "result": result.get("data", {}),
        })

    store = SensoriumStore(instance=instance, state_dir=state_dir)
    return {
        "probes": results,
        "counts": {
            "signals": len(store.read_jsonl("signals")),
            "events": len(store.read_jsonl("events")),
            "candidates": len(store.read_jsonl("candidates")),
        },
        "all_promoted": all(p["result"].get("promoted") for p in results),
    }


def audit_store(*, state_dir: str | None = None, instance: str = "default") -> dict:
    """Audit an existing store and report coverage metrics."""
    store = SensoriumStore(instance=instance, state_dir=state_dir)

    signals = store.read_jsonl("signals")
    events = store.read_jsonl("events")
    candidates = store.read_jsonl("candidates")

    by_sensor: dict[str, int] = {}
    by_source: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    latest_by_source: dict[str, str] = {}

    for sig in signals:
        sensor = sig.get("sensor", "unknown")
        source = sig.get("source", "unknown")
        kind = sig.get("kind", "unknown")
        ts = sig.get("ts", "")

        by_sensor[sensor] = by_sensor.get(sensor, 0) + 1
        by_source[source] = by_source.get(source, 0) + 1
        by_kind[kind] = by_kind.get(kind, 0) + 1

        if ts > latest_by_source.get(source, ""):
            latest_by_source[source] = ts

    active_sources = set(by_source.keys())
    configured_sources = VALID_SOURCES - {"feedback"}
    total_signals = len(signals)
    promoted_count = len(events)

    return {
        "counts": {
            "signals": total_signals,
            "events": promoted_count,
            "candidates": len(candidates),
            "active_candidates": len([c for c in candidates if c.get("status") == "candidate"]),
        },
        "by_sensor": by_sensor,
        "by_source": by_source,
        "by_kind": by_kind,
        "freshness": latest_by_source,
        "promotion_yield": {
            "total_signals": total_signals,
            "promoted_events": promoted_count,
            "yield_pct": round(promoted_count / total_signals * 100, 1) if total_signals else 0.0,
        },
        "silent_sources": sorted(configured_sources - active_sources),
        "blind_spots": [b["name"] for b in BLIND_SPOTS],
    }
