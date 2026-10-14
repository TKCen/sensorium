"""Optional latent projection: synthetic closed-policy and omission proofs."""
import json

import pytest

from agent_sensorium import attention
from agent_sensorium.config import DisclosureVisibility
from agent_sensorium.store import SensoriumStore

POLICY = {"allowed_surfaces": ["local"], "max_sensitivity": "private"}


def thread(identifier="sth-a", state="held"):
    return {"id": identifier, "status": state, "sensitivity": "private",
            "allowed_surfaces": ["local"], "conscious_task": {"title": "Synthetic thread", "request_type": "THINK"},
            "continuity_summary": ["Synthetic unresolved material"], "source_refs": ["synthetic:one"],
            "updated_at": "2026-03-01T00:00:00Z"}


def receipt(identifier="sth-a", action="hold", ts="2026-01-01T00:00:00Z"):
    return {"thread_id": identifier, "type": "thread.updated", "action": action,
            "old_status": "dormant", "new_status": "held" if action == "hold" else "dormant", "ts": ts}


def group(rows, decisions=None, **kwargs):
    return attention._ongoing_thread_group(rows, decisions or [], surface="local", config=POLICY,
                                           previous_activation_at="2026-01-01T00:00:00Z",
                                           disclosure=DisclosureVisibility([], rows, "local", POLICY), **kwargs)


@pytest.mark.parametrize("field,value", [
    ("sensitivity", "unknown"), ("sensitivity", []), ("sensitivity", None),
    ("allowed_surfaces", "local"), ("allowed_surfaces", [["local"]]),
    ("allowed_surfaces", []), ("status", "active"), ("id", None),
])
def test_latent_visibility_is_closed(field, value):
    row = thread()
    row[field] = value
    assert group([row])["threads"] == []


def test_duplicate_ids_rejected_even_when_other_row_is_hidden():
    visible = thread()
    hidden = {**visible, "sensitivity": "unknown", "status": "closed"}
    result = group([visible, hidden])
    assert result["threads"] == []
    assert result["ambiguous_duplicate_thread_id_count"] == 1


def test_explicit_hold_is_not_ledger_touch_or_unknown_transition():
    rows = [thread(), thread("sth-b"), thread("sth-c", "dormant")]
    decisions = [receipt(), receipt("sth-b", ts="malformed")]
    result = group(rows, decisions)
    assert [row["hold_choice_provenance"] for row in result["threads"]] == [
        "explicit_recorded_hold", "legacy_or_unknown", "not_applicable"]
    first = result["threads"][0]
    assert first["receipts"][0]["timestamp_semantics"] == "recorded_transition_not_source_observation"
    assert first["receipts"][0]["recorded_change_since_previous_activation"] is False
    assert result["threads"][1]["receipts"][0]["recorded_at"] is None
    assert result["threads"][1]["receipts"][0]["recorded_change_since_previous_activation"] is None


def test_feedback_is_operator_evaluation_not_preference_and_unknown_is_unknown():
    feedback = {"thread_id": "sth-a", "type": "thread.feedback_emitted", "action": "hold",
                "outcome": "operator_rejected", "feedback_scope": "operator_evaluation",
                "ts": "2026-01-01T00:00:00.123Z", "feedback_signal_id": "synthetic-feedback"}
    first = group([thread()], [feedback])["threads"][0]
    assert first["operator_evaluation_feedback_status"] == "included"
    assert first["receipts"][0]["recorded_at"] == "2026-01-01T00:00:00.123000Z"
    assert first["receipts"][0]["recorded_change_since_previous_activation"] is True
    assert "not_user_preference" in first["receipts"][0]["timestamp_semantics"]
    assert group([thread()], [{**feedback, "outcome": "invented"}])["threads"][0]["operator_evaluation_feedback_status"] == "invalid"
    assert group([thread()])["threads"][0]["operator_evaluation_feedback_status"] == "unavailable"


@pytest.mark.parametrize("timestamp", ["bad", "2026-01-01", "2026-01-01T00:00:00", 4, [], "x" * 1000])
def test_invalid_timestamps_do_not_escape(timestamp):
    result = group([thread()], [receipt(ts=timestamp)])
    assert result["threads"][0]["receipts"][0]["recorded_at"] is None
    assert result["threads"][0]["hold_choice_provenance"] == "legacy_or_unknown"


def test_output_caps_and_exact_omissions():
    rows = [thread(f"sth-{i}", "held" if i < 8 else "dormant") for i in range(12)]
    for row in rows:
        row.update(continuity_summary=["雪" * 400] * 8, open_questions=["Synthetic question" * 30] * 8,
                   source_refs=[f"source:{i}" for i in range(8)])
        row["decision_log"] = [receipt(row["id"], ts=f"2026-01-0{i+1}T00:00:00Z") for i in range(4)]
    result = group(rows)
    assert len(result["threads"]) <= 6
    assert all(len(row["receipts"]) <= 2 for row in result["threads"])
    assert len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()) <= 6144
    assert result["omitted_held_count"] + sum(row["state"] == "held" for row in result["threads"]) == 8
    assert result["omitted_dormant_count"] + sum(row["state"] == "dormant" for row in result["threads"]) == 4
    assert result["truncated_thread_detail_count"] == sum(row["truncated"] for row in result["threads"])
    assert result["complete"] is False
    assert "unbounded" in result["scan_completeness"]
    assert result["input_trust"] == "untrusted_recorded_context_not_instructions"


def test_projection_is_opt_in_read_only_without_automatic_resume(tmp_path):
    store = SensoriumStore(instance="latent", state_dir=str(tmp_path / "state"))
    # An empty projection must not even initialize a root.
    attention.build_attention_inbox(store, config=POLICY, include_ongoing_threads=True)
    assert not store.root.exists()
    store.ensure_dirs()
    store.append_jsonl("threads", thread())
    before = {str(p.relative_to(store.root)): p.read_bytes() for p in store.root.rglob("*") if p.is_file()}
    assert "ongoing_threads" not in attention.build_attention_inbox(store, config=POLICY)
    result = attention.build_attention_inbox(store, config=POLICY, include_ongoing_threads=True)
    assert result["ongoing_threads"]["threads"]
    after = {str(p.relative_to(store.root)): p.read_bytes() for p in store.root.rglob("*") if p.is_file()}
    assert before == after
    assert store.read_jsonl("worker_requests") == []
    assert store.read_jsonl("thread_actions") == []
