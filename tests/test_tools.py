"""Tests for agent_sensorium.tools — callable without live Hermes runtime."""

import json

import pytest

from agent_sensorium.tools import (
    handle_sensorium_candidate_update,
    handle_sensorium_dispatch_once,
    handle_sensorium_ingest_signal,
    handle_sensorium_status,
)


@pytest.fixture
def state_dir(tmp_path):
    return str(tmp_path / "sensorium")


class TestSensoriumStatus:
    def test_initialized_empty(self, state_dir):
        raw = handle_sensorium_status(instance="test", state_dir=state_dir)
        result = json.loads(raw)
        assert result["success"] is True
        assert result["instance"] == "test"
        assert result["data"]["counts"]["signals"] == 0
        assert result["data"]["counts"]["events"] == 0
        assert result["data"]["counts"]["candidates"] == 0
        assert result["data"]["counts"]["threads"] == 0
        assert result["data"]["top_candidates"] == []

    def test_status_after_ingest(self, state_dir):
        signal = {
            "sensor": "test",
            "source": "manual",
            "kind": "design_decision",
            "summary": "Test signal",
            "strength_hint": 0.9,
        }
        handle_sensorium_ingest_signal(signal=signal, instance="test", state_dir=state_dir)
        raw = handle_sensorium_status(instance="test", state_dir=state_dir)
        result = json.loads(raw)
        assert result["data"]["counts"]["signals"] == 1
        assert result["data"]["counts"]["events"] == 1
        assert result["data"]["counts"]["candidates"] == 1
        assert result["data"]["counts"]["active_candidates"] == 1
        assert len(result["data"]["top_candidates"]) == 1


class TestSensoriumIngestSignal:
    def test_valid_signal_ingested(self, state_dir):
        signal = {
            "sensor": "explicit_operator_signal",
            "source": "manual",
            "kind": "design_decision",
            "summary": "Operator corrected image workflow",
            "strength_hint": 0.9,
            "correlation_keys": ["visual-continuity"],
        }
        raw = handle_sensorium_ingest_signal(signal=signal, instance="test", state_dir=state_dir)
        result = json.loads(raw)
        assert result["success"] is True
        assert result["data"]["promoted"] is True
        assert "event_id" in result["data"]
        assert "candidate_id" in result["data"]

    def test_weak_signal_not_promoted(self, state_dir):
        signal = {
            "sensor": "test",
            "source": "manual",
            "kind": "note",
            "summary": "Faint observation",
            "strength_hint": 0.2,
        }
        raw = handle_sensorium_ingest_signal(signal=signal, instance="test", state_dir=state_dir)
        result = json.loads(raw)
        assert result["success"] is True
        assert result["data"]["promoted"] is False
        assert "event_id" not in result["data"]

    def test_invalid_signal_returns_error(self, state_dir):
        raw = handle_sensorium_ingest_signal(
            signal={"sensor": "test"}, instance="test", state_dir=state_dir
        )
        result = json.loads(raw)
        assert result["success"] is False
        assert "missing required fields" in result["error"]

    def test_multiple_signals_accumulate(self, state_dir):
        for i in range(3):
            signal = {
                "sensor": "test",
                "source": "manual",
                "kind": "design_decision",
                "summary": f"Signal {i}",
                "strength_hint": 0.85,
            }
            handle_sensorium_ingest_signal(signal=signal, instance="test", state_dir=state_dir)

        raw = handle_sensorium_status(instance="test", state_dir=state_dir)
        result = json.loads(raw)
        assert result["data"]["counts"]["signals"] == 3
        assert result["data"]["counts"]["events"] == 3
        assert result["data"]["counts"]["candidates"] == 3


class TestSensoriumDispatchOnce:
    def test_dispatch_dry_run_via_tool(self, state_dir):
        signal = {
            "sensor": "test",
            "source": "manual",
            "kind": "design_decision",
            "summary": "Strong signal for dispatch",
            "strength_hint": 0.9,
        }
        handle_sensorium_ingest_signal(signal=signal, instance="test", state_dir=state_dir)
        raw = handle_sensorium_dispatch_once(instance="test", state_dir=state_dir, dry_run=True)
        result = json.loads(raw)
        assert result["success"] is True
        assert result["data"]["action"] == "would_promote"
        assert result["data"]["dry_run"] is True

    def test_dispatch_real_creates_thread(self, state_dir):
        signal = {
            "sensor": "test",
            "source": "manual",
            "kind": "design_decision",
            "summary": "Strong signal",
            "strength_hint": 0.9,
        }
        handle_sensorium_ingest_signal(signal=signal, instance="test", state_dir=state_dir)
        raw = handle_sensorium_dispatch_once(instance="test", state_dir=state_dir, dry_run=False)
        result = json.loads(raw)
        assert result["success"] is True
        assert result["data"]["action"] == "promoted"
        assert result["data"]["thread_id"].startswith("sth_")

    def test_status_shows_dormant_thread(self, state_dir):
        signal = {
            "sensor": "test",
            "source": "manual",
            "kind": "design_decision",
            "summary": "Thread test",
            "strength_hint": 0.9,
        }
        handle_sensorium_ingest_signal(signal=signal, instance="test", state_dir=state_dir)
        handle_sensorium_dispatch_once(instance="test", state_dir=state_dir, dry_run=False)
        raw = handle_sensorium_status(instance="test", state_dir=state_dir)
        result = json.loads(raw)
        assert result["data"]["counts"]["dormant_threads"] == 1
        assert len(result["data"]["top_threads"]) == 1
        assert result["data"]["top_threads"][0]["status"] == "dormant"


class TestSensoriumCandidateUpdate:
    def test_suppress_candidate(self, state_dir):
        signal = {
            "sensor": "test",
            "source": "manual",
            "kind": "design_decision",
            "summary": "To suppress",
            "strength_hint": 0.9,
        }
        raw = handle_sensorium_ingest_signal(signal=signal, instance="test", state_dir=state_dir)
        cand_id = json.loads(raw)["data"]["candidate_id"]

        raw = handle_sensorium_candidate_update(
            candidate_id=cand_id, action="suppress", reason="Not relevant",
            instance="test", state_dir=state_dir,
        )
        result = json.loads(raw)
        assert result["success"] is True
        assert result["data"]["old_status"] == "candidate"
        assert result["data"]["new_status"] == "suppressed"

    def test_hold_candidate(self, state_dir):
        signal = {
            "sensor": "test",
            "source": "manual",
            "kind": "design_decision",
            "summary": "To hold",
            "strength_hint": 0.9,
        }
        raw = handle_sensorium_ingest_signal(signal=signal, instance="test", state_dir=state_dir)
        cand_id = json.loads(raw)["data"]["candidate_id"]

        raw = handle_sensorium_candidate_update(
            candidate_id=cand_id, action="hold", instance="test", state_dir=state_dir,
        )
        result = json.loads(raw)
        assert result["success"] is True
        assert result["data"]["new_status"] == "held"

    def test_invalid_action(self, state_dir):
        raw = handle_sensorium_candidate_update(
            candidate_id="cand_x", action="invalid",
            instance="test", state_dir=state_dir,
        )
        result = json.loads(raw)
        assert result["success"] is False
        assert "Invalid action" in result["error"]

    def test_nonexistent_candidate(self, state_dir):
        raw = handle_sensorium_candidate_update(
            candidate_id="cand_nonexist", action="suppress",
            instance="test", state_dir=state_dir,
        )
        result = json.loads(raw)
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_suppressed_candidate_excluded_from_dispatch(self, state_dir):
        signal = {
            "sensor": "test",
            "source": "manual",
            "kind": "design_decision",
            "summary": "Will suppress then try dispatch",
            "strength_hint": 0.9,
        }
        raw = handle_sensorium_ingest_signal(signal=signal, instance="test", state_dir=state_dir)
        cand_id = json.loads(raw)["data"]["candidate_id"]

        handle_sensorium_candidate_update(
            candidate_id=cand_id, action="suppress",
            instance="test", state_dir=state_dir,
        )

        raw = handle_sensorium_dispatch_once(instance="test", state_dir=state_dir, dry_run=False)
        result = json.loads(raw)
        assert result["data"]["action"] == "no_candidate"
