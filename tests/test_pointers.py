"""Tests for active-session Sensorium pointers."""

from agent_sensorium.pointers import (
    handle_pointer_pre_llm,
    pointer_context_for_llm,
    select_attention_pointer,
)
from agent_sensorium.schemas import truncate_text
from agent_sensorium.store import SensoriumStore


def _thread(**overrides):
    base = {
        "id": "sth_testpointer",
        "status": "dormant",
        "origin": "candidate",
        "conscious_task": {
            "id": "ctask_testpointer",
            "request_type": "THINK",
            "title": "Review design_decision: Operator corrected that Sera identity images should use references for sustained continuity",
            "why": "test",
            "expected_decision": "test",
        },
        "origin_candidate_id": "cand_testpointer",
        "continuity_summary": ["summary"],
        "decision_log": [],
        "interaction_refs": [],
        "summary_dirty": False,
        "open_questions": [],
        "next_prompt_to_operator": "Take up this thread?",
        "sensitivity": "private",
        "allowed_surfaces": ["local", "dashboard"],
        "created_at": "2026-05-24T10:00:00Z",
        "updated_at": "2026-05-24T10:00:00Z",
        "expires_at": "2026-05-31T10:00:00Z",
    }
    base.update(overrides)
    return base


def test_pointer_requires_allowed_surface(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    store.append_jsonl("threads", _thread())

    local = select_attention_pointer(store, surface="local")
    assert local["action"] == "pointer_available"
    assert local["thread_id"] == "sth_testpointer"

    discord = select_attention_pointer(store, surface="discord")
    assert discord["action"] == "no_pointer"
    assert discord["reason"] == "no_visible_thread_for_surface"


def test_pre_llm_pointer_records_cooldown_receipt(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    store.append_jsonl("threads", _thread(allowed_surfaces=["discord"]))

    first = handle_pointer_pre_llm(
        instance="test",
        platform="discord",
        session_id="session-1",
        state_dir=str(tmp_path),
    )
    assert first is not None
    assert "[Sensorium Pointer]" in first["context"]
    assert "Pending thread: sth_testpointer" in first["context"]
    assert "If the user says" in first["context"]
    assert "sensorium_thread_open" in first["context"]
    assert "surface=\"discord\"" in first["context"]
    assert "thread_id=\"sth_testpointer\"" in first["context"]
    assert "Do not reveal capsule content unless opened" in first["context"]

    receipts = store.read_jsonl("decisions")
    assert len(receipts) == 1
    assert receipts[0]["type"] == "pointer.presented"
    assert receipts[0]["surface"] == "discord"

    second = handle_pointer_pre_llm(
        instance="test",
        platform="discord",
        session_id="session-1",
        state_dir=str(tmp_path),
    )
    assert second is None


def test_pointer_context_is_door_handle_not_capsule():
    pointer = {
        "thread_id": "sth_x",
        "title": "A small title",
        "invitation": "Sensorium has a pending thread: A small title. Say ‘take it up’ if you want me to open it.",
    }
    context = pointer_context_for_llm(pointer)
    assert "continuity_summary" not in context
    assert "decision_log" not in context
    assert "take it up" in context
    assert "sensorium_thread_open" in context
    assert "sth_x" in context


def test_pointer_preview_reports_cooldown_reason(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    store.append_jsonl("threads", _thread(allowed_surfaces=["discord"]))

    first = handle_pointer_pre_llm(
        instance="test",
        platform="discord",
        session_id="session-1",
        state_dir=str(tmp_path),
    )
    assert first is not None

    preview = select_attention_pointer(store, surface="discord")
    assert preview["action"] == "no_pointer"
    assert preview["thread_id"] == "sth_testpointer"
    assert preview["reason"].startswith("cooldown_until:")


def test_truncate_text_avoids_mid_word_guillotine():
    text = "Operator corrected that Sera identity images should use references for sustained continuity"
    out = truncate_text(text, 62)
    assert out.endswith("…")
    assert "continuit…" not in out
    assert len(out) <= 62
