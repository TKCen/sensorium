"""Tests for dynamic salience substrate primitives."""

import json

import pytest

from agent_sensorium.gate import build_pressure_pitch, decayed_candidate, inhibited_by_sensor_policy
from agent_sensorium.sensors import (
    apply_pressure_delta,
    classify_codex_usage_pressure,
    codex_usage_compact_sample,
    load_sensor_registry,
    register_sensor_kind,
    session_event_signal,
)
from agent_sensorium.store import SensoriumStore
from agent_sensorium.subconscious import validate_advisory_output
from agent_sensorium.tools import (
    handle_sensorium_candidate_update,
    handle_sensorium_ingest_signal,
    handle_sensorium_sensor_config,
    handle_sensorium_service_threads,
)


def _payload(raw: str) -> dict:
    return json.loads(raw)


class TestRuntimeSensorRegistry:
    def test_register_modify_pause_deprecate_runtime_sensor(self, tmp_path):
        store = SensoriumStore(instance="test", state_dir=str(tmp_path / "sensorium"))
        store.ensure_dirs()

        registered = register_sensor_kind(
            "design_pull",
            defaults={"strength_hint": 0.82, "sensitivity": "private"},
            store=store,
        )
        assert registered["name"] == "design_pull"
        assert registered["status"] == "active"

        sig = session_event_signal(kind="design_pull", summary="Repeated design pull")
        assert sig["strength_hint"] == 0.82
        assert sig["sensitivity"] == "private"

        paused = register_sensor_kind("design_pull", status="paused", store=store)
        assert paused["status"] == "paused"
        assert session_event_signal(kind="design_pull", summary="x")["strength_hint"] == 0.5

        deprecated = register_sensor_kind("design_pull", status="deprecated", store=store)
        assert deprecated["status"] == "deprecated"

        loaded = load_sensor_registry(store)
        assert loaded["design_pull"]["status"] == "deprecated"

    def test_sensor_registry_tool_lists_and_persists(self, tmp_path):
        raw = handle_sensorium_sensor_config(
            action="register",
            name="operator_salience",
            defaults={"strength_hint": 0.88, "correlation_keys": ["operator"]},
            instance="test",
            state_dir=str(tmp_path / "sensorium"),
        )
        data = _payload(raw)
        assert data["success"] is True
        assert data["data"]["registry"]["operator_salience"]["status"] == "active"

        listed = _payload(handle_sensorium_sensor_config(action="list", instance="test", state_dir=str(tmp_path / "sensorium")))
        assert "operator_salience" in listed["data"]["registry"]
        assert listed["data"]["registry"]["operator_salience"]["defaults"]["strength_hint"] == 0.88


class TestCodexUsagePressureSensor:
    def _codex_payload(self, *, primary=42, weekly=32, reset=7200, weekly_reset=None, allowed=True, reached=False):
        weekly_reset = weekly_reset if weekly_reset is not None else round(604800 * (1 - (weekly / 100)))
        return {
            "available": True,
            "plan_type": "prolite",
            "main_rate_limit": {
                "allowed": allowed,
                "limit_reached": reached,
                "primary": {"used_percent": primary, "reset_after_seconds": reset, "window_seconds": 10000},
                "secondary": {"used_percent": weekly, "reset_after_seconds": weekly_reset, "window_seconds": 604800},
            },
            "additional_rate_limits": [
                {
                    "limit_name": "GPT-5.3-Codex-Spark",
                    "metered_feature": "codex_bengalfox",
                    "rate_limit": {
                        "allowed": True,
                        "limit_reached": False,
                        "primary": {"used_percent": 3, "reset_after_seconds": 18000, "window_seconds": 10000},
                        "secondary": {"used_percent": 0, "reset_after_seconds": 604800, "window_seconds": 604800},
                    },
                }
            ],
        }

    def test_codex_usage_compact_sample_strips_to_budget_metadata(self):
        sample = codex_usage_compact_sample(self._codex_payload(), generated_at="2026-06-01T17:24:27+02:00")
        assert sample == {
            "available": True,
            "generated_at": "2026-06-01T17:24:27+02:00",
            "plan_type": "prolite",
            "allowed": True,
            "limit_reached": False,
            "primary_used_percent": 42,
            "primary_reset_after_seconds": 7200,
            "primary_window_seconds": 10000,
            "weekly_used_percent": 32,
            "weekly_reset_after_seconds": 411264,
            "weekly_window_seconds": 604800,
            "additional_limits": [
                {
                    "limit_name": "GPT-5.3-Codex-Spark",
                    "metered_feature": "codex_bengalfox",
                    "allowed": True,
                    "limit_reached": False,
                    "primary_used_percent": 3,
                    "primary_reset_after_seconds": 18000,
                    "primary_window_seconds": 10000,
                    "weekly_used_percent": 0,
                    "weekly_reset_after_seconds": 604800,
                    "weekly_window_seconds": 604800,
                }
            ],
            "error": "",
        }

    def test_codex_usage_compact_sample_maps_lone_weekly_primary_by_duration(self):
        payload = {
            "available": True,
            "plan_type": "pro",
            "main_rate_limit": {
                "allowed": True,
                "limit_reached": False,
                "primary": {
                    "used_percent": 9,
                    "reset_after_seconds": 508597,
                    "window_seconds": 604800,
                },
                "secondary": None,
            },
        }
        sample = codex_usage_compact_sample(payload, generated_at="2026-07-19T10:02:06+02:00")
        assert sample["primary_used_percent"] is None
        assert sample["primary_window_seconds"] is None
        assert sample["weekly_used_percent"] == 9
        assert sample["weekly_reset_after_seconds"] == 508597
        assert sample["weekly_window_seconds"] == 604800

        signal, state = classify_codex_usage_pressure(sample, state={"level": "healthy"})
        assert signal is None
        assert state["last_values"]["primary_used_percent"] is None
        assert state["last_values"]["weekly_used_percent"] == 9.0
        assert state["last_values"]["weekly_over_expected_pp"] < 0
        assert state["last_values"]["weekly_projected_window_percent"] < 100

    def test_codex_usage_projection_overrun_reaches_sensorium_before_pace_threshold(self):
        sample = codex_usage_compact_sample(
            self._codex_payload(
                primary=50,
                reset=5000,
                weekly=53,
                weekly_reset=302400,
            )
        )
        signal, state = classify_codex_usage_pressure(sample, state={"level": "healthy"})
        assert signal is not None
        assert signal["transition"] == "healthy_to_degraded"
        assert signal["metric_family"] == "weekly_projection"
        assert signal["values"]["weekly_over_expected_pp"] == 3.0
        assert signal["values"]["weekly_projected_window_percent"] == 106.0
        assert state["level"] == "degraded"

    def test_codex_usage_pressure_emits_transition_only_on_level_change(self):
        healthy = codex_usage_compact_sample(self._codex_payload(primary=42, reset=5800))
        first_signal, state = classify_codex_usage_pressure(healthy, state={"level": "healthy"})
        assert first_signal is None
        assert state["level"] == "healthy"

        pressured = codex_usage_compact_sample(self._codex_payload(primary=66, reset=4700))
        signal, state = classify_codex_usage_pressure(pressured, state=state)
        assert signal is not None
        assert signal["kind"] == "inference_budget_pressure"
        assert signal["sensor"] == "sensorium.codex_usage_pressure"
        assert signal["transition"] == "healthy_to_degraded"
        assert signal["correlation_keys"] == ["codex-openai-energy", "codex-openai-energy:primary_pace"]
        assert signal["values"]["primary_used_percent"] == 66.0

        again, state = classify_codex_usage_pressure(pressured, state=state)
        assert again is None
        assert state["level"] == "degraded"

        recovered, state = classify_codex_usage_pressure(healthy, state=state)
        assert recovered is not None
        assert recovered["transition"] == "degraded_to_recovered"

    def test_codex_usage_pressure_uses_percentage_points_over_window_pace(self):
        # 53% usage at 53% elapsed is equilibrium, not pressure.
        equilibrium = codex_usage_compact_sample(self._codex_payload(primary=53, weekly=53, reset=4700))
        signal, state = classify_codex_usage_pressure(equilibrium, state={"level": "healthy"})
        assert signal is None
        assert state["level"] == "healthy"
        assert state["last_values"]["primary_elapsed_percent"] == 53.0
        assert state["last_values"]["primary_over_expected_pp"] == 0.0

        # Same raw usage shape, but 13pp ahead of elapsed-window pace: real pressure.
        ahead = codex_usage_compact_sample(self._codex_payload(primary=66, weekly=53, reset=4700))
        signal, state = classify_codex_usage_pressure(ahead, state=state)
        assert signal is not None
        assert signal["transition"] == "healthy_to_degraded"
        assert signal["metric_family"] == "primary_pace"
        assert signal["values"]["primary_over_expected_pp"] == 13.0

    def test_codex_weekly_over_pace_is_weighted_more_than_primary_window(self):
        weekly_53_elapsed = round(604800 * 0.47)
        weekly_ahead = codex_usage_compact_sample(self._codex_payload(primary=53, weekly=59, reset=4700, weekly_reset=weekly_53_elapsed))
        signal, state = classify_codex_usage_pressure(weekly_ahead, state={"level": "healthy"})
        assert signal is not None
        assert signal["transition"] == "healthy_to_degraded"
        assert signal["metric_family"] == "weekly_pace"
        assert signal["values"]["weekly_over_expected_pp"] == 6.0

        weekly_critical = codex_usage_compact_sample(self._codex_payload(primary=53, weekly=69, reset=4700, weekly_reset=weekly_53_elapsed))
        signal, state = classify_codex_usage_pressure(weekly_critical, state={"level": "healthy"})
        assert signal is not None
        assert signal["transition"] == "healthy_to_critical"
        assert signal["metric_family"] == "weekly_pace"
        assert signal["values"]["weekly_over_expected_pp"] == 16.0

    def test_codex_high_usage_near_reset_stays_healthy_until_limit_reached(self):
        near_reset = codex_usage_compact_sample(self._codex_payload(primary=92, weekly=92, reset=900, weekly_reset=60480))
        signal, state = classify_codex_usage_pressure(near_reset, state={"level": "healthy"})
        assert signal is None
        assert state["level"] == "healthy"
        assert state["last_values"]["reset_near"] is True

        reached = codex_usage_compact_sample(self._codex_payload(primary=99, weekly=99, reset=900, weekly_reset=60480, allowed=False, reached=True))
        signal, state = classify_codex_usage_pressure(reached, state=state)
        assert signal is not None
        assert signal["transition"] == "healthy_to_critical"
        assert signal["metric_family"] == "quota"


class TestPressureDecayPitchAndExtinction:
    def test_decayed_candidate_habituates_pressure_without_mutating_input(self):
        cand = {"id": "cand_x", "pressure": 0.8, "updated_at": "2026-06-01T00:00:00Z"}
        one_hour = decayed_candidate(cand, now="2026-06-01T01:00:00Z", config={"decay": {"half_life_hours": 24}})
        two_days = decayed_candidate(cand, now="2026-06-03T00:00:00Z", config={"decay": {"half_life_hours": 24}})
        assert two_days["pressure"] < one_hour["pressure"] < cand["pressure"]
        assert cand["pressure"] == 0.8
        assert one_hour["pressure_meta"]["decay_applied"] is True

    def test_apply_pressure_delta_clamps_and_records_reason(self):
        cand = {"id": "cand_x", "pressure": 0.9}
        raised = apply_pressure_delta(cand, 0.3, reason="repeat")
        lowered = apply_pressure_delta(cand, -1.2, reason="rejected")
        assert raised["pressure"] == 1.0
        assert lowered["pressure"] == 0.0
        assert lowered["pressure_meta"]["last_delta_reason"] == "rejected"

    def test_build_pressure_pitch_is_low_token_traceable_payload(self):
        candidate = {
            "id": "cand_x",
            "kind": "explicit_correction",
            "pressure": 0.78,
            "summary": "Repeated correction around background code creep",
            "correlation_keys": ["sensorium", "code-creep"],
            "event_ids": ["evt_a", "evt_b"],
            "updated_at": "2026-06-01T10:00:00Z",
        }
        events = [
            {"id": "evt_a", "summary": "First correction", "ts": "2026-06-01T09:00:00Z"},
            {"id": "evt_b", "summary": "Second correction", "ts": "2026-06-01T10:00:00Z"},
        ]
        pitch = build_pressure_pitch(candidate, events=events, threshold=0.65)
        assert pitch == {
            "candidate_id": "cand_x",
            "kind": "explicit_correction",
            "summary": "Repeated correction around background code creep",
            "pressure": 0.78,
            "threshold": 0.65,
            "event_count": 2,
            "timeframe": "2026-06-01T09:00:00Z..2026-06-01T10:00:00Z",
            "sample": "Second correction",
            "correlation_keys": ["sensorium", "code-creep"],
            "recommended_prompt": "I noticed repeated pressure around explicit_correction. Do we want to invest in this?",
        }

    def test_pressure_pitch_filters_to_candidate_events_only(self):
        candidate = {
            "id": "cand_x",
            "kind": "explicit_correction",
            "pressure": 0.78,
            "summary": "Repeated correction",
            "event_ids": ["evt_b"],
        }
        pitch = build_pressure_pitch(
            candidate,
            events=[
                {"id": "evt_a", "summary": "Unrelated", "ts": "2026-06-01T08:00:00Z"},
                {"id": "evt_b", "summary": "Related latest", "ts": "2026-06-01T09:00:00Z"},
            ],
            threshold=0.65,
        )
        assert pitch["event_count"] == 1
        assert pitch["timeframe"] == "2026-06-01T09:00:00Z..2026-06-01T09:00:00Z"
        assert pitch["sample"] == "Related latest"

    def test_service_threads_persists_candidate_decay(self, tmp_path):
        store = SensoriumStore(instance="test", state_dir=str(tmp_path / "sensorium"))
        store.ensure_dirs()
        store.append_jsonl("candidates", {
            "id": "cand_decay",
            "status": "candidate",
            "kind": "durable_importance",
            "pressure": 0.8,
            "summary": "Old pressure",
            "event_ids": ["evt_decay"],
            "correlation_keys": ["decay"],
            "sensitivity": "private",
            "allowed_surfaces": ["local"],
            "created_at": "2026-06-01T00:00:00Z",
            "updated_at": "2026-06-01T00:00:00Z",
            "expires_at": "",
        })

        raw = handle_sensorium_service_threads(
            instance="test",
            state_dir=str(tmp_path / "sensorium"),
            now="2026-06-02T00:00:00Z",
            config={"decay": {"half_life_hours": 24}, "silence_ttl_hours": 240},
        )
        assert _payload(raw)["data"]["decayed_candidates"] == 1
        updated = store.read_jsonl("candidates")[0]
        assert updated["pressure"] == 0.4
        assert updated["last_decay_at"] == "2026-06-02T00:00:00Z"

    def test_expired_inhibition_does_not_block_pathway(self):
        inhibited, _ = inhibited_by_sensor_policy(
            {"kind": "noise", "correlation_keys": ["noisy"], "strength_hint": 0.9},
            {
                "inhibitions": [{
                    "kind": "noise",
                    "correlation_keys": ["noisy"],
                    "reason": "old",
                    "created_at": "2026-06-01T00:00:00Z",
                    "expires_at": "2026-06-02T00:00:00Z",
                }]
            },
            now="2026-06-03T00:00:00Z",
        )
        assert inhibited is False

    def test_silence_ttl_extinguishes_stale_candidate(self, tmp_path):
        store = SensoriumStore(instance="test", state_dir=str(tmp_path / "sensorium"))
        store.ensure_dirs()
        store.append_jsonl("candidates", {
            "id": "cand_stale",
            "status": "candidate",
            "kind": "durable_importance",
            "pressure": 0.3,
            "summary": "Stale pressure",
            "event_ids": ["evt_stale"],
            "correlation_keys": ["stale"],
            "sensitivity": "private",
            "allowed_surfaces": ["local"],
            "created_at": "2026-06-01T00:00:00Z",
            "updated_at": "2026-06-01T00:00:00Z",
            "expires_at": "",
        })

        raw = handle_sensorium_service_threads(
            instance="test",
            state_dir=str(tmp_path / "sensorium"),
            now="2026-06-03T00:00:00Z",
            config={"silence_ttl_hours": 24},
        )
        assert _payload(raw)["data"]["decayed_candidates"] == 1
        updated = store.read_jsonl("candidates")[0]
        assert updated["status"] == "suppressed"
        assert updated["extinct"] is True
        assert updated["pressure"] == 0.0

    def test_candidate_reject_silence_marks_extinction_and_inhibits_pathway(self, tmp_path):
        store = SensoriumStore(instance="test", state_dir=str(tmp_path / "sensorium"))
        store.ensure_dirs()
        cand = {
            "id": "cand_x",
            "status": "candidate",
            "kind": "noise",
            "pressure": 0.8,
            "summary": "Noisy pathway",
            "event_ids": ["evt_x"],
            "correlation_keys": ["noisy"],
            "sensitivity": "private",
            "allowed_surfaces": ["local"],
            "created_at": "2026-06-01T00:00:00Z",
            "updated_at": "2026-06-01T00:00:00Z",
            "expires_at": "",
        }
        store.append_jsonl("candidates", cand)

        raw = handle_sensorium_candidate_update(
            candidate_id="cand_x",
            action="suppress",
            reason="operator rejected repeated noise",
            instance="test",
            state_dir=str(tmp_path / "sensorium"),
        )
        assert _payload(raw)["success"] is True
        updated = store.read_jsonl("candidates")[0]
        assert updated["extinct"] is True
        assert updated["pressure"] == 0.0
        assert inhibited_by_sensor_policy(
            {"kind": "noise", "correlation_keys": ["noisy"], "strength_hint": 0.9},
            store.read_sensor_policy(),
        )[0] is True

        ingested = _payload(handle_sensorium_ingest_signal(
            signal={
                "sensor": "sensorium.test",
                "source": "test",
                "kind": "noise",
                "summary": "Same noisy pathway",
                "strength_hint": 0.95,
                "sensitivity": "private",
                "allowed_surfaces": ["local"],
                "correlation_keys": ["noisy"],
            },
            instance="test",
            state_dir=str(tmp_path / "sensorium"),
        ))
        assert ingested["data"]["promoted"] is False
        assert "inhibited" in ingested["data"]["reason"]


def test_subconscious_advisory_rejects_reach_out_request_type():
    with pytest.raises(ValueError, match="REACH_OUT"):
        validate_advisory_output({
            "action": "CREATE_CONSCIOUS_TASK",
            "rationale": "relational pull",
            "event_ids": ["evt_x"],
            "candidate_ids": [],
            "conscious_task": {
                "request_type": "REACH_OUT",
                "title": "Reach out",
                "why": "relational",
                "expected_decision": "operator decides",
            },
        })
