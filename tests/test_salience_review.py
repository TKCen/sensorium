"""Tests for salience review loop additions.

Covers:
  - _SALIENCE_REVIEW_PROMPT (agent_sensorium.improvement)
  - build_salience_review_context (agent_sensorium.improvement)
  - parse_salience_review_decision (agent_sensorium.improvement)
  - apply_salience_review_decision (agent_sensorium.improvement)
  - SALIENCE_REVIEW_ENABLED, _MISSING_SEAMS, run_bounded_salience_review_session
    (agent_sensorium.salience_review)
"""

import json
import re

import pytest

from agent_sensorium.improvement import (
    VALID_ATTENTION_DECISIONS,
    _SALIENCE_REVIEW_PROMPT,
    apply_salience_review_decision,
    build_salience_review_context,
    collect_improvement_evidence,
    parse_salience_review_decision,
    run_improvement_collector,
)
from agent_sensorium.salience_review import (
    SALIENCE_REVIEW_ENABLED,
    _MISSING_SEAMS,
    run_bounded_salience_review_session,
)
from agent_sensorium.store import SensoriumStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


def _valid_decision_payload(**overrides) -> dict:
    base = {
        "decision": "NO_CHANGE",
        "reason": "Reviewed; no policy change warranted.",
        "future_tendency_delta": "No threshold change expected.",
        "verification_condition": "Collector returns recently_decided on next tick.",
        "rollback_condition": "Reopen if settlement gaps recur.",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_store_with_candidate(store) -> str:
    """Populate store with 3 drops and run collector; return candidate_id."""
    for idx in range(3):
        store.append_jsonl("decisions", _drop_receipt(f"cand_{idx}"))
    result = run_improvement_collector(store)
    return result["candidate_id"]


# ---------------------------------------------------------------------------
# Prompt tests
# ---------------------------------------------------------------------------


def test_salience_review_prompt_contains_all_valid_decisions():
    for decision_name in VALID_ATTENTION_DECISIONS:
        assert decision_name in _SALIENCE_REVIEW_PROMPT, (
            f"Expected decision '{decision_name}' to appear in _SALIENCE_REVIEW_PROMPT"
        )


def test_salience_review_prompt_contains_required_fields():
    for field in ("future_tendency_delta", "verification_condition", "rollback_condition"):
        assert field in _SALIENCE_REVIEW_PROMPT, (
            f"Expected field '{field}' to appear in _SALIENCE_REVIEW_PROMPT"
        )


def test_salience_review_prompt_forbids_unsafe_actions():
    prompt_lower = _SALIENCE_REVIEW_PROMPT.lower()
    assert "soul" in prompt_lower, "Expected 'SOUL' safety guardrail reference in prompt"
    assert "code" in prompt_lower, "Expected 'code' safety guardrail reference in prompt"


# ---------------------------------------------------------------------------
# Context builder tests
# ---------------------------------------------------------------------------


def test_build_salience_review_context_is_json_serializable(store):
    for idx in range(3):
        store.append_jsonl("decisions", _drop_receipt(f"cand_{idx}"))
    run_improvement_collector(store)
    evidence = collect_improvement_evidence(store)
    ctx = build_salience_review_context(store, evidence)
    # Must not raise
    json.dumps(ctx)


def test_build_salience_review_context_includes_required_keys(store):
    for idx in range(3):
        store.append_jsonl("decisions", _drop_receipt(f"cand_{idx}"))
    run_improvement_collector(store)
    evidence = collect_improvement_evidence(store)
    ctx = build_salience_review_context(store, evidence)
    required_keys = {
        "evidence",
        "recent_attention_decisions",
        "candidate_counts",
        "open_candidates",
        "policy_config_snapshot",
        "constraints",
    }
    for key in required_keys:
        assert key in ctx, f"Expected key '{key}' in build_salience_review_context result"


def test_build_salience_review_context_constraints_has_valid_decisions(store):
    evidence = collect_improvement_evidence(store)
    ctx = build_salience_review_context(store, evidence)
    constraints = ctx["constraints"]
    assert constraints["valid_decisions"] == sorted(VALID_ATTENTION_DECISIONS)


def test_build_salience_review_context_forbidden_list_in_constraints(store):
    evidence = collect_improvement_evidence(store)
    ctx = build_salience_review_context(store, evidence)
    forbidden = ctx["constraints"]["forbidden"]
    assert "edit_python_source" in forbidden
    assert "patch_soul" in forbidden
    assert "broaden_privacy" in forbidden


def test_build_salience_review_context_respects_max_decisions(store):
    # Add 30 attention_policy_review.decision type receipts
    for idx in range(30):
        store.append_jsonl("decisions", {
            "ts": f"2026-05-28T00:{idx:02d}:00Z",
            "type": "attention_policy_review.decision",
            "decision": "NO_CHANGE",
            "reason": f"Review {idx}",
            "future_tendency_delta": "No change.",
            "candidate_id": f"cand_{idx:04d}",
        })
    evidence = collect_improvement_evidence(store)
    ctx = build_salience_review_context(store, evidence, max_decisions=5)
    assert len(ctx["recent_attention_decisions"]) <= 5


def test_build_salience_review_context_decisions_have_bounded_fields(store):
    for idx in range(5):
        store.append_jsonl("decisions", {
            "ts": f"2026-05-28T00:{idx:02d}:00Z",
            "type": "attention_policy_review.decision",
            "decision": "NO_CHANGE",
            "reason": f"Review {idx}",
            "future_tendency_delta": "No change.",
            "candidate_id": f"cand_{idx:04d}",
            "extra_field": "should_not_leak",
        })
    evidence = collect_improvement_evidence(store)
    ctx = build_salience_review_context(store, evidence)
    allowed_fields = {"ts", "type", "decision", "reason", "future_tendency_delta"}
    for decision_entry in ctx["recent_attention_decisions"]:
        extra = set(decision_entry.keys()) - allowed_fields
        assert not extra, f"Unexpected fields in recent_attention_decisions entry: {extra}"


def test_build_salience_review_context_no_raw_transcript_or_logs(store):
    for idx in range(3):
        store.append_jsonl("decisions", _drop_receipt(f"cand_{idx}"))
    run_improvement_collector(store)
    evidence = collect_improvement_evidence(store)
    ctx = build_salience_review_context(store, evidence)
    serialized = str(ctx).lower()
    assert "transcript" not in serialized
    assert "raw_log" not in serialized


# ---------------------------------------------------------------------------
# Decision parser tests
# ---------------------------------------------------------------------------


def test_parse_salience_review_decision_accepts_valid_payload():
    payload = _valid_decision_payload()
    result = parse_salience_review_decision(payload)
    assert result["decision"] == "NO_CHANGE"
    assert result["reason"] == payload["reason"]
    assert result["future_tendency_delta"] == payload["future_tendency_delta"]
    assert result["verification_condition"] == payload["verification_condition"]
    assert result["rollback_condition"] == payload["rollback_condition"]


def test_parse_salience_review_decision_normalizes_decision_to_upper():
    payload = _valid_decision_payload(decision="no_change")
    result = parse_salience_review_decision(payload)
    assert result["decision"] == "NO_CHANGE"


def test_parse_salience_review_decision_rejects_invalid_decision():
    payload = _valid_decision_payload(decision="INVALID_DECISION")
    with pytest.raises(ValueError):
        parse_salience_review_decision(payload)


def test_parse_salience_review_decision_rejects_empty_reason():
    payload = _valid_decision_payload(reason="")
    with pytest.raises(ValueError, match="reason"):
        parse_salience_review_decision(payload)


def test_parse_salience_review_decision_rejects_empty_future_tendency_delta():
    payload = _valid_decision_payload(future_tendency_delta="  ")
    with pytest.raises(ValueError, match="future_tendency_delta"):
        parse_salience_review_decision(payload)


def test_parse_salience_review_decision_rejects_multiple_missing_fields():
    payload = _valid_decision_payload()
    del payload["future_tendency_delta"]
    del payload["verification_condition"]
    with pytest.raises(ValueError):
        parse_salience_review_decision(payload)


def test_parse_salience_review_decision_ignores_extra_fields():
    payload = _valid_decision_payload()
    payload["injected_code"] = "malicious()"
    result = parse_salience_review_decision(payload)
    assert "injected_code" not in result


def test_parse_salience_review_decision_default_decided_by():
    payload = _valid_decision_payload()
    # Ensure decided_by is absent from input
    payload.pop("decided_by", None)
    result = parse_salience_review_decision(payload)
    assert result["decided_by"] == "salience-review"


# ---------------------------------------------------------------------------
# Apply seam tests
# ---------------------------------------------------------------------------


def test_apply_salience_review_decision_writes_exactly_one_receipt(store):
    candidate_id = _setup_store_with_candidate(store)
    payload = _valid_decision_payload()
    apply_salience_review_decision(store, candidate_id, payload)
    receipts = [
        d for d in store.read_jsonl("decisions")
        if d.get("type") == "attention_policy_review.decision"
    ]
    assert len(receipts) == 1


def test_apply_salience_review_decision_transitions_candidate_to_reviewed(store):
    candidate_id = _setup_store_with_candidate(store)
    payload = _valid_decision_payload(decision="NO_CHANGE")
    apply_salience_review_decision(store, candidate_id, payload)
    candidates = store.read_jsonl("candidates")
    target = next(c for c in candidates if c["id"] == candidate_id)
    assert target["status"] == "reviewed"


def test_apply_salience_review_decision_hold_keeps_candidate_held(store):
    candidate_id = _setup_store_with_candidate(store)
    payload = _valid_decision_payload(decision="HOLD")
    apply_salience_review_decision(store, candidate_id, payload)
    candidates = store.read_jsonl("candidates")
    target = next(c for c in candidates if c["id"] == candidate_id)
    assert target["status"] == "held"


def test_apply_salience_review_decision_receipt_has_future_tendency_and_rollback(store):
    candidate_id = _setup_store_with_candidate(store)
    payload = _valid_decision_payload()
    apply_salience_review_decision(store, candidate_id, payload)
    receipts = [
        d for d in store.read_jsonl("decisions")
        if d.get("type") == "attention_policy_review.decision"
    ]
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt.get("future_tendency_delta", "").strip()
    assert receipt.get("rollback_condition", "").strip()


def test_apply_salience_review_decision_sets_decided_by(store):
    candidate_id = _setup_store_with_candidate(store)
    payload = _valid_decision_payload(decided_by="test-salience-runner")
    apply_salience_review_decision(store, candidate_id, payload, decided_by="test-salience-runner")
    receipts = [
        d for d in store.read_jsonl("decisions")
        if d.get("type") == "attention_policy_review.decision"
    ]
    assert receipts[0]["decided_by"] == "test-salience-runner"


def test_apply_salience_review_decision_rejects_invalid_decision_payload(store):
    candidate_id = _setup_store_with_candidate(store)
    payload = _valid_decision_payload(decision="BOGUS")
    receipts_before = [
        d for d in store.read_jsonl("decisions")
        if d.get("type") == "attention_policy_review.decision"
    ]
    with pytest.raises(ValueError):
        apply_salience_review_decision(store, candidate_id, payload)
    receipts_after = [
        d for d in store.read_jsonl("decisions")
        if d.get("type") == "attention_policy_review.decision"
    ]
    assert len(receipts_after) == len(receipts_before)


# ---------------------------------------------------------------------------
# Runner stub tests
# ---------------------------------------------------------------------------


def test_salience_review_enabled_is_false_by_default():
    assert SALIENCE_REVIEW_ENABLED is False


def test_run_bounded_salience_review_returns_disabled_when_disabled(store):
    result = run_bounded_salience_review_session(
        store, "cand_test_0001", {}, dry_run=True
    )
    assert result["status"] == "disabled"


def test_run_bounded_salience_review_missing_seams_is_nonempty():
    assert len(_MISSING_SEAMS) > 0


def test_salience_review_authority_bound_no_unsafe_imports():
    import pathlib
    src_path = pathlib.Path(__file__).parent.parent / "agent_sensorium" / "salience_review.py"
    source = src_path.read_text()
    # Check for actual dangerous usage patterns (imports/calls), not incidental
    # mention of these terms in comments or string constants.
    forbidden_patterns = [
        "import subprocess",
        "os.system(",
        "eval(",
        "exec(",
        "import socket",
    ]
    for pattern in forbidden_patterns:
        assert pattern not in source, (
            f"Unsafe pattern '{pattern}' found in salience_review.py"
        )
