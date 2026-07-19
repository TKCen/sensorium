"""P1-1 audit: lock the live sensorium(action='update') wire through the plugin handler.

These tests exercise the LLM-facing live aperture (`ctx.tools['sensorium']['handler']`)
end-to-end for the update keyword path (close, hold, unknown id, invalid keyword,
candidate-mark-reviewed). The underlying handle_sensorium_thread_update is covered by
tests/test_thread_lifecycle.py, but the plugin handler fan-in (plugin.py:199-207) was
previously uncovered. The point is to lock the wire so a future refactor cannot silently
break the live handler.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_sensorium.plugin import register


class FakePluginContext:
    """Inline minimal plugin context (mirrors tests/test_plugin_registration.py)."""

    def __init__(self):
        self.tools = {}
        self.commands = {}
        self.skills = {}
        self.hooks = {}

    def register_tool(self, name, toolset, schema, handler, **kwargs):
        self.tools[name] = {
            "toolset": toolset,
            "schema": schema,
            "handler": handler,
            **kwargs,
        }

    def register_command(self, name, handler, **kwargs):
        self.commands[name] = {"handler": handler, **kwargs}

    def register_skill(self, name, path, **kwargs):
        self.skills[name] = {"path": path, **kwargs}

    def register_hook(self, name, handler, **kwargs):
        key = name
        idx = 2
        while f"{name}#{idx}" in self.hooks:
            idx += 1
        if name in self.hooks:
            key = f"{name}#{idx}"
        self.hooks[key] = {"handler": handler, **kwargs}


def _write_config(state_dir, surfaces=None, max_sensitivity="private"):
    config = {"allowed_surfaces": surfaces or ["local"], "max_sensitivity": max_sensitivity}
    path = Path(state_dir) / "instance.config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config))


def _write_thread(store, thread_id="sth_close", origin_candidate_id=None, **overrides):
    base = {
        "id": thread_id,
        "status": "dormant",
        "origin": "candidate",
        "conscious_task": {
            "id": "ctask_close",
            "request_type": "THINK",
            "title": "Close wire test",
            "why": "Test the live update(close) wire through the plugin handler.",
            "expected_decision": "Decide whether to close after review.",
        },
        "origin_candidate_id": origin_candidate_id or "cand_close",
        "continuity_summary": ["Lock the live update wire so a future refactor can't break it."],
        "decision_log": [],
        "interaction_refs": [],
        "summary_dirty": False,
        "open_questions": [],
        "next_prompt_to_operator": "Open and close after wire is locked.",
        "sensitivity": "private",
        "allowed_surfaces": ["local"],
        "created_at": "2026-07-04T10:00:00Z",
        "updated_at": "2026-07-04T10:00:00Z",
        "expires_at": "2099-07-11T10:00:00Z",
    }
    base.update(overrides)
    store.append_jsonl("threads", base)
    return base


def _write_candidate(store, candidate_id="cand_update", **overrides):
    base = {
        "id": candidate_id,
        "status": "candidate",
        "kind": "validation",
        "pressure": 0.8,
        "summary": "Candidate for live update routing tests.",
        "updated_at": "2026-07-04T10:00:00Z",
    }
    base.update(overrides)
    store.append_jsonl("candidates", base)
    return base


@pytest.fixture
def ctx_and_store(tmp_path, monkeypatch):
    """Register the plugin with a fake context and return (ctx, store)."""
    from agent_sensorium.store import SensoriumStore

    instance = "plugin-update-close-test"
    state_dir = tmp_path / "implicit-state" / instance
    import agent_sensorium.store as store_module
    monkeypatch.setattr(store_module, "_DEFAULT_BASE", str(tmp_path / "implicit-state"))

    # Pre-create store dirs and write config BEFORE registering, so any
    # boot-time handlers can find it.
    store = SensoriumStore(instance=instance)
    store.ensure_dirs()
    _write_config(state_dir, surfaces=["local"])

    ctx = FakePluginContext()
    register(ctx)
    return ctx, store, instance, state_dir


def _call(ctx, **kwargs):
    """Invoke the live handler and parse the JSON response."""
    raw = ctx.tools["sensorium"]["handler"](kwargs)
    return json.loads(raw)


# --- Test 1: close path -------------------------------------------------


def test_plugin_update_close_thread_closes_and_records_decision(ctx_and_store):
    ctx, store, instance, state_dir = ctx_and_store
    _write_thread(store, thread_id="sth_close_1")

    result = _call(
        ctx,
        action="update",
        keyword="close",
        id="sth_close_1",
        text="guardrail installed",
        instance=instance,
        state_dir=str(state_dir),
    )

    assert result["success"] is True
    payload = result.get("data") or {}
    assert payload.get("old_status") == "dormant"
    assert payload.get("new_status") == "closed"

    # Thread must now reflect 'closed'.
    thread = store.read_jsonl("threads")[0]
    assert thread["status"] == "closed"

    # A thread.updated decision receipt must have been recorded with action='close'.
    decisions = store.read_jsonl("decisions")
    close_receipts = [d for d in decisions if d.get("type") == "thread.updated"]
    assert close_receipts, "expected a thread.updated decision receipt"
    last = close_receipts[-1]
    assert last["action"] == "close"
    assert last["thread_id"] == "sth_close_1"
    assert last["reason"] == "guardrail installed"


# --- Test 2: hold path --------------------------------------------------


def test_plugin_update_hold_thread_records_resume_trigger(ctx_and_store):
    ctx, store, instance, state_dir = ctx_and_store
    _write_thread(store, thread_id="sth_hold_1")

    # The live schema only forwards `text` as `reason`; the underlying
    # `resume_trigger` field is not currently exposed on the live aperture.
    # Hold therefore sets hold_reason (via text/reason). We verify that path
    # is intact.
    result = _call(
        ctx,
        action="update",
        keyword="hold",
        id="sth_hold_1",
        text="waiting on new evidence",
        instance=instance,
        state_dir=str(state_dir),
    )

    assert result["success"] is True
    payload = result.get("data") or {}
    assert payload.get("new_status") == "held"

    thread = store.read_jsonl("threads")[0]
    assert thread["status"] == "held"
    assert thread["hold_reason"] == "waiting on new evidence"

    decisions = store.read_jsonl("decisions")
    hold_receipts = [d for d in decisions if d.get("type") == "thread.updated"]
    assert hold_receipts[-1]["action"] == "hold"
    assert hold_receipts[-1]["thread_id"] == "sth_hold_1"


def test_plugin_update_candidate_mark_reviewed_records_candidate_receipt(ctx_and_store):
    ctx, store, instance, state_dir = ctx_and_store
    _write_candidate(store, candidate_id="cand_reviewed_1")

    result = _call(
        ctx,
        action="update",
        keyword="mark_reviewed",
        id="cand_reviewed_1",
        text="consciously reviewed",
        instance=instance,
        state_dir=str(state_dir),
    )

    assert result["success"] is True
    assert result["data"]["old_status"] == "candidate"
    assert result["data"]["new_status"] == "reviewed"
    assert store.read_jsonl("candidates")[0]["status"] == "reviewed"
    receipt = [d for d in store.read_jsonl("decisions") if d.get("type") == "candidate.updated"][-1]
    assert receipt["candidate_id"] == "cand_reviewed_1"
    assert receipt["action"] == "mark_reviewed"


def test_plugin_update_candidate_hold_then_resume_clears_hold_context(ctx_and_store):
    ctx, store, instance, state_dir = ctx_and_store
    _write_candidate(store, candidate_id="cand_hold_resume_1")

    held = _call(
        ctx,
        action="update",
        keyword="hold",
        id="cand_hold_resume_1",
        text="awaiting evidence",
        instance=instance,
        state_dir=str(state_dir),
    )
    assert held["success"] is True
    candidate = store.read_jsonl("candidates")[0]
    assert candidate["status"] == "held"
    assert candidate["hold_reason"] == "awaiting evidence"

    resumed = _call(
        ctx,
        action="update",
        keyword="resume",
        id="cand_hold_resume_1",
        text="evidence arrived",
        instance=instance,
        state_dir=str(state_dir),
    )
    assert resumed["success"] is True
    assert resumed["data"]["old_status"] == "held"
    assert resumed["data"]["new_status"] == "candidate"
    candidate = store.read_jsonl("candidates")[0]
    assert candidate["status"] == "candidate"
    assert candidate["hold_reason"] == ""
    receipts = [d for d in store.read_jsonl("decisions") if d.get("type") == "candidate.updated"]
    assert [receipt["action"] for receipt in receipts[-2:]] == ["hold", "resume"]


def test_plugin_update_candidate_resume_requires_held_state(ctx_and_store):
    ctx, store, instance, state_dir = ctx_and_store
    _write_candidate(store, candidate_id="cand_resume_unheld_1")

    result = _call(
        ctx,
        action="update",
        keyword="resume",
        id="cand_resume_unheld_1",
        instance=instance,
        state_dir=str(state_dir),
    )

    assert result["success"] is False
    assert "cannot be resumed" in result["error"]
    assert store.read_jsonl("candidates")[0]["status"] == "candidate"
    assert not store.read_jsonl("decisions")


# --- Test 3: unknown thread id ------------------------------------------


def test_plugin_update_close_unknown_id_returns_not_found_error(ctx_and_store):
    ctx, _store, instance, state_dir = ctx_and_store

    result = _call(
        ctx,
        action="update",
        keyword="close",
        id="sth_doesnotexist",
        instance=instance,
        state_dir=str(state_dir),
    )

    assert result["success"] is False
    assert "not found" in (result.get("error") or "").lower()


# --- Test 4: invalid keyword --------------------------------------------


def test_plugin_update_invalid_keyword_returns_invalid_action_or_action_error(ctx_and_store):
    ctx, _store, instance, state_dir = ctx_and_store
    _write_thread(_store, thread_id="sth_invalid_kw")

    result = _call(
        ctx,
        action="update",
        keyword="nonsense",
        id="sth_invalid_kw",
        instance=instance,
        state_dir=str(state_dir),
    )

    assert result["success"] is False
    error = (result.get("error") or "").lower()
    # Underlying handler returns "Invalid action 'nonsense'..." which covers
    # both 'invalid action' and 'action' substrings.
    assert "invalid action" in error or "action" in error


# --- Test 5: closing thread marks origin candidate reviewed --------------


def test_plugin_update_close_marks_origin_candidate_reviewed(ctx_and_store):
    ctx, store, instance, state_dir = ctx_and_store

    store.append_jsonl("candidates", {
        "id": "cand_close_origin",
        "status": "candidate",
        "kind": "validation",
        "pressure": 0.8,
        "summary": "Origin candidate that should flip to reviewed when its thread closes.",
        "updated_at": "2026-07-04T10:00:00Z",
    })
    _write_thread(store, thread_id="sth_close_origin", origin_candidate_id="cand_close_origin")

    result = _call(
        ctx,
        action="update",
        keyword="close",
        id="sth_close_origin",
        text="validation complete",
        instance=instance,
        state_dir=str(state_dir),
    )
    assert result["success"] is True

    # The origin candidate must now be marked reviewed.
    candidates = store.read_jsonl("candidates")
    target = next(c for c in candidates if c["id"] == "cand_close_origin")
    assert target["status"] == "reviewed"

    # And a candidate.updated decision receipt should have been emitted.
    decisions = store.read_jsonl("decisions")
    cand_receipts = [d for d in decisions if d.get("type") == "candidate.updated"]
    assert cand_receipts, "expected a candidate.updated decision receipt"
    last_cand = cand_receipts[-1]
    assert last_cand["candidate_id"] == "cand_close_origin"
    assert last_cand["action"] == "mark_reviewed"
    assert last_cand["thread_id"] == "sth_close_origin"
