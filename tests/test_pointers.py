"""Tests for active-session Sensorium pointers."""

import json

from agent_sensorium.pointers import (
    handle_pointer_pre_llm,
    pointer_context_for_llm,
    record_pointer_presented,
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
            "title": "Review design_decision: Operator corrected that demo identity images should use references for sustained continuity",
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


def _candidate(**overrides):
    base = {
        "id": "cand_livepointer",
        "status": "candidate",
        "kind": "relational_salience",
        "summary": "Sebastian misses small private presents and wants salience left open for later",
        "pressure": 0.82,
        "sensitivity": "private",
        "allowed_surfaces": ["local", "discord"],
        "created_at": "2026-06-10T05:40:00Z",
        "updated_at": "2026-06-10T05:40:00Z",
    }
    base.update(overrides)
    return base


def _write_config(state_dir, surfaces=None, max_sensitivity="private"):
    from pathlib import Path
    config = {"allowed_surfaces": surfaces or ["local"], "max_sensitivity": max_sensitivity}
    path = Path(state_dir) / "instance.config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config))


def _messages_for_user_turn(turn_index: int, text: str) -> list[dict]:
    messages: list[dict] = []
    for idx in range(1, turn_index + 1):
        messages.append({"role": "user", "content": text if idx == turn_index else f"earlier turn {idx}"})
        if idx != turn_index:
            messages.append({"role": "assistant", "content": f"assistant reply {idx}"})
    return messages


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
    _write_config(tmp_path, surfaces=["discord"])
    store.append_jsonl("threads", _thread(allowed_surfaces=["discord"]))

    first = handle_pointer_pre_llm(
        instance="test",
        platform="discord",
        session_id="session-1",
        state_dir=str(tmp_path),
    )
    assert first is not None
    assert "[Sensorium Pointer]" in first["context"]
    assert "Pointer type: thread — sth_testpointer" in first["context"]
    assert "If the user says" in first["context"]
    assert "sensorium(action=\"open\"" in first["context"]
    assert "surface=\"discord\"" in first["context"]
    assert "id=\"sth_testpointer\"" in first["context"]
    assert "Do not reveal capsule content unless opened" in first["context"]
    # Honest copy: a thread pointer must say so explicitly; never claim a
    # thread exists for a candidate pointer.
    assert "conscious thread waiting" in first["context"].lower()
    assert "NOT an openable thread" not in first["context"]

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
    assert second is not None
    assert "Pointer type: thread — sth_testpointer" in second["context"]

    receipts = store.read_jsonl("decisions")
    assert len(receipts) == 2


def test_pointer_context_is_door_handle_not_capsule():
    pointer = {
        "pointer_type": "thread",
        "thread_id": "sth_x",
        "title": "A small title",
        "invitation": '🧠 I have a conscious thread waiting: A small title. Say "take it up" if you want me to open it. 🧵',
    }
    context = pointer_context_for_llm(pointer)
    assert "continuity_summary" not in context
    assert "decision_log" not in context
    assert "take it up" in context
    assert "sensorium(action=\"open\"" in context
    assert "sth_x" in context
    # Honest wording: thread pointer must NOT be confused with candidate.
    assert "Pointer type: thread" in context
    assert "NOT an openable thread" not in context


def test_candidate_fallback_pointer_when_no_threads(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    _write_config(tmp_path, surfaces=["discord"])
    store.append_jsonl("candidates", _candidate(allowed_surfaces=["discord"]))

    pointer = select_attention_pointer(store, surface="discord")
    assert pointer["action"] == "pointer_available"
    assert pointer["pointer_type"] == "candidate"
    assert pointer["candidate_id"] == "cand_livepointer"
    assert "I have a salience candidate" in pointer["invitation"]
    # Honest copy: the candidate pointer must never claim to be a thread.
    assert "not an openable thread" in pointer["invitation"].lower()


def test_candidate_pointer_context_uses_exact_candidate_open_not_rotating_status():
    pointer = {
        "pointer_type": "candidate",
        "candidate_id": "cand_x",
        "title": "Live salience",
        "surface": "discord",
        "invitation": "🧠 ✨ I have a salience candidate waiting (not an openable thread): Live salience.",
    }
    context = pointer_context_for_llm(pointer)
    assert "Pointer type: candidate" in context
    assert "cand_x" in context
    # Direction: use exact candidate id. Calling status after pointer receipt can
    # rotate to a different candidate/residue and mismatch the doorway.
    assert 'sensorium(action="open", surface="discord", id="cand_x")' in context
    assert "do not switch to a different status pointer" in context
    # Honest: the candidate context must not say it is a thread.
    assert "Do not mark it reviewed merely because it was shown" in context
    assert "NOT an openable thread" in context
    assert "no openable thread" in context


def test_candidate_pointer_records_candidate_cooldown_receipt(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    _write_config(tmp_path, surfaces=["discord"])
    store.append_jsonl("candidates", _candidate(allowed_surfaces=["discord"]))

    first = handle_pointer_pre_llm(
        instance="test",
        platform="discord",
        session_id="session-1",
        state_dir=str(tmp_path),
        current_text="take a look",
    )
    assert first is not None
    assert "Pointer type: candidate" in first["context"]
    assert "cand_livepointer" in first["context"]

    receipts = store.read_jsonl("decisions")
    assert len(receipts) == 1
    assert receipts[0]["type"] == "pointer.presented"
    assert receipts[0]["candidate_id"] == "cand_livepointer"

    second = select_attention_pointer(
        store,
        surface="discord",
        config={"fallback_when_all_visible_on_cooldown": False},
    )
    assert second["action"] == "no_pointer"


def test_thread_pointer_preferred_over_candidate_fallback(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    _write_config(tmp_path, surfaces=["discord"])
    store.append_jsonl("candidates", _candidate(allowed_surfaces=["discord"]))
    store.append_jsonl("threads", _thread(allowed_surfaces=["discord"]))

    pointer = select_attention_pointer(store, surface="discord")
    assert pointer["action"] == "pointer_available"
    assert pointer["pointer_type"] == "thread"
    assert pointer["thread_id"] == "sth_testpointer"


def test_pointer_preview_bypasses_cooldown_when_every_visible_thread_is_blocked(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    _write_config(tmp_path, surfaces=["discord"])
    store.append_jsonl("threads", _thread(allowed_surfaces=["discord"]))

    first = handle_pointer_pre_llm(
        instance="test",
        platform="discord",
        session_id="session-1",
        state_dir=str(tmp_path),
    )
    assert first is not None

    preview = select_attention_pointer(store, surface="discord")
    assert preview["action"] == "pointer_available"
    assert preview["thread_id"] == "sth_testpointer"
    assert preview["cooldown_bypassed"] is True
    assert preview["reason"].startswith("cooldown_bypassed_all_visible_threads:cooldown_until:")


def test_pointer_preview_can_still_fail_closed_on_cooldown_when_fallback_disabled(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    _write_config(tmp_path, surfaces=["discord"])
    store.append_jsonl("threads", _thread(allowed_surfaces=["discord"]))

    first = handle_pointer_pre_llm(
        instance="test",
        platform="discord",
        session_id="session-1",
        state_dir=str(tmp_path),
        config={"fallback_when_all_visible_on_cooldown": False},
    )
    assert first is not None

    preview = select_attention_pointer(
        store,
        surface="discord",
        config={"fallback_when_all_visible_on_cooldown": False},
    )
    assert preview["action"] == "no_pointer"
    assert preview["thread_id"] == "sth_testpointer"
    assert preview["reason"].startswith("cooldown_until:")


def test_truncate_text_avoids_mid_word_guillotine():
    text = "Operator corrected that demo identity images should use references for sustained continuity"
    out = truncate_text(text, 62)
    assert out.endswith("…")
    assert "continuit…" not in out
    assert len(out) <= 62


def test_pointer_presented_receipt_keeps_subject_signature(tmp_path):
    """A valid pointer receipt records the exact subject kind/id and displayed title."""
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    _write_config(tmp_path, surfaces=["discord"])
    store.append_jsonl("candidates", _candidate(allowed_surfaces=["discord"]))

    pointer = select_attention_pointer(store, surface="discord")
    receipt = record_pointer_presented(store, pointer, session_id="s1", surface="discord")

    assert receipt["type"] == "pointer.presented"
    assert receipt["pointer_type"] == "candidate"
    assert receipt["subject_kind"] == "candidate"
    assert receipt["subject_id"] == "cand_livepointer"
    assert receipt["presented_title"] == pointer["title"]


def test_pointer_presented_guard_blocks_title_mismatch(tmp_path):
    """Wrong displayed subject text writes a guard event instead of cooldown receipt."""
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    _write_config(tmp_path, surfaces=["discord"])
    store.append_jsonl("candidates", _candidate(allowed_surfaces=["discord"]))

    pointer = select_attention_pointer(store, surface="discord")
    pointer["title"] = "Different residue from status rotation"
    receipt = record_pointer_presented(store, pointer, session_id="s1", surface="discord")

    assert receipt["type"] == "pointer.presented.guard"
    assert receipt["outcome"] == "blocked"
    assert receipt["reason"] == "candidate_title_mismatch"
    assert receipt["subject_id"] == "cand_livepointer"
    assert "Sebastian misses small private presents" in receipt["expected_title"]


def test_pointer_presented_guard_blocks_saved_residue_without_settlement(tmp_path):
    """A saved_residue pointer must point to a row with durable Kanban linkage."""
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    _write_config(tmp_path, surfaces=["discord"])
    store.append_jsonl("candidates", _candidate(
        status="archived",
        allowed_surfaces=["discord"],
        kanban_settlement={},
    ))

    pointer = {
        "action": "pointer_available",
        "pointer_type": "saved_residue",
        "candidate_id": "cand_livepointer",
        "title": "Sebastian misses small private presents and wants salience left open for later",
        "surface": "discord",
    }
    receipt = record_pointer_presented(store, pointer, session_id="s1", surface="discord")

    assert receipt["type"] == "pointer.presented.guard"
    assert receipt["reason"] == "saved_residue_settlement_missing"


def test_candidate_pointer_suppressed_before_render_when_not_relevant(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    _write_config(tmp_path, surfaces=["discord"])
    store.append_jsonl("candidates", _candidate(allowed_surfaces=["discord"], pressure=0.82))

    result = handle_pointer_pre_llm(
        instance="test",
        platform="discord",
        session_id="session-a",
        state_dir=str(tmp_path),
    )

    assert result is None
    receipts = store.read_jsonl("decisions")
    assert len(receipts) == 1
    assert receipts[0]["type"] == "pointer.suppressed"
    assert receipts[0]["pointer_type"] == "candidate"
    assert receipts[0]["reason"] == "relevance_gate"
    assert receipts[0]["candidate_id"] == "cand_livepointer"


def test_high_urgency_candidate_pointer_can_still_inject_without_user_text(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    _write_config(tmp_path, surfaces=["discord"])
    store.append_jsonl("candidates", _candidate(allowed_surfaces=["discord"], pressure=0.96))

    result = handle_pointer_pre_llm(
        instance="test",
        platform="discord",
        session_id="session-a",
        state_dir=str(tmp_path),
    )

    assert result is not None
    assert "Pointer type: candidate" in result["context"]
    receipts = store.read_jsonl("decisions")
    assert receipts[-1]["type"] == "pointer.presented"
    assert receipts[-1]["foreground_turn_index"] == 1


def test_saved_residue_pointer_requires_explicit_pathway_or_relevance(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    _write_config(tmp_path, surfaces=["discord"])
    store.append_jsonl("candidates", _candidate(
        allowed_surfaces=["discord"],
        status="archived",
        pressure=0.61,
        kanban_settlement={
            "decision": "SAVE",
            "intake_task_id": "kt_saved_1",
            "review_task_id": "kt_saved_review_1",
            "settled_at": "2026-06-10T05:41:00Z",
            "reason_label": "saved-residue-test",
        },
    ))

    blocked = handle_pointer_pre_llm(
        instance="test",
        platform="discord",
        session_id="session-a",
        state_dir=str(tmp_path),
    )
    assert blocked is None

    opened = handle_pointer_pre_llm(
        instance="test",
        platform="discord",
        session_id="session-b",
        state_dir=str(tmp_path),
        current_text="please check saved residue",
        config={"cooldown_minutes": 0},
    )
    assert opened is not None
    assert "Pointer type: saved_residue" in opened["context"]


def test_candidate_pointer_min_turn_gap_uses_user_turn_index_not_pointer_count(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    _write_config(tmp_path, surfaces=["discord"])
    store.append_jsonl("candidates", _candidate(allowed_surfaces=["discord"], pressure=0.96))

    turn_1 = handle_pointer_pre_llm(
        instance="test",
        platform="discord",
        session_id="session-a",
        state_dir=str(tmp_path),
        config={"cooldown_minutes": 0},
        messages=_messages_for_user_turn(1, "general check-in"),
    )
    assert turn_1 is not None

    turn_2 = handle_pointer_pre_llm(
        instance="test",
        platform="discord",
        session_id="session-a",
        state_dir=str(tmp_path),
        config={"cooldown_minutes": 0},
        messages=_messages_for_user_turn(2, "another general check-in"),
    )
    assert turn_2 is None

    turn_4 = handle_pointer_pre_llm(
        instance="test",
        platform="discord",
        session_id="session-a",
        state_dir=str(tmp_path),
        config={"cooldown_minutes": 0},
        messages=_messages_for_user_turn(4, "Sebastian private presents still matter"),
    )
    assert turn_4 is not None
    assert "Pointer type: candidate" in turn_4["context"]

    receipts = store.read_jsonl("decisions")
    assert [r["type"] for r in receipts] == [
        "pointer.presented",
        "pointer.suppressed",
        "pointer.presented",
    ]
    assert receipts[0]["foreground_turn_index"] == 1
    assert receipts[1]["reason"] == "min_turn_gap"
    assert receipts[1]["foreground_turn_index"] == 2
    assert receipts[2]["foreground_turn_index"] == 4


def test_max_cards_per_turn_counts_only_same_foreground_turn(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    _write_config(tmp_path, surfaces=["discord"])
    store.append_jsonl("candidates", _candidate(allowed_surfaces=["discord"], pressure=0.96))

    turn_1 = handle_pointer_pre_llm(
        instance="test",
        platform="discord",
        session_id="session-a",
        state_dir=str(tmp_path),
        config={"cooldown_minutes": 0, "min_turn_gap": 0},
        messages=_messages_for_user_turn(1, "general check-in"),
    )
    assert turn_1 is not None

    turn_1_retry = handle_pointer_pre_llm(
        instance="test",
        platform="discord",
        session_id="session-a",
        state_dir=str(tmp_path),
        config={"cooldown_minutes": 0, "min_turn_gap": 0},
        messages=_messages_for_user_turn(1, "general check-in"),
    )
    assert turn_1_retry is None

    turn_2 = handle_pointer_pre_llm(
        instance="test",
        platform="discord",
        session_id="session-a",
        state_dir=str(tmp_path),
        config={"cooldown_minutes": 0, "min_turn_gap": 0},
        messages=_messages_for_user_turn(2, "general check-in again"),
    )
    assert turn_2 is not None

    receipts = store.read_jsonl("decisions")
    assert [r["type"] for r in receipts] == [
        "pointer.presented",
        "pointer.suppressed",
        "pointer.presented",
    ]
    assert receipts[1]["reason"] == "max_cards_per_turn"
    assert receipts[1]["foreground_turn_index"] == 1
    assert receipts[2]["foreground_turn_index"] == 2


def test_explicit_request_bypass_still_works(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    _write_config(tmp_path, surfaces=["discord"])
    store.append_jsonl("candidates", _candidate(allowed_surfaces=["discord"], pressure=0.4))

    result = handle_pointer_pre_llm(
        instance="test",
        platform="discord",
        session_id="session-a",
        state_dir=str(tmp_path),
        current_text="please take a look at the sensorium inbox",
        config={"cooldown_minutes": 0},
        messages=_messages_for_user_turn(1, "please take a look at the sensorium inbox"),
    )

    assert result is not None
    assert "Pointer type: candidate" in result["context"]
    receipts = store.read_jsonl("decisions")
    assert receipts[-1]["type"] == "pointer.presented"


def test_openable_thread_pointer_bypasses_non_openable_budget(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    _write_config(tmp_path, surfaces=["discord"])
    store.append_jsonl("threads", _thread(allowed_surfaces=["discord"]))

    first = handle_pointer_pre_llm(
        instance="test",
        platform="discord",
        session_id="session-a",
        state_dir=str(tmp_path),
        messages=_messages_for_user_turn(1, "general check-in"),
    )
    second = handle_pointer_pre_llm(
        instance="test",
        platform="discord",
        session_id="session-a",
        state_dir=str(tmp_path),
        messages=_messages_for_user_turn(1, "general check-in"),
    )

    assert first is not None
    assert second is not None
    assert "Pointer type: thread" in second["context"]
    receipts = store.read_jsonl("decisions")
    assert [r["type"] for r in receipts] == ["pointer.presented", "pointer.presented"]


class TestPointerPolicyUnification:
    """Verify pointer selection enforces instance config policy."""

    def test_config_excludes_surface_returns_no_pointer(self, tmp_path):
        """Thread allows discord+local, config allows local only → discord no_pointer."""
        store = SensoriumStore(instance="test", state_dir=str(tmp_path))
        store.ensure_dirs()
        _write_config(tmp_path, surfaces=["local"])
        store.append_jsonl("threads", _thread(allowed_surfaces=["discord", "local"]))

        result = select_attention_pointer(store, surface="discord")
        assert result["action"] == "no_pointer"
        assert result["reason"] == "no_visible_thread_for_surface"

    def test_config_allows_surface_returns_pointer(self, tmp_path):
        """Thread allows local, config allows local → pointer available."""
        store = SensoriumStore(instance="test", state_dir=str(tmp_path))
        store.ensure_dirs()
        _write_config(tmp_path, surfaces=["local"])
        store.append_jsonl("threads", _thread(allowed_surfaces=["local"]))

        result = select_attention_pointer(store, surface="local")
        assert result["action"] == "pointer_available"

    def test_pre_llm_no_receipt_when_config_excludes_surface(self, tmp_path):
        """pre_llm on excluded surface → None, no pointer.presented receipt."""
        store = SensoriumStore(instance="test", state_dir=str(tmp_path))
        store.ensure_dirs()
        _write_config(tmp_path, surfaces=["local"])
        store.append_jsonl("threads", _thread(allowed_surfaces=["discord", "local"]))

        result = handle_pointer_pre_llm(
            instance="test", platform="discord",
            session_id="s1", state_dir=str(tmp_path),
        )
        assert result is None
        assert store.read_jsonl("decisions") == []

    def test_sensitivity_gate_blocks_pointer(self, tmp_path):
        """Thread sensitivity=public_safe, config max_sensitivity=private → no pointer."""
        store = SensoriumStore(instance="test", state_dir=str(tmp_path))
        store.ensure_dirs()
        _write_config(tmp_path, surfaces=["local"], max_sensitivity="private")
        store.append_jsonl("threads", _thread(
            allowed_surfaces=["local"], sensitivity="public_safe",
        ))

        result = select_attention_pointer(store, surface="local")
        assert result["action"] == "no_pointer"

    def test_missing_config_defaults_to_local_only(self, tmp_path):
        """No config file → SAFE_DEFAULTS (local only). Discord pointer blocked."""
        store = SensoriumStore(instance="test", state_dir=str(tmp_path))
        store.ensure_dirs()
        store.append_jsonl("threads", _thread(allowed_surfaces=["discord"]))

        result = select_attention_pointer(store, surface="discord")
        assert result["action"] == "no_pointer"

    def test_local_still_works_with_default_config(self, tmp_path):
        """No config file → local still works since SAFE_DEFAULTS allows local."""
        store = SensoriumStore(instance="test", state_dir=str(tmp_path))
        store.ensure_dirs()
        store.append_jsonl("threads", _thread(allowed_surfaces=["local"]))

        result = select_attention_pointer(store, surface="local")
        assert result["action"] == "pointer_available"
