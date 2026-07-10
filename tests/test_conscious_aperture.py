import json
import subprocess
import sys
from pathlib import Path

from agent_sensorium.conscious_aperture import open_conscious_aperture, settle_conscious_aperture_item
from agent_sensorium.store import SensoriumStore


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "sensorium_conscious_aperture_tick.py"
SETTLE_SCRIPT = ROOT / "scripts" / "sensorium_conscious_aperture_settle.py"
WAKE_SCRIPT = ROOT / "scripts" / "sensorium_conscious_wake_tick.py"


def _candidate(candidate_id, *, pressure=0.7, request_type="THINK", created_at="2026-06-07T10:00:00Z"):
    return {
        "id": candidate_id,
        "status": "candidate",
        "kind": "subconscious_advisory",
        "pressure": pressure,
        "summary": f"Candidate {candidate_id}",
        "event_ids": [f"evt_{candidate_id}"],
        "source_candidate_ids": [],
        "correlation_keys": ["test"],
        "sensitivity": "private",
        "allowed_surfaces": ["local"],
        "created_at": created_at,
        "updated_at": created_at,
        "conscious_task": {
            "id": f"ctask_{candidate_id}",
            "request_type": request_type,
            "title": f"Task {candidate_id}",
            "why": "Needs coherent Conscious attention.",
            "expected_decision": "Decide save, hold, or external work.",
        },
        "advisory_meta": {"rationale": "test"},
    }


def test_dry_run_previews_bounded_candidates_without_mutation(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "sensorium"))
    store.ensure_dirs()
    store.append_jsonl("candidates", _candidate("low", pressure=0.2))
    store.append_jsonl("candidates", _candidate("high", pressure=0.9))
    store.append_jsonl("candidates", _candidate("mid", pressure=0.5))

    result = open_conscious_aperture(store, aperture_size=2, dry_run=True, now="2026-06-07T12:00:00Z")

    assert result["action"] == "would_open_aperture"
    assert result["candidate_ids"] == ["high", "mid"]
    assert result["selected_count"] == 2
    assert [c["status"] for c in store.read_jsonl("candidates")] == ["candidate", "candidate", "candidate"]
    assert store.read_jsonl("decisions") == []
    assert store.read_jsonl("worker_requests") == []


def test_open_marks_selected_candidates_and_records_receipt(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "sensorium"))
    store.ensure_dirs()
    store.append_jsonl("candidates", _candidate("one", pressure=0.8))
    store.append_jsonl("candidates", _candidate("two", pressure=0.7))

    result = open_conscious_aperture(store, aperture_size=1, dry_run=False, now="2026-06-07T12:00:00Z")

    assert result["action"] == "opened_aperture"
    assert result["candidate_ids"] == ["one"]
    candidates = {c["id"]: c for c in store.read_jsonl("candidates")}
    assert candidates["one"]["status"] == "in_conscious_aperture"
    assert candidates["one"]["conscious_aperture"]["id"] == result["aperture_id"]
    assert candidates["two"]["status"] == "candidate"
    decisions = store.read_jsonl("decisions")
    assert decisions[-1]["type"] == "conscious.aperture.opened"
    assert decisions[-1]["candidate_ids"] == ["one"]
    assert store.read_jsonl("worker_requests") == []


def test_active_aperture_guard_prevents_second_open(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "sensorium"))
    store.ensure_dirs()
    store.append_jsonl("candidates", _candidate("one", pressure=0.8))
    store.append_jsonl("candidates", _candidate("two", pressure=0.7))

    first = open_conscious_aperture(store, aperture_size=1, dry_run=False, now="2026-06-07T12:00:00Z")
    second = open_conscious_aperture(store, aperture_size=1, dry_run=False, now="2026-06-07T12:01:00Z")

    assert first["action"] == "opened_aperture"
    assert second["action"] == "active_aperture_exists"
    assert second["active_candidate_ids"] == ["one"]
    decisions = [d for d in store.read_jsonl("decisions") if d.get("type") == "conscious.aperture.opened"]
    assert len(decisions) == 1


def test_stale_active_aperture_blocks_replacement_open(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "sensorium"))
    store.ensure_dirs()
    stale = _candidate("stale", pressure=0.9)
    stale["status"] = "in_conscious_aperture"
    stale["conscious_aperture"] = {"id": "cap_old", "opened_at": "2026-06-07T08:00:00Z", "state": "open"}
    store.append_jsonl("candidates", stale)
    store.append_jsonl("candidates", _candidate("fresh", pressure=0.8))

    result = open_conscious_aperture(
        store,
        aperture_size=1,
        dry_run=False,
        now="2026-06-07T12:00:00Z",
        stale_after_minutes=60,
    )

    assert result["action"] == "stale_aperture_requires_settlement"
    assert result["stale_active_candidate_ids"] == ["stale"]
    assert result["candidate_ids"] == []
    assert [row["status"] for row in store.read_jsonl("candidates")] == ["in_conscious_aperture", "candidate"]
    assert store.read_jsonl("decisions") == []


def test_cli_opens_aperture_packet(tmp_path):
    state_dir = tmp_path / "sensorium"
    store = SensoriumStore(instance="test", state_dir=str(state_dir))
    store.ensure_dirs()
    store.append_jsonl("candidates", _candidate("cli", pressure=0.6))

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--instance",
            "test",
            "--state-dir",
            str(state_dir),
            "--aperture-size",
            "1",
            "--now",
            "2026-06-07T12:00:00Z",
            "--open",
            "--json",
        ],
        text=True,
        capture_output=True,
        check=True,
    )

    payload = json.loads(proc.stdout)
    assert payload["action"] == "opened_aperture"
    assert payload["candidate_ids"] == ["cli"]
    candidates = store.read_jsonl("candidates")
    assert candidates[0]["status"] == "in_conscious_aperture"
    assert store.read_jsonl("worker_requests") == []


def test_settle_aperture_item_marks_reviewed_and_records_receipt(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "sensorium"))
    store.ensure_dirs()
    store.append_jsonl("candidates", _candidate("one", pressure=0.8))
    opened = open_conscious_aperture(store, aperture_size=1, dry_run=False, now="2026-06-07T12:00:00Z")

    result = settle_conscious_aperture_item(
        store,
        candidate_id="one",
        aperture_id=opened["aperture_id"],
        decision="REVIEWED",
        reason="Conscious reviewed the canary and no external work is needed.",
        dry_run=False,
        now="2026-06-07T12:05:00Z",
    )

    assert result["action"] == "settled_aperture_item"
    assert result["new_status"] == "reviewed"
    candidate = store.read_jsonl("candidates")[0]
    assert candidate["status"] == "reviewed"
    assert candidate["conscious_aperture"]["state"] == "settled"
    assert candidate["conscious_aperture"]["decision"] == "REVIEWED"
    receipts = [d for d in store.read_jsonl("decisions") if d.get("type") == "conscious.aperture.settled"]
    assert len(receipts) == 1
    assert receipts[0]["candidate_id"] == "one"
    assert receipts[0]["decision"] == "REVIEWED"
    assert store.read_jsonl("worker_requests") == []


def test_settle_aperture_item_dry_run_does_not_mutate(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "sensorium"))
    store.ensure_dirs()
    store.append_jsonl("candidates", _candidate("one", pressure=0.8))
    open_conscious_aperture(store, aperture_size=1, dry_run=False, now="2026-06-07T12:00:00Z")

    result = settle_conscious_aperture_item(
        store,
        candidate_id="one",
        decision="HELD",
        reason="Needs operator foreground context later.",
        dry_run=True,
        now="2026-06-07T12:05:00Z",
    )

    assert result["action"] == "would_settle_aperture_item"
    assert result["receipt_preview"]["decision"] == "HELD"
    assert store.read_jsonl("candidates")[0]["status"] == "in_conscious_aperture"
    assert [d for d in store.read_jsonl("decisions") if d.get("type") == "conscious.aperture.settled"] == []
    assert store.read_jsonl("worker_requests") == []


def test_held_checkpoint_returns_only_when_due_and_retains_context_once(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "sensorium"))
    store.ensure_dirs()
    original = _candidate("returning", pressure=0.8)
    original["source_candidate_ids"] = ["source-private"]
    original["correlation_keys"] = ["continuity"]
    store.append_jsonl("candidates", original)
    opened = open_conscious_aperture(store, aperture_size=1, dry_run=False, now="2026-06-07T12:00:00Z")

    held = settle_conscious_aperture_item(
        store, candidate_id="returning", aperture_id=opened["aperture_id"], decision="HELD",
        reason="Return this to conscious attention later.", return_at="2026-06-07T13:00:00Z",
        dry_run=False, now="2026-06-07T12:05:00Z",
    )
    assert held["new_status"] == "held"
    persisted = store.read_jsonl("candidates")[0]
    assert persisted["held_return"] == {"not_before": "2026-06-07T13:00:00Z", "reason_code": "time_checkpoint"}
    assert open_conscious_aperture(store, dry_run=True, now="2026-06-07T12:59:59Z")["candidate_ids"] == []

    due = open_conscious_aperture(store, aperture_size=1, dry_run=False, now="2026-06-07T13:00:00Z")
    repeated = open_conscious_aperture(store, aperture_size=1, dry_run=False, now="2026-06-07T13:01:00Z")
    candidate = store.read_jsonl("candidates")[0]
    returned = [row for row in store.read_jsonl("decisions") if row.get("type") == "conscious.aperture.returned"]

    assert due["candidate_ids"] == ["returning"]
    assert repeated["action"] == "active_aperture_exists"
    assert candidate["status"] == "in_conscious_aperture"
    assert "held_return" not in candidate
    assert candidate["source_candidate_ids"] == ["source-private"]
    assert candidate["correlation_keys"] == ["continuity"]
    assert candidate["conscious_task"] == original["conscious_task"]
    assert len(returned) == 1
    assert returned[0]["return_reason_code"] == "time_checkpoint"
    assert store.read_jsonl("worker_requests") == []
    assert store.read_jsonl("outbox") == []
    assert store.read_jsonl("threads") == []


def test_fractional_held_checkpoint_preserves_precision_until_due(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "sensorium"))
    store.ensure_dirs()
    store.append_jsonl("candidates", _candidate("fractional", pressure=0.8))
    opened = open_conscious_aperture(
        store, aperture_size=1, dry_run=False, now="2026-06-07T12:00:00Z"
    )

    result = settle_conscious_aperture_item(
        store,
        candidate_id="fractional",
        aperture_id=opened["aperture_id"],
        decision="HELD",
        reason="Preserve the exact checkpoint instant.",
        return_at="2026-06-07T13:00:00.123456Z",
        dry_run=False,
        now="2026-06-07T12:05:00Z",
    )

    assert result["new_status"] == "held"
    assert store.read_jsonl("candidates")[0]["held_return"]["not_before"] == (
        "2026-06-07T13:00:00.123456Z"
    )
    assert open_conscious_aperture(
        store, dry_run=True, now="2026-06-07T13:00:00Z"
    )["candidate_ids"] == []
    assert open_conscious_aperture(
        store, dry_run=True, now="2026-06-07T13:00:00.123456Z"
    )["candidate_ids"] == ["fractional"]


def test_due_held_checkpoint_respects_active_and_stale_aperture_guards(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "sensorium"))
    store.ensure_dirs()
    held = _candidate("returning", pressure=0.8)
    held["status"] = "held"
    held["held_return"] = {"not_before": "2026-06-07T11:00:00Z", "reason_code": "time_checkpoint"}
    active = _candidate("active", pressure=0.7)
    active["status"] = "in_conscious_aperture"
    active["conscious_aperture"] = {"id": "cap_active", "opened_at": "2026-06-07T11:59:00Z", "state": "open"}
    store.append_jsonl("candidates", held)
    store.append_jsonl("candidates", active)

    blocked = open_conscious_aperture(store, dry_run=False, now="2026-06-07T12:00:00Z")
    assert blocked["action"] == "active_aperture_exists"
    assert store.read_jsonl("candidates")[0]["status"] == "held"

    active["conscious_aperture"]["opened_at"] = "2026-06-07T08:00:00Z"
    store.rewrite_jsonl("candidates", [held, active])
    stale = open_conscious_aperture(store, dry_run=False, now="2026-06-07T12:00:00Z", stale_after_minutes=60)
    assert stale["action"] == "stale_aperture_requires_settlement"
    assert store.read_jsonl("candidates")[0]["status"] == "held"
    assert [row for row in store.read_jsonl("decisions") if row.get("type") == "conscious.aperture.returned"] == []


def test_held_checkpoint_rejects_malformed_or_nonfuture_timestamp(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "sensorium"))
    store.ensure_dirs()
    store.append_jsonl("candidates", _candidate("one", pressure=0.8))
    opened = open_conscious_aperture(store, aperture_size=1, dry_run=False, now="2026-06-07T12:00:00Z")

    for value in (
        "not-a-time",
        "2026-06-07T12:00:00Z",
        "2026-06-07T13:00:00+01:00",
        "2027-06-07Z",
        "2026-06-07 13:00:00Z",
        "2026-06-07T13:00Z",
        "2026-06-07T13:00:00.1234567Z",
        " 2026-06-07T13:00:00Z",
        "2026-06-07T13:00:00Z ",
    ):
        result = settle_conscious_aperture_item(
            store, candidate_id="one", aperture_id=opened["aperture_id"], decision="HELD",
            reason="Hold only with a valid future UTC checkpoint.", return_at=value,
            dry_run=False, now="2026-06-07T12:00:00Z",
        )
        assert result["error"] == "invalid_return_at"
    assert store.read_jsonl("candidates")[0]["status"] == "in_conscious_aperture"
    assert store.read_jsonl("decisions")[-1]["type"] == "conscious.aperture.opened"


def test_settle_aperture_item_records_external_work_spec_without_worker_request(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "sensorium"))
    store.ensure_dirs()
    store.append_jsonl("candidates", _candidate("one", pressure=0.8, request_type="DELEGATE_WORK"))
    open_conscious_aperture(store, aperture_size=1, dry_run=False, now="2026-06-07T12:00:00Z")

    result = settle_conscious_aperture_item(
        store,
        candidate_id="one",
        decision="PREPARED_EXTERNAL_WORK",
        reason="A bounded Kanban cleanup request should be prepared later.",
        external_work={
            "title": "Classify old conscious backlog",
            "summary": "Tiny batch classification only; no stampede.",
            "worker_type": "kanban_task",
            "profile": {"name": "subconscious-reviewer"},
            "target": {"board": "sensorium"},
        },
        dry_run=False,
        now="2026-06-07T12:05:00Z",
    )

    assert result["new_status"] == "prepared_external_work"
    receipt = result["receipt"]
    assert receipt["external_work"]["title"] == "Classify old conscious backlog"
    assert receipt["external_work"]["profile"] == {"name": "subconscious-reviewer"}
    assert store.read_jsonl("worker_requests") == []


def test_settle_aperture_item_is_idempotent(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "sensorium"))
    store.ensure_dirs()
    store.append_jsonl("candidates", _candidate("one", pressure=0.8))
    opened = open_conscious_aperture(store, aperture_size=1, dry_run=False, now="2026-06-07T12:00:00Z")

    first = settle_conscious_aperture_item(
        store,
        candidate_id="one",
        aperture_id=opened["aperture_id"],
        decision="REVIEWED",
        reason="Done.",
        dry_run=False,
        now="2026-06-07T12:05:00Z",
    )
    second = settle_conscious_aperture_item(
        store,
        candidate_id="one",
        aperture_id=opened["aperture_id"],
        decision="REVIEWED",
        reason="Done.",
        dry_run=False,
        now="2026-06-07T12:06:00Z",
    )

    assert first["action"] == "settled_aperture_item"
    assert second["action"] == "already_settled"
    receipts = [d for d in store.read_jsonl("decisions") if d.get("type") == "conscious.aperture.settled"]
    assert len(receipts) == 1


def test_settle_cli_applies_record(tmp_path):
    state_dir = tmp_path / "sensorium"
    store = SensoriumStore(instance="test", state_dir=str(state_dir))
    store.ensure_dirs()
    store.append_jsonl("candidates", _candidate("cli_settle", pressure=0.8))
    opened = open_conscious_aperture(store, aperture_size=1, dry_run=False, now="2026-06-07T12:00:00Z")
    record = {
        "candidate_id": "cli_settle",
        "aperture_id": opened["aperture_id"],
        "decision": "SETTLED",
        "reason": "CLI settlement path verified.",
    }

    proc = subprocess.run(
        [
            sys.executable,
            str(SETTLE_SCRIPT),
            "--instance",
            "test",
            "--state-dir",
            str(state_dir),
            "--record",
            json.dumps(record),
            "--apply",
            "--now",
            "2026-06-07T12:05:00Z",
            "--json",
        ],
        text=True,
        capture_output=True,
        check=True,
    )

    payload = json.loads(proc.stdout)
    assert payload["success"] is True
    assert payload["settled"] == 1
    candidate = store.read_jsonl("candidates")[0]
    assert candidate["status"] == "reviewed"
    assert candidate["conscious_aperture"]["decision"] == "SETTLED"


def test_wake_tick_dry_run_previews_without_mutation(tmp_path):
    state_dir = tmp_path / "sensorium"
    store = SensoriumStore(instance="test", state_dir=str(state_dir))
    store.ensure_dirs()
    store.append_jsonl("candidates", _candidate("wake", pressure=0.8))

    proc = subprocess.run(
        [
            sys.executable,
            str(WAKE_SCRIPT),
            "--instance",
            "test",
            "--state-dir",
            str(state_dir),
            "--aperture-size",
            "1",
            "--now",
            "2026-06-07T12:00:00Z",
            "--json",
        ],
        text=True,
        capture_output=True,
        check=True,
    )

    payload = json.loads(proc.stdout)
    assert payload["success"] is True
    assert payload["aperture"]["action"] == "would_open_aperture"
    assert payload["next_action"] == "settle_aperture_items"
    assert store.read_jsonl("candidates")[0]["status"] == "candidate"
    assert store.read_jsonl("worker_requests") == []


def test_wake_tick_opens_and_applies_settlement_without_worker_request(tmp_path):
    state_dir = tmp_path / "sensorium"
    store = SensoriumStore(instance="test", state_dir=str(state_dir))
    store.ensure_dirs()
    store.append_jsonl("candidates", _candidate("wake_settle", pressure=0.8))
    settlement_file = tmp_path / "settlement.json"
    settlement_file.write_text(json.dumps({
        "candidate_id": "wake_settle",
        "decision": "REVIEWED",
        "reason": "Wake runner applied explicit Conscious settlement.",
    }))

    proc = subprocess.run(
        [
            sys.executable,
            str(WAKE_SCRIPT),
            "--instance",
            "test",
            "--state-dir",
            str(state_dir),
            "--aperture-size",
            "1",
            "--open",
            "--settlements",
            str(settlement_file),
            "--apply-settlements",
            "--now",
            "2026-06-07T12:00:00Z",
            "--json",
        ],
        text=True,
        capture_output=True,
        check=True,
    )

    payload = json.loads(proc.stdout)
    assert payload["success"] is True
    assert payload["aperture"]["action"] == "opened_aperture"
    assert payload["settlements"]["applied"] == 1
    assert payload["worker_requests"]["delta"] == 0
    candidate = store.read_jsonl("candidates")[0]
    assert candidate["status"] == "reviewed"
    assert candidate["conscious_aperture"]["decision"] == "REVIEWED"
    assert store.read_jsonl("worker_requests") == []


def test_wake_tick_applies_held_return_checkpoint(tmp_path):
    state_dir = tmp_path / "sensorium"
    store = SensoriumStore(instance="test", state_dir=str(state_dir))
    store.ensure_dirs()
    store.append_jsonl("candidates", _candidate("wake_return", pressure=0.8))
    settlement_file = tmp_path / "settlement.json"
    settlement_file.write_text(json.dumps({
        "candidate_id": "wake_return",
        "decision": "HELD",
        "reason": "Return this through the aperture later.",
        "return_at": "2026-06-07T13:00:00Z",
    }))

    proc = subprocess.run(
        [
            sys.executable,
            str(WAKE_SCRIPT),
            "--instance",
            "test",
            "--state-dir",
            str(state_dir),
            "--aperture-size",
            "1",
            "--open",
            "--settlements",
            str(settlement_file),
            "--apply-settlements",
            "--now",
            "2026-06-07T12:00:00Z",
            "--json",
        ],
        text=True,
        capture_output=True,
        check=True,
    )

    payload = json.loads(proc.stdout)
    candidate = store.read_jsonl("candidates")[0]
    assert payload["success"] is True
    assert payload["settlements"]["applied"] == 1
    assert payload["settlement_record_shape"]["return_at"] == "optional future UTC-Z checkpoint for HELD"
    assert candidate["status"] == "held"
    assert candidate["held_return"] == {
        "not_before": "2026-06-07T13:00:00Z",
        "reason_code": "time_checkpoint",
    }
    assert store.read_jsonl("worker_requests") == []
