"""Tests for memory-grounded Conscious review contracts.

Covers:
  - requires_memory_grounding (actuator_contracts)
  - memory_grounded_review_contract (actuator_contracts)
  - attention_policy_review_candidate attaches contract for memory-grounded evidence
    (agent_sensorium.improvement)
  - _compact_event_body embeds contract for memory-grounded events
    (live-scripts/sensorium_kanban_sensor_tick)
  - _candidate_intake_body embeds contract for memory-grounded candidates
    (live-scripts/sensorium_kanban_sensor_tick)
"""

from __future__ import annotations

import json

import pytest

from agent_sensorium.actuator_contracts import (
    MEMORY_GROUNDED_REVIEW_CONTRACT_TYPE,
    memory_grounded_review_contract,
    requires_memory_grounding,
)
from agent_sensorium.improvement import (
    attention_policy_review_candidate,
    run_improvement_collector,
)
from agent_sensorium.store import SensoriumStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path):
    s = SensoriumStore(instance="test-memory-grounding", state_dir=str(tmp_path))
    s.ensure_dirs()
    return s


# ---------------------------------------------------------------------------
# requires_memory_grounding — positive cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "row",
    [
        # Kind markers
        {"kind": "explicit_correction", "summary": "some summary"},
        {"kind": "sensorium_strategy_insight", "summary": "some summary"},
        {"kind": "embodiment_insight", "summary": "some summary"},
        # Correlation key markers
        {"kind": "kanban_pressure", "summary": "some summary", "correlation_keys": ["feedback-tendency"]},
        {"kind": "kanban_pressure", "summary": "some summary", "correlation_keys": ["feedback_tendency"]},
        {"kind": "attention_policy_review", "summary": "some summary", "correlation_keys": ["mediated-presence"]},
        {"kind": "attention_policy_review", "summary": "some summary", "correlation_keys": ["memory-grounding"]},
        {"kind": "attention_policy_review", "summary": "some summary", "correlation_keys": ["strategy-insight"]},
        {"kind": "attention_policy_review", "summary": "some summary", "correlation_keys": ["embodiment-insight"]},
        # Summary markers
        {"kind": "kanban_pressure", "summary": "explicit correction"},
        {"kind": "kanban_pressure", "summary": "feedback tendency"},
        {"kind": "kanban_pressure", "summary": "feedback-tendency"},
        {"kind": "kanban_pressure", "summary": "strategy insight"},
        {"kind": "kanban_pressure", "summary": "embodiment insight"},
        {"kind": "kanban_pressure", "summary": "memory grounding"},
        {"kind": "kanban_pressure", "summary": "memory-grounding"},
    ],
)
def test_requires_memory_grounding_positive(row):
    assert requires_memory_grounding(row) is True


# ---------------------------------------------------------------------------
# requires_memory_grounding — negative cases (generic candidates / unrelated rows)
# ---------------------------------------------------------------------------


def test_requires_memory_grounding_positive_for_mediated_presence_relational():
    # mediated-presence is an explicit memory-grounding key marker
    row = {
        "kind": "relational_thread_pickup_request",
        "summary": "Operator said this work matters to them",
        "correlation_keys": ["mediated-presence"],
    }
    assert requires_memory_grounding(row) is True


@pytest.mark.parametrize(
    "row",
    [
        # Generic attention-policy candidate (no memory-grounding markers)
        {"kind": "attention_policy_review", "summary": "repeated suppression evidence"},
        # Kanban pressure (generic)
        {"kind": "kanban_pressure", "summary": "blocked tasks rising"},
        # None / non-dict
        None,
        {},
    ],
)
def test_requires_memory_grounding_negative(row):
    assert requires_memory_grounding(row) is False


# ---------------------------------------------------------------------------
# memory_grounded_review_contract — structure
# ---------------------------------------------------------------------------


def test_memory_grounded_review_contract_type():
    assert MEMORY_GROUNDED_REVIEW_CONTRACT_TYPE == "memory_grounded_conscious_review"


def test_memory_grounded_review_contract_returns_correct_structure():
    row = {
        "kind": "explicit_correction",
        "summary": "operator correction: stop using numeric threshold for feedback tendency",
        "correlation_keys": ["feedback-tendency", "explicit-correction"],
    }
    contract = memory_grounded_review_contract(row)

    assert contract is not None
    assert contract["type"] == MEMORY_GROUNDED_REVIEW_CONTRACT_TYPE
    assert contract["source_kind"] == "explicit_correction"
    assert "correction" in contract["source_summary"].lower()
    assert "required_conscious_decision" in contract
    assert set(contract["required_conscious_decision"]).issubset({
        "NO_CHANGE",
        "THRESHOLD_COALESCING_TWEAK_PROPOSAL",
        "SENSOR_ADDITION_TASK",
        "PRIORITY_MAP_CHANGE",
        "MEMORY_SKILL_PATCH",
        "HOLD",
    })
    assert "required_receipt" in contract
    assert "retrieval_contract" in contract
    rc = contract["retrieval_contract"]
    assert rc["max_facts"] == 8
    assert rc["preferred_facts"] == 3
    assert rc["recall_mode"] == "bounded_recall"
    assert "candidate_summary" in rc["query_sources"]
    assert "forbidden" in rc
    assert "raw_transcript" in rc["forbidden"]
    assert "session_log" in rc["forbidden"]
    assert "secret" in rc["forbidden"]
    assert "raw_memory_dump" in rc["forbidden"]
    assert contract["outbound_delivery_authorized"] is False
    assert contract["no_live_delivery"] is True
    assert contract["allowed_tool"] is None


def test_memory_grounded_review_contract_returns_none_for_generic_candidate():
    row = {
        "kind": "attention_policy_review",
        "summary": "repeated suppression evidence",
        "correlation_keys": ["kanban-pressure"],
    }
    assert memory_grounded_review_contract(row) is None


def test_memory_grounded_review_contract_returns_contract_for_mediated_presence():
    # mediated-presence is a memory-grounding key marker
    row = {
        "kind": "relational_thread_pickup_request",
        "summary": "Operator said this work matters to them",
        "correlation_keys": ["mediated-presence"],
    }
    contract = memory_grounded_review_contract(row)
    # mediated-presence triggers memory-grounding; returns a contract
    assert contract is not None
    assert contract["type"] == MEMORY_GROUNDED_REVIEW_CONTRACT_TYPE


def test_memory_grounded_review_contract_returns_none_for_empty_row():
    assert memory_grounded_review_contract(None) is None
    assert memory_grounded_review_contract({}) is None


# ---------------------------------------------------------------------------
# attention_policy_review_candidate — contract attached for memory-grounded evidence
# ---------------------------------------------------------------------------


def _feedback_tendency_evidence() -> dict:
    """Evidence block simulating a feedback-tendency correction signal."""
    return {
        "type": "repeated_suppression_or_drop",
        "evidence_key": "explicit_correction:feedback-tendency",
        "count": 3,
        "kind": "explicit_correction",
        "correlation_keys": ["feedback-tendency", "explicit-correction"],
        "summary": "operator explicit correction: feedback tendency must not use numeric threshold",
        "policy_rule": {},
        "items": [],
        "suggested_review": "decide whether to hold or tune feedback-tendency handling",
    }


def _generic_repeated_drop_evidence() -> dict:
    """Generic repeated-drop evidence without memory-grounding markers."""
    return {
        "type": "repeated_suppression_or_drop",
        "evidence_key": "kanban_pressure:repeat-noise",
        "count": 4,
        "kind": "kanban_pressure",
        "correlation_keys": ["kanban-pressure", "repeat-noise"],
        "summary": "4 DROP/suppress receipts suggest noisy salience or threshold/coalescing issue",
        "policy_rule": {},
        "items": [],
        "suggested_review": "decide whether to tune threshold/coalescing or record NO_CHANGE",
    }


def test_attention_policy_review_candidate_gets_memory_contract_for_feedback_tendency(store):
    evidence = _feedback_tendency_evidence()
    candidate = attention_policy_review_candidate(evidence)

    assert "conscious_review_contract" in candidate
    contract = candidate["conscious_review_contract"]
    assert contract["type"] == MEMORY_GROUNDED_REVIEW_CONTRACT_TYPE
    assert "feedback tendency" in contract["source_summary"].lower()


def test_attention_policy_review_candidate_gets_memory_contract_for_strategy_insight(store):
    evidence = {
        "type": "repeated_suppression_or_drop",
        "evidence_key": "sensorium_strategy_insight",
        "count": 2,
        "kind": "sensorium_strategy_insight",
        "correlation_keys": ["strategy-insight"],
        "summary": "strategy insight: attention policy missed a recurring pattern",
        "policy_rule": {},
        "items": [],
        "suggested_review": "decide whether to add a sensor or adjust priority mapping",
    }
    candidate = attention_policy_review_candidate(evidence)

    assert "conscious_review_contract" in candidate
    contract = candidate["conscious_review_contract"]
    assert contract["type"] == MEMORY_GROUNDED_REVIEW_CONTRACT_TYPE


def test_attention_policy_review_candidate_gets_memory_contract_for_embodiment_insight(store):
    evidence = {
        "type": "repeated_suppression_or_drop",
        "evidence_key": "embodiment_insight",
        "count": 2,
        "kind": "embodiment_insight",
        "correlation_keys": ["embodiment-insight"],
        "summary": "embodiment insight: recurring missed embodied signal",
        "policy_rule": {},
        "items": [],
        "suggested_review": "decide whether embodiment salience mapping needs adjustment",
    }
    candidate = attention_policy_review_candidate(evidence)

    assert "conscious_review_contract" in candidate
    assert candidate["conscious_review_contract"]["type"] == MEMORY_GROUNDED_REVIEW_CONTRACT_TYPE


def test_attention_policy_review_candidate_no_contract_for_generic_evidence(store):
    evidence = _generic_repeated_drop_evidence()
    candidate = attention_policy_review_candidate(evidence)

    # Generic repeated-drop does not match memory-grounding markers
    assert candidate.get("conscious_review_contract") is None


def test_memory_contract_does_not_include_raw_transcript_or_secrets(store):
    evidence = _feedback_tendency_evidence()
    candidate = attention_policy_review_candidate(evidence)
    contract = candidate["conscious_review_contract"]

    # Scan only actual data fields for privacy-sensitive content.
    # The retrieval_contract.forbidden list contains constraint descriptors
    # (e.g. "raw_transcript") — those are legitimate policy metadata, not data.
    for field in ("source_kind", "source_summary", "why", "required_receipt"):
        value = str(contract.get(field) or "").lower()
        assert "transcript" not in value, f"Field '{field}' contains 'transcript'"
        assert "session_log" not in value, f"Field '{field}' contains 'session_log'"
        assert "secret" not in value, f"Field '{field}' contains 'secret'"
        assert "raw_memory_dump" not in value, f"Field '{field}' contains 'raw_memory_dump'"


def test_memory_contract_expected_decision_cites_memory_facts(store):
    evidence = _feedback_tendency_evidence()
    candidate = attention_policy_review_candidate(evidence)

    expected = candidate["conscious_task"]["expected_decision"]
    assert "cite" in expected.lower() or "memory fact" in expected.lower()


# ---------------------------------------------------------------------------
# Integration: collector creates candidate with memory-grounded contract
# ---------------------------------------------------------------------------


def test_run_improvement_collector_creates_candidate_with_memory_contract(store):
    # Simulate 3 explicit-correction DROP receipts
    for idx in range(3):
        store.append_jsonl("decisions", {
            "ts": "2026-05-28T00:00:00Z",
            "type": "kanban.settlement.applied",
            "decision": "DROP",
            "candidate_id": f"cand_{idx}",
            "candidate_kind": "explicit_correction",
            "correlation_keys": ["feedback-tendency", "explicit-correction"],
            "intake_task_id": f"t_{idx}",
            "reason": "operator explicit correction",
        })

    result = run_improvement_collector(store)
    assert result["created"] is True
    candidate_id = result["candidate_id"]

    candidates = store.read_jsonl("candidates")
    candidate = next(c for c in candidates if c["id"] == candidate_id)

    assert "conscious_review_contract" in candidate
    assert candidate["conscious_review_contract"]["type"] == MEMORY_GROUNDED_REVIEW_CONTRACT_TYPE


# ---------------------------------------------------------------------------
# Integration: live-scripts _compact_event_body embeds memory-grounded contract
# ---------------------------------------------------------------------------


def test_compact_event_body_embeds_memory_contract_for_explicit_correction():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "live-scripts" / "sensorium_kanban_sensor_tick.py"
    spec = importlib.util.spec_from_file_location("sensorium_kanban_sensor_tick_memory_test", path)
    assert spec and spec.loader
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)

    event = {
        "id": "evt_correction",
        "ts": "2026-05-31T12:00:00Z",
        "kind": "explicit_correction",
        "strength": 0.88,
        "summary": "operator explicit correction: feedback tendency must be memory-grounded",
        "source_signal_ids": ["sig_correction"],
        "correlation_keys": ["feedback-tendency", "explicit-correction"],
        "sensitivity": "private",
        "allowed_surfaces": ["local"],
        "fingerprint": "fp_correction",
    }

    body = bridge._compact_event_body(event)

    assert "Memory-grounded contract" in body
    assert "conscious_review_contract" in body
    assert MEMORY_GROUNDED_REVIEW_CONTRACT_TYPE in body
    # Verify the contract type appears in the JSON payload section
    payload_text = body.split("Compact event payload:", 1)[1]
    payload, _ = json.JSONDecoder().raw_decode(payload_text[payload_text.index("{"):])
    assert payload["conscious_review_contract"]["type"] == MEMORY_GROUNDED_REVIEW_CONTRACT_TYPE


def test_compact_event_body_no_memory_contract_for_generic_event():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "live-scripts" / "sensorium_kanban_sensor_tick.py"
    spec = importlib.util.spec_from_file_location("sensorium_kanban_sensor_tick_memory_test2", path)
    assert spec and spec.loader
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)

    event = {
        "id": "evt_pressure",
        "ts": "2026-05-31T12:00:00Z",
        "kind": "kanban_pressure",
        "strength": 0.55,
        "summary": "blocked tasks rising",
        "source_signal_ids": ["sig_pressure"],
        "correlation_keys": ["kanban-pressure"],
        "sensitivity": "local",
        "allowed_surfaces": ["local"],
        "fingerprint": "fp_pressure",
    }

    body = bridge._compact_event_body(event)

    # No memory-grounded contract for generic kanban_pressure
    assert "Memory-grounded contract" not in body
    assert "memory_grounded_conscious_review" not in body


# ---------------------------------------------------------------------------
# Integration: live-scripts _candidate_intake_body embeds memory-grounded contract
# ---------------------------------------------------------------------------


def test_candidate_intake_body_embeds_memory_contract_for_explicit_correction():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "live-scripts" / "sensorium_kanban_sensor_tick.py"
    spec = importlib.util.spec_from_file_location("sensorium_kanban_sensor_tick_cand_test", path)
    assert spec and spec.loader
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)

    candidate = {
        "id": "cand_correction",
        "status": "candidate",
        "kind": "attention_policy_review",
        "pressure": 0.81,
        "summary": "operator explicit correction: feedback tendency must be memory-grounded",
        "event_ids": [],
        "correlation_keys": ["feedback-tendency", "explicit-correction"],
        "fingerprint": "fp_correction",
        "sensitivity": "private",
        "allowed_surfaces": ["local"],
    }

    body = bridge._candidate_intake_body(candidate)

    assert "Memory-grounded contract" in body
    assert "conscious_review_contract" in body
    assert MEMORY_GROUNDED_REVIEW_CONTRACT_TYPE in body
    payload_text = body.split("Compact candidate payload:", 1)[1]
    payload, _ = json.JSONDecoder().raw_decode(payload_text[payload_text.index("{"):])
    assert payload["conscious_review_contract"]["type"] == MEMORY_GROUNDED_REVIEW_CONTRACT_TYPE


def test_candidate_intake_body_no_memory_contract_for_generic_attention_candidate():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "live-scripts" / "sensorium_kanban_sensor_tick.py"
    spec = importlib.util.spec_from_file_location("sensorium_kanban_sensor_tick_cand_test2", path)
    assert spec and spec.loader
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)

    candidate = {
        "id": "cand_generic",
        "status": "candidate",
        "kind": "attention_policy_review",
        "pressure": 0.81,
        "summary": "repeated suppression evidence",
        "event_ids": [],
        "correlation_keys": ["kanban-pressure"],
        "fingerprint": "fp_generic",
        "sensitivity": "local_only",
        "allowed_surfaces": ["local"],
    }

    body = bridge._candidate_intake_body(candidate)

    assert "Memory-grounded contract" not in body
    assert "memory_grounded_conscious_review" not in body


# ---------------------------------------------------------------------------
# Privacy: no raw memories / transcripts / secrets in any contract payload
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "row",
    [
        {"kind": "explicit_correction", "summary": "explicit correction on feedback tendency", "correlation_keys": ["feedback-tendency"]},
        {"kind": "sensorium_strategy_insight", "summary": "strategy insight", "correlation_keys": ["strategy-insight"]},
        {"kind": "embodiment_insight", "summary": "embodiment insight", "correlation_keys": ["embodiment-insight"]},
    ],
)
def test_memory_contract_payload_contains_no_forbidden_fields(row):
    contract = memory_grounded_review_contract(row)
    assert contract is not None

    # Scan only actual data fields for privacy-sensitive content.
    # The retrieval_contract.forbidden list contains constraint descriptors
    # (e.g. "raw_transcript") — those are legitimate policy metadata, not data.
    for field in ("source_kind", "source_summary", "why", "required_receipt"):
        value = str(contract.get(field) or "").lower()
        forbidden = ["transcript", "session_log", "secret", "raw_memory_dump", "raw_log", "memory_content"]
        for word in forbidden:
            assert word not in value, f"Field '{field}' contains forbidden word '{word}'"

    # The retrieval_contract itself is a constraint descriptor; its forbidden list
    # is legitimate policy metadata, not data, and the data fields were covered above.
