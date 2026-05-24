"""Tests for sensorium_compact — TTL cleanup and archive receipts."""

import json

import pytest

from agent_sensorium.store import SensoriumStore
from agent_sensorium.tools import (
    handle_sensorium_compact,
    handle_sensorium_ingest_signal,
    handle_sensorium_dispatch_once,
    handle_sensorium_status,
)


@pytest.fixture
def state_dir(tmp_path):
    return str(tmp_path / "sensorium")


@pytest.fixture
def store(state_dir):
    s = SensoriumStore(instance="test", state_dir=state_dir)
    s.ensure_dirs()
    return s


def _expired_candidate(cand_id="cand_exp1", status="candidate"):
    return {
        "id": cand_id,
        "status": status,
        "kind": "design_decision",
        "pressure": 0.7,
        "summary": "Expired candidate",
        "event_ids": ["evt_x"],
        "correlation_keys": ["test"],
        "sensitivity": "private",
        "allowed_surfaces": ["local"],
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "expires_at": "2026-01-02T00:00:00Z",
    }


def _active_candidate(cand_id="cand_act1"):
    return {
        "id": cand_id,
        "status": "candidate",
        "kind": "design_decision",
        "pressure": 0.7,
        "summary": "Active candidate",
        "event_ids": ["evt_y"],
        "correlation_keys": ["test"],
        "sensitivity": "private",
        "allowed_surfaces": ["local"],
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "expires_at": "2099-12-31T23:59:59Z",
    }


def _expired_thread(thread_id="sth_exp1", pinned=False):
    t = {
        "id": thread_id,
        "status": "dormant",
        "origin": "candidate",
        "conscious_task": {"id": "ctask_x", "request_type": "THINK", "title": "Test"},
        "origin_candidate_id": "cand_x",
        "continuity_summary": ["test"],
        "decision_log": [],
        "interaction_refs": [],
        "summary_dirty": False,
        "open_questions": [],
        "next_prompt_to_operator": "?",
        "sensitivity": "private",
        "allowed_surfaces": ["local"],
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "expires_at": "2026-01-02T00:00:00Z",
    }
    if pinned:
        t["pinned"] = True
    return t


class TestCompactCandidates:
    def test_expired_candidate_archived(self, store, state_dir):
        store.append_jsonl("candidates", _expired_candidate())
        raw = handle_sensorium_compact(instance="test", state_dir=state_dir)
        result = json.loads(raw)
        assert result["success"] is True
        assert "cand_exp1" in result["data"]["archived_candidates"]
        assert result["data"]["receipts_written"] == 1

        candidates = store.read_jsonl("candidates")
        assert candidates[0]["status"] == "archived"

    def test_active_candidate_not_archived(self, store, state_dir):
        store.append_jsonl("candidates", _active_candidate())
        raw = handle_sensorium_compact(instance="test", state_dir=state_dir)
        result = json.loads(raw)
        assert result["data"]["archived_candidates"] == []

        candidates = store.read_jsonl("candidates")
        assert candidates[0]["status"] == "candidate"

    def test_suppressed_candidate_archived(self, store, state_dir):
        c = _active_candidate(cand_id="cand_supp")
        c["status"] = "suppressed"
        store.append_jsonl("candidates", c)
        raw = handle_sensorium_compact(instance="test", state_dir=state_dir)
        result = json.loads(raw)
        assert "cand_supp" in result["data"]["archived_candidates"]

        decisions = store.read_jsonl("decisions")
        receipt = decisions[0]
        assert receipt["type"] == "compact.candidate_archived"
        assert receipt["previous_status"] == "suppressed"
        assert "terminal_status" in receipt["reason"]

    def test_cancelled_candidate_archived(self, store, state_dir):
        c = _active_candidate(cand_id="cand_canc")
        c["status"] = "cancelled"
        store.append_jsonl("candidates", c)
        raw = handle_sensorium_compact(instance="test", state_dir=state_dir)
        result = json.loads(raw)
        assert "cand_canc" in result["data"]["archived_candidates"]

    def test_already_archived_candidate_untouched(self, store, state_dir):
        c = _expired_candidate(cand_id="cand_already")
        c["status"] = "archived"
        store.append_jsonl("candidates", c)
        raw = handle_sensorium_compact(instance="test", state_dir=state_dir)
        result = json.loads(raw)
        assert result["data"]["archived_candidates"] == []
        assert result["data"]["receipts_written"] == 0

    def test_no_expires_not_archived(self, store, state_dir):
        c = _active_candidate(cand_id="cand_noexp")
        c["expires_at"] = ""
        store.append_jsonl("candidates", c)
        raw = handle_sensorium_compact(instance="test", state_dir=state_dir)
        result = json.loads(raw)
        assert result["data"]["archived_candidates"] == []


class TestCompactThreads:
    def test_expired_thread_archived(self, store, state_dir):
        store.append_jsonl("threads", _expired_thread())
        raw = handle_sensorium_compact(instance="test", state_dir=state_dir)
        result = json.loads(raw)
        assert "sth_exp1" in result["data"]["archived_threads"]
        assert result["data"]["receipts_written"] == 1

        threads = store.read_jsonl("threads")
        assert threads[0]["status"] == "archived"

    def test_pinned_thread_not_archived(self, store, state_dir):
        store.append_jsonl("threads", _expired_thread(thread_id="sth_pinned", pinned=True))
        raw = handle_sensorium_compact(instance="test", state_dir=state_dir)
        result = json.loads(raw)
        assert result["data"]["archived_threads"] == []

        threads = store.read_jsonl("threads")
        assert threads[0]["status"] == "dormant"

    def test_archived_thread_excluded_from_status(self, store, state_dir):
        store.append_jsonl("threads", _expired_thread())
        handle_sensorium_compact(instance="test", state_dir=state_dir)
        raw = handle_sensorium_status(instance="test", state_dir=state_dir)
        status = json.loads(raw)["data"]
        assert status["counts"]["dormant_threads"] == 0
        assert status["top_threads"] == []


class TestCompactReceipts:
    def test_receipt_written_for_each_archive(self, store, state_dir):
        store.append_jsonl("candidates", _expired_candidate(cand_id="cand_r1"))
        store.append_jsonl("candidates", _expired_candidate(cand_id="cand_r2"))
        store.append_jsonl("threads", _expired_thread(thread_id="sth_r1"))
        raw = handle_sensorium_compact(instance="test", state_dir=state_dir)
        result = json.loads(raw)
        assert result["data"]["receipts_written"] == 3

        decisions = store.read_jsonl("decisions")
        assert len(decisions) == 3
        types = {d["type"] for d in decisions}
        assert "compact.candidate_archived" in types
        assert "compact.thread_archived" in types

    def test_empty_state_compact_is_noop(self, state_dir):
        raw = handle_sensorium_compact(instance="test", state_dir=state_dir)
        result = json.loads(raw)
        assert result["success"] is True
        assert result["data"]["archived_candidates"] == []
        assert result["data"]["archived_threads"] == []
        assert result["data"]["receipts_written"] == 0


class TestCompactEndToEnd:
    def test_ingest_dispatch_compact_flow(self, state_dir):
        signal = {
            "sensor": "test",
            "source": "manual",
            "kind": "design_decision",
            "summary": "E2E compact test",
            "strength_hint": 0.9,
        }
        handle_sensorium_ingest_signal(signal=signal, instance="test", state_dir=state_dir)
        handle_sensorium_dispatch_once(instance="test", state_dir=state_dir, dry_run=False)

        status_before = json.loads(
            handle_sensorium_status(instance="test", state_dir=state_dir)
        )["data"]
        assert status_before["counts"]["dormant_threads"] == 1

        store = SensoriumStore(instance="test", state_dir=state_dir)
        threads = store.read_jsonl("threads")
        threads[0]["expires_at"] = "2020-01-01T00:00:00Z"
        from agent_sensorium.tools import _rewrite_jsonl
        _rewrite_jsonl(store, "threads", threads)

        handle_sensorium_compact(instance="test", state_dir=state_dir)

        status_after = json.loads(
            handle_sensorium_status(instance="test", state_dir=state_dir)
        )["data"]
        assert status_after["counts"]["dormant_threads"] == 0
