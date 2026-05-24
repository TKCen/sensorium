"""Tests for agent_sensorium.store."""

import json
import tempfile

import pytest

from agent_sensorium.store import SensoriumStore


@pytest.fixture
def tmp_store(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "sensorium"))
    store.ensure_dirs()
    return store


class TestEnsureDirs:
    def test_creates_structure(self, tmp_path):
        store = SensoriumStore(instance="test", state_dir=str(tmp_path / "new"))
        store.ensure_dirs()
        assert (tmp_path / "new").is_dir()
        assert (tmp_path / "new" / "signals").is_dir()
        assert (tmp_path / "new" / "archive").is_dir()

    def test_idempotent(self, tmp_store):
        tmp_store.ensure_dirs()
        tmp_store.ensure_dirs()


class TestAppendReadJsonl:
    def test_round_trip(self, tmp_store):
        obj = {"id": "sig_001", "kind": "test"}
        tmp_store.append_jsonl("signals", obj)
        result = tmp_store.read_jsonl("signals")
        assert len(result) == 1
        assert result[0]["id"] == "sig_001"

    def test_multiple_appends(self, tmp_store):
        for i in range(5):
            tmp_store.append_jsonl("events", {"id": f"evt_{i}"})
        result = tmp_store.read_jsonl("events")
        assert len(result) == 5

    def test_limit(self, tmp_store):
        for i in range(10):
            tmp_store.append_jsonl("signals", {"id": f"sig_{i}"})
        result = tmp_store.read_jsonl("signals", limit=3)
        assert len(result) == 3
        assert result[0]["id"] == "sig_7"

    def test_corrupted_line_skipped(self, tmp_store):
        path = tmp_store._resolve("signals")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            f.write('{"id":"sig_1"}\n')
            f.write("NOT VALID JSON\n")
            f.write('{"id":"sig_2"}\n')
        result = tmp_store.read_jsonl("signals")
        assert len(result) == 2
        assert result[0]["id"] == "sig_1"
        assert result[1]["id"] == "sig_2"

    def test_empty_file(self, tmp_store):
        result = tmp_store.read_jsonl("signals")
        assert result == []

    def test_unknown_name_raises(self, tmp_store):
        with pytest.raises(ValueError, match="Unknown state name"):
            tmp_store.append_jsonl("bogus", {})


class TestState:
    def test_write_read_state(self, tmp_store):
        state = {"version": 1, "last_ts": "2026-01-01T00:00:00Z"}
        tmp_store.write_state(state)
        result = tmp_store.read_state()
        assert result["version"] == 1

    def test_read_missing_state(self, tmp_store):
        assert tmp_store.read_state() == {}


class TestPaths:
    def test_paths_property(self, tmp_store):
        paths = tmp_store.paths
        assert "signals" in paths
        assert "events" in paths
        assert str(paths["signals"]).endswith("signals/inbox.jsonl")
