"""Tests for Phase 8 — Subconscious advisory dry-run."""

import json

import pytest

from agent_sensorium.schemas import utc_now_iso
from agent_sensorium.store import SensoriumStore
from agent_sensorium.subconscious import (
    build_advisory_context,
    generate_advisory_output,
    _extract_json_object,
    _model_prompt,
    run_subconscious_advisory,
    validate_advisory_output,
)
from agent_sensorium.tools import handle_sensorium_subconscious_advisory


@pytest.fixture
def state_dir(tmp_path):
    return str(tmp_path / "sensorium")


def _event(idx: int, *, kind="design_decision", summary=None, sensitivity="local_only", surfaces=None):
    return {
        "id": f"evt_{idx}",
        "ts": "2026-05-26T10:00:00Z",
        "type": "sensor.event.promoted",
        "kind": kind,
        "summary": summary or f"Event {idx}",
        "strength": 0.8,
        "correlation_keys": [kind, f"k{idx}"],
        "sensitivity": sensitivity,
        "allowed_surfaces": surfaces or ["local"],
    }


def _candidate(idx: int, *, pressure=0.7, summary=None, kind="design_decision"):
    return {
        "id": f"cand_{idx}",
        "status": "candidate",
        "kind": kind,
        "pressure": pressure,
        "summary": summary or f"Candidate {idx}",
        "event_ids": [f"evt_{idx}"],
        "correlation_keys": [kind],
        "sensitivity": "local_only",
        "allowed_surfaces": ["local"],
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
        "expires_at": "",
    }


def test_context_builder_is_bounded_and_uses_events_not_raw_signals(state_dir):
    store = SensoriumStore(instance="test", state_dir=state_dir)
    store.ensure_dirs()
    store.append_jsonl("signals", {
        "id": "sig_raw",
        "sensor": "test",
        "source": "manual",
        "kind": "design_decision",
        "summary": "RAW SIGNAL SHOULD NOT APPEAR",
    })
    for i in range(8):
        store.append_jsonl("events", _event(i, summary="x" * 500))
    for i in range(4):
        store.append_jsonl("candidates", _candidate(i, pressure=0.9 - i * 0.1))

    context = build_advisory_context(store, config={"event_limit": 3, "candidate_limit": 2, "decision_limit": 1})

    assert context["schema_version"] == 1
    assert len(context["recent_events"]) == 3
    assert len(context["top_candidates"]) == 2
    assert "probe_audit_summary" in context
    assert "wired_live_probes" in context["probe_audit_summary"]
    serialized = json.dumps(context)
    assert "RAW SIGNAL SHOULD NOT APPEAR" not in serialized
    assert len(context["recent_events"][0]["summary"]) <= 180


def test_context_builder_excludes_direct_quantified_pressure_from_subconscious_context(state_dir):
    store = SensoriumStore(instance="test", state_dir=state_dir)
    store.ensure_dirs()
    store.append_jsonl("events", _event(10, kind="kanban_pressure", summary="blocked=17"))
    store.append_jsonl("events", _event(11, kind="design_decision", summary="semantic link candidate"))
    store.append_jsonl("candidates", _candidate(10, kind="kanban_pressure", summary="blocked=17", pressure=0.68))
    store.append_jsonl("candidates", _candidate(11, kind="design_decision", summary="semantic candidate", pressure=0.72))
    store.append_jsonl("candidates", _candidate(12, kind="subconscious_advisory", summary="prior advisory", pressure=0.8))

    context = build_advisory_context(store)

    assert [e["kind"] for e in context["recent_events"]] == ["design_decision"]
    assert [c["kind"] for c in context["top_candidates"]] == ["design_decision"]
    assert context["config_summary"]["direct_conscious_material_omitted"] == {"events": 1, "candidates": 1}


def test_advisory_output_schema_rejects_unknown_action():
    with pytest.raises(ValueError, match="Invalid advisory action"):
        validate_advisory_output({"action": "SEND_MESSAGE", "rationale": "nope"})


def test_create_conscious_task_requires_task_fields():
    with pytest.raises(ValueError, match="conscious_task missing"):
        validate_advisory_output({"action": "CREATE_CONSCIOUS_TASK", "rationale": "needs work"})


def test_create_conscious_task_rejects_reach_out_until_policy_phase():
    with pytest.raises(ValueError, match="Invalid conscious_task request_type"):
        validate_advisory_output({
            "action": "CREATE_CONSCIOUS_TASK",
            "rationale": "not yet",
            "conscious_task": {
                "request_type": "REACH_OUT",
                "title": "reach out",
                "why": "relational lane is deferred",
                "expected_decision": "reject",
            },
        })


def test_dry_run_stores_receipt_but_does_not_create_candidate_or_thread(state_dir):
    store = SensoriumStore(instance="test", state_dir=state_dir)
    store.ensure_dirs()
    store.append_jsonl("events", _event(1, kind="hindsight_pressure", summary="failed=9"))
    output = {
        "action": "CREATE_CONSCIOUS_TASK",
        "rationale": "Hindsight failures are persistent enough to inspect.",
        "event_ids": ["evt_1"],
        "candidate_ids": [],
        "pressure": 0.72,
        "conscious_task": {
            "request_type": "THINK",
            "title": "Review Hindsight failed operations",
            "why": "Subconscious advisory linked recent Hindsight pressure events.",
            "expected_decision": "Suppress, hold, or create a bounded repair follow-up.",
        },
    }

    result = run_subconscious_advisory(store, advisory_output=output, enabled=True, dry_run=True)

    assert result["action"] == "would_create_conscious_task"
    assert result["dry_run"] is True
    assert result["candidate_preview"]["kind"] == "subconscious_advisory"
    assert store.read_jsonl("candidates") == []
    assert store.read_jsonl("threads") == []
    receipts = [d for d in store.read_jsonl("decisions") if d.get("type") == "subconscious.advisory"]
    assert len(receipts) == 1
    assert receipts[0]["dry_run"] is True
    assert receipts[0]["output_action"] == "CREATE_CONSCIOUS_TASK"


def test_non_dry_run_is_disabled_by_default(state_dir):
    store = SensoriumStore(instance="test", state_dir=state_dir)
    store.ensure_dirs()
    result = run_subconscious_advisory(store, advisory_output={"action": "DROP", "rationale": "idle"}, dry_run=False)
    assert result["action"] == "disabled"
    assert store.read_jsonl("candidates") == []


def test_disabled_path_never_invokes_model_transport(state_dir):
    """When enabled=False the model transport must not be called (no outbound requests)."""
    store = SensoriumStore(instance="test", state_dir=state_dir)
    store.ensure_dirs()

    transport_calls = []

    def sentinel_transport(url, payload, headers, timeout):
        transport_calls.append(url)
        return {"choices": [{"message": {"content": '{"action":"DROP","rationale":"x"}'}}]}

    result = run_subconscious_advisory(
        store,
        advisory_output=None,
        enabled=False,
        dry_run=False,
        config={"model_enabled": True, "model_api_key": "k"},
        model_generate=lambda context, config: sentinel_transport("sentinel", {}, {}, 0),
    )
    assert result["action"] == "disabled"
    assert transport_calls == [], "transport must not be called when advisory is disabled"
    assert store.read_jsonl("candidates") == []


def test_enabled_non_dry_run_creates_internal_candidate_only(state_dir):
    store = SensoriumStore(instance="test", state_dir=state_dir)
    store.ensure_dirs()
    store.append_jsonl("events", _event(2, kind="kanban_pressure", summary="blocked=17"))
    output = {
        "action": "CREATE_CONSCIOUS_TASK",
        "rationale": "Kanban pressure is actionable.",
        "event_ids": ["evt_2"],
        "candidate_ids": [],
        "pressure": 0.7,
        "conscious_task": {
            "request_type": "THINK",
            "title": "Review blocked Kanban tasks",
            "why": "Subconscious advisory saw blocked task pressure.",
            "expected_decision": "Suppress, hold, or create a bounded cleanup task.",
        },
    }

    result = run_subconscious_advisory(store, advisory_output=output, enabled=True, dry_run=False)

    assert result["action"] == "created_conscious_task_candidate"
    candidates = store.read_jsonl("candidates")
    assert len(candidates) == 1
    assert candidates[0]["kind"] == "subconscious_advisory"
    assert candidates[0]["conscious_task"]["title"] == "Review blocked Kanban tasks"
    assert store.read_jsonl("threads") == []


def test_generate_advisory_output_uses_cheap_openai_compatible_model():
    requests = []

    def fake_transport(url, payload, headers, timeout):
        requests.append({"url": url, "payload": payload, "headers": headers, "timeout": timeout})
        return {
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "action": "SAVE",
                        "rationale": "Cheap model saw weak but useful signal pressure.",
                        "event_ids": ["evt_1"],
                        "candidate_ids": [],
                    })
                }
            }]
        }

    output = generate_advisory_output(
        {"recent_events": [_event(1)], "top_candidates": [], "recent_decisions": []},
        config={
            "model_enabled": True,
            "model_provider": "minimax",
            "model": "MiniMax-M2.5",
            "model_base_url": "https://api.minimax.io/v1",
            "model_api_key": "test-key",
            "model_timeout_seconds": 7,
        },
        transport=fake_transport,
    )

    assert output["action"] == "SAVE"
    assert requests[0]["url"] == "https://api.minimax.io/v1/chat/completions"
    assert requests[0]["payload"]["model"] == "MiniMax-M2.5"
    assert requests[0]["payload"]["response_format"] == {"type": "json_object"}
    assert requests[0]["headers"]["Authorization"] == "Bearer test-key"
    assert requests[0]["timeout"] == 7


def test_minimax_think_wrapper_is_stripped_before_json_parse():
    content = """<think>MiniMax may include internal reasoning with {\"action\":\"DROP\",\"rationale\":\"wrong-place\"}.</think>

{"action":"SAVE","rationale":"final advisory","event_ids":["evt_1"],"candidate_ids":[]}"""

    parsed = _extract_json_object(content)

    assert parsed == {
        "action": "SAVE",
        "rationale": "final advisory",
        "event_ids": ["evt_1"],
        "candidate_ids": [],
    }


def test_model_prompt_routes_quantified_thresholds_outside_subconscious():
    messages = _model_prompt({"top_candidates": [{"kind": "design_decision", "summary": "semantic pattern", "pressure": 0.68}]})

    system = messages[0]["content"]
    assert "Do not adjudicate directly quantified" in system
    assert "semantic linking" in system


def test_enabled_model_lane_reasons_when_no_advisory_output(state_dir):
    store = SensoriumStore(instance="test", state_dir=state_dir)
    store.ensure_dirs()
    store.append_jsonl("events", _event(4, kind="design_decision", summary="repeated session theme"))

    def fake_generate(context, *, config):
        assert context["recent_events"]
        assert config["model_enabled"] is True
        return {
            "action": "CREATE_CONSCIOUS_TASK",
            "rationale": "Cheap model saw repeated Hindsight failure pressure.",
            "event_ids": ["evt_4"],
            "candidate_ids": [],
            "pressure": 0.71,
            "conscious_task": {
                "request_type": "THINK",
                "title": "Review Hindsight pressure",
                "why": "A cheap Subconscious pass saw failed operation pressure.",
                "expected_decision": "Suppress, hold, or create a bounded repair follow-up.",
            },
        }

    result = run_subconscious_advisory(
        store,
        enabled=True,
        dry_run=False,
        config={"model_enabled": True},
        model_generate=fake_generate,
    )

    assert result["action"] == "created_conscious_task_candidate"
    assert result["model_used"] is True
    candidates = store.read_jsonl("candidates")
    assert len(candidates) == 1
    assert candidates[0]["summary"] == "Review Hindsight pressure"


def test_tool_handler_runs_advisory_dry_run(state_dir):
    store = SensoriumStore(instance="test", state_dir=state_dir)
    store.ensure_dirs()
    store.append_jsonl("events", _event(3))

    raw = handle_sensorium_subconscious_advisory(
        instance="test",
        state_dir=state_dir,
        dry_run=True,
        enabled=False,
    )

    result = json.loads(raw)
    assert result["success"] is True
    assert result["data"]["action"] == "disabled"
    assert "context" in result["data"]
