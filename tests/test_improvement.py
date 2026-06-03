"""Tests for Sensorium self-improvement harness v0."""

import pytest

from agent_sensorium.improvement import (
    ATTENTION_POLICY_KIND,
    collect_improvement_evidence,
    record_attention_policy_decision,
    run_improvement_collector,
)
from agent_sensorium.actuator_contracts import (
    MEMORY_GROUNDED_REVIEW_CONTRACT_TYPE,
    memory_grounded_review_contract,
    requires_memory_grounding,
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


def _memory_fact(idx: int, *, affected: bool = False) -> dict:
    return {
        "fact_ref": f"mem_{idx}",
        "fact": f"Memory fact {idx}: Operator wants feedback tendency grounded in lived context, not scalar tuning.",
        "source_tool": "hindsight_recall",
        "source_refs": [f"hindsight://memory/{idx}"],
        "query": "feedback tendency memory grounding",
        "affected_choice": affected,
    }


def _manual_attention_candidate(store, *, memory_required: bool = True) -> str:
    candidate = {
        "id": "cand_memory_contract" if memory_required else "cand_operational",
        "status": "candidate",
        "kind": ATTENTION_POLICY_KIND,
        "pressure": 0.75,
        "summary": (
            "Explicit correction: feedback tendency must be memory grounded, not scalar."
            if memory_required else
            "Operational kanban pressure repeated drops should stay ordinary policy review."
        ),
        "correlation_keys": (
            ["explicit-correction", "memory-grounding", "feedback-tendency"]
            if memory_required else
            ["kanban_pressure", "repeat-noise"]
        ),
        "sensitivity": "private",
        "allowed_surfaces": ["local", "discord"],
        "improvement_meta": {"evidence_key": "manual-memory" if memory_required else "kanban_pressure:repeat-noise"},
    }
    if memory_required:
        candidate["conscious_review_contract"] = memory_grounded_review_contract(candidate)
    store.append_jsonl("candidates", candidate)
    return candidate["id"]


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


def test_attention_policy_rule_controls_repeated_drop_threshold(store):
    for idx in range(3):
        store.append_jsonl("decisions", _drop_receipt(f"cand_{idx}"))

    evidence = collect_improvement_evidence(
        store,
        config={
            "attention_policy": {
                "evidence_rules": {
                    "repeated_suppression_or_drop": {"min_count": 4}
                }
            }
        },
    )

    assert evidence == []


def test_attention_policy_rule_controls_actionable_key_requirement(store):
    for idx in range(3):
        store.append_jsonl("decisions", _unkeyed_drop_receipt(f"cand_unkeyed_{idx}"))

    evidence = collect_improvement_evidence(
        store,
        config={
            "attention_policy": {
                "evidence_rules": {
                    "repeated_suppression_or_drop": {"require_actionable_key": False}
                }
            }
        },
    )

    assert evidence
    assert evidence[0]["evidence_key"] == "unknown"
    assert evidence[0]["policy_rule"]["require_actionable_key"] is False


def test_attention_policy_rule_controls_ignored_correlation_keys(store):
    for idx in range(3):
        store.append_jsonl("decisions", _canary_drop_receipt(f"cand_canary_{idx}"))

    evidence = collect_improvement_evidence(
        store,
        config={
            "attention_policy": {
                "evidence_rules": {
                    "repeated_suppression_or_drop": {"ignore_correlation_keys": []}
                }
            }
        },
    )

    assert evidence
    assert evidence[0]["kind"] == "attention_policy_harness_canary"
    assert evidence[0]["policy_rule"]["ignore_correlation_keys"] == []


def test_attention_policy_rule_takes_precedence_over_legacy_knobs(store):
    for idx in range(3):
        store.append_jsonl("decisions", _drop_receipt(f"cand_{idx}"))

    evidence = collect_improvement_evidence(
        store,
        config={
            "repeated_drop_threshold": 3,
            "attention_policy": {
                "evidence_rules": {
                    "repeated_suppression_or_drop": {"min_count": 4}
                }
            },
        },
    )

    assert evidence == []


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


def test_memory_grounded_contract_triggers_for_relational_identity_and_correction_rows():
    assert requires_memory_grounding({"kind": "relational_salience", "summary": "I miss you"})
    assert requires_memory_grounding({"kind": "identity", "summary": "demo embodiment insight"})
    assert requires_memory_grounding({"kind": "explicit_correction", "summary": "that's wrong"})
    assert not requires_memory_grounding({"kind": "kanban_pressure", "correlation_keys": ["repeat-noise"]})

    contract = memory_grounded_review_contract({
        "kind": "explicit_correction",
        "summary": "feedback tendency must be memory/context grounded",
        "correlation_keys": ["feedback-tendency"],
    })
    assert contract["type"] == MEMORY_GROUNDED_REVIEW_CONTRACT_TYPE
    assert contract["retrieval_contract"]["default_tool"] == "hindsight_recall"
    assert contract["retrieval_contract"]["min_facts"] == 3
    assert contract["retrieval_contract"]["max_facts"] == 8


def test_memory_grounded_decision_requires_retrieval_or_skip_reason(store):
    candidate_id = _manual_attention_candidate(store, memory_required=True)

    with pytest.raises(ValueError, match="memory-grounded Conscious review"):
        record_attention_policy_decision(
            store,
            candidate_id=candidate_id,
            decision="NO_CHANGE",
            reason="Cannot decide without memory grounding.",
            future_tendency_delta="No change until retrieval exists.",
            verification_condition="A later review includes memory grounding.",
            rollback_condition="Retry if the same candidate stays open.",
        )

    facts = [_memory_fact(1, affected=True), _memory_fact(2), _memory_fact(3)]
    result = record_attention_policy_decision(
        store,
        candidate_id=candidate_id,
        decision="NO_CHANGE",
        reason="mem_1 grounds the correction: this is not a threshold-only issue.",
        future_tendency_delta="Future explicit corrections should request bounded recall before policy decisions.",
        verification_condition="Next memory-grounded candidate receipt includes fact refs or skip reason.",
        rollback_condition="Revisit if receipts again omit memory context.",
        memory_context=facts,
        cited_memory_fact_refs=["mem_1"],
    )

    grounding = result["receipt"]["memory_grounding"]
    assert grounding["retrieval_required"] is True
    assert grounding["retrieval_skipped"] is False
    assert grounding["source_tools"] == ["hindsight_recall"]
    assert grounding["fact_count"] == 3
    assert grounding["cited_memory_fact_refs"] == ["mem_1"]
    assert "raw" not in str(grounding).lower()


def test_memory_grounded_decision_can_record_retrieval_skipped_reason(store):
    candidate_id = _manual_attention_candidate(store, memory_required=True)

    result = record_attention_policy_decision(
        store,
        candidate_id=candidate_id,
        decision="HOLD",
        reason="Retrieval unavailable; hold instead of making scalar tuning claims.",
        future_tendency_delta="Hold similar corrections when recall cannot be reached rather than converting them into threshold tweaks.",
        verification_condition="Retry when Hindsight recall is available.",
        rollback_condition="Close as NO_CHANGE if no recurrence appears after recall returns.",
        retrieval_skipped_reason="hindsight_recall unavailable in this bounded smoke run",
    )

    grounding = result["receipt"]["memory_grounding"]
    assert grounding["retrieval_skipped"] is True
    assert grounding["retrieval_skipped_reason"].startswith("hindsight_recall unavailable")
    assert grounding["facts"] == []


def test_operational_candidate_not_forced_through_memory_retrieval(store):
    candidate_id = _manual_attention_candidate(store, memory_required=False)

    result = record_attention_policy_decision(
        store,
        candidate_id=candidate_id,
        decision="NO_CHANGE",
        reason="Ordinary operational pressure can be reviewed from bounded evidence only.",
        future_tendency_delta="No memory retrieval requirement for generic kanban_pressure noise.",
        verification_condition="Future operational reviews still complete without memory_context.",
        rollback_condition="Revisit if a correction/identity/mediated-presence key appears.",
    )

    assert "memory_grounding" not in result["receipt"]
    candidate = store.read_jsonl("candidates")[0]
    assert candidate["sensitivity"] == "private"
    assert candidate["allowed_surfaces"] == ["local", "discord"]


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
