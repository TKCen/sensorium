"""Tests for the deterministic pressure-explanation builder.

Proves: deterministic JSON-identical replay, soft degradation to warnings on
missing data, correct inhibitor surfacing with safe labels and source refs,
and a hard no-secret-leak property for a secret-shaped sentinel placed into
every free-form input field.
"""

from __future__ import annotations

import json

import pytest

from agent_sensorium.dampener_blocker import evaluate_blocker_block, run_dampener_block
from agent_sensorium.explanations import (
    ExplanationError,
    build_explanation,
)
from agent_sensorium.store import SensoriumStore
from agent_sensorium.temporal_sensor import evaluate_temporal_block

_SENTINEL = "sk-explain-AAAA1111BBBB2222CCCC3333DDDD4444EEEE5566"


@pytest.fixture
def store(tmp_path):
    return SensoriumStore(instance="test", state_dir=str(tmp_path / "sensorium"))


def _candidate(**overrides):
    candidate = {
        "id": "cand_1",
        "kind": "subconscious_advisory",
        "status": "candidate",
        "pressure": 0.72,
        "strength_hint": 0.6,
        "summary": "compact candidate summary",
        "event_ids": ["evt_a", "evt_b"],
        "correlation_keys": ["gateway", "review"],
    }
    candidate.update(overrides)
    return candidate


# --- deterministic replay ----------------------------------------------------


def test_explanation_is_json_identical_on_replay():
    record = _candidate()
    config = {"weighting": "v1", "operator_token": "anything"}
    first = build_explanation("cand_1", record=record, config=config)
    second = build_explanation("cand_1", record=record, config=config)

    assert first == second
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert first["replay_key"] == second["replay_key"]
    assert first["replay_key"].startswith("sha256:")


def test_explanation_has_expected_top_level_shape():
    explanation = build_explanation("cand_1", record=_candidate())
    for key in (
        "subject_id",
        "subject",
        "pressure",
        "effective_pressure",
        "components",
        "inhibitors",
        "warnings",
        "replay_key",
    ):
        assert key in explanation
    assert explanation["pressure"] == 0.72
    # No inhibitors -> effective == base pressure.
    assert explanation["effective_pressure"] == 0.72
    component_kinds = {c["kind"] for c in explanation["components"]}
    assert "base_pressure" in component_kinds
    assert "strength_hint" in component_kinds
    assert "source_event" in component_kinds
    assert "correlation" in component_kinds


def test_records_index_resolves_subject():
    records = {"cand_1": _candidate(), "cand_2": _candidate(id="cand_2")}
    explanation = build_explanation("cand_1", records=records)
    assert explanation["pressure"] == 0.72
    assert explanation["warnings"] == []


# --- soft degradation on missing data ----------------------------------------


def test_missing_subject_record_degrades_to_warning_not_exception():
    explanation = build_explanation("ghost", records={})
    assert "subject_record_unavailable" in explanation["warnings"]
    assert explanation["pressure"] is None
    # Still returns a valid, serializable dict.
    json.dumps(explanation, sort_keys=True)


def test_missing_pressure_field_is_a_warning():
    record = _candidate()
    del record["pressure"]
    explanation = build_explanation("cand_1", record=record)
    assert "missing_or_invalid_pressure" in explanation["warnings"]
    assert explanation["pressure"] is None


def test_malformed_event_ids_degrade_to_warning():
    explanation = build_explanation("cand_1", record=_candidate(event_ids="not-a-list"))
    assert "event_ids_not_a_list" in explanation["warnings"]


def test_malformed_evidence_degrades_to_warning():
    explanation = build_explanation(
        "cand_1",
        record=_candidate(),
        dampener_evidence="not-a-mapping",
        temporal_evidence=12345,
    )
    assert "dampener_evidence_not_a_mapping_or_list" in explanation["warnings"]
    assert "temporal_evidence_not_a_mapping_or_list" in explanation["warnings"]


def test_non_mapping_config_raises_explanation_error():
    with pytest.raises(ExplanationError):
        build_explanation("cand_1", record=_candidate(), config=["not", "a", "mapping"])


# --- inhibitors --------------------------------------------------------------


def test_dampener_inhibitor_appears_with_safe_labels(store):
    block = {"type": "dampener", "mode": "cooldown", "cooldown_seconds": 60}
    # First pass elapses; second is suppressed (after_pressure 0.0).
    run_dampener_block("d_cool", block, store=store, now="2026-06-20T00:00:00Z", pressure=0.72, target="cand_1")
    suppressed = run_dampener_block(
        "d_cool", block, store=store, now="2026-06-20T00:00:30Z", pressure=0.72, target="cand_1"
    )

    explanation = build_explanation("cand_1", record=_candidate(), dampener_evidence=suppressed)
    inhibitors = explanation["inhibitors"]
    assert len(inhibitors) == 1
    inhibitor = inhibitors[0]
    assert inhibitor["kind"] == "dampener"
    assert inhibitor["label"] == "cooldown"
    assert inhibitor["after_pressure"] == 0.0
    assert inhibitor["source_refs"]  # safe-labeled node refs present
    # A fully suppressed dampener floors effective pressure to 0.0.
    assert explanation["effective_pressure"] == 0.0


def test_dampener_inhibitor_with_secret_shaped_mode_never_echoes_raw_label():
    """A corrupt/legacy/caller-supplied dampener sidecar can put arbitrary
    text in `mode`. Only the closed DampenerModeKind vocab may render
    verbatim; anything else must be safe-labeled, never echoed."""
    secret_mode = f"{_SENTINEL}-mode"
    raw_effect = {
        "type": "dampener_effect",
        "source_node": _SENTINEL,
        "target_node": _SENTINEL,
        "mode": secret_mode,
        "before_pressure": 0.71,
        "after_pressure": 0.11,
        "reason": _SENTINEL,
    }
    explanation = build_explanation("cand_1", record=_candidate(), dampener_evidence=raw_effect)
    serialized = json.dumps(explanation, sort_keys=True)
    assert _SENTINEL not in serialized
    assert secret_mode not in serialized
    inhibitor = explanation["inhibitors"][0]
    assert inhibitor["label"] != secret_mode
    assert inhibitor["label"].startswith("mode#")


def test_blocker_inhibitor_floors_effective_pressure():
    block = {
        "type": "blocker",
        "reason_kind": "policy",
        "policy_id": "surface_sensitivity_v1",
        "reason": "private item cannot route to public surface",
    }
    effect = evaluate_blocker_block("b_policy", block, target="cand_1", now="2026-06-20T00:00:00Z")
    explanation = build_explanation("cand_1", record=_candidate(), blocker_evidence=effect)
    inhibitors = explanation["inhibitors"]
    assert len(inhibitors) == 1
    assert inhibitors[0]["kind"] == "blocker"
    assert inhibitors[0]["reason"] == "policy"
    assert explanation["effective_pressure"] == 0.0


def test_temporal_evidence_becomes_a_component(store):
    for minute in ("00", "10", "20"):
        store.append_jsonl(
            "signals",
            {
                "id": f"sig_{minute}",
                "ts": f"2026-06-20T12:{minute}:00Z",
                "sensor": "fixture",
                "source": "machine",
                "kind": "gateway_error",
                "summary": "compact fixture row",
                "strength_hint": 0.7,
                "correlation_keys": ["gateway"],
                "sensitivity": "private",
                "allowed_surfaces": ["local"],
            },
        )
    block = {
        "type": "temporal_sensor",
        "enabled": True,
        "max_lookback_seconds": 3600,
        "predicate": {
            "kind": "rate_window",
            "state": "signals",
            "match": {"kind": "gateway_error", "correlation_key": "gateway"},
            "window_seconds": 1800,
            "threshold_count": 3,
        },
    }
    result = evaluate_temporal_block("trend", block, store=store, now="2026-06-20T12:30:00Z")
    assert result["emitted"] is True
    explanation = build_explanation(
        "cand_1", record=_candidate(), temporal_evidence=result["signal"]
    )
    temporal_components = [c for c in explanation["components"] if c["kind"] == "temporal_trend"]
    assert len(temporal_components) == 1
    assert temporal_components[0]["value"] == 3.0


# --- privacy / secret sentinel -----------------------------------------------


def test_secret_sentinel_never_leaks_into_explanation(store):
    """A secret-shaped sentinel placed into every free-form field must never
    appear verbatim in the explanation dict or its JSON serialization."""
    record = {
        "id": _SENTINEL,
        "kind": _SENTINEL,
        "status": _SENTINEL,
        "pressure": 0.72,
        "strength_hint": 0.6,
        "summary": _SENTINEL,
        "event_ids": [f"{_SENTINEL}-evt"],
        "correlation_keys": [f"{_SENTINEL}-corr"],
        "reason": _SENTINEL,
        "source_refs": [_SENTINEL],
        "provenance": {"task_id": _SENTINEL},
    }
    # Dampener/blocker effects built over secret-shaped ids/targets/reasons.
    damp_block = {"type": "dampener", "mode": "cooldown", "cooldown_seconds": 60}
    damp = run_dampener_block(_SENTINEL, damp_block, store=store, now="2026-06-20T00:00:00Z", target=_SENTINEL)
    block_effect = evaluate_blocker_block(
        _SENTINEL,
        {
            "type": "blocker",
            "reason_kind": "policy",
            "policy_id": _SENTINEL,
            "reason": _SENTINEL,
        },
        target=_SENTINEL,
        now="2026-06-20T00:00:00Z",
    )
    temporal_evidence = {
        "temporal": {
            "predicate": _SENTINEL,
            "count": 3,
            "source_refs": [{"id": _SENTINEL}, _SENTINEL],
        },
        "id": _SENTINEL,
    }

    explanation = build_explanation(
        _SENTINEL,
        record=record,
        dampener_evidence=damp,
        blocker_evidence=block_effect,
        temporal_evidence=temporal_evidence,
        config={"operator_secret": _SENTINEL, _SENTINEL: "x"},
    )

    serialized = json.dumps(explanation, sort_keys=True)
    assert _SENTINEL not in serialized
    # Spot-check structured fields too.
    assert _SENTINEL not in explanation["subject_id"]
    assert _SENTINEL not in json.dumps(explanation["subject"])
    assert _SENTINEL not in json.dumps(explanation["components"])
    assert _SENTINEL not in json.dumps(explanation["inhibitors"])
    assert _SENTINEL not in explanation["replay_key"]
    # And it still produced a usable explanation despite all-secret input.
    assert explanation["pressure"] == 0.72
    assert explanation["inhibitors"]
