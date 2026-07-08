"""Focused regression tests for the false-doorway / saved-salience bug.

Acceptance criteria (from the live repair task):

  (a) The surface-facing pointer/doorway must advertise an actually openable
      thread/artifact OR use honest wording for candidate-only salience.
  (b) ``open`` latest/candidate semantics can recover relevant candidate
      details when the allowed surface permits, OR the doorway avoids saying
      "open thread".
  (c) Relevant research_source_signal candidates with a kanban SAVE must not
      be silently suppressed away from conscious access; an honest
      saved-candidate state should be visible.
  (d) These tests cover the exact mismatch shape: a real-world sera profile
      state where a research_source_signal candidate existed with a
      kanban SAVE settlement and no thread was minted, and the live pointer
      was offering a non-existent thread.

The bug shape was:
  - signals/evt_599831499464 + evt_e4cdf8214518 + evt_e4cdf8214518 coalesced
    into cand_b39e18bbb527 with kanban_settlement.decision="SAVE", intake
    t_a0098881, review t_39275e24.
  - status correctly said no openable thread existed (all 25 archived).
  - pre_LLM pointer said "I have something for you" (template) → human-facing
    line "I have a doorway for the arXiv research" → assistant invented
    "open thread X" → opening latest returned "Thread 'latest' not found".
  - The candidate was archived away from conscious access.

These tests reproduce that state and assert the new honest behavior.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_sensorium.pointers import (
    pointer_context_for_llm,
    select_attention_pointer,
)
from agent_sensorium.tools import (
    handle_sensorium_candidate_open,
    handle_sensorium_status,
    handle_sensorium_thread_open,
)
from agent_sensorium.store import SensoriumStore


# ---------- helpers ----------


def _arxiv_candidate(**overrides):
    base = {
        "id": "cand_arxiv_regression",
        "status": "archived",
        "kind": "research_source_signal",
        "pressure": 0.783,
        "summary": (
            "Research source feed (arXiv cs.AI: agent collaboration / governance): "
            "From Signals to Structure: How Memory Architecture Drives Language "
            "Emergence in LLM Agents. Authors: Talebirad et al. Abstract: arXiv:2607.00233."
        ),
        "correlation_keys": [
            "lane:agent-society",
            "source:arxiv-cs-ai-agent-collaboration-governance",
            "topic:memory",
            "topic:multi-agent",
        ],
        "sensitivity": "private",
        "allowed_surfaces": ["local", "discord"],
        "created_at": "2026-07-02T04:18:00Z",
        "updated_at": "2026-07-02T04:39:17Z",
        "kanban_settlement": {
            "decision": "SAVE",
            "intake_task_id": "t_a0098881",
            "review_task_id": "t_39275e24",
            "settled_at": "2026-07-02T04:29:29Z",
            "reason_label": "reason#bdf731842efcbb5b",
        },
    }
    base.update(overrides)
    return base


def _correction_candidate(**overrides):
    base = {
        "id": "cand_user_correction",
        "status": "candidate",
        "kind": "explicit_correction",
        "pressure": 0.716,
        "summary": (
            "Sebastian corrected Sensorium behavior: do not offer/open a thread "
            "when no openable thread exists; highly relevant arXiv agent "
            "collaboration/governance salience should remain consciously accessible."
        ),
        "correlation_keys": [
            "active-session", "surface:discord", "explicit_correction",
        ],
        "sensitivity": "private",
        "allowed_surfaces": ["local", "discord"],
        "created_at": "2026-07-02T05:00:00Z",
        "updated_at": "2026-07-02T05:00:00Z",
    }
    base.update(overrides)
    return base


def _write_config(state_dir, *, surfaces=("discord", "local")):
    config = {
        "allowed_surfaces": list(surfaces),
        "max_sensitivity": "private",
        "instance_name": "test",
    }
    path = Path(state_dir) / "instance.config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config))


def _seed_state(tmp_path):
    """Reproduce the exact mismatch shape from the live sera profile."""
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    _write_config(tmp_path)
    # All threads are archived (matching the live state of 25 archived).
    store.append_jsonl("candidates", _arxiv_candidate())
    store.append_jsonl("candidates", _correction_candidate())
    return store


def _seeded_with_arxiv(tmp_path):
    """Like _seed_state but only the saved-residue is present (no active)."""
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    _write_config(tmp_path)
    store.append_jsonl("candidates", _arxiv_candidate())
    return store


def _seeded_with_active_and_saved(tmp_path):
    """Like _seed_state."""
    return _seed_state(tmp_path)


# ---------- (a) honest pointer wording ----------


def test_pointer_with_no_threads_and_only_saved_residue_says_so_honestly(tmp_path):
    """When no dormant/held thread exists AND no active candidate exists,
    the surface-facing pointer must fall back to the saved-residue pathway
    and honestly say it is a saved residue, not a thread.
    """
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    _write_config(tmp_path)
    store.append_jsonl("candidates", _arxiv_candidate())

    pointer = select_attention_pointer(store, surface="discord")
    assert pointer["action"] == "pointer_available"
    assert pointer["pointer_type"] == "saved_residue"
    assert pointer["candidate_id"] == "cand_arxiv_regression"
    # Honest copy: claim "not an openable thread", do not claim a thread.
    assert "not an openable thread" in pointer["invitation"].lower()
    assert pointer["invitation"].lower().startswith("i previously saved")
    # Linked intake visible.
    assert pointer["settlement_decision"] == "SAVE"
    assert pointer["intake_task_id"] == "t_a0098881"
    assert pointer["review_task_id"] == "t_39275e24"
    # Pointer context must explicitly call out that this is NOT a thread, while
    # using the exact candidate id for recovery. Calling status here is unsafe:
    # the pre-LLM hook has already recorded a pointer receipt, so cooldown
    # selection may advance to a different saved residue.
    context = pointer_context_for_llm(pointer)
    assert "Pointer type: saved_residue" in context
    assert "NOT an openable thread" in context
    assert 'sensorium(action="open", surface="discord", id="cand_arxiv_regression")' in context
    assert "do not call status as the primary lookup" in context


def test_pointer_falls_back_to_active_candidate_before_saved_residue(tmp_path):
    """When both an active candidate and saved residues exist, prefer the
    active candidate pointer so the user gets actionable salience first.
    """
    store = _seed_state(tmp_path)
    pointer = select_attention_pointer(store, surface="discord")
    assert pointer["pointer_type"] == "candidate"
    assert pointer["candidate_id"] == "cand_user_correction"


def test_pointer_prefers_thread_when_present(tmp_path):
    """A real thread pointer wins over candidates and saved-residue."""
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    _write_config(tmp_path)
    store.append_jsonl("threads", {
        "id": "sth_x",
        "status": "dormant",
        "origin": "candidate",
        "conscious_task": {
            "id": "ctask_x",
            "request_type": "THINK",
            "title": "Review arXiv agent collaboration governance item",
            "why": "test",
            "expected_decision": "decide",
        },
        "created_at": "2026-07-02T05:00:00Z",
        "updated_at": "2026-07-02T05:00:00Z",
        "sensitivity": "private",
        "allowed_surfaces": ["discord"],
    })
    store.append_jsonl("candidates", _arxiv_candidate())

    pointer = select_attention_pointer(store, surface="discord")
    assert pointer["pointer_type"] == "thread"
    assert pointer["thread_id"] == "sth_x"
    # Honest copy: thread pointer says "conscious thread waiting", never
    # claims something is "not openable" the way a candidate pointer does.
    context = pointer_context_for_llm(pointer)
    assert "Pointer type: thread" in context
    assert "NOT an openable thread" not in context


# ---------- (b) open semantics recover candidate details ----------


def test_open_thread_latest_returns_honest_not_found_when_no_thread(tmp_path):
    _seeded_with_active_and_saved(tmp_path)
    raw = handle_sensorium_thread_open(
        instance="test", state_dir=str(tmp_path), thread_id="latest", surface="discord",
    )
    payload = json.loads(raw)
    assert payload["success"] is False
    # The honest error is preserved — the doorway must not invent a thread.
    assert "Thread 'latest' not found." in (payload.get("error") or "")
    assert payload.get("data") is None


def test_open_candidate_by_id_recovers_archived_saved_residue(tmp_path):
    """The exact live mismatch: opening a candidate by id must return the
    saved-residue candidate capsule, not a fake thread.
    """
    store = _seed_state(tmp_path)
    raw = handle_sensorium_candidate_open(
        instance="test", state_dir=str(tmp_path),
        candidate_id="cand_arxiv_regression", surface="discord",
    )
    payload = json.loads(raw)
    assert payload["success"] is True
    data = payload["data"]
    # Critical honesty markers — these are what the agent must see.
    assert data["object_kind"] == "candidate"
    assert data["is_openable_thread"] is False
    assert data["candidate_id"] == "cand_arxiv_regression"
    assert data["kind"] == "research_source_signal"
    # Durability: the kanban settlement block is the trace.
    assert data["kanban_settlement"]["decision"] == "SAVE"
    assert data["kanban_settlement"]["intake_task_id"] == "t_a0098881"
    assert data["kanban_settlement"]["review_task_id"] == "t_39275e24"
    # Honest title carries the agent-society/arXiv topic markers, so the
    # conscious layer can act on it without further decoding.
    assert "arXiv" in data["title"]
    assert "agent collaboration" in data["title"].lower() or "governance" in data["title"].lower()


def test_open_candidate_latest_prefers_active_then_saved_residue(tmp_path):
    store = _seed_state(tmp_path)
    raw = handle_sensorium_candidate_open(
        instance="test", state_dir=str(tmp_path),
        candidate_id="latest", surface="discord",
    )
    payload = json.loads(raw)
    assert payload["success"] is True
    assert payload["data"]["candidate_id"] == "cand_user_correction"


def test_open_candidate_latest_falls_back_to_saved_when_no_active(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    _write_config(tmp_path)
    store.append_jsonl("candidates", _arxiv_candidate())  # only saved residue

    raw = handle_sensorium_candidate_open(
        instance="test", state_dir=str(tmp_path),
        candidate_id="latest", surface="discord",
    )
    payload = json.loads(raw)
    assert payload["success"] is True
    assert payload["data"]["candidate_id"] == "cand_arxiv_regression"
    assert payload["data"]["is_openable_thread"] is False


def test_open_candidate_for_disallowed_surface_refuses(tmp_path):
    store = _seed_state(tmp_path)
    raw = handle_sensorium_candidate_open(
        instance="test", state_dir=str(tmp_path),
        candidate_id="cand_arxiv_regression",
        surface="twitter",  # not in any config
    )
    payload = json.loads(raw)
    assert payload["success"] is False
    assert "not allowed on surface" in (payload.get("error") or "")


# ---------- (c) saved_residue_fallback_enabled gating ----------


def test_saved_residue_fallback_can_be_disabled(tmp_path):
    """Operators can opt out of saved-residue promotion via config."""
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    _write_config(tmp_path)
    store.append_jsonl("candidates", _arxiv_candidate())

    pointer = select_attention_pointer(
        store, surface="discord",
        config={"saved_residue_fallback_enabled": False},
    )
    assert pointer["action"] == "no_pointer"
    assert pointer["reason"] == "no_visible_thread_for_surface"


def test_saved_residue_pointer_includes_kanban_settlement_block():
    """Surface-facing pointer must carry the Kanban intake/review ids so
    conscious access is preserved without a separate tool call.
    """
    pointer = {
        "pointer_type": "saved_residue",
        "candidate_id": "cand_arxiv_regression",
        "title": "Research source feed (arXiv cs.AI: agent collaboration / governance)",
        "surface": "discord",
        "settlement_decision": "SAVE",
        "intake_task_id": "t_a0098881",
        "review_task_id": "t_39275e24",
        "kanban_settlement": {
            "decision": "SAVE",
            "intake_task_id": "t_a0098881",
            "review_task_id": "t_39275e24",
            "settled_at": "2026-07-02T04:29:29Z",
            "reason_label": "reason#bdf731842efcbb5b",
        },
        "invitation": (
            "I previously saved a salience residue (Kanban SAVE): arXiv governance. "
            "This is not an openable thread — say 'check saved residue' to recap."
        ),
    }
    context = pointer_context_for_llm(pointer)
    assert "Kanban SAVE" in context
    assert "t_a0098881" in context


# ---------- end-to-end live dispatch via the plugin handler ----------


def test_plugin_handler_routes_open_to_candidate_when_id_prefixes_cand(tmp_path):
    """The live aperture must mirror the offline handler: ``sensorium`` with
    ``action="open"`` and an explicit candidate id should return the
    candidate capsule, not a fake thread error.
    """
    from agent_sensorium.plugin import register

    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    _write_config(tmp_path)
    store.append_jsonl("candidates", _arxiv_candidate())

    captured: dict = {}

    class FakeCtx:
        def register_tool(self, name, **kw):
            captured["tool"] = (name, kw)

        def register_command(self, *a, **kw):
            pass

        def register_hook(self, *a, **kw):
            pass

        def register_skill(self, *a, **kw):
            pass

    register(FakeCtx())
    handler = captured["tool"][1]["handler"]

    raw = handler({
        "action": "open",
        "id": "cand_arxiv_regression",
        "surface": "discord",
        "instance": "test",
        "state_dir": str(tmp_path),
    })
    payload = json.loads(raw)
    assert payload["success"] is True
    assert payload["data"]["object_kind"] == "candidate"
    assert payload["data"]["candidate_id"] == "cand_arxiv_regression"
    assert payload["data"]["is_openable_thread"] is False
    # The arXiv settlement block is visible to the conscious layer.
    assert payload["data"]["kanban_settlement"]["intake_task_id"] == "t_a0098881"


def test_plugin_handler_status_does_not_invent_thread_when_only_saved_residue(tmp_path):
    """The handle_sensorium status path must stay consistent with the new
    pointer shape: when no dormant/held thread exists, top_threads is empty.
    """
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    _write_config(tmp_path)
    store.append_jsonl("candidates", _arxiv_candidate())

    raw = handle_sensorium_status(instance="test", state_dir=str(tmp_path))
    payload = json.loads(raw)["data"]
    assert payload["counts"]["dormant_threads"] == 0
    assert payload["counts"]["held_threads"] == 0
    assert payload["top_threads"] == []
    # active_candidates counts only candidates with status=='candidate'
    assert payload["counts"]["active_candidates"] == 0
    # But archived candidates with kanban SAVE are still retrievable via
    # handle_sensorium_candidate_open (covered by other tests).
