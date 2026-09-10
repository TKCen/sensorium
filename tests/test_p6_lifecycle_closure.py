from __future__ import annotations

import json
import queue
import threading

import pytest

from agent_sensorium.conscious_aperture import (
    open_conscious_aperture,
    settle_conscious_aperture_item,
)
from agent_sensorium.plugin import register
from agent_sensorium.store import SensoriumStore
from agent_sensorium.tools import handle_sensorium_candidate_update


def _candidate(candidate_id: str) -> dict:
    created_at = "2026-06-07T10:00:00Z"
    return {
        "id": candidate_id,
        "status": "candidate",
        "kind": "subconscious_advisory",
        "pressure": 0.8,
        "summary": f"Candidate {candidate_id}",
        "fingerprint": f"fp-{candidate_id}",
        "event_ids": [f"evt-{candidate_id}"],
        "source_candidate_ids": [],
        "correlation_keys": ["synthetic"],
        "sensitivity": "private",
        "allowed_surfaces": ["local"],
        "created_at": created_at,
        "updated_at": created_at,
        "conscious_task": {
            "id": f"task-{candidate_id}",
            "request_type": "THINK",
            "title": candidate_id,
            "why": "Synthetic lifecycle regression.",
            "expected_decision": "Review.",
        },
        "advisory_meta": {"source_fingerprint": f"source-{candidate_id}"},
    }


def _update(store: SensoriumStore, candidate_id: str, action: str) -> dict:
    return json.loads(
        handle_sensorium_candidate_update(
            candidate_id=candidate_id,
            action=action,
            reason="Synthetic lifecycle update.",
            instance="test",
            state_dir=str(store.root),
        )
    )


class _LiveContext:
    def __init__(self) -> None:
        self.tools = {}

    def register_tool(self, *, name, handler, **kwargs) -> None:
        self.tools[name] = handler

    def register_hook(self, *args, **kwargs) -> None:
        pass

    def register_command(self, *args, **kwargs) -> None:
        pass

    def register_skill(self, *args, **kwargs) -> None:
        pass


def _live_resume(store: SensoriumStore, candidate_id: str) -> dict:
    context = _LiveContext()
    register(context)
    return json.loads(
        context.tools["sensorium"](
            {
                "action": "update",
                "instance": "test",
                "id": candidate_id,
                "keyword": "resume",
                "text": "Synthetic lifecycle update.",
            },
            state_dir=str(store.root),
        )
    )


def test_checkpoint_held_candidate_cannot_resume_early_and_malformed_fails_closed(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "resume-guards"))
    future = _candidate("cand_future")
    future["status"] = "held"
    future["held_return"] = {
        "not_before": "9999-12-31T23:59:59Z",
        "reason_code": "time_checkpoint",
    }
    malformed = _candidate("cand_malformed")
    malformed["status"] = "held"
    malformed["held_return"] = {
        "not_before": "not-a-checkpoint",
        "reason_code": "time_checkpoint",
    }
    store.rewrite_jsonl("candidates", [future, malformed])

    before = store.read_jsonl("candidates")
    future_result = _live_resume(store, "cand_future")
    malformed_result = _live_resume(store, "cand_malformed")

    assert future_result["success"] is False
    assert future_result["error"] == "candidate_checkpoint_not_due"
    assert malformed_result["success"] is False
    assert malformed_result["error"] == "candidate_checkpoint_malformed"
    assert store.read_jsonl("candidates") == before
    assert store.read_jsonl("decisions") == []


def test_due_checkpoint_resume_clears_hold_metadata_and_legacy_hold_still_resumes(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "resume-positive"))
    due = _candidate("due")
    due.update(
        {
            "status": "held",
            "hold_reason": "Wait for checkpoint.",
            "held_return": {
                "not_before": "2000-01-01T00:00:00Z",
                "reason_code": "time_checkpoint",
            },
        }
    )
    legacy = _candidate("legacy")
    legacy.update({"status": "held", "hold_reason": "Legacy manual hold."})
    store.rewrite_jsonl("candidates", [due, legacy])

    due_result = _update(store, "due", "resume")
    legacy_result = _update(store, "legacy", "resume")
    rows = {row["id"]: row for row in store.read_jsonl("candidates")}

    assert due_result["success"] is True
    assert legacy_result["success"] is True
    assert rows["due"]["status"] == rows["legacy"]["status"] == "candidate"
    assert "held_return" not in rows["due"]
    assert rows["due"]["hold_reason"] == rows["legacy"]["hold_reason"] == ""


def _owned_candidate(store: SensoriumStore, candidate_id: str) -> tuple[str, str]:
    store.append_jsonl("candidates", _candidate(candidate_id))
    packet = open_conscious_aperture(
        store,
        aperture_size=1,
        max_active_items=1,
        consumer_id="exact-owner",
        lease_minutes=15,
        dry_run=False,
        now="2026-06-07T12:00:00Z",
    )
    item = packet["aperture"][0]
    return item["aperture_id"], item["consumer_id"]


def test_settlement_legacy_expiry_fallback_matches_aperture_reclaim_policy(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "legacy-expiry"))
    aperture_id, consumer_id = _owned_candidate(store, "legacy-expiry")
    rows = store.read_jsonl("candidates")
    aperture = rows[0]["conscious_aperture"]
    aperture.pop("lease_expires_at")
    aperture["opened_at"] = "2026-06-07T10:00:00Z"
    store.rewrite_jsonl("candidates", rows)
    before = store.read_jsonl("candidates")

    result = settle_conscious_aperture_item(
        store,
        candidate_id="legacy-expiry",
        aperture_id=aperture_id,
        consumer_id=consumer_id,
        decision="SETTLED",
        reason="Expired legacy ownership must not settle.",
        dry_run=False,
        now="2026-06-07T13:00:00Z",
    )

    assert result["success"] is False
    assert result["error"] == "aperture_lease_expired"
    assert result["lease_expires_at"] == "2026-06-07T13:00:00Z"
    assert store.read_jsonl("candidates") == before


def test_settlement_fails_closed_on_malformed_explicit_expiry(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "malformed-expiry"))
    aperture_id, consumer_id = _owned_candidate(store, "malformed-expiry")
    rows = store.read_jsonl("candidates")
    rows[0]["conscious_aperture"]["lease_expires_at"] = "not-an-expiry"
    store.rewrite_jsonl("candidates", rows)
    before = store.read_jsonl("candidates")

    result = settle_conscious_aperture_item(
        store,
        candidate_id="malformed-expiry",
        aperture_id=aperture_id,
        consumer_id=consumer_id,
        decision="SETTLED",
        reason="Malformed explicit expiry must fail closed.",
        dry_run=False,
        now="2026-06-07T12:01:00Z",
    )

    assert result["success"] is False
    assert result["error"] == "aperture_lease_expiry_malformed"
    assert store.read_jsonl("candidates") == before


def test_exact_same_owner_settlement_remains_idempotent_after_expiry(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "idempotent"))
    aperture_id, consumer_id = _owned_candidate(store, "idempotent")
    arguments = {
        "candidate_id": "idempotent",
        "aperture_id": aperture_id,
        "consumer_id": consumer_id,
        "decision": "SETTLED",
        "reason": "Exact repeated settlement.",
        "dry_run": False,
    }

    first = settle_conscious_aperture_item(store, now="2026-06-07T12:01:00Z", **arguments)
    second = settle_conscious_aperture_item(store, now="2026-06-07T13:00:00Z", **arguments)

    assert first["action"] == "settled_aperture_item"
    assert second["action"] == "already_settled"
    assert second["receipt"] == first["receipt"]
    settlements = [
        row
        for row in store.read_jsonl("decisions")
        if row.get("type") == "conscious.aperture.settled"
    ]
    assert len(settlements) == 1


def test_concurrent_cross_root_ab_ba_rejects_without_deadlock_and_same_root_reenters(tmp_path):
    stores = {
        "a": SensoriumStore(instance="a", state_dir=str(tmp_path / "a")),
        "b": SensoriumStore(instance="b", state_dir=str(tmp_path / "b")),
    }
    same_a = SensoriumStore(instance="a", state_dir=str(tmp_path / "a"))
    with stores["a"].candidate_transaction():
        with same_a.candidate_transaction():
            same_a.append_jsonl("candidates", {"id": "same-root"})

    barrier = threading.Barrier(2)
    outcomes: queue.Queue[tuple[str, str]] = queue.Queue()

    def invert(first: str, second: str) -> None:
        try:
            with stores[first].candidate_transaction():
                barrier.wait(timeout=2)
                with stores[second].candidate_transaction():
                    pytest.fail("cross-root nested transaction was accepted")
        except RuntimeError as exc:
            outcomes.put((first, str(exc)))

    threads = [
        threading.Thread(target=invert, args=("a", "b"), daemon=True),
        threading.Thread(target=invert, args=("b", "a"), daemon=True),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert not any(thread.is_alive() for thread in threads), "AB/BA lock-order deadlock"
    assert sorted(outcomes.get_nowait() for _ in range(2)) == [
        ("a", "candidate transactions cannot nest across profile roots"),
        ("b", "candidate transactions cannot nest across profile roots"),
    ]
    assert stores["a"].read_jsonl("candidates") == [{"id": "same-root"}]
