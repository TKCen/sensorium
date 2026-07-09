"""Adversarial first-slice liveness reconciliation coverage."""

import json

from agent_sensorium.conscious_aperture import open_conscious_aperture
from agent_sensorium.settlement import LIVENESS_REASON_CODES, LIVENESS_STATES, plan_liveness_reconciliation
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
