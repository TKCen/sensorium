"""Tests for the Attention Inbox / conscious aperture surface."""

import json
import os

import pytest

from agent_sensorium.attention import (
    CANDIDATE_DECISIONS,
    THREAD_DECISIONS,
    build_attention_inbox,
    visible_on_surface,
)
from agent_sensorium.store import SensoriumStore
from agent_sensorium.tools import handle_sensorium_attention_inbox


@pytest.fixture
def state_dir(tmp_path):
    return str(tmp_path / "sensorium")


@pytest.fixture
def store(state_dir):
    s = SensoriumStore(instance="test", state_dir=state_dir)
    s.ensure_dirs()
    return s


def _make_candidate(
    cand_id="cand_1",
    pressure=0.7,
    status="candidate",
    kind="design_decision",
    sensitivity="private",
    surfaces=None,
):
    return {
        "id": cand_id,
        "status": status,
        "kind": kind,
        "pressure": pressure,
        "summary": f"Test candidate {cand_id}",
        "event_ids": ["evt_x"],
        "correlation_keys": ["test-key"],
        "sensitivity": sensitivity,
        "allowed_surfaces": surfaces or ["local"],
        "created_at": "2026-05-26T10:00:00Z",
        "updated_at": "2026-05-26T12:00:00Z",
        "expires_at": "",
    }


def _make_thread(
    thread_id="sth_1",
    status="dormant",
    sensitivity="private",
    surfaces=None,
    title="Review something",
    hold_reason="",
    resume_trigger="",
):
    return {
        "id": thread_id,
        "status": status,
        "origin": "candidate",
        "conscious_task": {
            "id": "ctask_1",
            "request_type": "THINK",
            "title": title,
            "why": "Candidate pressure crossed threshold.",
            "expected_decision": "Suppress, hold, or open.",
        },
        "origin_candidate_id": "cand_origin",
        "continuity_summary": ["Test continuity"],
        "decision_log": [],
        "interaction_refs": [],
        "summary_dirty": False,
        "open_questions": [],
        "next_prompt_to_operator": "test",
        "sensitivity": sensitivity,
        "allowed_surfaces": surfaces or ["local"],
        "dirty_since": None,
        "hold_reason": hold_reason,
        "resume_trigger": resume_trigger,
        "last_interaction_at": "2026-05-26T11:00:00Z",
        "source_refs": ["ref-1"],
        "created_at": "2026-05-26T10:00:00Z",
        "updated_at": "2026-05-26T11:00:00Z",
        "expires_at": "2026-06-02T10:00:00Z",
    }


class TestVisibleOnSurface:
    def test_item_on_allowed_surface(self):
        item = {"allowed_surfaces": ["local", "dashboard"], "sensitivity": "private"}
        config = {"allowed_surfaces": ["local"], "max_sensitivity": "private"}
        assert visible_on_surface(item, "local", config) is True

    def test_item_not_on_requested_surface(self):
        item = {"allowed_surfaces": ["local"], "sensitivity": "private"}
        config = {"allowed_surfaces": ["local", "dashboard"], "max_sensitivity": "private"}
        assert visible_on_surface(item, "dashboard", config) is False

    def test_surface_not_in_config(self):
        item = {"allowed_surfaces": ["local", "discord"], "sensitivity": "private"}
        config = {"allowed_surfaces": ["local"], "max_sensitivity": "private"}
        assert visible_on_surface(item, "discord", config) is False

    def test_sensitivity_exceeds_max(self):
        item = {"allowed_surfaces": ["local"], "sensitivity": "public_safe"}
        config = {"allowed_surfaces": ["local"], "max_sensitivity": "private"}
        assert visible_on_surface(item, "local", config) is False

    def test_local_only_sensitivity_passes_on_local(self):
        item = {"allowed_surfaces": ["local"], "sensitivity": "local_only"}
        config = {"allowed_surfaces": ["local"], "max_sensitivity": "private"}
        assert visible_on_surface(item, "local", config) is True

    def test_empty_surface_fails_closed(self):
        item = {"allowed_surfaces": ["local"], "sensitivity": "private"}
        config = {"allowed_surfaces": ["local"], "max_sensitivity": "private"}
        assert visible_on_surface(item, "", config) is False

    def test_missing_allowed_surfaces_fails_closed(self):
        item = {"sensitivity": "private"}
        config = {"allowed_surfaces": ["local"], "max_sensitivity": "private"}
        assert visible_on_surface(item, "local", config) is False

    def test_missing_config_surfaces_fails_closed(self):
        item = {"allowed_surfaces": ["local"], "sensitivity": "private"}
        config = {"max_sensitivity": "private"}
        assert visible_on_surface(item, "local", config) is False


class TestBuildAttentionInbox:
    def test_empty_store(self, store):
        inbox = build_attention_inbox(store, surface="local")
        assert inbox["counts"]["total"] == 0
        assert inbox["counts"]["candidates"] == 0
        assert inbox["counts"]["threads"] == 0
        assert inbox["counts"]["filtered_out"] == 0
        assert inbox["items"] == []
        assert inbox["config_diagnostics"]["surface"] == "local"
        assert "ts" in inbox

    def test_includes_active_candidates(self, store):
        store.append_jsonl("candidates", _make_candidate("cand_a", pressure=0.8))
        store.append_jsonl("candidates", _make_candidate("cand_b", pressure=0.6))
        inbox = build_attention_inbox(store, surface="local")
        assert inbox["counts"]["candidates"] == 2
        ids = [i["id"] for i in inbox["items"]]
        assert "cand_a" in ids
        assert "cand_b" in ids

    def test_excludes_non_active_candidates(self, store):
        store.append_jsonl("candidates", _make_candidate("cand_active"))
        store.append_jsonl("candidates", _make_candidate("cand_suppressed", status="suppressed"))
        store.append_jsonl("candidates", _make_candidate("cand_reviewed", status="reviewed"))
        inbox = build_attention_inbox(store, surface="local")
        assert inbox["counts"]["candidates"] == 1
        assert inbox["items"][0]["id"] == "cand_active"

    def test_includes_dormant_and_held_threads(self, store):
        store.append_jsonl("threads", _make_thread("sth_dormant", status="dormant"))
        store.append_jsonl("threads", _make_thread("sth_held", status="held", hold_reason="waiting"))
        inbox = build_attention_inbox(store, surface="local")
        assert inbox["counts"]["threads"] == 2
        ids = [i["id"] for i in inbox["items"]]
        assert "sth_dormant" in ids
        assert "sth_held" in ids

    def test_excludes_closed_and_archived_threads(self, store):
        store.append_jsonl("threads", _make_thread("sth_dormant"))
        store.append_jsonl("threads", _make_thread("sth_closed", status="closed"))
        store.append_jsonl("threads", _make_thread("sth_archived", status="archived"))
        inbox = build_attention_inbox(store, surface="local")
        assert inbox["counts"]["threads"] == 1
        assert inbox["items"][0]["id"] == "sth_dormant"

    def test_surface_filtering_hides_items(self, store):
        store.append_jsonl("candidates", _make_candidate("cand_local", surfaces=["local"]))
        store.append_jsonl("candidates", _make_candidate("cand_dash", surfaces=["dashboard"]))
        store.append_jsonl("threads", _make_thread("sth_local", surfaces=["local"]))
        store.append_jsonl("threads", _make_thread("sth_dash", surfaces=["dashboard"]))
        inbox = build_attention_inbox(
            store, surface="local",
            config={"allowed_surfaces": ["local", "dashboard"]},
        )
        ids = [i["id"] for i in inbox["items"]]
        assert "cand_local" in ids
        assert "sth_local" in ids
        assert "cand_dash" not in ids
        assert "sth_dash" not in ids
        assert inbox["counts"]["filtered_out"] == 2

    def test_sensitivity_gate_fails_closed(self, store):
        store.append_jsonl("candidates", _make_candidate("cand_safe", sensitivity="public_safe"))
        store.append_jsonl("candidates", _make_candidate("cand_priv", sensitivity="private"))
        inbox = build_attention_inbox(
            store, surface="local",
            config={"allowed_surfaces": ["local"], "max_sensitivity": "private"},
        )
        ids = [i["id"] for i in inbox["items"]]
        assert "cand_priv" in ids
        assert "cand_safe" not in ids
        assert inbox["counts"]["filtered_out"] == 1

    def test_local_only_sensitivity_allowed_on_local(self, store):
        store.append_jsonl("candidates", _make_candidate("cand_lo", sensitivity="local_only"))
        inbox = build_attention_inbox(
            store, surface="local",
            config={"allowed_surfaces": ["local"], "max_sensitivity": "private"},
        )
        assert inbox["counts"]["candidates"] == 1

    def test_candidate_compact_fields(self, store):
        store.append_jsonl("candidates", _make_candidate("cand_1"))
        inbox = build_attention_inbox(store, surface="local")
        item = inbox["items"][0]
        assert item["type"] == "candidate"
        assert item["id"] == "cand_1"
        assert item["kind"] == "design_decision"
        assert isinstance(item["pressure"], (int, float))
        assert isinstance(item["age_hours"], (int, float, type(None)))
        assert item["freshness"] in ("fresh", "recent", "stale", "unknown")
        assert item["sensitivity"] == "private"
        assert isinstance(item["allowed_decisions"], list)
        assert item["title"]
        assert item["summary"]

    def test_thread_compact_fields(self, store):
        store.append_jsonl("threads", _make_thread("sth_1"))
        inbox = build_attention_inbox(store, surface="local")
        item = inbox["items"][0]
        assert item["type"] == "thread"
        assert item["id"] == "sth_1"
        assert item["kind"] == "THINK"
        assert item["pressure"] is None
        assert item["origin_candidate_id"] == "cand_origin"
        assert item["hold_reason"] == ""
        assert isinstance(item["source_refs"], list)
        assert item["expires_at"]

    def test_held_thread_shows_hold_reason(self, store):
        store.append_jsonl("threads", _make_thread(
            "sth_held", status="held", hold_reason="waiting for evidence", resume_trigger="new_data",
        ))
        inbox = build_attention_inbox(store, surface="local")
        item = inbox["items"][0]
        assert item["hold_reason"] == "waiting for evidence"
        assert item["resume_trigger"] == "new_data"

    def test_candidate_allowed_decisions(self, store):
        store.append_jsonl("candidates", _make_candidate("cand_1"))
        inbox = build_attention_inbox(store, surface="local")
        item = inbox["items"][0]
        assert item["allowed_decisions"] == CANDIDATE_DECISIONS

    def test_dormant_thread_allowed_decisions(self, store):
        store.append_jsonl("threads", _make_thread("sth_1", status="dormant"))
        inbox = build_attention_inbox(store, surface="local")
        item = inbox["items"][0]
        assert item["allowed_decisions"] == THREAD_DECISIONS["dormant"]
        assert "open" in item["allowed_decisions"]
        assert "hold" in item["allowed_decisions"]

    def test_held_thread_allowed_decisions(self, store):
        store.append_jsonl("threads", _make_thread("sth_1", status="held"))
        inbox = build_attention_inbox(store, surface="local")
        item = inbox["items"][0]
        assert item["allowed_decisions"] == THREAD_DECISIONS["held"]
        assert "resume" in item["allowed_decisions"]
        assert "hold" not in item["allowed_decisions"]

    def test_threads_sorted_before_candidates(self, store):
        store.append_jsonl("candidates", _make_candidate("cand_1", pressure=0.9))
        store.append_jsonl("threads", _make_thread("sth_1"))
        inbox = build_attention_inbox(store, surface="local")
        assert inbox["items"][0]["type"] == "thread"
        assert inbox["items"][1]["type"] == "candidate"

    def test_candidates_sorted_by_pressure_desc(self, store):
        store.append_jsonl("candidates", _make_candidate("cand_low", pressure=0.5))
        store.append_jsonl("candidates", _make_candidate("cand_high", pressure=0.9))
        inbox = build_attention_inbox(store, surface="local")
        cands = [i for i in inbox["items"] if i["type"] == "candidate"]
        assert cands[0]["id"] == "cand_high"
        assert cands[1]["id"] == "cand_low"

    def test_limit_caps_results(self, store):
        for i in range(10):
            store.append_jsonl("candidates", _make_candidate(f"cand_{i}"))
        inbox = build_attention_inbox(store, surface="local", limit=3)
        assert len(inbox["items"]) == 3

    def test_config_diagnostics_present(self, store):
        inbox = build_attention_inbox(store, surface="dashboard")
        diag = inbox["config_diagnostics"]
        assert diag["surface"] == "dashboard"
        assert "allowed_surfaces" in diag
        assert "max_sensitivity" in diag
        assert "instance_name" in diag


class TestReadOnlyGuarantee:
    """Verify the inbox does not mutate any JSONL state files."""

    def test_inbox_does_not_write_decisions(self, store):
        store.append_jsonl("candidates", _make_candidate("cand_1"))
        store.append_jsonl("threads", _make_thread("sth_1"))
        before_decisions = store.read_jsonl("decisions")
        build_attention_inbox(store, surface="local")
        after_decisions = store.read_jsonl("decisions")
        assert before_decisions == after_decisions

    def test_inbox_does_not_write_outbox(self, store):
        store.append_jsonl("candidates", _make_candidate("cand_1"))
        before_outbox = store.read_jsonl("outbox")
        build_attention_inbox(store, surface="local")
        after_outbox = store.read_jsonl("outbox")
        assert before_outbox == after_outbox

    def test_inbox_does_not_write_threads(self, store):
        store.append_jsonl("threads", _make_thread("sth_1"))
        before_threads = store.read_jsonl("threads")
        build_attention_inbox(store, surface="local")
        after_threads = store.read_jsonl("threads")
        assert before_threads == after_threads

    def test_inbox_does_not_write_signals(self, store):
        store.append_jsonl("candidates", _make_candidate("cand_1"))
        before_signals = store.read_jsonl("signals")
        build_attention_inbox(store, surface="local")
        after_signals = store.read_jsonl("signals")
        assert before_signals == after_signals

    def test_inbox_does_not_modify_candidates(self, store):
        store.append_jsonl("candidates", _make_candidate("cand_1"))
        before_candidates = store.read_jsonl("candidates")
        build_attention_inbox(store, surface="local")
        after_candidates = store.read_jsonl("candidates")
        assert before_candidates == after_candidates


class TestToolHandler:
    def test_handler_returns_success(self, state_dir):
        result = json.loads(handle_sensorium_attention_inbox(
            instance="test", state_dir=state_dir, surface="local",
        ))
        assert result["success"] is True
        assert result["data"]["counts"]["total"] == 0

    def test_handler_with_data(self, state_dir):
        store = SensoriumStore(instance="test", state_dir=state_dir)
        store.ensure_dirs()
        store.append_jsonl("candidates", _make_candidate("cand_1"))
        store.append_jsonl("threads", _make_thread("sth_1"))
        result = json.loads(handle_sensorium_attention_inbox(
            instance="test", state_dir=state_dir, surface="local",
        ))
        assert result["success"] is True
        assert result["data"]["counts"]["total"] == 2

    def test_handler_surface_filtering(self, state_dir):
        store = SensoriumStore(instance="test", state_dir=state_dir)
        store.ensure_dirs()
        store.append_jsonl("candidates", _make_candidate("cand_local", surfaces=["local"]))
        store.append_jsonl("candidates", _make_candidate("cand_dash", surfaces=["dashboard"]))
        result = json.loads(handle_sensorium_attention_inbox(
            instance="test", state_dir=state_dir, surface="local",
        ))
        assert result["data"]["counts"]["candidates"] == 1
        assert result["data"]["items"][0]["id"] == "cand_local"

    def test_handler_does_not_mutate_state(self, state_dir):
        store = SensoriumStore(instance="test", state_dir=state_dir)
        store.ensure_dirs()
        store.append_jsonl("candidates", _make_candidate("cand_1"))

        handle_sensorium_attention_inbox(
            instance="test", state_dir=state_dir, surface="local",
        )

        assert store.read_jsonl("decisions") == []
        assert store.read_jsonl("outbox") == []
        assert store.read_jsonl("threads") == []
        assert store.read_jsonl("signals") == []


class TestDashboardDefaultInstance:
    def test_default_instance_from_env(self, monkeypatch):
        monkeypatch.setenv("AGENT_SENSORIUM_DEFAULT_INSTANCE", "sera")
        from agent_sensorium.config import default_instance_name
        assert default_instance_name("default") == "sera"

    def test_env_var_takes_precedence(self, monkeypatch):
        monkeypatch.setenv("SENSORIUM_INSTANCE", "env-inst")
        monkeypatch.delenv("AGENT_SENSORIUM_DEFAULT_INSTANCE", raising=False)
        from agent_sensorium.config import default_instance_name
        assert default_instance_name("default") == "env-inst"

    def test_missing_state_returns_empty_inbox(self, tmp_path):
        store = SensoriumStore(instance="nonexistent", state_dir=str(tmp_path / "missing"))
        store.ensure_dirs()
        inbox = build_attention_inbox(store, surface="local")
        assert inbox["counts"]["total"] == 0
