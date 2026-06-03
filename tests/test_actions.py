"""Tests for Phase 9C — Generic thread actions / prepared-action substrate."""

import json

import pytest

from agent_sensorium.actions import (
    attach_action_ref,
    list_thread_actions,
    prepare_action,
    record_action_result,
)
from agent_sensorium.schemas import validate_signal
from agent_sensorium.store import SensoriumStore
from agent_sensorium.tools import (
    handle_sensorium_action_attach,
    handle_sensorium_action_prepare,
    handle_sensorium_action_result,
    handle_sensorium_action_status,
    handle_sensorium_thread_open,
)


@pytest.fixture
def state_dir(tmp_path):
    return str(tmp_path / "sensorium")


@pytest.fixture
def store(state_dir):
    s = SensoriumStore(instance="test", state_dir=state_dir)
    s.ensure_dirs()
    return s


def _make_thread(
    thread_id="sth_test1",
    status="dormant",
    allowed_surfaces=None,
    sensitivity="private",
    origin_candidate_id="cand_1",
):
    if allowed_surfaces is None:
        allowed_surfaces = ["local"]
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
        "sensitivity": sensitivity,
        "allowed_surfaces": allowed_surfaces,
        "created_at": "2026-05-24T10:00:00Z",
        "updated_at": "2026-05-24T10:00:00Z",
        "expires_at": "2026-05-31T10:00:00Z",
    }


def _make_candidate(candidate_id="cand_1", correlation_keys=None):
    return {
        "id": candidate_id,
        "status": "candidate",
        "kind": "process_pressure",
        "pressure": 0.8,
        "summary": "Test candidate from pressure",
        "correlation_keys": correlation_keys or ["process_pressure", "zombie"],
        "created_at": "2026-05-24T10:00:00Z",
        "updated_at": "2026-05-24T10:00:00Z",
    }


def _write_config(state_dir, surfaces=None, max_sensitivity="private"):
    import json as _json
    from pathlib import Path

    config = {"allowed_surfaces": surfaces or ["local"], "max_sensitivity": max_sensitivity}
    path = Path(state_dir) / "instance.config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json.dumps(config))


# --- Prepare action tests ---


class TestPrepareAction:
    def test_prepare_success_from_dormant_thread(self, store):
        store.append_jsonl("threads", _make_thread())

        result = prepare_action(
            store,
            thread_id="sth_test1",
            intent="inspect_pressure",
            title="Inspect current process pressure",
            summary="Zombie processes detected, inspect and decide.",
            why_now="Pressure crossed threshold.",
        )

        assert result["success"] is True
        data = result["data"]
        assert data["id"].startswith("tact_")
        assert data["status"] == "proposed"
        assert data["intent"] == "inspect_pressure"
        assert data["origin_thread_id"] == "sth_test1"
        assert data["origin_candidate_id"] == "cand_1"
        assert data["idempotency_key"]

        actions = store.read_jsonl("thread_actions")
        assert len(actions) == 1

        decisions = store.read_jsonl("decisions")
        assert len(decisions) == 1
        assert decisions[0]["type"] == "action.proposed"

    def test_prepare_success_from_held_thread(self, store):
        store.append_jsonl("threads", _make_thread(status="held"))

        result = prepare_action(
            store,
            thread_id="sth_test1",
            intent="resolve_current_pressure",
            title="Resolve held pressure thread",
        )

        assert result["success"] is True
        assert result["data"]["intent"] == "resolve_current_pressure"

    def test_prepare_idempotent(self, store):
        store.append_jsonl("threads", _make_thread())

        kwargs = dict(
            thread_id="sth_test1",
            intent="inspect_pressure",
            title="Inspect pressure",
        )

        result1 = prepare_action(store, **kwargs)
        result2 = prepare_action(store, **kwargs)

        assert result1["success"] is True
        assert result2["success"] is True
        assert result2.get("idempotent_hit") is True
        assert len(store.read_jsonl("thread_actions")) == 1

    def test_prepare_updates_thread_interaction_refs(self, store):
        store.append_jsonl("threads", _make_thread())

        result = prepare_action(
            store,
            thread_id="sth_test1",
            intent="inspect_pressure",
            title="Ref test",
        )

        assert result["success"] is True
        threads = store.read_jsonl("threads")
        refs = threads[0].get("interaction_refs", [])
        assert any(
            r.get("type") == "action_proposed"
            and r.get("action_id") == result["data"]["id"]
            for r in refs
        )

    def test_intent_is_free_string_not_enum(self, store):
        """Intent can be any free string; no hard-coded artifact/expression enum."""
        store.append_jsonl("threads", _make_thread())

        for intent in [
            "inspect_pressure",
            "resolve_current_pressure",
            "compose_a_haiku_about_entropy",
            "review and refactor the authentication module",
            "send_warm_greeting",
        ]:
            result = prepare_action(
                store,
                thread_id="sth_test1",
                intent=intent,
                title=f"Action: {intent[:30]}",
            )
            assert result["success"] is True
            assert result["data"]["intent"] == intent

    def test_intent_is_bounded(self, store):
        store.append_jsonl("threads", _make_thread())

        result = prepare_action(
            store,
            thread_id="sth_test1",
            intent="x" * 2000,
            title="Bounded intent",
            config={"max_intent_chars": 100},
        )

        assert result["success"] is True
        assert len(result["data"]["intent"]) <= 100

    def test_summary_is_bounded(self, store):
        store.append_jsonl("threads", _make_thread())

        result = prepare_action(
            store,
            thread_id="sth_test1",
            intent="test",
            title="Bounded summary",
            summary="x" * 5000,
            config={"max_summary_chars": 200},
        )

        assert result["success"] is True
        assert len(result["data"]["summary"]) <= 200

    def test_refs_are_bounded(self, store):
        store.append_jsonl("threads", _make_thread())

        big_refs = {f"ref_{i}": f"value_{i}" for i in range(50)}
        result = prepare_action(
            store,
            thread_id="sth_test1",
            intent="test",
            title="Bounded refs",
            refs=big_refs,
            config={"max_refs": 5},
        )

        assert result["success"] is True
        assert len(result["data"]["refs"]) <= 5

    def test_inherits_thread_sensitivity_and_surfaces(self, store):
        store.append_jsonl("threads", _make_thread(
            sensitivity="local_only",
            allowed_surfaces=["local", "discord"],
        ))

        result = prepare_action(
            store,
            thread_id="sth_test1",
            intent="test",
            title="Inherits sensitivity",
        )

        assert result["success"] is True
        assert result["data"]["sensitivity"] == "local_only"
        assert result["data"]["allowed_surfaces"] == ["local", "discord"]


class TestPrepareActionDenials:
    def test_missing_thread(self, store):
        result = prepare_action(
            store,
            thread_id="sth_nonexistent",
            intent="test",
            title="Missing thread",
        )
        assert result["success"] is False
        assert result["error"] == "thread_not_found"

    def test_closed_thread(self, store):
        store.append_jsonl("threads", _make_thread(status="closed"))

        result = prepare_action(
            store,
            thread_id="sth_test1",
            intent="test",
            title="Closed thread",
        )
        assert result["success"] is False
        assert result["error"] == "thread_not_openable"

    def test_archived_thread(self, store):
        store.append_jsonl("threads", _make_thread(status="archived"))

        result = prepare_action(
            store,
            thread_id="sth_test1",
            intent="test",
            title="Archived thread",
        )
        assert result["success"] is False
        assert result["error"] == "thread_not_openable"

    def test_actions_disabled(self, store):
        store.append_jsonl("threads", _make_thread())

        result = prepare_action(
            store,
            thread_id="sth_test1",
            intent="test",
            title="Disabled",
            config={"enabled": False},
        )
        assert result["success"] is False
        assert result["error"] == "actions_disabled"

    def test_missing_intent(self, store):
        store.append_jsonl("threads", _make_thread())

        result = prepare_action(
            store,
            thread_id="sth_test1",
            intent="",
            title="No intent",
        )
        assert result["success"] is False
        assert result["error"] == "missing_intent"

    def test_whitespace_only_intent(self, store):
        store.append_jsonl("threads", _make_thread())

        result = prepare_action(
            store,
            thread_id="sth_test1",
            intent="   ",
            title="Whitespace intent",
        )
        assert result["success"] is False
        assert result["error"] == "missing_intent"


# --- Attach ref tests ---


class TestAttachActionRef:
    def _prepare(self, store):
        store.append_jsonl("threads", _make_thread())
        return prepare_action(
            store,
            thread_id="sth_test1",
            intent="test",
            title="Attach test",
        )

    def test_attach_worker_request(self, store):
        prep = self._prepare(store)
        action_id = prep["data"]["id"]

        result = attach_action_ref(
            store,
            action_id=action_id,
            kind="worker_request",
            ref_id="wreq_abc123",
            metadata={"worker_type": "manual"},
        )

        assert result["success"] is True
        assert len(result["data"]["attachments"]) == 1
        att = result["data"]["attachments"][0]
        assert att["kind"] == "worker_request"
        assert att["ref_id"] == "wreq_abc123"
        assert att["metadata"] == {"worker_type": "manual"}

        decisions = store.read_jsonl("decisions")
        attached = [d for d in decisions if d["type"] == "action.attached"]
        assert len(attached) == 1

    def test_attach_outbox_request(self, store):
        prep = self._prepare(store)

        result = attach_action_ref(
            store,
            action_id=prep["data"]["id"],
            kind="outbox_request",
            ref_id="obx_def456",
        )
        assert result["success"] is True
        assert result["data"]["attachments"][0]["kind"] == "outbox_request"

    def test_attach_artifact_ref(self, store):
        prep = self._prepare(store)

        result = attach_action_ref(
            store,
            action_id=prep["data"]["id"],
            kind="artifact_ref",
            ref_id="art_ghi789",
        )
        assert result["success"] is True

    def test_attach_external_ref(self, store):
        prep = self._prepare(store)

        result = attach_action_ref(
            store,
            action_id=prep["data"]["id"],
            kind="external_ref",
            ref_id="ext_jkl012",
            metadata={"url": "https://example.com"},
        )
        assert result["success"] is True

    def test_reject_invalid_kind(self, store):
        prep = self._prepare(store)

        result = attach_action_ref(
            store,
            action_id=prep["data"]["id"],
            kind="raw_media_blob",
            ref_id="blob_123",
        )
        assert result["success"] is False
        assert result["error"] == "invalid_attachment_kind"

    def test_reject_too_many_attachments(self, store):
        prep = self._prepare(store)
        action_id = prep["data"]["id"]

        for i in range(3):
            attach_action_ref(
                store,
                action_id=action_id,
                kind="artifact_ref",
                ref_id=f"art_{i}",
                config={"max_attachments": 3},
            )

        result = attach_action_ref(
            store,
            action_id=action_id,
            kind="artifact_ref",
            ref_id="art_overflow",
            config={"max_attachments": 3},
        )
        assert result["success"] is False
        assert result["error"] == "too_many_attachments"

    def test_reject_metadata_too_large(self, store):
        prep = self._prepare(store)

        result = attach_action_ref(
            store,
            action_id=prep["data"]["id"],
            kind="external_ref",
            ref_id="ext_big",
            metadata={"payload": "x" * 1000},
            config={"max_attachment_metadata_chars": 100},
        )
        assert result["success"] is False
        assert result["error"] == "metadata_too_large"

    def test_reject_empty_ref_id(self, store):
        prep = self._prepare(store)

        result = attach_action_ref(
            store,
            action_id=prep["data"]["id"],
            kind="worker_request",
            ref_id="",
        )
        assert result["success"] is False
        assert result["error"] == "missing_ref_id"

    def test_reject_attach_to_terminal_action(self, store):
        prep = self._prepare(store)
        action_id = prep["data"]["id"]

        record_action_result(
            store, action_id=action_id, outcome="completed",
        )

        result = attach_action_ref(
            store,
            action_id=action_id,
            kind="worker_request",
            ref_id="wreq_late",
        )
        assert result["success"] is False
        assert result["error"] == "action_terminal"

    def test_reject_attach_to_nonexistent_action(self, store):
        result = attach_action_ref(
            store,
            action_id="tact_nonexistent",
            kind="worker_request",
            ref_id="wreq_123",
        )
        assert result["success"] is False
        assert result["error"] == "action_not_found"


# --- Record result tests ---


class TestRecordActionResult:
    def _prepare(self, store):
        store.append_jsonl("threads", _make_thread())
        store.append_jsonl("candidates", _make_candidate())
        return prepare_action(
            store,
            thread_id="sth_test1",
            intent="inspect_pressure",
            title="Result test",
        )

    def test_completed_result(self, store):
        prep = self._prepare(store)
        action_id = prep["data"]["id"]

        result = record_action_result(
            store,
            action_id=action_id,
            outcome="completed",
            result_summary="Pressure inspected and resolved.",
        )

        assert result["success"] is True
        assert result["data"]["status"] == "acted"
        assert result["data"]["outcome"] == "completed"
        assert result["data"]["result_summary"] == "Pressure inspected and resolved."

        decisions = store.read_jsonl("decisions")
        result_receipts = [d for d in decisions if d["type"] == "action.result"]
        assert len(result_receipts) == 1
        assert result_receipts[0]["outcome"] == "completed"

        assert result.get("feedback_signal") is not None
        sig = result["feedback_signal"]
        assert sig["source"] == "feedback"
        assert sig["feedback_scope"] == "system_action"
        assert sig["caused_by"]["action_id"] == action_id
        assert sig["caused_by"]["origin_thread_id"] == "sth_test1"

    def test_rejected_result(self, store):
        prep = self._prepare(store)

        result = record_action_result(
            store,
            action_id=prep["data"]["id"],
            outcome="rejected",
            closed_reason="Not appropriate right now.",
        )

        assert result["success"] is True
        assert result["data"]["status"] == "rejected"
        assert result["data"]["outcome"] == "rejected"
        assert result["data"]["closed_reason"] == "Not appropriate right now."

    def test_cancelled_result(self, store):
        prep = self._prepare(store)

        result = record_action_result(
            store, action_id=prep["data"]["id"], outcome="cancelled",
        )
        assert result["success"] is True
        assert result["data"]["status"] == "cancelled"

    def test_expired_result(self, store):
        prep = self._prepare(store)

        result = record_action_result(
            store, action_id=prep["data"]["id"], outcome="expired",
        )
        assert result["success"] is True
        assert result["data"]["status"] == "expired"

    def test_failed_maps_to_closed(self, store):
        prep = self._prepare(store)

        result = record_action_result(
            store, action_id=prep["data"]["id"], outcome="failed",
        )
        assert result["success"] is True
        assert result["data"]["status"] == "closed"

    def test_superseded_maps_to_closed(self, store):
        prep = self._prepare(store)

        result = record_action_result(
            store, action_id=prep["data"]["id"], outcome="superseded",
        )
        assert result["success"] is True
        assert result["data"]["status"] == "closed"

    def test_invalid_outcome(self, store):
        prep = self._prepare(store)

        result = record_action_result(
            store, action_id=prep["data"]["id"], outcome="maybe",
        )
        assert result["success"] is False
        assert result["error"] == "invalid_outcome"

    def test_already_terminal(self, store):
        prep = self._prepare(store)
        action_id = prep["data"]["id"]

        record_action_result(store, action_id=action_id, outcome="completed")

        result = record_action_result(
            store, action_id=action_id, outcome="failed",
        )
        assert result["success"] is False
        assert result["error"] == "already_terminal"

    def test_nonexistent_action(self, store):
        result = record_action_result(
            store, action_id="tact_nonexistent", outcome="completed",
        )
        assert result["success"] is False
        assert result["error"] == "action_not_found"

    def test_feedback_signal_has_correlation_keys_from_candidate(self, store):
        store.append_jsonl("candidates", _make_candidate(
            candidate_id="cand_1",
            correlation_keys=["process_pressure", "zombie"],
        ))
        store.append_jsonl("threads", _make_thread())
        prep = prepare_action(
            store,
            thread_id="sth_test1",
            intent="inspect_pressure",
            title="Corr keys test",
        )

        result = record_action_result(
            store, action_id=prep["data"]["id"], outcome="completed",
        )

        sig = result["feedback_signal"]
        assert "process_pressure" in sig["correlation_keys"]
        assert "zombie" in sig["correlation_keys"]


class TestFeedbackSignalValidation:
    def test_feedback_signal_validates_with_existing_schemas(self, store):
        store.append_jsonl("candidates", _make_candidate())
        store.append_jsonl("threads", _make_thread())
        prep = prepare_action(
            store,
            thread_id="sth_test1",
            intent="validate_test",
            title="Schema validation",
        )

        result = record_action_result(
            store, action_id=prep["data"]["id"], outcome="completed",
        )

        sig = result["feedback_signal"]
        validate_signal(sig)


# --- List/status tests ---


class TestListThreadActions:
    def test_list_all(self, store):
        store.append_jsonl("threads", _make_thread())
        prepare_action(store, thread_id="sth_test1", intent="a", title="A")
        prepare_action(store, thread_id="sth_test1", intent="b", title="B")

        items = list_thread_actions(store)
        assert len(items) == 2
        assert items[0]["title"] == "A"
        assert items[1]["title"] == "B"

    def test_list_filtered_by_thread(self, store):
        store.append_jsonl("threads", _make_thread(thread_id="sth_1"))
        store.append_jsonl("threads", _make_thread(thread_id="sth_2"))
        prepare_action(store, thread_id="sth_1", intent="x", title="Thread 1")
        prepare_action(store, thread_id="sth_2", intent="y", title="Thread 2")

        items = list_thread_actions(store, thread_id="sth_1")
        assert len(items) == 1
        assert items[0]["title"] == "Thread 1"

    def test_list_filtered_by_status(self, store):
        store.append_jsonl("threads", _make_thread())
        prep = prepare_action(store, thread_id="sth_test1", intent="a", title="A")
        prepare_action(store, thread_id="sth_test1", intent="b", title="B")

        record_action_result(
            store, action_id=prep["data"]["id"], outcome="completed",
        )

        proposed = list_thread_actions(store, status="proposed")
        assert len(proposed) == 1
        assert proposed[0]["title"] == "B"

        acted = list_thread_actions(store, status="acted")
        assert len(acted) == 1
        assert acted[0]["title"] == "A"

    def test_compact_includes_attachment_count(self, store):
        store.append_jsonl("threads", _make_thread())
        prep = prepare_action(store, thread_id="sth_test1", intent="a", title="A")
        attach_action_ref(
            store,
            action_id=prep["data"]["id"],
            kind="worker_request",
            ref_id="wreq_1",
        )

        items = list_thread_actions(store)
        assert items[0]["attachment_count"] == 1


# --- Thread open integration ---


class TestThreadOpenActionIntegration:
    def test_thread_open_includes_actions_when_visible(self, state_dir):
        store = SensoriumStore(instance="test", state_dir=state_dir)
        store.ensure_dirs()
        _write_config(state_dir, surfaces=["local"])
        store.append_jsonl("threads", _make_thread(allowed_surfaces=["local"]))

        prepare_action(
            store,
            thread_id="sth_test1",
            intent="inspect_pressure",
            title="Visible action",
        )

        raw = handle_sensorium_thread_open(
            instance="test", state_dir=state_dir,
            thread_id="sth_test1", surface="local",
        )
        result = json.loads(raw)
        assert result["success"] is True
        assert "actions" in result["data"]
        assert result["data"]["action_count"] == 1
        assert result["data"]["actions"][0]["intent"] == "inspect_pressure"

    def test_thread_open_omits_terminal_actions(self, state_dir):
        store = SensoriumStore(instance="test", state_dir=state_dir)
        store.ensure_dirs()
        _write_config(state_dir, surfaces=["local"])
        store.append_jsonl("threads", _make_thread(allowed_surfaces=["local"]))

        prep = prepare_action(
            store,
            thread_id="sth_test1",
            intent="old_action",
            title="Done action",
        )
        record_action_result(
            store, action_id=prep["data"]["id"], outcome="completed",
        )
        prepare_action(
            store,
            thread_id="sth_test1",
            intent="current_action",
            title="Active action",
        )

        raw = handle_sensorium_thread_open(
            instance="test", state_dir=state_dir,
            thread_id="sth_test1", surface="local",
        )
        result = json.loads(raw)
        assert result["success"] is True
        assert result["data"]["action_count"] == 1
        assert result["data"]["actions"][0]["intent"] == "current_action"

    def test_thread_open_omits_actions_blocked_by_surface(self, state_dir):
        store = SensoriumStore(instance="test", state_dir=state_dir)
        store.ensure_dirs()
        _write_config(state_dir, surfaces=["local", "discord"])
        store.append_jsonl("threads", _make_thread(
            allowed_surfaces=["local", "discord"],
        ))

        # Action inherits thread surfaces ["local", "discord"]
        prepare_action(
            store,
            thread_id="sth_test1",
            intent="test",
            title="Surface test",
        )

        # But if config only allows local, discord request should not see actions
        _write_config(state_dir, surfaces=["local"])
        raw = handle_sensorium_thread_open(
            instance="test", state_dir=state_dir,
            thread_id="sth_test1", surface="local",
        )
        result = json.loads(raw)
        assert result["success"] is True
        # Action has allowed_surfaces=["local","discord"], local is in config -> visible
        assert result["data"].get("action_count", 0) == 1

    def test_thread_open_no_actions_key_when_empty(self, state_dir):
        store = SensoriumStore(instance="test", state_dir=state_dir)
        store.ensure_dirs()
        _write_config(state_dir, surfaces=["local"])
        store.append_jsonl("threads", _make_thread(allowed_surfaces=["local"]))

        raw = handle_sensorium_thread_open(
            instance="test", state_dir=state_dir,
            thread_id="sth_test1", surface="local",
        )
        result = json.loads(raw)
        assert result["success"] is True
        assert "actions" not in result["data"]


# --- Pointer integration ---


class TestPointerActionIntegration:
    def test_pointer_includes_action_count(self, state_dir):
        from agent_sensorium.pointers import select_attention_pointer

        store = SensoriumStore(instance="test", state_dir=state_dir)
        store.ensure_dirs()
        _write_config(state_dir, surfaces=["local"])
        store.append_jsonl("threads", _make_thread(allowed_surfaces=["local"]))

        prepare_action(
            store,
            thread_id="sth_test1",
            intent="pending_action",
            title="Pointer test",
        )

        from agent_sensorium.config import load_instance_config
        inst_cfg, _ = load_instance_config(state_dir=state_dir)

        pointer = select_attention_pointer(
            store, surface="local", instance_config=inst_cfg,
        )
        assert pointer["action"] == "pointer_available"
        assert pointer.get("action_count") == 1
        assert "prepared action" in pointer["invitation"]

    def test_pointer_no_action_count_when_no_actions(self, state_dir):
        from agent_sensorium.pointers import select_attention_pointer

        store = SensoriumStore(instance="test", state_dir=state_dir)
        store.ensure_dirs()
        _write_config(state_dir, surfaces=["local"])
        store.append_jsonl("threads", _make_thread(allowed_surfaces=["local"]))

        from agent_sensorium.config import load_instance_config
        inst_cfg, _ = load_instance_config(state_dir=state_dir)

        pointer = select_attention_pointer(
            store, surface="local", instance_config=inst_cfg,
        )
        assert pointer["action"] == "pointer_available"
        assert "action_count" not in pointer
        assert "prepared action" not in pointer["invitation"]

    def test_pointer_does_not_leak_action_content(self, state_dir):
        from agent_sensorium.pointers import select_attention_pointer

        store = SensoriumStore(instance="test", state_dir=state_dir)
        store.ensure_dirs()
        _write_config(state_dir, surfaces=["local"])
        store.append_jsonl("threads", _make_thread(allowed_surfaces=["local"]))

        prepare_action(
            store,
            thread_id="sth_test1",
            intent="secret_operation",
            title="Classified action",
            summary="Top secret details here.",
        )

        from agent_sensorium.config import load_instance_config
        inst_cfg, _ = load_instance_config(state_dir=state_dir)

        pointer = select_attention_pointer(
            store, surface="local", instance_config=inst_cfg,
        )
        pointer_str = json.dumps(pointer)
        assert "secret_operation" not in pointer_str
        assert "Top secret" not in pointer_str
        assert "Classified action" not in pointer_str


# --- Tool handler tests ---


class TestToolHandlers:
    def test_action_prepare_tool(self, state_dir):
        store = SensoriumStore(instance="test", state_dir=state_dir)
        store.ensure_dirs()
        store.append_jsonl("threads", _make_thread())

        raw = handle_sensorium_action_prepare(
            thread_id="sth_test1",
            intent="inspect_pressure",
            title="Tool test",
            instance="test",
            state_dir=state_dir,
        )

        result = json.loads(raw)
        assert result["success"] is True
        assert result["data"]["data"]["status"] == "proposed"

    def test_action_attach_tool(self, state_dir):
        store = SensoriumStore(instance="test", state_dir=state_dir)
        store.ensure_dirs()
        store.append_jsonl("threads", _make_thread())

        prep_raw = handle_sensorium_action_prepare(
            thread_id="sth_test1",
            intent="test",
            title="Attach tool test",
            instance="test",
            state_dir=state_dir,
        )
        action_id = json.loads(prep_raw)["data"]["data"]["id"]

        raw = handle_sensorium_action_attach(
            action_id=action_id,
            kind="worker_request",
            ref_id="wreq_tool_test",
            instance="test",
            state_dir=state_dir,
        )

        result = json.loads(raw)
        assert result["success"] is True
        assert len(result["data"]["data"]["attachments"]) == 1

    def test_action_result_tool_emits_feedback(self, state_dir):
        store = SensoriumStore(instance="test", state_dir=state_dir)
        store.ensure_dirs()
        store.append_jsonl("candidates", _make_candidate())
        store.append_jsonl("threads", _make_thread())

        prep_raw = handle_sensorium_action_prepare(
            thread_id="sth_test1",
            intent="feedback_test",
            title="Feedback test",
            instance="test",
            state_dir=state_dir,
        )
        action_id = json.loads(prep_raw)["data"]["data"]["id"]

        raw = handle_sensorium_action_result(
            action_id=action_id,
            outcome="completed",
            result_summary="Done",
            instance="test",
            state_dir=state_dir,
        )

        result = json.loads(raw)
        assert result["success"] is True
        assert result["data"]["feedback_signal_id"]

        decisions = store.read_jsonl("decisions")
        feedback_receipts = [
            d for d in decisions if d["type"] == "action.feedback_emitted"
        ]
        assert len(feedback_receipts) == 1
        assert feedback_receipts[0]["feedback_scope"] == "system_action"

    def test_action_status_tool(self, state_dir):
        store = SensoriumStore(instance="test", state_dir=state_dir)
        store.ensure_dirs()
        store.append_jsonl("threads", _make_thread())
        prepare_action(store, thread_id="sth_test1", intent="a", title="A")
        prepare_action(store, thread_id="sth_test1", intent="b", title="B")

        raw = handle_sensorium_action_status(
            instance="test", state_dir=state_dir,
        )

        result = json.loads(raw)
        assert result["success"] is True
        assert result["data"]["count"] == 2


# --- Live-pressure fixture tests ---


class TestLivePressureFixture:
    """Tests analogous to sensory pressure threads showing generic actions."""

    def test_action_from_process_pressure_thread(self, store):
        """Prepare a generic action from a process_pressure thread."""
        store.append_jsonl("candidates", _make_candidate(
            candidate_id="cand_proc",
            correlation_keys=["process_pressure", "zombie"],
        ))
        store.append_jsonl("threads", _make_thread(
            thread_id="sth_proc",
            origin_candidate_id="cand_proc",
        ))

        result = prepare_action(
            store,
            thread_id="sth_proc",
            intent="inspect_pressure",
            title="Inspect zombie process pressure",
            summary="3 zombie processes detected; inspect and decide.",
            why_now="Process pressure sensor crossed threshold.",
            refs={"sensor": "process_pressure", "zombie_count": "3"},
        )

        assert result["success"] is True
        data = result["data"]
        assert data["intent"] == "inspect_pressure"
        assert data["origin_candidate_id"] == "cand_proc"
        assert "zombie_count" in data["refs"]

    def test_action_from_kanban_pressure_thread(self, store):
        """Prepare a generic action from a kanban_pressure thread."""
        store.append_jsonl("candidates", _make_candidate(
            candidate_id="cand_kanban",
            correlation_keys=["kanban_pressure", "blocked"],
        ))
        store.append_jsonl("threads", _make_thread(
            thread_id="sth_kanban",
            origin_candidate_id="cand_kanban",
        ))

        result = prepare_action(
            store,
            thread_id="sth_kanban",
            intent="resolve_current_pressure",
            title="Triage blocked kanban tasks",
            summary="5 blocked tasks on work board.",
            why_now="Kanban pressure sensor detected blocked task accumulation.",
        )

        assert result["success"] is True
        assert result["data"]["intent"] == "resolve_current_pressure"

    def test_full_lifecycle_pressure_action(self, store):
        """Full lifecycle: prepare -> attach worker ref -> result -> feedback."""
        store.append_jsonl("candidates", _make_candidate())
        store.append_jsonl("threads", _make_thread())

        prep = prepare_action(
            store,
            thread_id="sth_test1",
            intent="inspect_pressure",
            title="Full lifecycle test",
        )
        assert prep["success"] is True
        action_id = prep["data"]["id"]

        att = attach_action_ref(
            store,
            action_id=action_id,
            kind="worker_request",
            ref_id="wreq_triage",
            metadata={"worker_type": "manual"},
        )
        assert att["success"] is True

        result = record_action_result(
            store,
            action_id=action_id,
            outcome="completed",
            result_summary="Pressure resolved after triage.",
        )
        assert result["success"] is True
        assert result["data"]["status"] == "acted"

        sig = result["feedback_signal"]
        validate_signal(sig)
        assert sig["caused_by"]["action_id"] == action_id
        assert sig["caused_by"]["origin_thread_id"] == "sth_test1"
        assert "process_pressure" in sig["correlation_keys"]
