"""Tests for agent_sensorium.dispatcher — candidate selection and thread creation."""

import json

import pytest

from agent_sensorium.dispatcher import (
    candidate_to_thread,
    dispatch_once,
    select_candidate,
)
from agent_sensorium.store import SensoriumStore
from agent_sensorium.tools import handle_sensorium_ingest_signal


@pytest.fixture
def state_dir(tmp_path):
    return str(tmp_path / "sensorium")


@pytest.fixture
def store(state_dir):
    s = SensoriumStore(instance="test", state_dir=state_dir)
    s.ensure_dirs()
    return s


def _make_candidate(pressure=0.7, cand_id="cand_test1", status="candidate"):
    return {
        "id": cand_id,
        "status": status,
        "kind": "design_decision",
        "pressure": pressure,
        "summary": "Test candidate",
        "event_ids": ["evt_x"],
        "correlation_keys": ["test-key"],
        "sensitivity": "private",
        "allowed_surfaces": ["local", "dashboard"],
        "created_at": "2026-05-24T10:00:00Z",
        "updated_at": "2026-05-24T10:00:00Z",
        "expires_at": "",
    }


class TestSelectCandidate:
    def test_no_candidates_returns_none(self):
        assert select_candidate([]) is None

    def test_below_threshold_returns_none(self):
        c = _make_candidate(pressure=0.3)
        assert select_candidate([c]) is None

    def test_selects_highest_pressure(self):
        c1 = _make_candidate(pressure=0.6, cand_id="cand_low")
        c2 = _make_candidate(pressure=0.9, cand_id="cand_high")
        result = select_candidate([c1, c2])
        assert result["id"] == "cand_high"

    def test_ignores_non_candidate_status(self):
        c = _make_candidate(pressure=0.9, status="suppressed")
        assert select_candidate([c]) is None


class TestCandidateToThread:
    def test_thread_shape(self):
        c = _make_candidate()
        thread = candidate_to_thread(c)
        assert thread["id"].startswith("sth_")
        assert thread["status"] == "dormant"
        assert thread["origin"] == "candidate"
        assert thread["origin_candidate_id"] == "cand_test1"
        assert thread["conscious_task"]["id"].startswith("ctask_")
        assert thread["conscious_task"]["request_type"] == "THINK"
        assert thread["summary_dirty"] is False
        assert isinstance(thread["decision_log"], list)
        assert isinstance(thread["continuity_summary"], list)
        assert len(thread["continuity_summary"]) >= 1
        assert thread["sensitivity"] == "private"
        assert thread["allowed_surfaces"] == ["local", "dashboard"]
        assert thread["created_at"]
        assert thread["expires_at"]

    def test_operational_pointer_policy_can_expose_thread_without_changing_candidate(self):
        c = _make_candidate()
        c["kind"] = "process_pressure"
        c["sensitivity"] = "local_only"
        c["allowed_surfaces"] = ["local"]
        thread = candidate_to_thread(c, {
            "operational_pointer": {
                "enabled": True,
                "kinds": ["process_pressure"],
                "surfaces": ["discord"],
                "sensitivity": "private",
            }
        })
        assert c["allowed_surfaces"] == ["local"]
        assert c["sensitivity"] == "local_only"
        assert thread["allowed_surfaces"] == ["discord", "local"]
        assert thread["sensitivity"] == "private"
        assert thread["visibility_policy"]["mode"] == "operational_pointer"

    def test_operational_pointer_policy_does_not_expose_unlisted_kinds(self):
        c = _make_candidate()
        c["kind"] = "body_pressure"
        c["allowed_surfaces"] = ["local"]
        thread = candidate_to_thread(c, {
            "operational_pointer": {
                "enabled": True,
                "kinds": ["process_pressure"],
                "surfaces": ["discord"],
                "sensitivity": "private",
            }
        })
        assert thread["allowed_surfaces"] == ["local"]
        assert "visibility_policy" not in thread


class TestDispatchOnce:
    def test_no_candidates(self, store):
        result = dispatch_once(store, dry_run=False)
        assert result["action"] == "no_candidate"

    def test_dry_run_does_not_mutate(self, store):
        store.append_jsonl("candidates", _make_candidate(pressure=0.8))
        result = dispatch_once(store, dry_run=True)
        assert result["action"] == "kanban_review_required"
        assert result["dry_run"] is True
        assert store.read_jsonl("threads") == []
        assert store.read_jsonl("decisions") == []

    def test_real_dispatch_disabled_by_default(self, store):
        store.append_jsonl("candidates", _make_candidate(pressure=0.8))
        result = dispatch_once(store, dry_run=False)
        assert result["action"] == "legacy_dispatch_disabled"
        assert result["recommended_activation"] == "kanban_bridge"
        assert store.read_jsonl("threads") == []
        assert store.read_jsonl("decisions") == []

    def test_legacy_dispatch_opt_in_creates_thread(self, store):
        store.append_jsonl("candidates", _make_candidate(pressure=0.8))
        result = dispatch_once(store, dry_run=False, config={"legacy_thread_dispatch_enabled": True})
        assert result["action"] == "promoted"
        assert result["thread_id"].startswith("sth_")
        threads = store.read_jsonl("threads")
        assert len(threads) == 1
        assert threads[0]["id"] == result["thread_id"]
        decisions = store.read_jsonl("decisions")
        assert len(decisions) == 1
        assert decisions[0]["type"] == "dispatch.promoted_to_thread"

    def test_idempotent_no_duplicate(self, store):
        store.append_jsonl("candidates", _make_candidate(pressure=0.8))
        r1 = dispatch_once(store, dry_run=False, config={"legacy_thread_dispatch_enabled": True})
        assert r1["action"] == "promoted"
        r2 = dispatch_once(store, dry_run=False, config={"legacy_thread_dispatch_enabled": True})
        assert r2["action"] == "already_exists"
        assert r2["thread_id"] == r1["thread_id"]
        assert len(store.read_jsonl("threads")) == 1

    def test_end_to_end_smoke(self, state_dir):
        """Ingest strong signal → dispatch dry → dispatch real → dispatch again → one thread only."""
        signal = {
            "sensor": "explicit_operator_signal",
            "source": "manual",
            "kind": "design_decision",
            "summary": "Operator corrected demo image references for continuity.",
            "strength_hint": 0.9,
            "correlation_keys": ["visual-continuity", "references-first"],
            "sensitivity": "private",
            "allowed_surfaces": ["local", "dashboard"],
        }
        raw = handle_sensorium_ingest_signal(signal=signal, instance="test", state_dir=state_dir)
        ingest_result = json.loads(raw)
        assert ingest_result["data"]["promoted"] is True

        store = SensoriumStore(instance="test", state_dir=state_dir)

        dry = dispatch_once(store, dry_run=True)
        assert dry["action"] == "kanban_review_required"
        assert store.read_jsonl("threads") == []

        real1 = dispatch_once(store, dry_run=False)
        assert real1["action"] == "legacy_dispatch_disabled"
        assert store.read_jsonl("threads") == []

        real1 = dispatch_once(store, dry_run=False, config={"legacy_thread_dispatch_enabled": True})
        assert real1["action"] == "promoted"

        real2 = dispatch_once(store, dry_run=False, config={"legacy_thread_dispatch_enabled": True})
        assert real2["action"] == "already_exists"
        assert real2["thread_id"] == real1["thread_id"]

        threads = store.read_jsonl("threads")
        assert len(threads) == 1
        assert threads[0]["status"] == "dormant"
