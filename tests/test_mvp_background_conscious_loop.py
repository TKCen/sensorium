"""MVP background-conscious loop end-to-end test.

Exercises the smallest sensori-motor pulse:

    signal -> event/candidate -> dispatch thread -> background claim ->
    worker request -> result with output refs -> origin thread refs +
    summary_dirty -> feedback signal -> self-loop suppression ->
    complete lease.

The test stays inside the local JSONL store; no external side effects,
no live Hermes install, no scheduler, no model call.
"""

from __future__ import annotations

import json

import pytest

from agent_sensorium.conscious import claim_dormant_thread, complete_claim
from agent_sensorium.dispatcher import dispatch_once
from agent_sensorium.gate import is_settled_feedback_signal
from agent_sensorium.store import SensoriumStore
from agent_sensorium.tools import (
    handle_sensorium_ingest_signal,
    handle_sensorium_worker_result,
)
from agent_sensorium.workers import prepare_worker_request


@pytest.fixture
def state_dir(tmp_path):
    return str(tmp_path / "sensorium")


@pytest.fixture
def store(state_dir):
    s = SensoriumStore(instance="test", state_dir=state_dir)
    s.ensure_dirs()
    return s


def test_mvp_background_conscious_loop_end_to_end(state_dir, store):
    # 1. Ingest a compact signal strong enough to promote.
    signal = {
        "sensor": "synthetic.test_signal",
        "source": "manual",
        "kind": "design_decision",
        "summary": "MVP loop: prove the unscheduled sensori-motor pulse end-to-end.",
        "strength_hint": 0.9,
        "correlation_keys": ["mvp-loop", "background-conscious"],
        "sensitivity": "private",
        "allowed_surfaces": ["local"],
    }
    ingest_raw = handle_sensorium_ingest_signal(
        signal=signal, instance="test", state_dir=state_dir,
    )
    ingest_result = json.loads(ingest_raw)
    assert ingest_result["success"] is True
    assert ingest_result["data"]["promoted"] is True
    candidate_id = ingest_result["data"]["candidate_id"]
    assert candidate_id

    # 2. Dispatch promotes the candidate to a dormant conscious thread.
    dispatch_result = dispatch_once(store, dry_run=False, config={"legacy_thread_dispatch_enabled": True})
    assert dispatch_result["action"] == "promoted"
    thread_id = dispatch_result["thread_id"]

    threads = store.read_jsonl("threads")
    thread = next(t for t in threads if t["id"] == thread_id)
    assert thread["status"] == "dormant"

    # 3. Dispatcher should mark the thread as background-eligible.
    pickup = thread.get("pickup") or {}
    assert pickup.get("background") is True
    assert "local" in (pickup.get("surfaces") or [])
    assert pickup.get("requires_user_open") is False

    # 4. Claim the thread for background conscious processing.
    claim = claim_dormant_thread(store, config={"enabled": True})
    assert claim["success"] is True
    lease_id = claim["data"]["lease_id"]
    assert lease_id.startswith("lease_")
    assert claim["data"]["thread"]["thread_id"] == thread_id

    threads = store.read_jsonl("threads")
    claimed = next(t for t in threads if t["id"] == thread_id)
    assert claimed["active_lease"]["lease_id"] == lease_id
    assert claimed["active_lease"]["actor"] == "sera_background_conscious"

    # A second claim should not pick the same thread again while leased.
    second_claim = claim_dormant_thread(store, config={"enabled": True})
    assert second_claim["success"] is False
    assert second_claim["error"] == "no_eligible_thread"

    # 5. Prepare a worker request (delegated action) from the conscious thread.
    prep = prepare_worker_request(
        store,
        thread_id=thread_id,
        worker_type="manual",
        title="MVP delegated work",
        task_summary="Background conscious delegates a tiny artifact-producing task.",
    )
    assert prep["success"] is True
    worker_request_id = prep["data"]["id"]

    # 6. Record the worker result with compact output refs.
    output_refs = [
        {"type": "file", "path": "/tmp/mvp-loop-artifact.txt"},
    ]
    result_raw = handle_sensorium_worker_result(
        worker_request_id=worker_request_id,
        outcome="completed",
        result_summary="Delegated work returned an artifact ref.",
        output_refs=output_refs,
        instance="test",
        state_dir=state_dir,
    )
    result = json.loads(result_raw)
    assert result["success"] is True
    feedback_signal_id = result["data"]["feedback_signal_id"]
    assert feedback_signal_id

    # 7. Origin thread must now carry compact refs and dirty state.
    threads = store.read_jsonl("threads")
    enriched = next(t for t in threads if t["id"] == thread_id)
    assert enriched["summary_dirty"] is True
    assert enriched.get("dirty_since")
    interaction = next(
        r for r in (enriched.get("interaction_refs") or [])
        if r.get("type") == "worker_result"
    )
    assert interaction["worker_request_id"] == worker_request_id
    assert interaction["outcome"] == "completed"
    assert interaction["output_refs"] == output_refs

    # 8. The feedback signal must be present in the inbox.
    signals = store.read_jsonl("signals")
    feedback_signals = [s for s in signals if s.get("id") == feedback_signal_id]
    assert len(feedback_signals) == 1
    feedback_signal = feedback_signals[0]
    assert feedback_signal["source"] == "feedback"
    assert feedback_signal["feedback_scope"] == "system_action"
    assert feedback_signal["caused_by"]["worker_request_id"] == worker_request_id

    # 9. Self-loop suppression: ordinary internal completion feedback is settled
    #    and must NOT spawn a new active candidate.
    assert is_settled_feedback_signal(feedback_signal) is True

    candidates_before = store.read_jsonl("candidates")
    active_before = [c for c in candidates_before if c.get("status") == "candidate"]
    # Re-ingesting the same feedback signal must remain idempotent / suppressed.
    handle_sensorium_ingest_signal(
        signal=feedback_signal, instance="test", state_dir=state_dir,
    )
    candidates_after = store.read_jsonl("candidates")
    active_after = [c for c in candidates_after if c.get("status") == "candidate"]
    assert len(active_after) == len(active_before)

    # The MVP origin candidate should still be the only active candidate, and
    # no candidate derived from the worker.feedback signal exists.
    for cand in active_after:
        meta = cand.get("feedback_meta") or {}
        assert meta.get("caused_by", {}).get("worker_request_id") != worker_request_id

    # 10. Complete the lease.
    complete = complete_claim(
        store,
        thread_id=thread_id,
        lease_id=lease_id,
        outcome="processed",
        notes="MVP loop test completed.",
    )
    assert complete["success"] is True

    threads = store.read_jsonl("threads")
    finished = next(t for t in threads if t["id"] == thread_id)
    assert finished["active_lease"] in (None, {})

    decisions = store.read_jsonl("decisions")
    decision_types = [d.get("type") for d in decisions]
    assert "conscious.claimed" in decision_types
    assert "conscious.claim_completed" in decision_types
    assert "dispatch.promoted_to_thread" in decision_types
    assert "worker.result" in decision_types

    # Thread still alive but unleased: a new claim should succeed again.
    reclaim = claim_dormant_thread(store, config={"enabled": True})
    assert reclaim["success"] is True
    assert reclaim["data"]["thread"]["thread_id"] == thread_id


def test_claim_rejects_thread_without_background_pickup(store):
    """Pickup policy gate: threads without background=true are not auto-claimed."""
    store.append_jsonl("threads", {
        "id": "sth_no_bg",
        "status": "dormant",
        "origin": "candidate",
        "conscious_task": {"id": "ct_1", "request_type": "THINK", "title": "x"},
        "origin_candidate_id": "cand_1",
        "continuity_summary": [],
        "decision_log": [],
        "interaction_refs": [],
        "summary_dirty": False,
        "open_questions": [],
        "next_prompt_to_operator": "x",
        "sensitivity": "private",
        "allowed_surfaces": ["local"],
        "pickup": {"background": False, "surfaces": ["local"], "requires_user_open": True},
        "active_lease": None,
        "created_at": "2026-05-28T10:00:00Z",
        "updated_at": "2026-05-28T10:00:00Z",
        "expires_at": "2026-06-04T10:00:00Z",
    })
    result = claim_dormant_thread(store, config={"enabled": True})
    assert result["success"] is False
    assert result["error"] == "no_eligible_thread"


def test_claim_rejects_unsafe_request_type(store):
    """REACH_OUT-style request types must not be auto-claimed by default."""
    store.append_jsonl("threads", {
        "id": "sth_reach",
        "status": "dormant",
        "origin": "candidate",
        "conscious_task": {
            "id": "ct_1", "request_type": "REACH_OUT", "title": "send msg",
        },
        "origin_candidate_id": "cand_1",
        "continuity_summary": [],
        "decision_log": [],
        "interaction_refs": [],
        "summary_dirty": False,
        "open_questions": [],
        "next_prompt_to_operator": "x",
        "sensitivity": "private",
        "allowed_surfaces": ["local"],
        "pickup": {"background": True, "surfaces": ["local"], "requires_user_open": False},
        "active_lease": None,
        "created_at": "2026-05-28T10:00:00Z",
        "updated_at": "2026-05-28T10:00:00Z",
        "expires_at": "2026-06-04T10:00:00Z",
    })
    result = claim_dormant_thread(store, config={"enabled": True})
    assert result["success"] is False
    assert result["error"] == "no_eligible_thread"


def test_complete_claim_rejects_lease_mismatch(store):
    """complete_claim must verify the lease_id."""
    store.append_jsonl("threads", {
        "id": "sth_lease",
        "status": "dormant",
        "origin": "candidate",
        "conscious_task": {"id": "ct_1", "request_type": "THINK", "title": "x"},
        "origin_candidate_id": "cand_1",
        "continuity_summary": [],
        "decision_log": [],
        "interaction_refs": [],
        "summary_dirty": False,
        "open_questions": [],
        "next_prompt_to_operator": "x",
        "sensitivity": "private",
        "allowed_surfaces": ["local"],
        "pickup": {"background": True, "surfaces": ["local"], "requires_user_open": False},
        "active_lease": None,
        "created_at": "2026-05-28T10:00:00Z",
        "updated_at": "2026-05-28T10:00:00Z",
        "expires_at": "2026-06-04T10:00:00Z",
    })
    claim = claim_dormant_thread(store, config={"enabled": True})
    assert claim["success"] is True

    bad = complete_claim(
        store, thread_id="sth_lease", lease_id="lease_wrong",
    )
    assert bad["success"] is False
    assert bad["error"] == "lease_mismatch"
