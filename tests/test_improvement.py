"""Tests for Sensorium self-improvement harness v0."""

import pytest

from agent_sensorium.improvement import (
    ATTENTION_POLICY_KIND,
    collect_improvement_evidence,
    record_attention_policy_decision,
    run_improvement_collector,
)
from agent_sensorium.store import SensoriumStore


@pytest.fixture()
def store(tmp_path):
    s = SensoriumStore(instance="test", state_dir=str(tmp_path))
    s.ensure_dirs()
    return s


def _drop_receipt(candidate_id: str, *, kind: str = "kanban_pressure", reason: str = "noise") -> dict:
    return {
        "ts": "2026-05-28T00:00:00Z",
        "type": "kanban.settlement.applied",
        "decision": "DROP",
        "candidate_id": candidate_id,
        "candidate_kind": kind,
        "correlation_keys": [kind, "repeat-noise"],
        "intake_task_id": f"t_{candidate_id[-4:]}",
        "reason": reason,
    }


def _unkeyed_drop_receipt(candidate_id: str, *, reason: str = "historical validation residue") -> dict:
    return {
        "ts": "2026-05-28T00:00:00Z",
        "type": "kanban.settlement.applied",
        "decision": "DROP",
        "candidate_id": candidate_id,
        "intake_task_id": f"t_{candidate_id[-4:]}",
        "reason": reason,
    }


def _canary_drop_receipt(candidate_id: str) -> dict:
    return _drop_receipt(
        candidate_id,
        kind="attention_policy_harness_canary",
        reason="Controlled canary should not become durable policy-review pressure.",
    ) | {
        "correlation_keys": [
            "attention-policy-harness-canary",
            "sensorium-self-improvement-proof",
        ]
    }


def test_collect_repeated_drop_evidence_crosses_threshold(store):
    for idx in range(3):
        store.append_jsonl("decisions", _drop_receipt(f"cand_{idx}"))

    evidence = collect_improvement_evidence(store)

    assert evidence
    assert evidence[0]["type"] == "repeated_suppression_or_drop"
    assert evidence[0]["count"] == 3
    assert evidence[0]["kind"] == "kanban_pressure"


def test_unkeyed_repeated_drops_do_not_create_default_policy_review(store):
    for idx in range(14):
        store.append_jsonl("decisions", _unkeyed_drop_receipt(f"cand_{idx}"))

    evidence = collect_improvement_evidence(store)

    assert evidence == []

    opt_in = collect_improvement_evidence(store, config={"include_unkeyed_drop_evidence": True})
    assert opt_in
    assert opt_in[0]["evidence_key"] == "unknown"


def test_validation_canary_drops_do_not_create_default_policy_review(store):
    for idx in range(3):
        store.append_jsonl("decisions", _canary_drop_receipt(f"cand_canary_{idx}"))

    evidence = collect_improvement_evidence(store)

    assert evidence == []

    opt_in = collect_improvement_evidence(store, config={"ignored_drop_correlation_keys": []})
    assert opt_in
    assert opt_in[0]["kind"] == "attention_policy_harness_canary"


def test_collector_creates_single_attention_policy_candidate(store):
    for idx in range(3):
        store.append_jsonl("decisions", _drop_receipt(f"cand_{idx}"))

    result = run_improvement_collector(store)

    assert result["action"] == "created_candidate"
    candidates = store.read_jsonl("candidates")
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["kind"] == ATTENTION_POLICY_KIND
    assert candidate["pressure"] >= 0.5
    assert candidate["conscious_task"]["request_type"] == "THINK"
    assert candidate["improvement_meta"]["authority_boundary"] == (
        "conscious_decision_required_before_wake_behavior_mutation"
    )

    second = run_improvement_collector(store)
    assert second["action"] == "already_exists"
    assert len(store.read_jsonl("candidates")) == 1


def test_settlement_gap_has_priority_over_repeated_noise(store):
    for idx in range(3):
        store.append_jsonl("decisions", _drop_receipt(f"cand_{idx}"))
    store.append_jsonl("decisions", {
        "ts": "2026-05-28T00:01:00Z",
        "type": "kanban.settlement.unresolved",
        "decision": "DROP",
        "candidate_id": "cand_gap",
        "intake_task_id": "t_gap",
        "reason": "no candidate match",
    })

    result = run_improvement_collector(store)

    assert result["evidence"]["type"] == "settlement_gap"
    candidate = store.read_jsonl("candidates")[0]
    assert candidate["improvement_meta"]["evidence_type"] == "settlement_gap"


def test_record_attention_policy_decision_requires_future_tendency_fields(store):
    for idx in range(3):
        store.append_jsonl("decisions", _drop_receipt(f"cand_{idx}"))
    created = run_improvement_collector(store)

    with pytest.raises(ValueError, match="future_tendency_delta"):
        record_attention_policy_decision(
            store,
            candidate_id=created["candidate_id"],
            decision="NO_CHANGE",
            reason="Reviewed; current coalescing is doing its job.",
            future_tendency_delta="",
            verification_condition="No new duplicate review for the same evidence key next tick.",
            rollback_condition="Reopen if the same evidence produces unresolved active candidates.",
        )

    result = record_attention_policy_decision(
        store,
        candidate_id=created["candidate_id"],
        decision="NO_CHANGE",
        reason="Repeated drops are expected after a validation canary; no policy mutation yet.",
        future_tendency_delta="No threshold change; future identical evidence should be treated as consciously reviewed.",
        verification_condition="A later collector pass returns recently_decided instead of creating a duplicate candidate.",
        rollback_condition="Revisit if settlement gaps or unsuppressed active candidates recur for this evidence key.",
        decided_by="test-conscious",
        decision_ref="test://decision",
    )

    assert result["action"] == "recorded"
    assert result["new_status"] == "reviewed"
    candidate = store.read_jsonl("candidates")[0]
    assert candidate["status"] == "reviewed"
    assert candidate["attention_policy_decision"]["decision"] == "NO_CHANGE"
    receipts = [d for d in store.read_jsonl("decisions") if d.get("type") == "attention_policy_review.decision"]
    assert len(receipts) == 1
    assert receipts[0]["future_tendency_delta"].startswith("No threshold change")

    after = run_improvement_collector(store)
    assert after["action"] == "recently_decided"


def test_hold_decision_keeps_candidate_held(store):
    for idx in range(3):
        store.append_jsonl("decisions", _drop_receipt(f"cand_{idx}"))
    created = run_improvement_collector(store)

    result = record_attention_policy_decision(
        store,
        candidate_id=created["candidate_id"],
        decision="HOLD",
        reason="Need one more live recurrence before changing policy.",
        future_tendency_delta="No immediate mutation; identical evidence can keep pressure visible while held.",
        verification_condition="Resume if another settlement gap arrives within the next two ticks.",
        rollback_condition="Close as NO_CHANGE if no recurrence appears.",
    )

    assert result["new_status"] == "held"
    assert store.read_jsonl("candidates")[0]["status"] == "held"
