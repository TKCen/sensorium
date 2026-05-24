"""Tests for agent_sensorium.tools — callable without live Hermes runtime."""

import json

import pytest

from agent_sensorium.tools import (
    handle_sensorium_candidate_update,
    handle_sensorium_dispatch_once,
    handle_sensorium_ingest_event,
    handle_sensorium_ingest_signal,
    handle_sensorium_status,
    handle_sensorium_thread_update,
)
from agent_sensorium.store import SensoriumStore


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

    def test_duplicate_strong_signal_is_idempotent_by_fingerprint(self, state_dir):
        signal = {
            "sensor": "test",
            "source": "manual",
            "kind": "design_decision",
            "summary": "Same observation should not create repeated work",
            "strength_hint": 0.9,
            "correlation_keys": ["dedupe"],
        }

        first = json.loads(handle_sensorium_ingest_signal(signal=signal, instance="test", state_dir=state_dir))
        second = json.loads(handle_sensorium_ingest_signal(signal=signal, instance="test", state_dir=state_dir))

        assert first["success"] is True
        assert second["success"] is True
        assert second["data"]["duplicate"] is True
        assert second["data"]["signal_id"] == first["data"]["signal_id"]
        assert second["data"]["event_id"] == first["data"]["event_id"]
        assert second["data"]["candidate_id"] == first["data"]["candidate_id"]

        status = json.loads(handle_sensorium_status(instance="test", state_dir=state_dir))["data"]
        assert status["counts"]["signals"] == 1
        assert status["counts"]["events"] == 1
        assert status["counts"]["candidates"] == 1

        store = SensoriumStore(instance="test", state_dir=state_dir)
        signal_record = store.read_jsonl("signals")[0]
        event_record = store.read_jsonl("events")[0]
        candidate_record = store.read_jsonl("candidates")[0]
        assert signal_record["fingerprint"]
        assert event_record["fingerprint"]
        assert candidate_record["fingerprint"]

    def test_duplicate_weak_signal_remains_noise_without_extra_records(self, state_dir):
        signal = {
            "sensor": "test",
            "source": "manual",
            "kind": "note",
            "summary": "Same weak observation",
            "strength_hint": 0.1,
            "correlation_keys": ["noise"],
        }

        first = json.loads(handle_sensorium_ingest_signal(signal=signal, instance="test", state_dir=state_dir))
        second = json.loads(handle_sensorium_ingest_signal(signal=signal, instance="test", state_dir=state_dir))

        assert first["data"]["promoted"] is False
        assert second["data"]["duplicate"] is True
        assert second["data"]["promoted"] is False

        status = json.loads(handle_sensorium_status(instance="test", state_dir=state_dir))["data"]
        assert status["counts"]["signals"] == 1
        assert status["counts"]["events"] == 0
        assert status["counts"]["candidates"] == 0


class TestSensoriumIngestEvent:
    def test_valid_event_ingested_and_candidate_created(self, state_dir):
        event = {
            "id": "evt_trusted",
            "ts": "2026-05-25T00:00:00Z",
            "type": "sensor.event.promoted",
            "kind": "task_result",
            "summary": "Trusted sensor already promoted this event",
            "source_signal_ids": ["sig_external"],
            "signal_count": 2,
            "strength": 0.88,
            "correlation_keys": ["trusted-import"],
            "sensitivity": "private",
            "allowed_surfaces": ["local", "discord"],
        }

        raw = handle_sensorium_ingest_event(event=event, instance="test", state_dir=state_dir)
        result = json.loads(raw)
        assert result["success"] is True
        assert result["data"]["event_id"] == "evt_trusted"
        assert result["data"]["candidate_id"].startswith("cand_")

        status = json.loads(handle_sensorium_status(instance="test", state_dir=state_dir))["data"]
        assert status["counts"]["signals"] == 0
        assert status["counts"]["events"] == 1
        assert status["counts"]["candidates"] == 1
        assert status["top_candidates"][0]["kind"] == "task_result"

    def test_invalid_event_returns_error(self, state_dir):
        raw = handle_sensorium_ingest_event(
            event={"kind": "task_result"}, instance="test", state_dir=state_dir
        )
        result = json.loads(raw)
        assert result["success"] is False
        assert "Event missing required fields" in result["error"]

    def test_invalid_event_sensitivity_returns_error(self, state_dir):
        event = {
            "id": "evt_bad_scope",
            "ts": "2026-05-25T00:00:00Z",
            "type": "sensor.event.promoted",
            "kind": "task_result",
            "summary": "Bad sensitivity should fail",
            "sensitivity": "world-readable",
        }
        raw = handle_sensorium_ingest_event(event=event, instance="test", state_dir=state_dir)
        result = json.loads(raw)
        assert result["success"] is False
        assert "Invalid sensitivity" in result["error"]

    def test_duplicate_event_import_is_idempotent(self, state_dir):
        event = {
            "id": "evt_dup",
            "ts": "2026-05-25T00:00:00Z",
            "type": "sensor.event.promoted",
            "kind": "task_result",
            "summary": "Trusted duplicate event",
            "source_signal_ids": ["sig_external"],
            "strength": 0.9,
            "correlation_keys": ["trusted-dedupe"],
            "sensitivity": "private",
            "allowed_surfaces": ["local"],
        }

        first = json.loads(handle_sensorium_ingest_event(event=event, instance="test", state_dir=state_dir))
        second = json.loads(handle_sensorium_ingest_event(event=event, instance="test", state_dir=state_dir))

        assert second["success"] is True
        assert second["data"]["duplicate"] is True
        assert second["data"]["event_id"] == first["data"]["event_id"]
        assert second["data"]["candidate_id"] == first["data"]["candidate_id"]

        status = json.loads(handle_sensorium_status(instance="test", state_dir=state_dir))["data"]
        assert status["counts"]["events"] == 1
        assert status["counts"]["candidates"] == 1

    def test_related_event_updates_existing_candidate(self, state_dir):
        first_event = {
            "id": "evt_rel_1",
            "ts": "2026-05-25T00:00:00Z",
            "type": "sensor.event.promoted",
            "kind": "task_result",
            "summary": "First related result",
            "strength": 0.8,
            "correlation_keys": ["same-thread"],
            "sensitivity": "public_safe",
            "allowed_surfaces": ["local", "discord"],
        }
        second_event = {
            "id": "evt_rel_2",
            "ts": "2026-05-25T00:05:00Z",
            "type": "sensor.event.promoted",
            "kind": "task_result",
            "summary": "Second related result",
            "strength": 0.95,
            "correlation_keys": ["same-thread", "new-key"],
            "sensitivity": "private",
            "allowed_surfaces": ["local"],
        }

        first = json.loads(handle_sensorium_ingest_event(event=first_event, instance="test", state_dir=state_dir))
        second = json.loads(handle_sensorium_ingest_event(event=second_event, instance="test", state_dir=state_dir))

        assert second["success"] is True
        assert second["data"]["coalesced"] is True
        assert second["data"]["candidate_id"] == first["data"]["candidate_id"]

        store = SensoriumStore(instance="test", state_dir=state_dir)
        events = store.read_jsonl("events")
        candidates = store.read_jsonl("candidates")
        decisions = store.read_jsonl("decisions")
        assert len(events) == 2
        assert len(candidates) == 1
        candidate = candidates[0]
        assert candidate["event_ids"] == ["evt_rel_1", "evt_rel_2"]
        assert candidate["repetition"] > 0
        assert candidate["pressure"] > 0.78
        assert candidate["updated_at"] >= candidate["created_at"]
        assert candidate["sensitivity"] == "private"
        assert candidate["allowed_surfaces"] == ["local"]
        assert decisions[0]["type"] == "candidate.coalesced"
        assert decisions[0]["candidate_id"] == first["data"]["candidate_id"]


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


class TestStatusTerminalCounts:
    def test_empty_status_has_zero_terminal_counts(self, state_dir):
        raw = handle_sensorium_status(instance="test", state_dir=state_dir)
        counts = json.loads(raw)["data"]["counts"]
        assert counts["closed_threads"] == 0
        assert counts["archived_threads"] == 0
        assert counts["archived_candidates"] == 0

    def test_closed_thread_counted(self, state_dir):
        signal = {
            "sensor": "test",
            "source": "manual",
            "kind": "design_decision",
            "summary": "Thread for close counting",
            "strength_hint": 0.9,
        }
        handle_sensorium_ingest_signal(signal=signal, instance="test", state_dir=state_dir)
        raw = handle_sensorium_dispatch_once(instance="test", state_dir=state_dir, dry_run=False)
        thread_id = json.loads(raw)["data"]["thread_id"]

        handle_sensorium_thread_update(
            thread_id=thread_id, action="close", reason="done",
            instance="test", state_dir=state_dir,
        )

        raw = handle_sensorium_status(instance="test", state_dir=state_dir)
        data = json.loads(raw)["data"]
        assert data["counts"]["closed_threads"] == 1
        assert data["counts"]["dormant_threads"] == 0

    def test_latest_decision_in_status(self, state_dir):
        signal = {
            "sensor": "test",
            "source": "manual",
            "kind": "design_decision",
            "summary": "Thread for decision receipt",
            "strength_hint": 0.9,
        }
        handle_sensorium_ingest_signal(signal=signal, instance="test", state_dir=state_dir)
        raw = handle_sensorium_dispatch_once(instance="test", state_dir=state_dir, dry_run=False)
        thread_id = json.loads(raw)["data"]["thread_id"]

        handle_sensorium_thread_update(
            thread_id=thread_id, action="close", reason="guardrail installed",
            instance="test", state_dir=state_dir,
        )

        raw = handle_sensorium_status(instance="test", state_dir=state_dir)
        data = json.loads(raw)["data"]
        assert "latest_decision" in data
        assert data["latest_decision"]["type"] == "thread.updated"
        assert data["latest_decision"]["action"] == "close"
        assert data["latest_decision"]["reason"] == "guardrail installed"

    def test_no_latest_decision_when_empty(self, state_dir):
        raw = handle_sensorium_status(instance="test", state_dir=state_dir)
        data = json.loads(raw)["data"]
        assert "latest_decision" not in data
