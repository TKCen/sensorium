"""Tests for the deterministic runtime heartbeat sensor."""

import json

from agent_sensorium.schemas import validate_signal
from agent_sensorium.sensors import (
    build_runtime_heartbeat_signal,
    register_sensor_kind,
    runtime_heartbeat_sample,
)
from agent_sensorium.store import SensoriumStore


def _make_store(tmp_path) -> SensoriumStore:
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "sensorium"))
    store.ensure_dirs()
    return store


class TestRuntimeHeartbeatSample:
    def test_empty_profile_is_all_zeros(self, tmp_path):
        store = _make_store(tmp_path)
        sample = runtime_heartbeat_sample(store=store)
        assert set(sample) == {
            "state_dir_exists",
            "counts",
            "pending_threads",
            "registry",
            "last_decision_age_seconds",
        }
        assert sample["state_dir_exists"] is True
        assert sample["counts"] == {"signals": 0, "events": 0, "candidates": 0, "threads": 0}
        assert sample["pending_threads"] == 0
        assert sample["registry"]["sensor_count"] == 0
        assert sample["last_decision_age_seconds"] is None

    def test_counts_and_pending_and_registry(self, tmp_path):
        store = _make_store(tmp_path)
        store.append_jsonl("signals", {"id": "s1"})
        store.append_jsonl("signals", {"id": "s2"})
        store.append_jsonl("threads", {"id": "t1", "status": "dormant"})
        store.append_jsonl("threads", {"id": "t2", "status": "active"})
        store.append_jsonl("threads", {"id": "t3", "status": "dormant"})
        register_sensor_kind("runtime_heartbeat", defaults={"strength_hint": 0.2}, store=store)

        sample = runtime_heartbeat_sample(store=store)
        assert sample["counts"]["signals"] == 2
        assert sample["counts"]["threads"] == 3
        assert sample["pending_threads"] == 2
        assert sample["registry"]["sensor_count"] == 1
        assert sample["registry"]["active"] == ["runtime_heartbeat"]

    def test_last_decision_age_seconds(self, tmp_path):
        store = _make_store(tmp_path)
        store.append_jsonl("decisions", {"ts": "2000-01-01T00:00:00Z", "type": "old"})
        sample = runtime_heartbeat_sample(store=store)
        assert isinstance(sample["last_decision_age_seconds"], float)
        assert sample["last_decision_age_seconds"] > 0


class TestBuildRuntimeHeartbeatSignal:
    def test_signal_validates_and_is_low_strength(self, tmp_path):
        store = _make_store(tmp_path)
        store.append_jsonl("signals", {"id": "s1"})
        sample = runtime_heartbeat_sample(store=store)
        signal = build_runtime_heartbeat_signal(sample)

        validate_signal(signal)
        assert signal["strength_hint"] == 0.2
        assert signal["sensitivity"] == "local_only"
        assert signal["allowed_surfaces"] == ["local"]
        assert signal["source"] == "machine"
        assert signal["kind"] == "runtime_heartbeat"

    def test_values_contain_no_absolute_path(self, tmp_path):
        store = _make_store(tmp_path)
        sample = runtime_heartbeat_sample(store=store)
        signal = build_runtime_heartbeat_signal(sample)
        serialized = json.dumps(signal["values"])
        assert str(tmp_path) not in serialized
        assert str(store.root) not in serialized
