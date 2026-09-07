from __future__ import annotations

import json

from agent_sensorium.conscious_aperture import (
    open_conscious_aperture,
    record_conscious_aperture_presentation_attempt,
    settle_conscious_aperture_item,
)
from agent_sensorium.conscious_doorway import handle_conscious_doorway_pre_llm
from agent_sensorium.store import SensoriumStore


def _candidate(candidate_id, *, pressure=0.7, created_at="2026-06-07T10:00:00Z"):
    return {
        "id": candidate_id, "status": "candidate", "kind": "subconscious_advisory",
        "pressure": pressure, "summary": f"Candidate {candidate_id}",
        "fingerprint": f"fp-{candidate_id}", "event_ids": [f"evt_{candidate_id}"],
        "source_candidate_ids": [], "correlation_keys": ["test"],
        "sensitivity": "private", "allowed_surfaces": ["local"],
        "created_at": created_at, "updated_at": created_at,
        "conscious_task": {"id": f"task-{candidate_id}", "request_type": "THINK",
                            "title": candidate_id, "why": "review", "expected_decision": "decide"},
        "advisory_meta": {"source_fingerprint": f"source-{candidate_id}"},
    }


def _expired(candidate_id):
    row = _candidate(candidate_id, pressure=0.1)
    row["status"] = "in_conscious_aperture"
    row["conscious_aperture"] = {
        "id": f"cap-{candidate_id}", "consumer_id": "dead-owner", "generation": 1,
        "opened_at": "2026-06-07T10:00:00Z", "lease_expires_at": "2026-06-07T10:05:00Z",
        "state": "open",
    }
    return row


def test_current_lease_requires_exact_tokens_and_rejects_stale_generation(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "state"))
    store.append_jsonl("candidates", _candidate("owned"))
    first = open_conscious_aperture(
        store, aperture_size=1, max_active_items=1, consumer_id="owner-a",
        lease_minutes=5, dry_run=False, now="2026-06-07T12:00:00Z",
    )
    cases = [
        ({"consumer_id": "owner-a"}, "aperture_id_required"),
        ({"aperture_id": first["aperture_id"]}, "consumer_id_required"),
        ({"aperture_id": "cap-wrong", "consumer_id": "owner-a"}, "aperture_id_mismatch"),
        ({"aperture_id": first["aperture_id"], "consumer_id": "owner-b"}, "consumer_id_mismatch"),
    ]
    for tokens, expected in cases:
        before = store.read_jsonl("candidates")
        result = settle_conscious_aperture_item(
            store, candidate_id="owned", decision="SETTLED", reason="Exact owner only.",
            dry_run=False, now="2026-06-07T12:01:00Z", **tokens,
        )
        assert result["error"] == expected
        assert store.read_jsonl("candidates") == before
    expired = settle_conscious_aperture_item(
        store, candidate_id="owned", aperture_id=first["aperture_id"], consumer_id="owner-a",
        decision="SETTLED", reason="Expired.", dry_run=False, now="2026-06-07T12:05:00Z",
    )
    assert expired["error"] == "aperture_lease_expired"
    second = open_conscious_aperture(
        store, aperture_size=1, max_active_items=1, consumer_id="owner-b",
        lease_minutes=5, dry_run=False, now="2026-06-07T12:06:00Z",
    )
    stale = settle_conscious_aperture_item(
        store, candidate_id="owned", aperture_id=first["aperture_id"], consumer_id="owner-a",
        decision="SETTLED", reason="Stale generation.", dry_run=False, now="2026-06-07T12:07:00Z",
    )
    assert second["aperture_id"] != first["aperture_id"]
    assert stale["error"] == "aperture_id_mismatch"


def test_legacy_ownerless_path_is_explicit_and_batch_wrong_owner_fails_closed(tmp_path):
    legacy_store = SensoriumStore(instance="legacy", state_dir=str(tmp_path / "legacy"))
    legacy = _candidate("legacy")
    legacy["status"] = "in_conscious_aperture"
    legacy["conscious_aperture"] = {"opened_at": "2026-06-07T12:00:00Z", "state": "open"}
    legacy_store.append_jsonl("candidates", legacy)
    blocked = settle_conscious_aperture_item(
        legacy_store, candidate_id="legacy", decision="SETTLED", reason="Legacy.",
        dry_run=False, now="2026-06-07T12:01:00Z",
    )
    allowed = settle_conscious_aperture_item(
        legacy_store, candidate_id="legacy", decision="SETTLED", reason="Legacy.",
        allow_legacy_ownerless=True, dry_run=False, now="2026-06-07T12:01:00Z",
    )
    assert blocked["error"] == "aperture_id_required"
    assert allowed["success"] is True

    store = SensoriumStore(instance="batch", state_dir=str(tmp_path / "batch"))
    store.rewrite_jsonl("candidates", [_candidate("alpha"), _candidate("beta")])
    packet = open_conscious_aperture(
        store, aperture_size=2, max_active_items=2, consumer_id="owner-a",
        dry_run=False, now="2026-06-07T12:00:00Z",
    )
    results = [settle_conscious_aperture_item(
        store, candidate_id=item["candidate_id"], aperture_id=item["aperture_id"],
        consumer_id="owner-a" if index == 0 else "owner-b", decision="SETTLED",
        reason="Mixed batch.", dry_run=False, now="2026-06-07T12:01:00Z",
    ) for index, item in enumerate(packet["aperture"])]
    assert results[0]["success"] is True
    assert results[1]["error"] == "consumer_id_mismatch"


def test_packet_attempt_is_all_or_none_and_host_drop_never_claims_consumption(tmp_path):
    store = SensoriumStore(instance="packet", state_dir=str(tmp_path / "packet"))
    store.rewrite_jsonl("candidates", [_candidate("alpha"), _candidate("beta")])
    packet = open_conscious_aperture(
        store, aperture_size=2, max_active_items=2, consumer_id="owner-a",
        dry_run=False, now="2026-06-07T12:00:00Z",
    )
    invalid = [dict(item) for item in packet["aperture"]]
    invalid[1]["aperture_id"] = "cap-stale"
    failed = record_conscious_aperture_presentation_attempt(
        store, aperture=invalid, consumer_id="owner-a", turn_id="turn-a",
        surface="local", now="2026-06-07T12:01:00Z",
    )
    assert failed["error"] == "aperture_id_mismatch" and failed["item_index"] == 1
    assert not [d for d in store.read_jsonl("decisions") if d.get("type") == "conscious.aperture.presentation_attempted"]

    doorway_dir = tmp_path / "doorway"
    doorway = SensoriumStore(instance="generic", state_dir=str(doorway_dir))
    doorway.ensure_dirs()
    (doorway_dir / "instance.config.json").write_text(json.dumps({
        "instance_name": "generic", "allowed_surfaces": ["local"],
        "conscious_doorway": {"enabled": True, "aperture_size": 1,
                              "max_active_items": 1, "surfaces": ["local"]},
    }))
    doorway.append_jsonl("candidates", _candidate("host-drop"))
    proposed = handle_conscious_doorway_pre_llm(
        instance="generic", platform="local", session_id="session-a",
        turn_id="turn-a", state_dir=str(doorway_dir),
    )
    assert proposed is not None  # Simulated host drops this proposed context.
    decisions = doorway.read_jsonl("decisions")
    attempts = [d for d in decisions if d.get("type") == "conscious.aperture.presentation_attempted"]
    assert len(attempts) == 1 and attempts[0]["host_consumption_confirmed"] is False
    assert not [d for d in decisions if d.get("type") == "conscious.aperture.consumed"]


def test_prepared_external_work_requires_validated_core_spec(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "state"))
    store.append_jsonl("candidates", _candidate("prepared"))
    packet = open_conscious_aperture(
        store, aperture_size=1, max_active_items=1, consumer_id="owner-a",
        dry_run=False, now="2026-06-07T12:00:00Z",
    )
    before = store.read_jsonl("candidates")
    for external_work, error in ((None, "external_work_spec_required"), ({"title": "Only"}, "invalid_external_work_spec")):
        result = settle_conscious_aperture_item(
            store, candidate_id="prepared", aperture_id=packet["aperture_id"],
            consumer_id="owner-a", decision="PREPARED_EXTERNAL_WORK", reason="Prepare.",
            external_work=external_work, dry_run=False, now="2026-06-07T12:01:00Z",
        )
        assert result["error"] == error
        assert store.read_jsonl("candidates") == before
    assert store.read_jsonl("worker_requests") == store.read_jsonl("outbox") == []


def test_capacity_one_persists_alternation_so_neither_lane_starves(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "state"))
    store.append_jsonl("candidates", _expired("stale"))
    picks, lanes = [], []
    for index in range(4):
        rows = store.read_jsonl("candidates")
        rows.append(_candidate(f"fresh-{index}", pressure=0.99))
        if index:
            rows.append(_expired(f"recovery-{index}"))
        store.rewrite_jsonl("candidates", rows)
        packet = open_conscious_aperture(
            store, aperture_size=1, max_active_items=1, consumer_id=f"owner-{index}",
            dry_run=False, now=f"2026-06-07T12:0{index}:00Z",
        )
        picks += packet["candidate_ids"]
        lanes += packet["fairness_service_lanes"]
        item = packet["aperture"][0]
        settle_conscious_aperture_item(
            store, candidate_id=item["candidate_id"], aperture_id=item["aperture_id"],
            consumer_id=f"owner-{index}", decision="SETTLED", reason="Clear.",
            dry_run=False, now=f"2026-06-07T12:0{index}:30Z",
        )
    assert picks[:2] == ["stale", "fresh-0"]
    assert lanes == ["recovery", "fresh", "recovery", "fresh"]


def test_mixed_lane_order_is_stable_by_time_then_id_under_reordering(tmp_path):
    expired = _expired("expired")
    expired["conscious_aperture"]["lease_expires_at"] = "2026-06-07T08:00:00Z"
    held = _candidate("held")
    held["status"] = "held"
    held["held_return"] = {"not_before": "2026-06-07T09:00:00Z", "reason_code": "time_checkpoint"}
    fresh = _candidate("fresh", pressure=0.99, created_at="2026-06-07T11:00:00Z")
    outputs = []
    for name, rows in (("forward", [expired, held, fresh]), ("reverse", [fresh, held, expired])):
        store = SensoriumStore(instance=name, state_dir=str(tmp_path / name))
        store.rewrite_jsonl("candidates", rows)
        result = open_conscious_aperture(
            store, aperture_size=3, max_active_items=3, consumer_id="owner",
            stale_after_minutes=60, dry_run=False, now="2026-06-07T12:00:00Z",
        )
        outputs.append((result["candidate_ids"], result["fairness_service_lanes"]))
        assert not [d for d in store.read_jsonl("decisions") if d.get("type") == "conscious.aperture.settled"]
        assert store.read_jsonl("worker_requests") == store.read_jsonl("outbox") == []
    expected = (["expired", "fresh", "held"], ["recovery", "fresh", "recovery"])
    assert outputs == [expected, expected]
