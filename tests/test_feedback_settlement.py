"""Tests for Phase 9D — Feedback settlement and self-loop suppression."""

import json

import pytest

from agent_sensorium.gate import (
    is_settled_feedback_signal,
    should_promote_feedback,
    should_promote_signal,
)
from agent_sensorium.schemas import validate_signal
from agent_sensorium.store import SensoriumStore
from agent_sensorium.tools import (
    handle_sensorium_action_prepare,
    handle_sensorium_action_result,
    handle_sensorium_ingest_signal,
    handle_sensorium_thread_update,
)


@pytest.fixture
def state_dir(tmp_path):
    return str(tmp_path / "sensorium")


@pytest.fixture
def store(state_dir):
    s = SensoriumStore(instance="test", state_dir=state_dir)
    s.ensure_dirs()
    return s


def _make_thread(thread_id="sth_test1", status="dormant", origin_candidate_id="cand_1"):
    return {
        "id": thread_id,
        "status": status,
        "origin": "candidate",
        "conscious_task": {"id": "ct_1", "request_type": "THINK", "title": "Test thread"},
        "origin_candidate_id": origin_candidate_id,
        "continuity_summary": [],
        "decision_log": [],
        "interaction_refs": [],
        "summary_dirty": False,
        "open_questions": [],
        "next_prompt_to_operator": "test",
        "sensitivity": "private",
        "allowed_surfaces": ["local"],
        "created_at": "2026-05-24T10:00:00Z",
        "updated_at": "2026-05-24T10:00:00Z",
        "expires_at": "2026-05-31T10:00:00Z",
    }


def _make_candidate(candidate_id="cand_1", correlation_keys=None):
    return {
        "id": candidate_id,
        "status": "candidate",
        "kind": "task_result",
        "pressure": 0.8,
        "summary": "Test candidate",
        "correlation_keys": correlation_keys or ["test_key"],
        "created_at": "2026-05-24T10:00:00Z",
        "updated_at": "2026-05-24T10:00:00Z",
    }


def _action_feedback_signal(
    *, outcome="completed", action_id="tact_abc123",
    thread_id="sth_test1", candidate_id="cand_1",
):
    return {
        "sensor": "sensorium.action_result",
        "source": "feedback",
        "kind": "task_result",
        "summary": f"Action {action_id} {outcome}: test result",
        "actor": "tool",
        "strength_hint": 0.6,
        "caused_by": {
            "action_id": action_id,
            "origin_thread_id": thread_id,
            "origin_candidate_id": candidate_id,
            "intent": "test_intent",
        },
        "outcome": outcome,
        "feedback_scope": "system_action",
        "correlation_keys": ["test_key"],
        "sensitivity": "private",
        "allowed_surfaces": ["local"],
        "source_ref": action_id,
    }


def _thread_feedback_signal(
    *, outcome="completed", thread_id="sth_test1",
    candidate_id="cand_1", action="close",
):
    return {
        "sensor": "sensorium.thread_update",
        "source": "feedback",
        "kind": "task_result",
        "summary": f"Thread {thread_id} {action}: test reason",
        "actor": "operator",
        "strength_hint": 0.5,
        "caused_by": {
            "thread_id": thread_id,
            "action": action,
            "origin_candidate_id": candidate_id,
        },
        "outcome": outcome,
        "feedback_scope": "operator_evaluation",
        "correlation_keys": ["test_key"],
        "sensitivity": "private",
        "allowed_surfaces": ["local"],
        "source_ref": thread_id,
    }


# --- Unit tests: is_settled_feedback_signal ---


class TestIsSettledFeedbackSignal:
    def test_ordinary_action_completion_is_settled(self):
        assert is_settled_feedback_signal(_action_feedback_signal(outcome="completed")) is True

    def test_failed_action_is_not_settled(self):
        assert is_settled_feedback_signal(_action_feedback_signal(outcome="failed")) is False

    def test_timeout_action_is_not_settled(self):
        assert is_settled_feedback_signal(_action_feedback_signal(outcome="timeout")) is False

    def test_thread_close_completed_is_settled(self):
        assert is_settled_feedback_signal(_thread_feedback_signal(outcome="completed")) is True

    def test_thread_archive_operator_rejected_is_not_settled(self):
        assert is_settled_feedback_signal(
            _thread_feedback_signal(outcome="operator_rejected", action="archive"),
        ) is False

    def test_non_feedback_source_is_not_settled(self):
        sig = _action_feedback_signal(outcome="completed")
        sig["source"] = "manual"
        assert is_settled_feedback_signal(sig) is False

    def test_non_sensorium_sensor_is_not_settled(self):
        sig = _action_feedback_signal(outcome="completed")
        sig["sensor"] = "external.action_result"
        assert is_settled_feedback_signal(sig) is False

    def test_promote_feedback_override_is_not_settled(self):
        sig = _action_feedback_signal(outcome="completed")
        sig["promote_feedback"] = True
        assert is_settled_feedback_signal(sig) is False

    def test_regression_keyword_in_summary_is_not_settled(self):
        sig = _action_feedback_signal(outcome="completed")
        sig["summary"] = "Action completed: regression detected in output"
        assert is_settled_feedback_signal(sig) is False

    def test_reopen_keyword_in_summary_is_not_settled(self):
        sig = _action_feedback_signal(outcome="completed")
        sig["summary"] = "Action completed: requires reopen"
        assert is_settled_feedback_signal(sig) is False

    def test_blocker_keyword_in_summary_is_not_settled(self):
        sig = _action_feedback_signal(outcome="completed")
        sig["summary"] = "Action completed: blocker found"
        assert is_settled_feedback_signal(sig) is False

    def test_escalation_keyword_in_summary_is_not_settled(self):
        sig = _action_feedback_signal(outcome="completed")
        sig["summary"] = "Action completed: escalation required"
        assert is_settled_feedback_signal(sig) is False

    def test_no_sensorium_ids_in_caused_by_is_not_settled(self):
        sig = {
            "sensor": "sensorium.action_result",
            "source": "feedback",
            "kind": "task_result",
            "summary": "External result",
            "caused_by": {"external_id": "ext_123", "platform": "github"},
            "outcome": "completed",
            "feedback_scope": "system_action",
        }
        assert is_settled_feedback_signal(sig) is False

    def test_missing_caused_by_is_not_settled(self):
        sig = _action_feedback_signal(outcome="completed")
        del sig["caused_by"]
        assert is_settled_feedback_signal(sig) is False

    def test_worker_result_completed_is_settled(self):
        sig = _action_feedback_signal(outcome="completed")
        sig["sensor"] = "sensorium.worker_result"
        sig["caused_by"]["worker_request_id"] = "wreq_abc123"
        assert is_settled_feedback_signal(sig) is True

    def test_worker_result_failed_is_not_settled(self):
        sig = _action_feedback_signal(outcome="failed")
        sig["sensor"] = "sensorium.worker_result"
        assert is_settled_feedback_signal(sig) is False


# --- Unit tests: should_promote_feedback ---


class TestShouldPromoteFeedback:
    def test_settled_feedback_not_promoted(self):
        promote, reason = should_promote_feedback(_action_feedback_signal(outcome="completed"))
        assert promote is False
        assert "settled_feedback" in reason

    def test_failed_feedback_promoted(self):
        promote, _ = should_promote_feedback(_action_feedback_signal(outcome="failed"))
        assert promote is True

    def test_config_override_promotes(self):
        sig = _action_feedback_signal(outcome="completed")
        promote, reason = should_promote_feedback(sig, config={"promote_all_feedback": True})
        assert promote is True
        assert "override" in reason

    def test_non_feedback_signal_promoted(self):
        sig = {
            "sensor": "sensorium.explicit_operator",
            "source": "manual",
            "kind": "user_correction",
            "summary": "test",
        }
        promote, _ = should_promote_feedback(sig)
        assert promote is True


# --- Integration: should_promote_signal with feedback ---


class TestShouldPromoteSignalFeedback:
    def test_ordinary_action_feedback_not_promoted(self):
        promoted, reason = should_promote_signal(_action_feedback_signal(outcome="completed"))
        assert promoted is False
        assert "settled_feedback" in reason

    def test_failed_action_feedback_still_promoted(self):
        promoted, reason = should_promote_signal(_action_feedback_signal(outcome="failed"))
        assert promoted is True

    def test_thread_close_feedback_not_promoted(self):
        promoted, reason = should_promote_signal(_thread_feedback_signal(outcome="completed"))
        assert promoted is False
        assert "settled_feedback" in reason

    def test_thread_archive_feedback_not_suppressed_by_settlement(self):
        """operator_rejected outcome bypasses settlement (goes to normal promotion rules)."""
        sig = _thread_feedback_signal(outcome="operator_rejected")
        promoted, reason = should_promote_signal(sig)
        assert "settled_feedback" not in reason

    def test_high_strength_feedback_still_suppressed(self):
        """Even high-strength ordinary feedback should not promote."""
        sig = _action_feedback_signal(outcome="completed")
        sig["strength_hint"] = 0.9
        promoted, reason = should_promote_signal(sig)
        assert promoted is False
        assert "settled_feedback" in reason

    def test_normal_signal_unaffected(self):
        sig = {
            "sensor": "sensorium.explicit_operator",
            "source": "manual",
            "kind": "user_correction",
            "summary": "Operator correction",
            "strength_hint": 0.8,
        }
        promoted, _ = should_promote_signal(sig)
        assert promoted is True


# --- End-to-end: JSONL signal/candidate behavior ---


class TestFeedbackSettlementE2E:
    def test_action_result_feedback_stored_but_not_promoted(self, store, state_dir):
        """Ordinary action completion feedback: signal stored, no new candidate."""
        store.append_jsonl("candidates", _make_candidate())
        store.append_jsonl("threads", _make_thread())

        prep_raw = handle_sensorium_action_prepare(
            thread_id="sth_test1",
            intent="test_action",
            title="Settlement test",
            instance="test",
            state_dir=state_dir,
        )
        action_id = json.loads(prep_raw)["data"]["data"]["id"]

        result_raw = handle_sensorium_action_result(
            action_id=action_id,
            outcome="completed",
            result_summary="Done successfully",
            instance="test",
            state_dir=state_dir,
        )
        result = json.loads(result_raw)
        assert result["success"] is True
        assert result["data"]["feedback_signal_id"]

        signals = store.read_jsonl("signals")
        feedback_signals = [s for s in signals if s.get("sensor") == "sensorium.action_result"]
        assert len(feedback_signals) == 1
        assert feedback_signals[0]["source"] == "feedback"
        assert feedback_signals[0]["outcome"] == "completed"
        assert "action_id" in feedback_signals[0].get("caused_by", {})

        candidates = store.read_jsonl("candidates")
        # Original candidate still active (thread not closed), but no NEW candidate
        assert len(candidates) == 1, "No new candidate created from ordinary feedback"
        assert candidates[0]["id"] == "cand_1"

    def test_failed_action_result_feedback_promoted(self, store, state_dir):
        """Failed action feedback should be promoted (not suppressed)."""
        store.append_jsonl("candidates", _make_candidate())
        store.append_jsonl("threads", _make_thread())

        prep_raw = handle_sensorium_action_prepare(
            thread_id="sth_test1",
            intent="failing_action",
            title="Failure test",
            instance="test",
            state_dir=state_dir,
        )
        action_id = json.loads(prep_raw)["data"]["data"]["id"]

        result_raw = handle_sensorium_action_result(
            action_id=action_id,
            outcome="failed",
            result_summary="Something went wrong",
            instance="test",
            state_dir=state_dir,
        )
        result = json.loads(result_raw)
        assert result["success"] is True

        signals = store.read_jsonl("signals")
        feedback_signals = [s for s in signals if s.get("sensor") == "sensorium.action_result"]
        assert len(feedback_signals) == 1
        assert feedback_signals[0]["outcome"] == "failed"

        events = store.read_jsonl("events")
        feedback_events = [e for e in events if e.get("kind") == "task_result"]
        assert len(feedback_events) >= 1, "Failed feedback should be promoted to event"

    def test_thread_close_feedback_stored_but_not_promoted(self, store, state_dir):
        """Thread close completed feedback: stored as signal, not promoted."""
        store.append_jsonl("candidates", _make_candidate())
        store.append_jsonl("threads", _make_thread())

        result_raw = handle_sensorium_thread_update(
            thread_id="sth_test1",
            action="close",
            reason="resolved",
            emit_feedback=True,
            instance="test",
            state_dir=state_dir,
        )
        result = json.loads(result_raw)
        assert result["success"] is True
        assert result["data"].get("feedback_emitted") is True

        signals = store.read_jsonl("signals")
        feedback_signals = [s for s in signals if s.get("sensor") == "sensorium.thread_update"]
        assert len(feedback_signals) == 1
        assert feedback_signals[0]["outcome"] == "completed"

        candidates = store.read_jsonl("candidates")
        assert candidates[0]["status"] == "reviewed"

        active_candidates = [c for c in candidates if c.get("status") == "candidate"]
        assert len(active_candidates) == 0

    def test_feedback_signal_has_causal_refs(self, store, state_dir):
        """Feedback signals carry full causal refs even when not promoted."""
        store.append_jsonl("candidates", _make_candidate())
        store.append_jsonl("threads", _make_thread())

        prep_raw = handle_sensorium_action_prepare(
            thread_id="sth_test1",
            intent="causal_test",
            title="Causal refs test",
            instance="test",
            state_dir=state_dir,
        )
        action_id = json.loads(prep_raw)["data"]["data"]["id"]

        handle_sensorium_action_result(
            action_id=action_id,
            outcome="completed",
            result_summary="Done",
            instance="test",
            state_dir=state_dir,
        )

        signals = store.read_jsonl("signals")
        feedback = [s for s in signals if s.get("sensor") == "sensorium.action_result"][0]

        assert feedback["caused_by"]["action_id"] == action_id
        assert feedback["caused_by"]["origin_thread_id"] == "sth_test1"
        assert feedback["caused_by"]["origin_candidate_id"] == "cand_1"
        assert feedback["outcome"] == "completed"
        assert feedback["feedback_scope"] == "system_action"

    def test_feedback_signal_validates(self, store, state_dir):
        """Settled feedback signals still pass schema validation."""
        store.append_jsonl("candidates", _make_candidate())
        store.append_jsonl("threads", _make_thread())

        prep_raw = handle_sensorium_action_prepare(
            thread_id="sth_test1",
            intent="validate_test",
            title="Validation test",
            instance="test",
            state_dir=state_dir,
        )
        action_id = json.loads(prep_raw)["data"]["data"]["id"]

        handle_sensorium_action_result(
            action_id=action_id,
            outcome="completed",
            result_summary="Done",
            instance="test",
            state_dir=state_dir,
        )

        signals = store.read_jsonl("signals")
        feedback = [s for s in signals if s.get("sensor") == "sensorium.action_result"][0]
        validate_signal(feedback)

    def test_full_smoke_sequence_no_self_loop_clutter(self, store, state_dir):
        """Replicate smoke-test sequence: no self-generated task_result candidate."""
        external_signal = {
            "sensor": "sensorium.machine_process_pressure",
            "source": "machine",
            "kind": "process_pressure",
            "summary": "Machine body pressure healthy_to_degraded: zombies=3",
            "strength_hint": 0.8,
            "sensitivity": "local_only",
            "allowed_surfaces": ["local"],
            "correlation_keys": ["machine-process-pressure"],
        }
        ingest_raw = handle_sensorium_ingest_signal(
            signal=external_signal, instance="test", state_dir=state_dir,
        )
        ingest_result = json.loads(ingest_raw)
        assert ingest_result["success"] is True
        assert ingest_result["data"]["promoted"] is True
        original_candidate_id = ingest_result["data"]["candidate_id"]

        store.append_jsonl("threads", _make_thread(
            thread_id="sth_smoke",
            origin_candidate_id=original_candidate_id,
        ))

        prep_raw = handle_sensorium_action_prepare(
            thread_id="sth_smoke",
            intent="investigate_pressure",
            title="Investigate process pressure",
            instance="test",
            state_dir=state_dir,
        )
        action_id = json.loads(prep_raw)["data"]["data"]["id"]

        handle_sensorium_action_result(
            action_id=action_id,
            outcome="completed",
            result_summary="Zombies cleaned up",
            instance="test",
            state_dir=state_dir,
        )

        handle_sensorium_thread_update(
            thread_id="sth_smoke",
            action="close",
            reason="pressure resolved",
            emit_feedback=True,
            instance="test",
            state_dir=state_dir,
        )

        signals = store.read_jsonl("signals")
        feedback_signals = [s for s in signals if s.get("source") == "feedback"]
        assert len(feedback_signals) == 2

        candidates = store.read_jsonl("candidates")
        active_candidates = [c for c in candidates if c.get("status") == "candidate"]
        assert len(active_candidates) == 0, (
            f"Expected 0 active candidates after settlement, got {len(active_candidates)}: "
            f"{[c.get('kind') for c in active_candidates]}"
        )

        original = next(c for c in candidates if c["id"] == original_candidate_id)
        assert original["status"] == "reviewed"

    def test_surfaces_not_broadened_by_feedback(self, store, state_dir):
        """Feedback signals preserve privacy/surface behavior."""
        store.append_jsonl("candidates", _make_candidate())
        store.append_jsonl("threads", _make_thread())

        prep_raw = handle_sensorium_action_prepare(
            thread_id="sth_test1",
            intent="privacy_test",
            title="Surface preservation test",
            instance="test",
            state_dir=state_dir,
        )
        action_id = json.loads(prep_raw)["data"]["data"]["id"]

        handle_sensorium_action_result(
            action_id=action_id,
            outcome="completed",
            result_summary="Done",
            instance="test",
            state_dir=state_dir,
        )

        signals = store.read_jsonl("signals")
        feedback = [s for s in signals if s.get("sensor") == "sensorium.action_result"][0]
        assert feedback["sensitivity"] == "private"
        assert feedback["allowed_surfaces"] == ["local"]
