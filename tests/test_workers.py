"""Tests for Phase 9B — Conscious delegation worker request lifecycle."""

import json

import pytest

from agent_sensorium.store import SensoriumStore
from agent_sensorium.workers import (
    FakeWorkerAdapter,
    dispatch_worker_request,
    list_worker_requests,
    prepare_worker_request,
    record_worker_result,
)
from agent_sensorium.tools import (
    handle_sensorium_worker_dispatch,
    handle_sensorium_worker_prepare,
    handle_sensorium_worker_result,
    handle_sensorium_worker_status,
)
from agent_sensorium.subconscious import (
    VALID_REQUEST_TYPES,
    validate_advisory_output,
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
        "kind": "design_decision",
        "pressure": 0.8,
        "summary": "Test candidate",
        "correlation_keys": correlation_keys or ["test-key"],
        "created_at": "2026-05-24T10:00:00Z",
        "updated_at": "2026-05-24T10:00:00Z",
    }


class TestPrepareWorkerRequest:
    def test_prepare_success_from_dormant_thread(self, store):
        store.append_jsonl("threads", _make_thread())

        result = prepare_worker_request(
            store,
            thread_id="sth_test1",
            worker_type="manual",
            title="Review blocked tasks",
            task_summary="Check kanban board for blocked items",
        )

        assert result["success"] is True
        data = result["data"]
        assert data["id"].startswith("wreq_")
        assert data["status"] == "prepared"
        assert data["worker_type"] == "manual"
        assert data["origin_thread_id"] == "sth_test1"
        assert data["origin_candidate_id"] == "cand_1"
        assert data["title"] == "Review blocked tasks"
        assert data["idempotency_key"]

        requests = store.read_jsonl("worker_requests")
        assert len(requests) == 1

        decisions = store.read_jsonl("decisions")
        assert len(decisions) == 1
        assert decisions[0]["type"] == "worker.prepared"

    def test_prepare_success_from_held_thread(self, store):
        store.append_jsonl("threads", _make_thread(status="held"))

        result = prepare_worker_request(
            store,
            thread_id="sth_test1",
            worker_type="hermes_subagent",
            title="Research task",
        )

        assert result["success"] is True
        assert result["data"]["worker_type"] == "hermes_subagent"

    def test_prepare_idempotent(self, store):
        store.append_jsonl("threads", _make_thread())

        kwargs = dict(
            thread_id="sth_test1",
            worker_type="manual",
            title="Idempotent task",
            task_summary="Same summary",
        )

        result1 = prepare_worker_request(store, **kwargs)
        result2 = prepare_worker_request(store, **kwargs)

        assert result1["success"] is True
        assert result2["success"] is True
        assert result2.get("idempotent_hit") is True
        assert len(store.read_jsonl("worker_requests")) == 1

    def test_prepare_updates_thread_interaction_refs(self, store):
        store.append_jsonl("threads", _make_thread())

        result = prepare_worker_request(
            store,
            thread_id="sth_test1",
            worker_type="manual",
            title="Ref test",
        )

        assert result["success"] is True
        threads = store.read_jsonl("threads")
        refs = threads[0].get("interaction_refs", [])
        assert any(
            r.get("type") == "worker_prepared" and r.get("worker_request_id") == result["data"]["id"]
            for r in refs
        )

    def test_prepare_truncates_long_summary(self, store):
        store.append_jsonl("threads", _make_thread())

        result = prepare_worker_request(
            store,
            thread_id="sth_test1",
            worker_type="manual",
            title="Truncation test",
            task_summary="x" * 5000,
            config={"max_prompt_chars": 100},
        )

        assert result["success"] is True
        assert len(result["data"]["task_summary"]) <= 100


class TestPrepareWorkerDenials:
    def test_missing_thread(self, store):
        result = prepare_worker_request(
            store,
            thread_id="sth_nonexistent",
            worker_type="manual",
            title="Missing thread",
        )

        assert result["success"] is False
        assert result["error"] == "thread_not_found"

    def test_closed_thread(self, store):
        store.append_jsonl("threads", _make_thread(status="closed"))

        result = prepare_worker_request(
            store,
            thread_id="sth_test1",
            worker_type="manual",
            title="Closed thread",
        )

        assert result["success"] is False
        assert result["error"] == "thread_not_openable"

    def test_archived_thread(self, store):
        store.append_jsonl("threads", _make_thread(status="archived"))

        result = prepare_worker_request(
            store,
            thread_id="sth_test1",
            worker_type="manual",
            title="Archived thread",
        )

        assert result["success"] is False
        assert result["error"] == "thread_not_openable"

    def test_invalid_worker_type(self, store):
        store.append_jsonl("threads", _make_thread())

        result = prepare_worker_request(
            store,
            thread_id="sth_test1",
            worker_type="teleportation_beam",
            title="Invalid type",
        )

        assert result["success"] is False
        assert result["error"] == "invalid_worker_type"

    def test_disallowed_worker_type(self, store):
        store.append_jsonl("threads", _make_thread())

        result = prepare_worker_request(
            store,
            thread_id="sth_test1",
            worker_type="script",
            title="Disallowed type",
            config={"allowed_worker_types": ["manual"]},
        )

        assert result["success"] is False
        assert result["error"] == "worker_type_not_allowed"

    def test_workers_disabled(self, store):
        store.append_jsonl("threads", _make_thread())

        result = prepare_worker_request(
            store,
            thread_id="sth_test1",
            worker_type="manual",
            title="Disabled",
            config={"enabled": False},
        )

        assert result["success"] is False
        assert result["error"] == "workers_disabled"


class TestDispatchWorkerRequest:
    def test_dispatch_without_execute_is_dry_run(self, store):
        store.append_jsonl("threads", _make_thread())
        prep = prepare_worker_request(
            store, thread_id="sth_test1", worker_type="manual", title="Test",
        )
        assert prep["success"] is True

        result = dispatch_worker_request(
            store,
            worker_request_id=prep["data"]["id"],
            adapter=FakeWorkerAdapter(),
        )

        assert result["success"] is False
        assert result["error"] == "execute_not_set"
        assert result.get("dry_run") is True

        requests = store.read_jsonl("worker_requests")
        assert requests[0]["status"] == "prepared"

    def test_dispatch_disabled_by_default_even_with_execute(self, store):
        store.append_jsonl("threads", _make_thread())
        prep = prepare_worker_request(
            store, thread_id="sth_test1", worker_type="manual", title="Test",
        )

        result = dispatch_worker_request(
            store,
            worker_request_id=prep["data"]["id"],
            adapter=FakeWorkerAdapter(),
            execute=True,
        )

        assert result["success"] is False
        assert result["error"] == "direct_dispatch_disabled"

        requests = store.read_jsonl("worker_requests")
        assert requests[0]["status"] == "prepared"

    def test_dispatch_with_fake_adapter_and_config_opt_in(self, store):
        store.append_jsonl("threads", _make_thread())
        prep = prepare_worker_request(
            store, thread_id="sth_test1", worker_type="hermes_subagent", title="Research",
        )
        assert prep["success"] is True

        adapter = FakeWorkerAdapter()
        result = dispatch_worker_request(
            store,
            worker_request_id=prep["data"]["id"],
            adapter=adapter,
            config={"direct_dispatch_enabled": True},
            execute=True,
        )

        assert result["success"] is True
        assert result["data"]["status"] == "dispatched"
        assert "dispatch_ref" in result["data"]["dispatch_refs"]
        assert len(adapter.calls) == 1

        decisions = store.read_jsonl("decisions")
        dispatched = [d for d in decisions if d["type"] == "worker.dispatched"]
        assert len(dispatched) == 1

    def test_dispatch_updates_thread_refs(self, store):
        store.append_jsonl("threads", _make_thread())
        prep = prepare_worker_request(
            store, thread_id="sth_test1", worker_type="manual", title="Ref check",
        )

        dispatch_worker_request(
            store,
            worker_request_id=prep["data"]["id"],
            adapter=FakeWorkerAdapter(),
            config={"direct_dispatch_enabled": True},
            execute=True,
        )

        threads = store.read_jsonl("threads")
        refs = threads[0].get("interaction_refs", [])
        assert any(r.get("type") == "worker_dispatched" for r in refs)

    def test_dispatch_adapter_failure_records_failed_status(self, store):
        store.append_jsonl("threads", _make_thread())
        prep = prepare_worker_request(
            store, thread_id="sth_test1", worker_type="manual", title="Fail test",
        )

        result = dispatch_worker_request(
            store,
            worker_request_id=prep["data"]["id"],
            adapter=FakeWorkerAdapter(fail=True),
            config={"direct_dispatch_enabled": True},
            execute=True,
        )

        assert result["success"] is False
        assert result["error"] == "dispatch_failed"

        requests = store.read_jsonl("worker_requests")
        assert requests[0]["status"] == "failed"

        decisions = store.read_jsonl("decisions")
        failed = [d for d in decisions if d["type"] == "worker.dispatch_failed"]
        assert len(failed) == 1

    def test_dispatch_no_adapter(self, store):
        store.append_jsonl("threads", _make_thread())
        prep = prepare_worker_request(
            store, thread_id="sth_test1", worker_type="manual", title="No adapter",
        )

        result = dispatch_worker_request(
            store,
            worker_request_id=prep["data"]["id"],
            config={"direct_dispatch_enabled": True},
            execute=True,
        )

        assert result["success"] is False
        assert result["error"] == "no_adapter"

    def test_dispatch_nonexistent_request(self, store):
        result = dispatch_worker_request(
            store,
            worker_request_id="wreq_nonexistent",
            config={"direct_dispatch_enabled": True},
            execute=True,
        )

        assert result["success"] is False
        assert result["error"] == "not_found"


class TestRecordWorkerResult:
    def test_record_completed_result(self, store):
        store.append_jsonl("threads", _make_thread())
        store.append_jsonl("candidates", _make_candidate())
        prep = prepare_worker_request(
            store, thread_id="sth_test1", worker_type="manual", title="Complete me",
        )

        result = record_worker_result(
            store,
            worker_request_id=prep["data"]["id"],
            outcome="completed",
            result_summary="Task done successfully",
            output_refs=[{"type": "artifact", "path": "/tmp/output.txt"}],
        )

        assert result["success"] is True
        assert result["data"]["status"] == "completed"
        assert result["data"]["outcome"] == "completed"
        assert result["data"]["result_summary"] == "Task done successfully"
        assert result["data"]["output_refs"] == [{"type": "artifact", "path": "/tmp/output.txt"}]

        decisions = store.read_jsonl("decisions")
        result_receipts = [d for d in decisions if d["type"] == "worker.result"]
        assert len(result_receipts) == 1
        assert result_receipts[0]["outcome"] == "completed"

        assert result.get("feedback_signal") is not None
        sig = result["feedback_signal"]
        assert sig["source"] == "feedback"
        assert sig["feedback_scope"] == "system_action"
        assert sig["caused_by"]["worker_request_id"] == prep["data"]["id"]
        assert sig["caused_by"]["origin_thread_id"] == "sth_test1"

    def test_record_failed_result(self, store):
        store.append_jsonl("threads", _make_thread())
        prep = prepare_worker_request(
            store, thread_id="sth_test1", worker_type="manual", title="Fail me",
        )

        result = record_worker_result(
            store,
            worker_request_id=prep["data"]["id"],
            outcome="failed",
            error_summary="Connection refused",
        )

        assert result["success"] is True
        assert result["data"]["status"] == "failed"
        assert result["data"]["outcome"] == "failed"
        assert result["data"]["error_summary"] == "Connection refused"

    def test_record_result_from_dispatched_status(self, store):
        store.append_jsonl("threads", _make_thread())
        prep = prepare_worker_request(
            store, thread_id="sth_test1", worker_type="manual", title="Dispatch first",
        )
        dispatch_worker_request(
            store,
            worker_request_id=prep["data"]["id"],
            adapter=FakeWorkerAdapter(),
            config={"direct_dispatch_enabled": True},
            execute=True,
        )

        result = record_worker_result(
            store,
            worker_request_id=prep["data"]["id"],
            outcome="completed",
            result_summary="Done after dispatch",
        )

        assert result["success"] is True
        assert result["data"]["status"] == "completed"

    def test_record_result_invalid_outcome(self, store):
        store.append_jsonl("threads", _make_thread())
        prep = prepare_worker_request(
            store, thread_id="sth_test1", worker_type="manual", title="Bad outcome",
        )

        result = record_worker_result(
            store,
            worker_request_id=prep["data"]["id"],
            outcome="maybe",
        )

        assert result["success"] is False
        assert result["error"] == "invalid_outcome"

    def test_record_result_already_completed(self, store):
        store.append_jsonl("threads", _make_thread())
        prep = prepare_worker_request(
            store, thread_id="sth_test1", worker_type="manual", title="Already done",
        )

        record_worker_result(
            store, worker_request_id=prep["data"]["id"], outcome="completed",
        )

        result = record_worker_result(
            store, worker_request_id=prep["data"]["id"], outcome="failed",
        )

        assert result["success"] is False
        assert result["error"] == "invalid_status"

    def test_record_result_truncates_long_summary(self, store):
        store.append_jsonl("threads", _make_thread())
        prep = prepare_worker_request(
            store, thread_id="sth_test1", worker_type="manual", title="Truncate",
        )

        result = record_worker_result(
            store,
            worker_request_id=prep["data"]["id"],
            outcome="completed",
            result_summary="x" * 5000,
            config={"max_result_chars": 100},
        )

        assert result["success"] is True
        assert len(result["data"]["result_summary"]) <= 100

    def test_feedback_signal_has_correlation_keys_from_origin_candidate(self, store):
        store.append_jsonl("candidates", _make_candidate(
            candidate_id="cand_1", correlation_keys=["kanban_pressure", "blocked"],
        ))
        store.append_jsonl("threads", _make_thread())
        prep = prepare_worker_request(
            store, thread_id="sth_test1", worker_type="manual", title="Corr keys",
        )

        result = record_worker_result(
            store, worker_request_id=prep["data"]["id"], outcome="completed",
        )

        sig = result["feedback_signal"]
        assert "kanban_pressure" in sig["correlation_keys"]
        assert "blocked" in sig["correlation_keys"]


class TestToolHandlers:
    def test_worker_prepare_tool(self, state_dir):
        store = SensoriumStore(instance="test", state_dir=state_dir)
        store.ensure_dirs()
        store.append_jsonl("threads", _make_thread())

        raw = handle_sensorium_worker_prepare(
            thread_id="sth_test1",
            worker_type="manual",
            title="Tool test",
            instance="test",
            state_dir=state_dir,
        )

        result = json.loads(raw)
        assert result["success"] is True
        assert result["data"]["data"]["status"] == "prepared"

    def test_worker_dispatch_tool_without_execute(self, state_dir):
        store = SensoriumStore(instance="test", state_dir=state_dir)
        store.ensure_dirs()
        store.append_jsonl("threads", _make_thread())
        prep = prepare_worker_request(
            store, thread_id="sth_test1", worker_type="manual", title="Tool dispatch",
        )

        raw = handle_sensorium_worker_dispatch(
            worker_request_id=prep["data"]["id"],
            execute=False,
            instance="test",
            state_dir=state_dir,
        )

        result = json.loads(raw)
        assert result["success"] is False
        assert "execute" in result["error"].lower()

    def test_worker_result_tool_emits_feedback(self, state_dir):
        store = SensoriumStore(instance="test", state_dir=state_dir)
        store.ensure_dirs()
        store.append_jsonl("candidates", _make_candidate())
        store.append_jsonl("threads", _make_thread())
        prep = prepare_worker_request(
            store, thread_id="sth_test1", worker_type="manual", title="Feedback test",
        )

        raw = handle_sensorium_worker_result(
            worker_request_id=prep["data"]["id"],
            outcome="completed",
            result_summary="All done",
            instance="test",
            state_dir=state_dir,
        )

        result = json.loads(raw)
        assert result["success"] is True
        data = result["data"]
        assert data["feedback_signal_id"]

        decisions = store.read_jsonl("decisions")
        feedback_receipts = [d for d in decisions if d["type"] == "worker.feedback_emitted"]
        assert len(feedback_receipts) == 1
        assert feedback_receipts[0]["feedback_scope"] == "system_action"
        assert feedback_receipts[0]["feedback_signal_id"]

    def test_worker_status_tool(self, state_dir):
        store = SensoriumStore(instance="test", state_dir=state_dir)
        store.ensure_dirs()
        store.append_jsonl("threads", _make_thread())
        prepare_worker_request(
            store, thread_id="sth_test1", worker_type="manual", title="Status test 1",
        )
        prepare_worker_request(
            store, thread_id="sth_test1", worker_type="hermes_subagent", title="Status test 2",
        )

        raw = handle_sensorium_worker_status(
            instance="test", state_dir=state_dir,
        )

        result = json.loads(raw)
        assert result["success"] is True
        assert result["data"]["count"] == 2


class TestFeedbackSignalValidation:
    def test_feedback_signal_validates_with_existing_schemas(self, store):
        """feedback_scope=system_action is a valid existing scope."""
        from agent_sensorium.schemas import validate_signal

        store.append_jsonl("candidates", _make_candidate())
        store.append_jsonl("threads", _make_thread())
        prep = prepare_worker_request(
            store, thread_id="sth_test1", worker_type="manual", title="Validate",
        )

        result = record_worker_result(
            store, worker_request_id=prep["data"]["id"], outcome="completed",
        )

        sig = result["feedback_signal"]
        validate_signal(sig)


class TestSubconsciousDelegateWork:
    def test_advisory_accepts_delegate_work_request_type(self):
        assert "DELEGATE_WORK" in VALID_REQUEST_TYPES

        output = validate_advisory_output({
            "action": "CREATE_CONSCIOUS_TASK",
            "rationale": "Blocked tasks need worker delegation.",
            "event_ids": ["evt_1"],
            "candidate_ids": [],
            "conscious_task": {
                "request_type": "DELEGATE_WORK",
                "title": "Delegate kanban cleanup",
                "why": "Blocked task count crossed threshold.",
                "expected_decision": "Prepare a manual worker request for kanban triage.",
            },
        })

        assert output["action"] == "CREATE_CONSCIOUS_TASK"
        assert output["conscious_task"]["request_type"] == "DELEGATE_WORK"

    def test_advisory_does_not_prepare_or_dispatch_worker(self, store):
        """Subconscious advisory creates candidate only, never worker requests."""
        from agent_sensorium.subconscious import run_subconscious_advisory

        store.append_jsonl("events", {
            "id": "evt_1", "ts": "2026-05-26T10:00:00Z",
            "type": "sensor.event.promoted", "kind": "kanban_pressure",
            "summary": "blocked=12", "strength": 0.8,
            "correlation_keys": ["kanban_pressure"],
            "sensitivity": "local_only", "allowed_surfaces": ["local"],
        })

        output = {
            "action": "CREATE_CONSCIOUS_TASK",
            "rationale": "Kanban pressure warrants worker delegation.",
            "event_ids": ["evt_1"],
            "candidate_ids": [],
            "pressure": 0.75,
            "conscious_task": {
                "request_type": "DELEGATE_WORK",
                "title": "Delegate kanban triage",
                "why": "Blocked count exceeds threshold.",
                "expected_decision": "Prepare manual worker for triage.",
            },
        }

        result = run_subconscious_advisory(
            store, advisory_output=output, enabled=True, dry_run=False,
        )

        assert result["action"] == "created_conscious_task_candidate"
        candidates = store.read_jsonl("candidates")
        assert len(candidates) == 1
        assert candidates[0]["conscious_task"]["request_type"] == "DELEGATE_WORK"

        assert store.read_jsonl("worker_requests") == []


class TestListWorkerRequests:
    def test_list_all(self, store):
        store.append_jsonl("threads", _make_thread())
        prepare_worker_request(
            store, thread_id="sth_test1", worker_type="manual", title="Task A",
        )
        prepare_worker_request(
            store, thread_id="sth_test1", worker_type="script", title="Task B",
        )

        items = list_worker_requests(store)
        assert len(items) == 2
        assert items[0]["title"] == "Task A"
        assert items[1]["title"] == "Task B"

    def test_list_filtered_by_status(self, store):
        store.append_jsonl("threads", _make_thread())
        prep = prepare_worker_request(
            store, thread_id="sth_test1", worker_type="manual", title="To complete",
        )
        prepare_worker_request(
            store, thread_id="sth_test1", worker_type="script", title="Still prepared",
        )
        record_worker_result(
            store, worker_request_id=prep["data"]["id"], outcome="completed",
        )

        prepared = list_worker_requests(store, status="prepared")
        assert len(prepared) == 1
        assert prepared[0]["title"] == "Still prepared"

        completed = list_worker_requests(store, status="completed")
        assert len(completed) == 1
        assert completed[0]["title"] == "To complete"
