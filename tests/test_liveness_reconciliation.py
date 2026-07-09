"""Adversarial first-slice liveness reconciliation coverage."""

import json

from agent_sensorium.conscious_aperture import open_conscious_aperture
from agent_sensorium.settlement import (
    LIVENESS_REASON_CODES,
    LIVENESS_RECEIPT_SCHEMA,
    LIVENESS_STATES,
    append_liveness_receipts,
    plan_liveness_reconciliation,
)
from agent_sensorium.store import SensoriumStore


def _candidate(candidate_id, pressure=0.9, status="candidate"):
    return {"id": candidate_id, "status": status, "pressure": pressure, "kind": "pressure_event"}


def _conscious_candidate(candidate_id, status="candidate"):
    row = _candidate(candidate_id, status=status)
    row["kind"] = "subconscious_advisory"
    row["conscious_task"] = {"id": f"task_{candidate_id}", "request_type": "THINK"}
    return row


def test_plan_is_deterministic_tie_sorted_capped_and_hostile_safe():
    sentinel = "RAW_SECRET_SENTINEL_SHOULD_NOT_LEAK"
    rows = [_candidate("z", 0.8), _candidate("a", 0.8), _candidate(sentinel, 0.9, "totally_corrupt")]
    first = plan_liveness_reconciliation(rows, now="2026-07-09T12:00:00Z", max_intakes=1)
    second = plan_liveness_reconciliation(rows, now="2026-07-09T12:00:00Z", max_intakes=1)

    assert first == second
    plan = first["candidate_reconciliation"]
    assert [item["candidate_id"] for item in plan["mint"]] == ["a"]
    assert plan["truncated"] == 1
    payload = json.dumps(first, sort_keys=True)
    assert sentinel not in payload
    for finding in first["classification"]["findings"]:
        assert finding["state"] in LIVENESS_STATES
        assert finding["reason_code"] in LIVENESS_REASON_CODES


def test_stale_aperture_blocks_dry_run_and_write_without_any_write(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "state"))
    store.ensure_dirs()
    stale = _conscious_candidate("stale", status="in_conscious_aperture")
    stale["conscious_aperture"] = {"id": "cap_old", "opened_at": "2026-07-09T08:00:00Z", "state": "open"}
    store.append_jsonl("candidates", stale)
    store.append_jsonl("candidates", _conscious_candidate("replacement"))
    before = {name: store.read_jsonl(name) for name in ("candidates", "decisions", "worker_requests", "threads", "thread_actions", "outbox")}

    dry = open_conscious_aperture(store, dry_run=True, now="2026-07-09T12:00:00Z", stale_after_minutes=60)
    write = open_conscious_aperture(store, dry_run=False, now="2026-07-09T12:00:00Z", stale_after_minutes=60)

    assert dry["action"] == write["action"] == "stale_aperture_requires_settlement"
    assert write["stale_active_candidate_ids"] == ["stale"]
    assert before == {name: store.read_jsonl(name) for name in before}


def test_historical_pointer_is_settled_and_non_actionable():
    plan = plan_liveness_reconciliation([], now="2026-07-09T12:00:00Z")
    # Classification accepts outbox lineage facts without inspecting raw content.
    from agent_sensorium.settlement import classify_liveness_snapshot
    result = classify_liveness_snapshot([], now="2026-07-09T12:00:00Z", outbox=[{"id": "pointer", "status": "prepared"}], historical_outbox_ids={"pointer"})
    finding = result["findings"][0]
    assert finding["state"] == "settled"
    assert finding["reason_code"] == "historical_prepared_pointer"
    assert finding["actionable"] is False
    assert plan["summary"]["mint"] == 0


def test_liveness_receipts_are_opaque_idempotent_and_decision_only(tmp_path):
    sentinel = "RAW_SECRET_SENTINEL_SHOULD_NOT_LEAK"
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "state"))
    store.ensure_dirs()
    store.append_jsonl("candidates", _candidate(sentinel, 0.9, "totally_corrupt"))
    plan = plan_liveness_reconciliation(store.read_jsonl("candidates"), now="2026-07-09T12:00:00Z")
    before = {name: store.read_jsonl(name) for name in ("candidates", "worker_requests", "threads", "thread_actions", "outbox")}

    first = append_liveness_receipts(store, plan["classification"]["findings"])
    receipt_rows = [row for row in store.read_jsonl("decisions") if row.get("schema") == LIVENESS_RECEIPT_SCHEMA]
    second = append_liveness_receipts(store, plan["classification"]["findings"])

    assert first == {"written": 1, "skipped": 0}
    assert second == {"written": 0, "skipped": 1}
    assert len(receipt_rows) == 1
    receipt = receipt_rows[0]
    assert receipt["receipt_kind"] == "liveness_reconciliation"
    assert receipt["action"] == "none"
    assert receipt["subject_ref"]["type"] == "candidate"
    assert receipt["subject_ref"]["id"].startswith("candidate#")
    assert receipt["reason_code"] in LIVENESS_REASON_CODES
    assert receipt["new_liveness"] in LIVENESS_STATES
    assert receipt["related_refs"] == []
    assert sentinel not in json.dumps(receipt, sort_keys=True)
    assert before == {name: store.read_jsonl(name) for name in before}
