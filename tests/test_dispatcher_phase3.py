"""Phase 3 tests: dispatcher lock/leases, budgets, and state.latest."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from agent_sensorium.store import SensoriumStore
from agent_sensorium.tools import (
    handle_sensorium_dispatch_once,
    handle_sensorium_ingest_signal,
    handle_sensorium_status,
)


def _strong_signal(summary: str = "Strong dispatch candidate") -> dict:
    return {
        "sensor": "test",
        "source": "manual",
        "kind": "design_decision",
        "summary": summary,
        "strength_hint": 0.9,
        "correlation_keys": ["phase3"],
    }


def _seed_candidate(state_dir: str, summary: str = "Strong dispatch candidate") -> str:
    raw = handle_sensorium_ingest_signal(
        signal=_strong_signal(summary), instance="test", state_dir=state_dir
    )
    return json.loads(raw)["data"]["candidate_id"]


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class TestDispatcherStateLatest:
    def test_real_dispatch_writes_state_latest_with_last_result_and_budgets(self, tmp_path):
        state_dir = str(tmp_path / "sensorium")
        candidate_id = _seed_candidate(state_dir)

        raw = handle_sensorium_dispatch_once(
            instance="test", state_dir=state_dir, dry_run=False
        )
        result = json.loads(raw)

        assert result["success"] is True
        assert result["data"]["action"] == "promoted"

        store = SensoriumStore(instance="test", state_dir=state_dir)
        state = store.read_state()
        assert state["state_version"] == 1
        assert state["last_dispatch_result"]["action"] == "promoted"
        assert state["last_dispatch_result"]["candidate_id"] == candidate_id
        assert state["last_dispatch_result"]["thread_id"] == result["data"]["thread_id"]
        assert set(state["budgets"]) >= {"dispatch", "pointer", "conscious", "advisory"}
        assert state["budgets"]["dispatch"]["capacity"] >= 1
        assert state["locks"]["dispatcher"]["status"] == "free"

        status = json.loads(handle_sensorium_status(instance="test", state_dir=state_dir))["data"]
        assert status["state_version"] == 1
        assert status["last_dispatch_result"]["action"] == "promoted"
        assert set(status["budgets"]) >= {"dispatch", "pointer", "conscious", "advisory"}

    def test_dry_run_updates_state_latest_without_consuming_dispatch_budget(self, tmp_path):
        state_dir = str(tmp_path / "sensorium")
        _seed_candidate(state_dir)

        raw = handle_sensorium_dispatch_once(
            instance="test", state_dir=state_dir, dry_run=True
        )
        result = json.loads(raw)
        assert result["data"]["action"] == "would_promote"

        store = SensoriumStore(instance="test", state_dir=state_dir)
        state = store.read_state()
        assert state["last_dispatch_result"]["action"] == "would_promote"
        assert state["budgets"]["dispatch"]["remaining"] == state["budgets"]["dispatch"]["capacity"]
        assert store.read_jsonl("threads") == []


class TestDispatcherLocksAndBudgets:
    def test_active_dispatch_lock_refuses_mutating_dispatch(self, tmp_path):
        state_dir = str(tmp_path / "sensorium")
        _seed_candidate(state_dir)
        store = SensoriumStore(instance="test", state_dir=state_dir)
        store.ensure_dirs()
        lock_dir = store.root / "locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        future = datetime.now(timezone.utc) + timedelta(minutes=5)
        (lock_dir / "dispatcher.lock").write_text(json.dumps({
            "owner": "other-process",
            "acquired_at": _iso(datetime.now(timezone.utc)),
            "expires_at": _iso(future),
        }))

        raw = handle_sensorium_dispatch_once(
            instance="test", state_dir=state_dir, dry_run=False
        )
        result = json.loads(raw)

        assert result["success"] is True
        assert result["data"]["action"] == "lock_unavailable"
        assert store.read_jsonl("threads") == []
        assert store.read_state()["last_dispatch_result"]["action"] == "lock_unavailable"

    def test_stale_dispatch_lock_is_recovered_with_receipt(self, tmp_path):
        state_dir = str(tmp_path / "sensorium")
        _seed_candidate(state_dir)
        store = SensoriumStore(instance="test", state_dir=state_dir)
        store.ensure_dirs()
        lock_dir = store.root / "locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        expired = datetime.now(timezone.utc) - timedelta(minutes=5)
        (lock_dir / "dispatcher.lock").write_text(json.dumps({
            "owner": "dead-process",
            "acquired_at": _iso(expired - timedelta(minutes=5)),
            "expires_at": _iso(expired),
        }))

        raw = handle_sensorium_dispatch_once(
            instance="test", state_dir=state_dir, dry_run=False
        )
        result = json.loads(raw)

        assert result["data"]["action"] == "promoted"
        decisions = store.read_jsonl("decisions")
        assert any(d.get("type") == "dispatch.lock_recovered" for d in decisions)
        assert not (lock_dir / "dispatcher.lock").exists()

    def test_dispatch_budget_exhaustion_refuses_new_thread(self, tmp_path):
        state_dir = str(tmp_path / "sensorium")
        _seed_candidate(state_dir)
        config = {
            "budgets": {"dispatch": {"capacity": 0, "window_seconds": 3600}},
        }

        raw = handle_sensorium_dispatch_once(
            instance="test", state_dir=state_dir, dry_run=False, config=config
        )
        result = json.loads(raw)

        assert result["data"]["action"] == "budget_exhausted"
        store = SensoriumStore(instance="test", state_dir=state_dir)
        assert store.read_jsonl("threads") == []
        assert store.read_state()["budgets"]["dispatch"]["remaining"] == 0

    def test_partial_dispatch_budget_override_preserves_default_window(self, tmp_path):
        state_dir = str(tmp_path / "sensorium")
        _seed_candidate(state_dir)
        config = {"budgets": {"dispatch": {"capacity": 5}}}

        raw = handle_sensorium_dispatch_once(
            instance="test", state_dir=state_dir, dry_run=True, config=config
        )
        result = json.loads(raw)
        assert result["data"]["action"] == "would_promote"

        store = SensoriumStore(instance="test", state_dir=state_dir)
        dispatch_budget = store.read_state()["budgets"]["dispatch"]
        assert dispatch_budget["capacity"] == 5
        assert dispatch_budget["remaining"] == 5
        assert dispatch_budget["window_seconds"] == 3600

    def test_repeated_dispatch_for_same_candidate_reuses_existing_thread(self, tmp_path):
        state_dir = str(tmp_path / "sensorium")
        _seed_candidate(state_dir)

        first = json.loads(handle_sensorium_dispatch_once(
            instance="test", state_dir=state_dir, dry_run=False
        ))
        second = json.loads(handle_sensorium_dispatch_once(
            instance="test", state_dir=state_dir, dry_run=False
        ))

        assert first["data"]["action"] == "promoted"
        assert second["data"]["action"] == "already_exists"
        assert second["data"]["thread_id"] == first["data"]["thread_id"]
        store = SensoriumStore(instance="test", state_dir=state_dir)
        assert len(store.read_jsonl("threads")) == 1
        assert store.read_state()["last_dispatch_result"]["action"] == "already_exists"
