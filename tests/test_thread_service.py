"""Tests for Phase 4 thread service pass."""
import json
from datetime import datetime, timezone, timedelta


from agent_sensorium.tools import (
    handle_sensorium_service_threads,
    handle_sensorium_status,
    handle_sensorium_thread_update,
)
from agent_sensorium.store import SensoriumStore


def _thread(tmp_path=None, store=None, **overrides):
    base = {
        "id": "sth_service_test",
        "status": "dormant",
        "origin": "candidate",
        "conscious_task": {
            "id": "ctask_t",
            "request_type": "THINK",
            "title": "Test",
            "why": "test",
            "expected_decision": "test",
        },
        "origin_candidate_id": "cand_service_test",
        "continuity_summary": ["test"],
        "decision_log": [],
        "interaction_refs": [],
        "summary_dirty": False,
        "open_questions": [],
        "next_prompt_to_operator": "test",
        "sensitivity": "private",
        "allowed_surfaces": ["local"],
        "created_at": "2026-05-20T10:00:00Z",
        "updated_at": "2026-05-20T10:00:00Z",
        "expires_at": "2026-05-27T10:00:00Z",
        "dirty_since": None,
        "hold_reason": "",
        "resume_trigger": "",
        "last_interaction_at": "2026-05-20T10:00:00Z",
        "source_refs": [],
    }
    base.update(overrides)
    return base


def _candidate(**overrides):
    base = {
        "id": "cand_service_test",
        "status": "candidate",
        "kind": "design_decision",
        "pressure": 0.8,
        "summary": "Service test candidate",
        "correlation_keys": ["test-key"],
        "fingerprint": "fp_service_test",
        "updated_at": "2026-05-20T10:00:00Z",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 1. Thread creation initializes Phase 4 fields
# ---------------------------------------------------------------------------

def test_thread_creation_initializes_phase4_fields(tmp_path):
    """Dispatching a candidate creates a thread with all Phase 4 fields set."""
    from agent_sensorium.tools import handle_sensorium_dispatch_once

    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    store.append_jsonl("candidates", _candidate())

    result = json.loads(
        handle_sensorium_dispatch_once(
            instance="test", state_dir=str(tmp_path), dry_run=False, config={"legacy_thread_dispatch_enabled": True}
        )
    )
    assert result["success"] is True
    assert result["data"]["action"] == "promoted"

    threads = store.read_jsonl("threads")
    assert len(threads) == 1
    t = threads[0]

    assert t["dirty_since"] is None
    assert t["hold_reason"] == ""
    assert t["resume_trigger"] == ""
    assert "last_interaction_at" in t
    assert t["last_interaction_at"] == t["created_at"]
    assert t["source_refs"] == []


# ---------------------------------------------------------------------------
# 2. Thread update stamps last_interaction_at
# ---------------------------------------------------------------------------

def test_thread_update_sets_last_interaction_at(tmp_path):
    """Any thread update stamps last_interaction_at on the thread."""
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    store.append_jsonl("threads", _thread(last_interaction_at="2026-05-20T10:00:00Z"))

    result = json.loads(
        handle_sensorium_thread_update(
            instance="test",
            state_dir=str(tmp_path),
            thread_id="sth_service_test",
            action="mark_reviewed",
            reason="reviewed",
        )
    )
    assert result["success"] is True

    threads = store.read_jsonl("threads")
    t = threads[0]
    # last_interaction_at must be updated (after or equal to the creation time)
    assert t["last_interaction_at"] > "2026-05-20T10:00:00Z"


# ---------------------------------------------------------------------------
# 3. Hold stores hold_reason and resume_trigger
# ---------------------------------------------------------------------------

def test_hold_stores_hold_reason_and_resume_trigger(tmp_path):
    """Holding a thread with reason/trigger persists both fields."""
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    store.append_jsonl("threads", _thread())

    result = json.loads(
        handle_sensorium_thread_update(
            instance="test",
            state_dir=str(tmp_path),
            thread_id="sth_service_test",
            action="hold",
            reason="waiting for external event",
            resume_trigger="event_received",
        )
    )
    assert result["success"] is True

    threads = store.read_jsonl("threads")
    t = threads[0]
    assert t["status"] == "held"
    assert t["hold_reason"] == "waiting for external event"
    assert t["resume_trigger"] == "event_received"


# ---------------------------------------------------------------------------
# 4. Resume clears hold_reason and resume_trigger
# ---------------------------------------------------------------------------

def test_resume_clears_hold_reason_and_resume_trigger(tmp_path):
    """Resuming a held thread clears hold_reason and resume_trigger."""
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    store.append_jsonl(
        "threads",
        _thread(
            status="held",
            hold_reason="waiting on deploy",
            resume_trigger="deploy_done",
        ),
    )

    result = json.loads(
        handle_sensorium_thread_update(
            instance="test",
            state_dir=str(tmp_path),
            thread_id="sth_service_test",
            action="resume",
        )
    )
    assert result["success"] is True

    threads = store.read_jsonl("threads")
    t = threads[0]
    assert t["status"] == "dormant"
    assert t["hold_reason"] == ""
    assert t["resume_trigger"] == ""


# ---------------------------------------------------------------------------
# 5. Service pass archives expired threads
# ---------------------------------------------------------------------------

def test_service_pass_archives_expired_threads(tmp_path):
    """Expired threads are archived and a decision receipt is written."""
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    store.append_jsonl(
        "threads",
        _thread(
            id="sth_expired",
            expires_at="2026-05-24T10:00:00Z",
            last_interaction_at="2026-05-20T10:00:00Z",
        ),
    )

    # now is after expiry
    now = "2026-05-25T12:00:00Z"
    result = json.loads(
        handle_sensorium_service_threads(
            instance="test", state_dir=str(tmp_path), now=now
        )
    )
    assert result["success"] is True
    data = result["data"]
    assert "sth_expired" in data["archived"]
    assert data["serviced"] >= 1

    threads = store.read_jsonl("threads")
    assert threads[0]["status"] == "archived"

    decisions = store.read_jsonl("decisions")
    archive_receipts = [d for d in decisions if "archived" in d.get("type", "")]
    assert len(archive_receipts) >= 1


# ---------------------------------------------------------------------------
# 6. Service pass identifies starved threads (not archived)
# ---------------------------------------------------------------------------

def test_service_pass_identifies_starved_threads(tmp_path):
    """Threads with last_interaction_at older than 72h are reported as starved but not archived."""
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    store.append_jsonl(
        "threads",
        _thread(
            id="sth_starved",
            expires_at="2026-06-20T10:00:00Z",  # not expired
            last_interaction_at="2026-05-20T10:00:00Z",  # 5 days ago
        ),
    )

    # now is 5 days after last_interaction_at but before expiry
    now = "2026-05-25T12:00:00Z"
    result = json.loads(
        handle_sensorium_service_threads(
            instance="test", state_dir=str(tmp_path), now=now
        )
    )
    assert result["success"] is True
    data = result["data"]
    assert "sth_starved" in data["starved"]
    assert "sth_starved" not in data["archived"]

    threads = store.read_jsonl("threads")
    assert threads[0]["status"] == "dormant"  # still alive, just starved


# ---------------------------------------------------------------------------
# 7. Service pass identifies dirty threads
# ---------------------------------------------------------------------------

def test_service_pass_identifies_dirty_threads(tmp_path):
    """Active threads with dirty_since set are reported in the dirty list."""
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    store.append_jsonl(
        "threads",
        _thread(
            id="sth_dirty",
            dirty_since="2026-05-24T08:00:00Z",
            expires_at="2026-06-20T10:00:00Z",
            last_interaction_at="2026-05-25T10:00:00Z",
        ),
    )

    now = "2026-05-25T12:00:00Z"
    result = json.loads(
        handle_sensorium_service_threads(
            instance="test", state_dir=str(tmp_path), now=now
        )
    )
    assert result["success"] is True
    data = result["data"]
    assert "sth_dirty" in data["dirty"]
    assert "sth_dirty" not in data["archived"]


# ---------------------------------------------------------------------------
# 8. Service pass identifies expiring-soon threads
# ---------------------------------------------------------------------------

def test_service_pass_identifies_expiring_threads(tmp_path):
    """Threads expiring within 24h are reported in the expiring list."""
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    store.append_jsonl(
        "threads",
        _thread(
            id="sth_expiring",
            expires_at="2026-05-26T06:00:00Z",  # ~18h from now
            last_interaction_at="2026-05-25T10:00:00Z",
        ),
    )

    now = "2026-05-25T12:00:00Z"
    result = json.loads(
        handle_sensorium_service_threads(
            instance="test", state_dir=str(tmp_path), now=now
        )
    )
    assert result["success"] is True
    data = result["data"]
    assert "sth_expiring" in data["expiring"]
    assert "sth_expiring" not in data["archived"]


# ---------------------------------------------------------------------------
# 9. Service pass skips closed and archived threads
# ---------------------------------------------------------------------------

def test_service_pass_skips_closed_and_archived(tmp_path):
    """Closed and archived threads are never included in service pass output lists."""
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    store.append_jsonl(
        "threads",
        _thread(
            id="sth_closed",
            status="closed",
            expires_at="2026-05-24T00:00:00Z",
            last_interaction_at="2026-05-01T10:00:00Z",
        ),
    )
    store.append_jsonl(
        "threads",
        _thread(
            id="sth_already_archived",
            status="archived",
            expires_at="2026-05-24T00:00:00Z",
            last_interaction_at="2026-05-01T10:00:00Z",
        ),
    )

    now = "2026-05-25T12:00:00Z"
    result = json.loads(
        handle_sensorium_service_threads(
            instance="test", state_dir=str(tmp_path), now=now
        )
    )
    assert result["success"] is True
    data = result["data"]
    assert "sth_closed" not in data["archived"]
    assert "sth_already_archived" not in data["archived"]
    assert "sth_closed" not in data["starved"]
    assert "sth_already_archived" not in data["starved"]


# ---------------------------------------------------------------------------
# 10. Service pass respects pinned threads (not archived even if expired)
# ---------------------------------------------------------------------------

def test_service_pass_respects_pinned_threads(tmp_path):
    """Pinned threads are not archived even when past their TTL."""
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    store.append_jsonl(
        "threads",
        _thread(
            id="sth_pinned",
            pinned=True,
            expires_at="2026-05-24T00:00:00Z",  # expired
            last_interaction_at="2026-05-20T10:00:00Z",
        ),
    )

    now = "2026-05-25T12:00:00Z"
    result = json.loads(
        handle_sensorium_service_threads(
            instance="test", state_dir=str(tmp_path), now=now
        )
    )
    assert result["success"] is True
    data = result["data"]
    assert "sth_pinned" not in data["archived"]

    threads = store.read_jsonl("threads")
    assert threads[0]["status"] != "archived"


# ---------------------------------------------------------------------------
# 11. Status output includes service counts
# ---------------------------------------------------------------------------

def test_status_includes_service_counts(tmp_path):
    """handle_sensorium_status includes dirty_threads, starved_threads, expiring_threads counts."""
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()

    now = datetime.now(timezone.utc)
    fresh = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    starved = (now - timedelta(days=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
    future = (now + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    expiring = (now + timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # One dirty thread
    store.append_jsonl(
        "threads",
        _thread(
            id="sth_dirty_status",
            dirty_since=fresh,
            expires_at=future,
            last_interaction_at=fresh,
        ),
    )
    # One starved thread (last_interaction_at > 72h ago)
    store.append_jsonl(
        "threads",
        _thread(
            id="sth_starved_status",
            dirty_since=None,
            expires_at=future,
            last_interaction_at=starved,
        ),
    )
    # One expiring thread (within 24h of expiry)
    store.append_jsonl(
        "threads",
        _thread(
            id="sth_expiring_status",
            dirty_since=None,
            expires_at=expiring,
            last_interaction_at=fresh,
        ),
    )

    result = json.loads(handle_sensorium_status(instance="test", state_dir=str(tmp_path)))
    assert result["success"] is True
    counts = result["data"]["counts"]
    assert "dirty_threads" in counts
    assert "starved_threads" in counts
    assert "expiring_threads" in counts
    assert counts["dirty_threads"] >= 1
    assert counts["starved_threads"] >= 1
    assert counts["expiring_threads"] >= 1


# ---------------------------------------------------------------------------
# 12. Close writes settlement hint
# ---------------------------------------------------------------------------

def test_close_writes_settlement_hint(tmp_path):
    """Closing a thread writes a thread.settlement decision receipt with correlation keys."""
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()

    store.append_jsonl(
        "candidates",
        _candidate(
            id="cand_settle",
            correlation_keys=["proj-x", "deploy-y"],
            fingerprint="fp_settle_001",
        ),
    )
    store.append_jsonl(
        "threads",
        _thread(
            id="sth_settle",
            origin_candidate_id="cand_settle",
        ),
    )

    result = json.loads(
        handle_sensorium_thread_update(
            instance="test",
            state_dir=str(tmp_path),
            thread_id="sth_settle",
            action="close",
            reason="done",
        )
    )
    assert result["success"] is True

    decisions = store.read_jsonl("decisions")
    settlement_receipts = [d for d in decisions if d.get("type") == "thread.settlement"]
    assert len(settlement_receipts) == 1

    receipt = settlement_receipts[0]
    assert receipt["thread_id"] == "sth_settle"
    assert receipt["origin_candidate_id"] == "cand_settle"
    assert set(receipt["correlation_keys"]) == {"proj-x", "deploy-y"}
    assert receipt["fingerprint"] == "fp_settle_001"
    assert receipt["settlement_type"] == "closed"


# ---------------------------------------------------------------------------
# 13. Service pass on empty store returns zero counts
# ---------------------------------------------------------------------------

def test_service_pass_empty_store_returns_zero_counts(tmp_path):
    """Service pass on an empty state returns a clean summary with zero counts."""
    result = json.loads(
        handle_sensorium_service_threads(
            instance="test",
            state_dir=str(tmp_path),
            now="2026-05-25T12:00:00Z",
        )
    )
    assert result["success"] is True
    data = result["data"]
    assert data["serviced"] == 0
    assert data["archived"] == []
    assert data["starved"] == []
    assert data["dirty"] == []
    assert data["expiring"] == []


# ---------------------------------------------------------------------------
# 14. Hold reason preserved when thread is opened
# ---------------------------------------------------------------------------

def test_hold_reason_preserved_in_thread_open(tmp_path):
    """Opening a held thread shows the hold_reason in the capsule."""
    from agent_sensorium.tools import handle_sensorium_thread_open

    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    store.append_jsonl(
        "threads",
        _thread(
            id="sth_held_open",
            status="held",
            hold_reason="blocked on design approval",
            resume_trigger="design_approved",
            dirty_since="2026-05-24T12:00:00Z",
            source_refs=[{"type": "event", "id": "evt_1"}],
        ),
    )

    result = json.loads(
        handle_sensorium_thread_open(
            instance="test",
            state_dir=str(tmp_path),
            thread_id="sth_held_open",
            surface="local",
        )
    )
    assert result["success"] is True
    data = result["data"]
    assert data["hold_reason"] == "blocked on design approval"
    assert data["resume_trigger"] == "design_approved"
    assert data["dirty_since"] == "2026-05-24T12:00:00Z"
    assert data["source_refs"] == [{"type": "event", "id": "evt_1"}]
