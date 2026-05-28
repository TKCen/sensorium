"""Tests for Phase 5 — feedback lane and loop breakers."""

import json

import pytest

from agent_sensorium.schemas import (
    DELIVERY_ONLY_OUTCOMES,
    FAILURE_OUTCOMES,
    FEEDBACK_REQUIRED_FIELDS,
    SUCCESS_OUTCOMES,
    VALID_FEEDBACK_SCOPES,
    classify_feedback_outcome,
    normalize_signal,
    validate_signal,
)
from agent_sensorium.gate import is_feedback_self_loop
from agent_sensorium.dispatcher import select_candidate
from agent_sensorium.store import SensoriumStore
from agent_sensorium.tools import (
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


def _feedback_signal(**overrides):
    base = {
        "sensor": "feedback_loop",
        "source": "feedback",
        "kind": "task_result",
        "summary": "Thread was closed after operator review",
        "strength_hint": 0.8,
        "caused_by": {"thread_id": "sth_abc123", "action": "close"},
        "outcome": "operator_approved",
        "feedback_scope": "operator_evaluation",
    }
    base.update(overrides)
    return base


def _make_candidate(
    pressure=0.7,
    cand_id="cand_test1",
    status="candidate",
    feedback_meta=None,
):
    c = {
        "id": cand_id,
        "status": status,
        "kind": "task_result",
        "pressure": pressure,
        "summary": "Test candidate",
        "event_ids": ["evt_x"],
        "correlation_keys": ["test-key"],
        "sensitivity": "private",
        "allowed_surfaces": ["local"],
        "created_at": "2026-05-24T10:00:00Z",
        "updated_at": "2026-05-24T10:00:00Z",
        "expires_at": "",
    }
    if feedback_meta is not None:
        c["feedback_meta"] = feedback_meta
    return c


class TestFeedbackSignalValidation:
    """Feedback signals require caused_by, outcome, and feedback_scope."""

    def test_valid_feedback_signal_passes(self):
        sig = _feedback_signal()
        validate_signal(sig)

    def test_missing_caused_by_rejected(self):
        sig = _feedback_signal()
        del sig["caused_by"]
        with pytest.raises(ValueError, match="caused_by"):
            validate_signal(sig)

    def test_missing_outcome_rejected(self):
        sig = _feedback_signal()
        del sig["outcome"]
        with pytest.raises(ValueError, match="outcome"):
            validate_signal(sig)

    def test_missing_feedback_scope_rejected(self):
        sig = _feedback_signal()
        del sig["feedback_scope"]
        with pytest.raises(ValueError, match="feedback_scope"):
            validate_signal(sig)

    def test_invalid_feedback_scope_rejected(self):
        sig = _feedback_signal(feedback_scope="invalid_scope")
        with pytest.raises(ValueError, match="feedback_scope"):
            validate_signal(sig)

    def test_valid_feedback_scope_list(self):
        sig = _feedback_signal(feedback_scope=["thread", "candidate"])
        validate_signal(sig)

    def test_feedback_scope_none_rejected(self):
        sig = _feedback_signal(feedback_scope=None)
        with pytest.raises(ValueError, match="feedback_scope"):
            validate_signal(sig)

    def test_feedback_scope_wrong_type_rejected(self):
        sig = _feedback_signal(feedback_scope={"scope": "thread"})
        with pytest.raises(ValueError, match="feedback_scope"):
            validate_signal(sig)

    def test_feedback_scope_list_values_must_be_strings(self):
        sig = _feedback_signal(feedback_scope=["thread", {"bad": "scope"}])
        with pytest.raises(ValueError, match="feedback_scope"):
            validate_signal(sig)

    def test_caused_by_must_be_dict(self):
        sig = _feedback_signal(caused_by="sth_abc123")
        with pytest.raises(ValueError, match="caused_by"):
            validate_signal(sig)

    def test_caused_by_must_not_be_empty(self):
        sig = _feedback_signal(caused_by={})
        with pytest.raises(ValueError, match="caused_by"):
            validate_signal(sig)

    def test_outcome_must_be_string(self):
        sig = _feedback_signal(outcome={"status": "completed"})
        with pytest.raises(ValueError, match="outcome"):
            validate_signal(sig)

    def test_non_feedback_source_skips_feedback_validation(self):
        sig = {
            "sensor": "test",
            "source": "manual",
            "kind": "note",
            "summary": "No feedback fields needed",
        }
        validate_signal(sig)


class TestFeedbackSignalNormalization:
    """Normalization preserves feedback-specific fields."""

    def test_preserves_caused_by(self):
        sig = _feedback_signal()
        normalized = normalize_signal(sig)
        assert normalized["caused_by"] == {"thread_id": "sth_abc123", "action": "close"}

    def test_preserves_outcome(self):
        sig = _feedback_signal()
        normalized = normalize_signal(sig)
        assert normalized["outcome"] == "operator_approved"

    def test_preserves_feedback_scope(self):
        sig = _feedback_signal()
        normalized = normalize_signal(sig)
        assert normalized["feedback_scope"] == "operator_evaluation"

    def test_preserves_feedback_scope_list(self):
        sig = _feedback_signal(feedback_scope=["thread", "delivery"])
        normalized = normalize_signal(sig)
        assert normalized["feedback_scope"] == ["thread", "delivery"]


class TestFeedbackOutcomeClassifier:
    """Delivery-only outcomes are not success; evaluated outcomes are deterministic."""

    def test_delivery_only_not_success(self):
        for outcome in DELIVERY_ONLY_OUTCOMES:
            result = classify_feedback_outcome(outcome)
            assert result == "delivery_only", f"{outcome} should be delivery_only, got {result}"

    def test_success_outcomes(self):
        for outcome in SUCCESS_OUTCOMES:
            result = classify_feedback_outcome(outcome)
            assert result == "success", f"{outcome} should be success, got {result}"

    def test_failure_outcomes(self):
        for outcome in FAILURE_OUTCOMES:
            result = classify_feedback_outcome(outcome)
            assert result == "failure", f"{outcome} should be failure, got {result}"

    def test_unknown_outcome_inconclusive(self):
        assert classify_feedback_outcome("something_weird") == "inconclusive"

    def test_delivered_is_not_success(self):
        assert classify_feedback_outcome("delivered") != "success"

    def test_sent_is_not_success(self):
        assert classify_feedback_outcome("sent") != "success"

    def test_operator_approved_is_success(self):
        assert classify_feedback_outcome("operator_approved") == "success"

    def test_completed_is_success(self):
        assert classify_feedback_outcome("completed") == "success"

    def test_no_response_is_failure(self):
        assert classify_feedback_outcome("no_response") == "failure"


class TestDispatcherSelfLoopRejection:
    """Dispatcher skips self-loop-only feedback candidates."""

    def test_normal_candidate_not_rejected(self):
        c = _make_candidate(pressure=0.8)
        result = select_candidate([c])
        assert result is not None
        assert result["id"] == "cand_test1"

    def test_self_loop_candidate_rejected(self):
        meta = {
            "caused_by": {"thread_id": "sth_abc123"},
            "outcome": "delivered",
            "feedback_scope": "delivery",
        }
        c = _make_candidate(pressure=0.9, feedback_meta=meta)
        result = select_candidate([c])
        assert result is None

    def test_operator_evaluation_not_rejected(self):
        meta = {
            "caused_by": {"thread_id": "sth_abc123"},
            "outcome": "operator_approved",
            "feedback_scope": "operator_evaluation",
        }
        c = _make_candidate(pressure=0.8, feedback_meta=meta)
        result = select_candidate([c])
        assert result is not None

    def test_self_loop_skipped_normal_selected(self):
        self_loop = _make_candidate(
            pressure=0.95,
            cand_id="cand_loop",
            feedback_meta={
                "caused_by": {"candidate_id": "cand_prev"},
                "outcome": "sent",
                "feedback_scope": "delivery",
            },
        )
        normal = _make_candidate(pressure=0.6, cand_id="cand_normal")
        result = select_candidate([self_loop, normal])
        assert result is not None
        assert result["id"] == "cand_normal"

    def test_external_caused_by_not_self_loop(self):
        meta = {
            "caused_by": {"external_task": "jira-1234"},
            "outcome": "completed",
            "feedback_scope": "system_action",
        }
        c = _make_candidate(pressure=0.8, feedback_meta=meta)
        result = select_candidate([c])
        assert result is not None


class TestIsFeedbackSelfLoop:
    """Unit tests for is_feedback_self_loop gate function."""

    def test_no_feedback_meta(self):
        assert is_feedback_self_loop(_make_candidate()) is False

    def test_operator_evaluation_not_self_loop(self):
        meta = {
            "caused_by": {"thread_id": "sth_abc"},
            "outcome": "operator_approved",
            "feedback_scope": "operator_evaluation",
        }
        assert is_feedback_self_loop(_make_candidate(feedback_meta=meta)) is False

    def test_sensorium_thread_ref_is_self_loop(self):
        meta = {
            "caused_by": {"thread_id": "sth_abc"},
            "outcome": "delivered",
            "feedback_scope": "thread",
        }
        assert is_feedback_self_loop(_make_candidate(feedback_meta=meta)) is True

    def test_sensorium_candidate_ref_is_self_loop(self):
        meta = {
            "caused_by": {"candidate_id": "cand_abc"},
            "outcome": "sent",
            "feedback_scope": "candidate",
        }
        assert is_feedback_self_loop(_make_candidate(feedback_meta=meta)) is True

    def test_external_ref_not_self_loop(self):
        meta = {
            "caused_by": {"jira_ticket": "PROJ-123"},
            "outcome": "completed",
            "feedback_scope": "system_action",
        }
        assert is_feedback_self_loop(_make_candidate(feedback_meta=meta)) is False


class TestThreadUpdateFeedbackEmission:
    """Thread close/update emits feedback only when configured."""

    def _setup_thread(self, store, state_dir):
        sig = {
            "sensor": "test",
            "source": "manual",
            "kind": "design_decision",
            "summary": "Test for feedback emission",
            "strength_hint": 0.9,
        }
        handle_sensorium_ingest_signal(signal=sig, instance="test", state_dir=state_dir)
        from agent_sensorium.tools import handle_sensorium_dispatch_once
        handle_sensorium_dispatch_once(
            instance="test",
            state_dir=state_dir,
            dry_run=False,
            config={"legacy_thread_dispatch_enabled": True},
        )
        threads = store.read_jsonl("threads")
        return threads[0]["id"]

    def test_close_without_emit_feedback_no_receipt(self, store, state_dir):
        thread_id = self._setup_thread(store, state_dir)
        raw = handle_sensorium_thread_update(
            thread_id=thread_id,
            action="close",
            reason="resolved",
            instance="test",
            state_dir=state_dir,
        )
        result = json.loads(raw)
        assert result["success"] is True
        decisions = store.read_jsonl("decisions")
        feedback_receipts = [d for d in decisions if d.get("type") == "thread.feedback_emitted"]
        assert len(feedback_receipts) == 0

    def test_close_with_emit_feedback_writes_receipt(self, store, state_dir):
        thread_id = self._setup_thread(store, state_dir)
        raw = handle_sensorium_thread_update(
            thread_id=thread_id,
            action="close",
            reason="resolved",
            emit_feedback=True,
            instance="test",
            state_dir=state_dir,
        )
        result = json.loads(raw)
        assert result["success"] is True
        assert result["data"].get("feedback_emitted") is True
        decisions = store.read_jsonl("decisions")
        feedback_receipts = [d for d in decisions if d.get("type") == "thread.feedback_emitted"]
        assert len(feedback_receipts) == 1
        r = feedback_receipts[0]
        assert r["thread_id"] == thread_id
        assert r["action"] == "close"
        assert r["feedback_signal_id"].startswith("sig_")

        feedback_signals = [s for s in store.read_jsonl("signals") if s.get("source") == "feedback"]
        assert len(feedback_signals) == 1
        signal = feedback_signals[0]
        assert signal["id"] == r["feedback_signal_id"]
        assert signal["caused_by"]["thread_id"] == thread_id
        assert signal["caused_by"]["action"] == "close"
        assert signal["outcome"] == "completed"
        assert signal["feedback_scope"] == "operator_evaluation"

    def test_archive_with_emit_feedback_marks_rejected_outcome(self, store, state_dir):
        thread_id = self._setup_thread(store, state_dir)
        raw = handle_sensorium_thread_update(
            thread_id=thread_id,
            action="archive",
            reason="not useful",
            emit_feedback=True,
            instance="test",
            state_dir=state_dir,
        )
        result = json.loads(raw)
        assert result["success"] is True
        feedback_signals = [s for s in store.read_jsonl("signals") if s.get("source") == "feedback"]
        assert len(feedback_signals) == 1
        assert feedback_signals[0]["caused_by"]["action"] == "archive"
        assert feedback_signals[0]["outcome"] == "operator_rejected"
        assert feedback_signals[0]["feedback_scope"] == "operator_evaluation"

    def test_hold_with_emit_feedback_no_receipt(self, store, state_dir):
        thread_id = self._setup_thread(store, state_dir)
        raw = handle_sensorium_thread_update(
            thread_id=thread_id,
            action="hold",
            reason="wait for more info",
            emit_feedback=True,
            instance="test",
            state_dir=state_dir,
        )
        result = json.loads(raw)
        assert result["success"] is True
        decisions = store.read_jsonl("decisions")
        feedback_receipts = [d for d in decisions if d.get("type") == "thread.feedback_emitted"]
        assert len(feedback_receipts) == 0


class TestPluginFeedbackSchema:
    """Plugin schema/handler forwards emit_feedback config."""

    def test_thread_update_schema_includes_emit_feedback(self):
        from agent_sensorium.plugin import register
        from tests.test_plugin_registration import FakePluginContext

        ctx = FakePluginContext()
        register(ctx)
        schema = ctx.tools["sensorium_thread_update"]["schema"]
        props = schema["parameters"]["properties"]
        assert "emit_feedback" in props
        assert props["emit_feedback"]["type"] == "boolean"

    def test_thread_update_handler_accepts_emit_feedback(self, state_dir):
        from agent_sensorium.plugin import register
        from tests.test_plugin_registration import FakePluginContext

        ctx = FakePluginContext()
        register(ctx)

        sig = {
            "sensor": "test",
            "source": "manual",
            "kind": "design_decision",
            "summary": "Plugin schema test",
            "strength_hint": 0.9,
        }
        handle_sensorium_ingest_signal(signal=sig, instance="test", state_dir=state_dir)
        from agent_sensorium.tools import handle_sensorium_dispatch_once
        handle_sensorium_dispatch_once(
            instance="test",
            state_dir=state_dir,
            dry_run=False,
            config={"legacy_thread_dispatch_enabled": True},
        )

        store = SensoriumStore(instance="test", state_dir=state_dir)
        threads = store.read_jsonl("threads")
        thread_id = threads[0]["id"]

        handler = ctx.tools["sensorium_thread_update"]["handler"]
        raw = handler({
            "thread_id": thread_id,
            "action": "close",
            "reason": "test",
            "emit_feedback": True,
            "instance": "test",
            "state_dir": state_dir,
        })
        result = json.loads(raw)
        assert result["success"] is True
        assert result["data"].get("feedback_emitted") is True


class TestFeedbackCoalescePath:
    """Feedback metadata propagates through coalesce into existing candidate."""

    def test_feedback_meta_set_on_coalesced_candidate(self, state_dir):
        normal_sig = {
            "sensor": "test",
            "source": "manual",
            "kind": "task_result",
            "summary": "Original observation",
            "strength_hint": 0.9,
            "correlation_keys": ["project-alpha"],
        }
        handle_sensorium_ingest_signal(
            signal=normal_sig, instance="test", state_dir=state_dir
        )
        store = SensoriumStore(instance="test", state_dir=state_dir)
        candidates_before = store.read_jsonl("candidates")
        assert len(candidates_before) == 1
        assert "feedback_meta" not in candidates_before[0]

        feedback_sig = _feedback_signal(
            kind="task_result",
            summary="Feedback on same topic",
            strength_hint=0.9,
            correlation_keys=["project-alpha"],
            caused_by={"thread_id": "sth_abc123"},
            outcome="delivered",
            feedback_scope="delivery",
        )
        handle_sensorium_ingest_signal(
            signal=feedback_sig, instance="test", state_dir=state_dir
        )
        candidates_after = store.read_jsonl("candidates")
        assert len(candidates_after) == 1
        meta = candidates_after[0].get("feedback_meta")
        assert meta is not None
        assert meta["caused_by"] == {"thread_id": "sth_abc123"}


class TestFeedbackSignalEndToEnd:
    """End-to-end: feedback signal ingest propagates metadata to candidate."""

    def test_feedback_signal_creates_candidate_with_meta(self, state_dir):
        sig = _feedback_signal(strength_hint=0.9)
        raw = handle_sensorium_ingest_signal(
            signal=sig, instance="test", state_dir=state_dir
        )
        result = json.loads(raw)
        assert result["success"] is True
        assert result["data"]["promoted"] is True

        store = SensoriumStore(instance="test", state_dir=state_dir)
        candidates = store.read_jsonl("candidates")
        assert len(candidates) == 1
        meta = candidates[0].get("feedback_meta")
        assert meta is not None
        assert meta["caused_by"] == {"thread_id": "sth_abc123", "action": "close"}
        assert meta["outcome"] == "operator_approved"
        assert meta["feedback_scope"] == "operator_evaluation"
