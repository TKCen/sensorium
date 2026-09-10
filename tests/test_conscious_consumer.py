from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent_sensorium.conscious_aperture import open_conscious_aperture
from agent_sensorium.conscious_consumer import (
    build_conscious_source_packet,
    consume_conscious_advisory,
    parse_conscious_decision,
)
from agent_sensorium.store import SensoriumStore


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "sensorium_conscious_consumer_tick.py"


def _store(tmp_path) -> SensoriumStore:
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "sensorium"))
    store.ensure_dirs()
    return store


def _advisory(candidate_id: str = "advisory_1", *, source_id: str = "source_1") -> dict:
    return {
        "id": candidate_id,
        "status": "candidate",
        "kind": "subconscious_advisory",
        "pressure": 0.9,
        "summary": "A bounded source deserves one conscious choice.",
        "event_ids": ["event_1"],
        "source_candidate_ids": [source_id],
        "source_candidate_fingerprint": "source-revision-1",
        "correlation_keys": ["creative-thread"],
        "sensitivity": "private",
        "allowed_surfaces": ["local"],
        "created_at": "2026-08-26T10:00:00Z",
        "updated_at": "2026-08-26T10:00:00Z",
        "conscious_task": {
            "id": "ctask_1",
            "request_type": "THINK",
            "title": "Choose whether to reach out",
            "why": "The source remains meaningfully unresolved.",
            "expected_decision": "Silence, hold, or author one local message.",
        },
    }


def _decision(name: str, **fields) -> dict:
    return {"decision": name, "reason": "A bounded conscious choice for this source.", **fields}


def _ledger_snapshot(store: SensoriumStore) -> dict[str, list[dict]]:
    return {
        name: store.read_jsonl(name)
        for name in ("candidates", "decisions", "outbox", "threads", "worker_requests", "artifacts")
    }


def test_one_advisory_has_one_bounded_choice_cycle(tmp_path):
    store = _store(tmp_path)
    store.append_jsonl("candidates", _advisory())

    result = consume_conscious_advisory(
        store,
        decision=_decision("SILENCE"),
        dry_run=False,
        now="2026-08-26T11:00:00Z",
    )

    assert result["success"] is True
    assert result["action"] == "settled_silence"
    assert result["candidate_id"] == "advisory_1"
    assert result["aperture_id"].startswith("cap_")
    candidates = store.read_jsonl("candidates")
    assert candidates[0]["status"] == "reviewed"
    assert candidates[0]["conscious_aperture"]["state"] == "settled"
    assert [row for row in store.read_jsonl("decisions") if row["type"] == "conscious.aperture.opened"]


def test_silence_settles_without_outbound_content(tmp_path):
    store = _store(tmp_path)
    store.append_jsonl("candidates", _advisory())

    result = consume_conscious_advisory(
        store,
        decision=_decision("SILENCE", reason="No message is needed now."),
        dry_run=False,
        now="2026-08-26T11:00:00Z",
    )

    assert result["action"] == "settled_silence"
    assert store.read_jsonl("outbox") == []
    assert not any(row.get("type", "").startswith("conscious_reachout.") for row in store.read_jsonl("decisions"))


def test_hold_preserves_future_checkpoint_without_outbound_content(tmp_path):
    store = _store(tmp_path)
    store.append_jsonl("candidates", _advisory())

    result = consume_conscious_advisory(
        store,
        decision=_decision("HOLD", return_at="2026-08-26T12:00:00Z"),
        dry_run=False,
        now="2026-08-26T11:00:00Z",
    )

    assert result["action"] == "settled_hold"
    candidate = store.read_jsonl("candidates")[0]
    assert candidate["status"] == "held"
    assert candidate["held_return"] == {
        "not_before": "2026-08-26T12:00:00Z",
        "reason_code": "time_checkpoint",
    }
    assert store.read_jsonl("outbox") == []


def test_reach_out_prepares_one_local_message_then_settles(tmp_path):
    store = _store(tmp_path)
    store.append_jsonl("candidates", _advisory())
    message = "I thought of you while the quiet idea kept unfolding."

    result = consume_conscious_advisory(
        store,
        decision=_decision("REACH_OUT", message=message),
        dry_run=False,
        now="2026-08-26T11:00:00Z",
    )

    assert result["success"] is True
    assert result["action"] == "prepared_reach_out"
    assert result["outbox_id"].startswith("obx_")
    assert result["message_hash"]
    assert result["message_chars"] == len(message)
    assert "unfolding" not in json.dumps(result)
    outbox = store.read_jsonl("outbox")
    assert len(outbox) == 1
    assert outbox[0]["status"] == "prepared"
    assert outbox[0]["origin_candidate_id"] == "advisory_1"
    assert outbox[0]["message_preview"] == message
    candidate = store.read_jsonl("candidates")[0]
    assert candidate["status"] == "reviewed"
    settlement = [row for row in store.read_jsonl("decisions") if row["type"] == "conscious.aperture.settled"][-1]
    assert result["outbox_id"] in settlement["reason"]
    assert result["message_hash"] in settlement["reason"]
    assert store.read_jsonl("threads") == []
    assert store.read_jsonl("worker_requests") == []
    assert store.read_jsonl("artifacts") == []


def test_same_source_revision_is_idempotent_without_second_outbox(tmp_path):
    store = _store(tmp_path)
    store.append_jsonl("candidates", _advisory())
    decision = _decision("REACH_OUT", message="I thought of you while the quiet idea kept unfolding.")

    first = consume_conscious_advisory(store, decision=decision, dry_run=False, now="2026-08-26T11:00:00Z")
    second = consume_conscious_advisory(
        store,
        decision=_decision("REACH_OUT", message="A different authored version is not a new source revision."),
        dry_run=False,
        now="2026-08-26T11:01:00Z",
    )

    assert first["action"] == "prepared_reach_out"
    assert second["action"] == "no_eligible_advisory"
    assert len(store.read_jsonl("outbox")) == 1
    assert len([row for row in store.read_jsonl("decisions") if row.get("type") == "conscious.aperture.settled"]) == 1


def test_generic_notification_copy_is_rejected_without_preparation(tmp_path):
    store = _store(tmp_path)
    store.append_jsonl("candidates", _advisory())

    with pytest.raises(ValueError, match="specific authored message"):
        parse_conscious_decision(
            _decision("REACH_OUT", message="Candidate pressure alert detected in the notification queue.")
        )
    assert consume_conscious_advisory(
        store,
        decision=_decision("SILENCE"),
        dry_run=True,
        now="2026-08-26T11:00:00Z",
    )["action"] == "would_apply_silence"
    assert store.read_jsonl("outbox") == []


def test_policy_denial_does_not_falsely_settle_and_holds_safely(tmp_path):
    store = _store(tmp_path)
    store.append_jsonl("candidates", _advisory())

    result = consume_conscious_advisory(
        store,
        decision=_decision("REACH_OUT", message="I thought of you while the quiet idea kept unfolding."),
        config={"conscious_reachout": {"enabled": False}},
        dry_run=False,
        now="2026-08-26T11:00:00Z",
    )

    assert result["success"] is False
    assert result["action"] == "reach_out_denied_held"
    assert result["error"] == "reachout_disabled"
    assert store.read_jsonl("outbox") == []
    assert store.read_jsonl("candidates")[0]["status"] == "held"
    assert not any(row.get("new_status") == "reviewed" for row in store.read_jsonl("decisions"))


def test_existing_reachout_cooldown_does_not_falsely_settle(tmp_path):
    store = _store(tmp_path)
    store.append_jsonl("candidates", _advisory())
    store.append_jsonl("decisions", {
        "ts": "2026-08-26T10:30:00Z",
        "type": "conscious_reachout.delivered",
        "target_ref": "local",
        "message_hash": "old-message-hash",
    })

    result = consume_conscious_advisory(
        store,
        decision=_decision("REACH_OUT", message="I thought of you while the quiet idea kept unfolding."),
        dry_run=False,
        now="2026-08-26T11:00:00Z",
    )

    assert result["success"] is False
    assert result["error"] == "cooldown_active"
    assert store.read_jsonl("outbox") == []
    assert store.read_jsonl("candidates")[0]["status"] == "held"


def test_active_aperture_is_not_reopened_by_consumer(tmp_path):
    store = _store(tmp_path)
    store.append_jsonl("candidates", _advisory())
    opened = open_conscious_aperture(store, aperture_size=1, dry_run=False, now="2026-08-26T11:00:00Z")

    result = consume_conscious_advisory(
        store,
        decision=_decision("SILENCE"),
        dry_run=False,
        now="2026-08-26T11:01:00Z",
    )

    assert result["action"] == "settled_silence"
    assert result["aperture_id"] == opened["aperture_id"]
    assert len([row for row in store.read_jsonl("decisions") if row.get("type") == "conscious.aperture.opened"]) == 1


def test_unrelated_active_aperture_blocks_advisory_without_being_consumed(tmp_path):
    store = _store(tmp_path)
    store.append_jsonl("candidates", _advisory("other_advisory"))
    store.append_jsonl("candidates", {
        **_advisory("other_kind", source_id="other_source"),
        "kind": "creative_pull",
        "pressure": 1.0,
        "status": "in_conscious_aperture",
        "conscious_aperture": {
            "id": "cap_existing",
            "opened_at": "2026-08-26T11:00:00Z",
        },
    })

    result = consume_conscious_advisory(
        store,
        decision=_decision("SILENCE"),
        dry_run=False,
        now="2026-08-26T11:01:00Z",
    )

    assert result["action"] == "no_eligible_advisory"
    assert not any(row["id"] == "other_kind" and row["status"] == "reviewed" for row in store.read_jsonl("candidates"))


def test_dry_run_is_read_only_and_returns_bounded_preview(tmp_path):
    store = _store(tmp_path)
    store.append_jsonl("candidates", _advisory())
    before = _ledger_snapshot(store)

    result = consume_conscious_advisory(
        store,
        decision=_decision("REACH_OUT", message="I thought of you while the quiet idea kept unfolding."),
        dry_run=True,
        now="2026-08-26T11:00:00Z",
    )

    assert result["success"] is True
    assert result["action"] == "would_apply_reach_out"
    assert result["message_hash"]
    assert "unfolding" not in json.dumps(result)
    assert _ledger_snapshot(store) == before


def test_strict_conscious_decision_parser_rejects_malformed_missing_and_extra_fields():
    with pytest.raises(ValueError, match="object"):
        parse_conscious_decision("not-json")
    with pytest.raises(ValueError, match="decision"):
        parse_conscious_decision({"reason": "missing decision"})
    with pytest.raises(ValueError, match="return_at"):
        parse_conscious_decision(_decision("HOLD"))
    with pytest.raises(ValueError, match="message"):
        parse_conscious_decision(_decision("REACH_OUT"))
    with pytest.raises(ValueError, match="unexpected fields"):
        parse_conscious_decision({**_decision("SILENCE"), "extra": True})


def test_source_packet_is_compact_and_does_not_contain_authored_message(tmp_path):
    store = _store(tmp_path)
    candidate = _advisory()
    store.append_jsonl("candidates", candidate)
    opened = open_conscious_aperture(store, aperture_size=1, dry_run=True, now="2026-08-26T11:00:00Z")

    packet = build_conscious_source_packet(opened)

    assert packet["candidate_id"] == candidate["id"]
    assert packet["source_candidate_ids"] == ["source_1"]
    assert packet["source_candidate_fingerprint"] == "source-revision-1"
    assert "message" not in packet


def test_cli_dry_run_accepts_structured_decision_without_writes(tmp_path):
    state_dir = tmp_path / "sensorium"
    store = SensoriumStore(instance="test", state_dir=str(state_dir))
    store.ensure_dirs()
    store.append_jsonl("candidates", _advisory())
    decision_file = tmp_path / "decision.json"
    decision_file.write_text(json.dumps(_decision("SILENCE")), encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--instance", "test",
            "--state-dir", str(state_dir),
            "--decision-file", str(decision_file),
            "--now", "2026-08-26T11:00:00Z",
            "--json",
        ],
        text=True,
        capture_output=True,
        check=True,
    )

    payload = json.loads(proc.stdout)
    assert payload["success"] is True
    assert payload["action"] == "would_apply_silence"
    assert store.read_jsonl("candidates")[0]["status"] == "candidate"
    assert store.read_jsonl("decisions") == []
    assert store.read_jsonl("outbox") == []
