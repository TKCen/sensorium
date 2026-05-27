"""Tests for active-session Sensorium pointers."""

import json

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


def _write_config(state_dir, surfaces=None, max_sensitivity="private"):
    from pathlib import Path
    config = {"allowed_surfaces": surfaces or ["local"], "max_sensitivity": max_sensitivity}
    path = Path(state_dir) / "instance.config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config))


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
    assert preview["action"] == "no_pointer"
    assert preview["thread_id"] == "sth_testpointer"
    assert preview["reason"].startswith("cooldown_until:")


def test_truncate_text_avoids_mid_word_guillotine():
    text = "Operator corrected that Sera identity images should use references for sustained continuity"
    out = truncate_text(text, 62)
    assert out.endswith("…")
    assert "continuit…" not in out
    assert len(out) <= 62


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
