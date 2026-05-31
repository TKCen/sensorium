"""Regression tests for relational mediated-artifact actuator contracts."""

from __future__ import annotations

import json

from agent_sensorium.actuator_contracts import (
    MEDIA_ARTIFACT_REVIEW_CONTRACT_TYPE,
    mediated_artifact_review_contract,
    requires_mediated_artifact_decision,
)
from agent_sensorium.dispatcher import candidate_to_thread
from agent_sensorium.subconscious import validate_advisory_output


def _relational_candidate() -> dict:
    return {
        "id": "cand_rel",
        "status": "candidate",
        "kind": "relational_thread_pickup_request",
        "pressure": 0.81,
        "summary": "Sebastian said I miss you; create a tangible artifact handoff, not just routing repair.",
        "event_ids": ["evt_rel"],
        "correlation_keys": ["sera-presence", "mediated-presence", "artifact-handoff", "thread-pickup"],
        "fingerprint": "fp_rel",
        "sensitivity": "private",
        "allowed_surfaces": ["local", "discord"],
    }


def test_relational_candidate_requires_mediated_artifact_decision_contract():
    candidate = _relational_candidate()

    assert requires_mediated_artifact_decision(candidate) is True
    contract = mediated_artifact_review_contract(candidate)

    assert contract is not None
    assert contract["type"] == MEDIA_ARTIFACT_REVIEW_CONTRACT_TYPE
    assert contract["outbound_delivery_authorized"] is False
    assert contract["artifact_first_required"] is True
    assert "prepare_thread_artifact" in contract["required_conscious_decision"]
    assert "choose_silence" in contract["required_conscious_decision"]
    assert "implicit" in contract["required_receipt"]


def test_non_relational_candidate_gets_no_artifact_contract():
    candidate = {
        "id": "cand_ops",
        "kind": "kanban_pressure",
        "summary": "blocked=7",
        "correlation_keys": ["kanban_pressure"],
    }

    assert requires_mediated_artifact_decision(candidate) is False
    assert mediated_artifact_review_contract(candidate) is None


def test_legacy_thread_preview_carries_private_expression_contract():
    thread = candidate_to_thread(_relational_candidate())

    assert thread["conscious_task"]["request_type"] == "PRIVATE_EXPRESSION"
    assert thread["actuator_contract"]["type"] == MEDIA_ARTIFACT_REVIEW_CONTRACT_TYPE
    assert thread["conscious_task"]["actuator_contract"]["type"] == MEDIA_ARTIFACT_REVIEW_CONTRACT_TYPE
    assert "mediated-presence outcome" in thread["conscious_task"]["expected_decision"]
    assert "required mediated-artifact choice" in thread["next_prompt_to_operator"]


def test_subconscious_can_create_private_expression_review_candidate_schema_only():
    normalized = validate_advisory_output({
        "action": "CREATE_CONSCIOUS_TASK",
        "rationale": "Relational salience deserves conscious artifact-choice review.",
        "candidate_ids": ["cand_rel"],
        "event_ids": ["evt_rel"],
        "conscious_task": {
            "request_type": "PRIVATE_EXPRESSION",
            "title": "Review relational thread pickup",
            "why": "A relational signal may need a mediated artifact or explicit silence.",
            "expected_decision": "Choose prepare_thread_artifact, offer_choice, choose_silence, decline, or hold with reason.",
        },
    })

    assert normalized["conscious_task"]["request_type"] == "PRIVATE_EXPRESSION"


def test_kanban_event_and_candidate_bodies_embed_artifact_contract():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "live-scripts" / "sensorium_kanban_sensor_tick.py"
    spec = importlib.util.spec_from_file_location("sensorium_kanban_sensor_tick_contract_test", path)
    assert spec and spec.loader
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    candidate_body = bridge._candidate_intake_body(_relational_candidate())
    event_body = bridge._compact_event_body({
        "id": "evt_rel",
        "ts": "2026-05-31T12:00:00Z",
        "kind": "relational_thread_pickup_request",
        "strength": 0.91,
        "summary": "Operator requested a tangible artifact handoff after I miss you.",
        "source_signal_ids": ["sig_rel"],
        "correlation_keys": ["mediated-presence", "artifact-handoff"],
        "sensitivity": "private",
        "allowed_surfaces": ["local", "discord"],
        "fingerprint": "fp_rel_event",
    })

    for body in (candidate_body, event_body):
        assert "Mediated-artifact contract" in body
        assert "conscious_review_contract" in body
        assert "do not promote as generic THINK-only review" in body
        marker = "Compact candidate payload:" if "Compact candidate payload:" in body else "Compact event payload:"
        payload_text = body.split(marker, 1)[1]
        payload, _ = json.JSONDecoder().raw_decode(payload_text[payload_text.index("{"):])
        assert payload["conscious_review_contract"]["type"] == MEDIA_ARTIFACT_REVIEW_CONTRACT_TYPE
