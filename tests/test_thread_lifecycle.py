"""Tests for conscious-thread open/update lifecycle."""

import json

from agent_sensorium.tools import (
    handle_sensorium_attention_pointer,
    handle_sensorium_thread_open,
    handle_sensorium_thread_update,
)
from agent_sensorium.store import SensoriumStore


def _thread(**overrides):
    base = {
        "id": "sth_lifecycle",
        "status": "dormant",
        "origin": "candidate",
        "conscious_task": {
            "id": "ctask_lifecycle",
            "request_type": "THINK",
            "title": "Review reference-first identity media correction",
            "why": "Operator corrected that Sera identity images need references for continuity.",
            "expected_decision": "Decide whether to save as identity/media guardrail.",
        },
        "origin_candidate_id": "cand_lifecycle",
        "continuity_summary": ["Identity media must be reference-first, not mood-card-only."],
        "decision_log": [],
        "interaction_refs": [],
        "summary_dirty": False,
        "open_questions": ["Should this become a hard media pipeline guard?"],
        "next_prompt_to_operator": "Open and close after guardrail is installed.",
        "sensitivity": "private",
        "allowed_surfaces": ["local", "discord"],
        "created_at": "2026-05-24T10:00:00Z",
        "updated_at": "2026-05-24T10:00:00Z",
        "expires_at": "2026-05-31T10:00:00Z",
    }
    base.update(overrides)
    return base


def test_thread_open_returns_compact_capsule_when_surface_allowed(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    store.append_jsonl("threads", _thread())

    result = json.loads(
        handle_sensorium_thread_open(
            instance="test", state_dir=str(tmp_path), thread_id="latest", surface="discord"
        )
    )

    assert result["success"] is True
    data = result["data"]
    assert data["thread_id"] == "sth_lifecycle"
    assert data["title"] == "Review reference-first identity media correction"
    assert data["continuity_summary"] == ["Identity media must be reference-first, not mood-card-only."]
    assert "decision_log" not in data


def test_thread_open_refuses_disallowed_surface(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    store.append_jsonl("threads", _thread(allowed_surfaces=["local"]))

    result = json.loads(
        handle_sensorium_thread_open(
            instance="test", state_dir=str(tmp_path), thread_id="sth_lifecycle", surface="discord"
        )
    )

    assert result["success"] is False
    assert "not allowed on surface" in result["error"]


def test_thread_update_closes_thread_and_removes_pointer_eligibility(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    store.append_jsonl("threads", _thread())

    before = json.loads(
        handle_sensorium_attention_pointer(
            instance="test", state_dir=str(tmp_path), surface="discord"
        )
    )
    assert before["data"]["action"] == "pointer_available"

    update = json.loads(
        handle_sensorium_thread_update(
            instance="test",
            state_dir=str(tmp_path),
            thread_id="sth_lifecycle",
            action="close",
            reason="guardrail installed",
        )
    )
    assert update["success"] is True
    assert update["data"]["old_status"] == "dormant"
    assert update["data"]["new_status"] == "closed"

    after = json.loads(
        handle_sensorium_attention_pointer(
            instance="test", state_dir=str(tmp_path), surface="discord"
        )
    )
    assert after["data"]["action"] == "no_pointer"

    receipts = store.read_jsonl("decisions")
    assert receipts[-1]["type"] == "thread.updated"
    assert receipts[-1]["reason"] == "guardrail installed"


def test_thread_update_supports_hold_resume_and_pin(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    store.append_jsonl("threads", _thread())

    for action in ("hold", "resume", "pin", "unpin"):
        result = json.loads(
            handle_sensorium_thread_update(
                instance="test", state_dir=str(tmp_path), thread_id="latest", action=action
            )
        )
        assert result["success"] is True

    thread = store.read_jsonl("threads")[0]
    assert thread["status"] == "dormant"
    assert thread["pinned"] is False


def test_open_closed_thread_by_explicit_id_refused(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    store.append_jsonl("threads", _thread(status="closed"))

    result = json.loads(
        handle_sensorium_thread_open(
            instance="test", state_dir=str(tmp_path), thread_id="sth_lifecycle", surface="local"
        )
    )
    assert result["success"] is False
    assert "closed" in result["error"]
    assert "cannot be opened" in result["error"]


def test_open_archived_thread_by_explicit_id_refused(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    store.append_jsonl("threads", _thread(status="archived"))

    result = json.loads(
        handle_sensorium_thread_open(
            instance="test", state_dir=str(tmp_path), thread_id="sth_lifecycle", surface="local"
        )
    )
    assert result["success"] is False
    assert "archived" in result["error"]


def test_resume_closed_thread_refused_no_receipt(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    store.append_jsonl("threads", _thread(status="closed"))

    result = json.loads(
        handle_sensorium_thread_update(
            instance="test", state_dir=str(tmp_path), thread_id="sth_lifecycle", action="resume"
        )
    )
    assert result["success"] is False
    assert "closed" in result["error"]
    assert store.read_jsonl("decisions") == []


def test_resume_archived_thread_refused_no_receipt(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    store.append_jsonl("threads", _thread(status="archived"))

    result = json.loads(
        handle_sensorium_thread_update(
            instance="test", state_dir=str(tmp_path), thread_id="sth_lifecycle", action="resume"
        )
    )
    assert result["success"] is False
    assert "archived" in result["error"]
    assert store.read_jsonl("decisions") == []


def test_repeated_close_refused_no_receipt(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    store.append_jsonl("threads", _thread())

    handle_sensorium_thread_update(
        instance="test", state_dir=str(tmp_path), thread_id="sth_lifecycle", action="close"
    )
    receipts_after_close = len(store.read_jsonl("decisions"))

    result = json.loads(
        handle_sensorium_thread_update(
            instance="test", state_dir=str(tmp_path), thread_id="sth_lifecycle", action="close"
        )
    )
    assert result["success"] is False
    assert "cannot be updated" in result["error"]
    assert len(store.read_jsonl("decisions")) == receipts_after_close


def test_hold_from_held_refused_no_receipt(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    store.append_jsonl("threads", _thread())

    handle_sensorium_thread_update(
        instance="test", state_dir=str(tmp_path), thread_id="sth_lifecycle", action="hold"
    )
    receipts_after_hold = len(store.read_jsonl("decisions"))

    result = json.loads(
        handle_sensorium_thread_update(
            instance="test", state_dir=str(tmp_path), thread_id="sth_lifecycle", action="hold"
        )
    )
    assert result["success"] is False
    assert "not allowed" in result["error"]
    assert len(store.read_jsonl("decisions")) == receipts_after_hold


def test_allowed_transitions_still_work(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    store.append_jsonl("threads", _thread())

    for action, expected_status in [("hold", "held"), ("resume", "dormant"), ("close", "closed")]:
        result = json.loads(
            handle_sensorium_thread_update(
                instance="test", state_dir=str(tmp_path), thread_id="sth_lifecycle", action=action
            )
        )
        assert result["success"] is True, f"{action} should succeed"
        assert result["data"]["new_status"] == expected_status


def test_pin_already_pinned_refused(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    store.append_jsonl("threads", _thread(pinned=True))

    result = json.loads(
        handle_sensorium_thread_update(
            instance="test", state_dir=str(tmp_path), thread_id="sth_lifecycle", action="pin"
        )
    )
    assert result["success"] is False
    assert "already pinned" in result["error"]
