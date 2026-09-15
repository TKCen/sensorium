"""Malformed optional advisory metadata cannot acquire or block authority."""
from __future__ import annotations

import pytest

from agent_sensorium.admission import binding_for_candidate, build_admission_plan
from agent_sensorium.store import SensoriumStore


def _source(tmp_path):
    store = SensoriumStore(instance="metadata", state_dir=str(tmp_path / "metadata"))
    store.ensure_dirs()
    store.append_jsonl("signals", {
        "id": "signal-a", "sensor": "research.source_feed", "source": "artifact",
        "kind": "creative_pull", "summary": "Synthetic source",
        "artifact_meta": {"source_id": "feed", "item_id": "item-a"},
    })
    store.append_jsonl("events", {
        "id": "event-a", "kind": "creative_pull", "summary": "Synthetic event",
        "source_signal_ids": ["signal-a"],
    })
    store.append_jsonl("candidates", {
        "id": "candidate-a", "status": "candidate", "kind": "creative_pull",
        "summary": "Synthetic candidate", "pressure": 0.8,
        "event_ids": ["event-a"], "created_at": "2026-01-01T00:00:00Z",
    })
    return store


@pytest.mark.parametrize("metadata", [["not-a-mapping"], "not-a-mapping", 1, True])
def test_malformed_advisory_does_not_abort_unrelated_source(tmp_path, metadata):
    store = _source(tmp_path)
    store.append_jsonl("candidates", {
        "id": "malformed", "kind": "subconscious_advisory", "status": "candidate",
        "advisory_meta": metadata,
    })
    before = {p: p.read_bytes() for p in store.root.rglob("*.jsonl")}
    plan = build_admission_plan(store)
    assert plan["selection"]["source_candidate_id"] == "candidate-a"
    assert {p: p.read_bytes() for p in store.root.rglob("*.jsonl")} == before


@pytest.mark.parametrize("top_binding", [["not-a-mapping"], "not-a-mapping", 1, True])
def test_valid_nested_binding_survives_malformed_top_binding(tmp_path, top_binding):
    store = _source(tmp_path)
    binding, error = binding_for_candidate(store, "candidate-a")
    assert error is None
    store.append_jsonl("candidates", {
        "id": "represented", "kind": "subconscious_advisory", "status": "held",
        "admission_binding": top_binding,
        "advisory_meta": {"admission_binding": binding, "action": "SAVE"},
    })
    plan = build_admission_plan(store)
    assert plan["selection"] is None
    assert plan["suppressed_counts"] == {"prior_disposition": 1}
