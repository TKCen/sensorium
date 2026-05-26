"""Tests for Phase 7 — deterministic compact sensor helpers."""

import json
import tempfile

import pytest

from agent_sensorium.sensors import (
    MAX_REF_CHARS,
    MAX_SUMMARY_CHARS,
    artifact_signal,
    classify_machine_body_pressure,
    file_content_hash,
    machine_body_pressure_sample,
    operator_signal,
    replay_machine_body_pressure,
    session_event_signal,
)


class TestSessionEventSignal:
    """Session-event sensor emits compact signals only."""

    def test_emits_compact_dict(self):
        sig = session_event_signal(kind="design_decision", summary="User discussed architecture")
        assert isinstance(sig, dict)
        assert sig["sensor"] == "sensorium.session_event"
        assert sig["source"] == "hermes_session"
        assert sig["kind"] == "design_decision"
        assert "User discussed" in sig["summary"]

    def test_truncates_long_summary(self):
        long_text = "x " * 300
        sig = session_event_signal(kind="note", summary=long_text)
        assert len(sig["summary"]) <= MAX_SUMMARY_CHARS

    def test_defaults_sensitivity_private(self):
        sig = session_event_signal(kind="note", summary="test")
        assert sig["sensitivity"] == "private"
        assert sig["allowed_surfaces"] == ["local"]

    def test_rejects_invalid_sensitivity_to_private(self):
        sig = session_event_signal(kind="note", summary="test", sensitivity="world_readable")
        assert sig["sensitivity"] == "private"

    def test_clamps_strength_high(self):
        sig = session_event_signal(kind="note", summary="test", strength_hint=5.0)
        assert sig["strength_hint"] == 1.0

    def test_clamps_strength_low(self):
        sig = session_event_signal(kind="note", summary="test", strength_hint=-1.0)
        assert sig["strength_hint"] == 0.0

    def test_no_raw_transcript_fields(self):
        sig = session_event_signal(kind="note", summary="test")
        for key in ("transcript", "raw", "content", "messages", "full_text"):
            assert key not in sig

    def test_session_ref_truncated(self):
        sig = session_event_signal(kind="note", summary="test", session_ref="r" * 500)
        assert len(sig["session_ref"]) <= MAX_REF_CHARS

    def test_sanitizes_optional_lists(self):
        sig = session_event_signal(
            kind="note",
            summary="test",
            allowed_surfaces=["local", "", "  ", "discord"],
            correlation_keys=["k" * 400, "", "  "],
        )
        assert sig["allowed_surfaces"] == ["local", "discord"]
        assert len(sig["correlation_keys"][0]) <= MAX_REF_CHARS
        assert sig["correlation_keys"][0].startswith("k")

    def test_non_numeric_strength_defaults_safely(self):
        strength = "high"
        sig = session_event_signal(kind="note", summary="test", strength_hint=strength)  # type: ignore[arg-type]
        assert sig["strength_hint"] == 0.5


class TestArtifactSignal:
    """Artifact sensor records metadata/hash/ref, not raw contents."""

    def test_emits_compact_dict_with_metadata(self):
        sig = artifact_signal(
            path="/tmp/file.py", summary="New module", size=1234, content_hash="abc123"
        )
        assert sig["sensor"] == "sensorium.artifact"
        assert sig["source"] == "artifact"
        assert sig["path"] == "/tmp/file.py"
        assert sig["size"] == 1234
        assert sig["content_hash"] == "abc123"

    def test_no_file_content_in_signal(self):
        sig = artifact_signal(path="/tmp/file.py", summary="New module")
        for key in ("content", "raw", "body", "file_content", "data", "text"):
            assert key not in sig

    def test_truncates_long_summary(self):
        sig = artifact_signal(path="/tmp/f.py", summary="x " * 300)
        assert len(sig["summary"]) <= MAX_SUMMARY_CHARS

    def test_truncates_long_hash(self):
        sig = artifact_signal(path="/tmp/f.py", summary="test", content_hash="a" * 100)
        assert len(sig["content_hash"]) <= 64

    def test_defaults_sensitivity_private(self):
        sig = artifact_signal(path="/tmp/f.py", summary="test")
        assert sig["sensitivity"] == "private"
        assert sig["allowed_surfaces"] == ["local"]

    def test_default_kind_artifact_created(self):
        sig = artifact_signal(path="/tmp/f.py", summary="test")
        assert sig["kind"] == "artifact_created"

    def test_empty_hash_stays_empty(self):
        sig = artifact_signal(path="/tmp/f.py", summary="test", content_hash="")
        assert sig["content_hash"] == ""

    def test_path_truncated(self):
        sig = artifact_signal(path="/" + "a" * 500, summary="test")
        assert len(sig["path"]) <= MAX_REF_CHARS


class TestOperatorSignal:
    """Explicit operator sensor sets appropriate source/kind/sensitivity/surfaces."""

    def test_emits_compact_dict(self):
        sig = operator_signal(summary="Please prioritize X")
        assert sig["sensor"] == "sensorium.explicit_operator"
        assert sig["source"] == "manual"
        assert sig["actor"] == "operator"
        assert sig["kind"] == "user_correction"

    def test_truncates_long_summary(self):
        sig = operator_signal(summary="x " * 300)
        assert len(sig["summary"]) <= MAX_SUMMARY_CHARS

    def test_defaults_sensitivity_private(self):
        sig = operator_signal(summary="test")
        assert sig["sensitivity"] == "private"
        assert sig["allowed_surfaces"] == ["local"]

    def test_rejects_invalid_sensitivity(self):
        sig = operator_signal(summary="test", sensitivity="public_danger")
        assert sig["sensitivity"] == "private"

    def test_source_ref_truncated(self):
        sig = operator_signal(summary="test", source_ref="r" * 500)
        assert len(sig["source_ref"]) <= MAX_REF_CHARS

    def test_high_default_strength(self):
        sig = operator_signal(summary="test")
        assert sig["strength_hint"] >= 0.7

    def test_custom_kind(self):
        sig = operator_signal(summary="New design requirement", kind="design_decision")
        assert sig["kind"] == "design_decision"


class TestMachineBodyPressure:
    """Body sensor emits only compact global transition signals from cheap samples."""

    def test_healthy_samples_emit_nothing(self):
        state = {}
        emitted = []
        for sample in [
            {"mem_available_pct": 80.0, "load_per_cpu": 0.2, "swap_used_pct": 0.0},
            {"mem_available_pct": 75.0, "load_per_cpu": 0.3, "swap_used_pct": 0.0},
            {"mem_available_pct": 70.0, "load_per_cpu": 0.4, "swap_used_pct": 0.0},
        ]:
            sig, state = classify_machine_body_pressure(sample, state=state)
            emitted.append(sig)
        assert emitted == [None, None, None]
        assert state["level"] == "healthy"

    def test_degraded_memory_requires_debounce_and_emits_compact_signal(self):
        state = {}
        sample = {"mem_available_pct": 8.0, "load_per_cpu": 0.2, "swap_used_pct": 0.0}
        first, state = classify_machine_body_pressure(sample, state=state)
        second, state = classify_machine_body_pressure(sample, state=state)
        third, state = classify_machine_body_pressure(sample, state=state)

        assert first is None
        assert second is None
        assert third is not None
        assert third["sensor"] == "sensorium.machine_body_pressure"
        assert third["source"] == "machine"
        assert third["kind"] == "body_pressure"
        assert third["scope"] == "global"
        assert third["pressure_level"] == "degraded"
        assert third["transition"] == "healthy_to_degraded"
        assert third["metric_family"] == "memory"
        assert third["window"]["samples"] == 3
        assert third["sensitivity"] == "local_only"
        assert third["allowed_surfaces"] == ["local"]
        assert third["strength_hint"] >= 0.75
        for forbidden in ("processes", "cmdline", "stdout", "stderr", "transcript", "raw"):
            assert forbidden not in third

    def test_critical_memory_emits_faster_than_degraded(self):
        state = {}
        sample = {"mem_available_pct": 3.0, "load_per_cpu": 0.2, "swap_used_pct": 0.0}
        first, state = classify_machine_body_pressure(sample, state=state)
        second, state = classify_machine_body_pressure(sample, state=state)

        assert first is None
        assert second is not None
        assert second["pressure_level"] == "critical"
        assert second["transition"] == "healthy_to_critical"
        assert second["strength_hint"] >= 0.9

    def test_recovery_requires_healthy_window(self):
        state = {}
        bad = {"mem_available_pct": 8.0, "load_per_cpu": 0.2, "swap_used_pct": 0.0}
        for _ in range(3):
            sig, state = classify_machine_body_pressure(bad, state=state)
        assert sig is not None
        healthy = {"mem_available_pct": 70.0, "load_per_cpu": 0.2, "swap_used_pct": 0.0}
        emitted = []
        for _ in range(5):
            sig, state = classify_machine_body_pressure(healthy, state=state)
            emitted.append(sig)

        assert emitted[:4] == [None, None, None, None]
        assert emitted[4] is not None
        assert emitted[4]["transition"] == "degraded_to_recovered"
        assert emitted[4]["pressure_level"] == "healthy"

    def test_sustained_pressure_heartbeat_is_rate_limited(self):
        config = {"degraded_samples": 1, "sustained_samples": 3}
        state = {}
        bad = {"mem_available_pct": 8.0, "load_per_cpu": 0.2, "swap_used_pct": 0.0}
        transition, state = classify_machine_body_pressure(bad, state=state, config=config)
        assert transition is not None

        one, state = classify_machine_body_pressure(bad, state=state, config=config)
        two, state = classify_machine_body_pressure(bad, state=state, config=config)
        three, state = classify_machine_body_pressure(bad, state=state, config=config)
        assert one is None
        assert two is None
        assert three is not None
        assert three["transition"] == "sustained_degraded"

    def test_replay_uses_same_online_classifier_without_runtime_history_scan(self):
        samples = [
            {"mem_available_pct": 80.0, "load_per_cpu": 0.2, "swap_used_pct": 0.0},
            {"mem_available_pct": 8.0, "load_per_cpu": 0.2, "swap_used_pct": 0.0},
            {"mem_available_pct": 8.0, "load_per_cpu": 0.2, "swap_used_pct": 0.0},
            {"mem_available_pct": 8.0, "load_per_cpu": 0.2, "swap_used_pct": 0.0},
        ]
        signals = replay_machine_body_pressure(samples)
        assert len(signals) == 1
        assert signals[0]["transition"] == "healthy_to_degraded"

    def test_procfs_sample_parses_fixture_without_process_lists(self, tmp_path):
        proc = tmp_path / "proc"
        proc.mkdir()
        (proc / "loadavg").write_text("0.50 0.40 0.30 1/100 123\n")
        (proc / "meminfo").write_text(
            "MemTotal:       1000000 kB\n"
            "MemAvailable:    250000 kB\n"
            "SwapTotal:        500000 kB\n"
            "SwapFree:         400000 kB\n"
        )
        pressure = proc / "pressure"
        pressure.mkdir()
        (pressure / "cpu").write_text("some avg10=1.00 avg60=0.50 avg300=0.10 total=10\n")
        (pressure / "memory").write_text("some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
        (pressure / "io").write_text("some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")

        sample = machine_body_pressure_sample(proc_root=str(proc), disk_paths=[str(tmp_path)])
        assert sample["mem_available_pct"] == 25.0
        assert sample["swap_used_pct"] == 20.0
        assert sample["psi_cpu_some_avg10"] == 1.0
        assert "processes" not in sample
        assert "cmdline" not in sample

    def test_body_pressure_signal_validates_and_ingests(self):
        from agent_sensorium.schemas import validate_signal
        from agent_sensorium.tools import handle_sensorium_ingest_signal

        state = {}
        sample = {"mem_available_pct": 8.0, "load_per_cpu": 0.2, "swap_used_pct": 0.0}
        sig = None
        for _ in range(3):
            sig, state = classify_machine_body_pressure(sample, state=state)
        assert sig is not None
        validate_signal(sig)
        with tempfile.TemporaryDirectory() as td:
            raw = handle_sensorium_ingest_signal(signal=sig, instance="test", state_dir=td)
            result = json.loads(raw)
            assert result["success"] is True


class TestFileContentHash:
    def test_hashes_real_file(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("hello")
        h = file_content_hash(str(f))
        assert len(h) == 64
        assert h == file_content_hash(str(f))

    def test_nonexistent_file_returns_empty(self, tmp_path):
        h = file_content_hash(str(tmp_path / "nope.txt"))
        assert h == ""


class TestSensorSignalsAreIngestable:
    """All sensor signals pass validate_signal and are suitable for ingest."""

    def test_session_event_validates(self):
        from agent_sensorium.schemas import validate_signal

        sig = session_event_signal(kind="design_decision", summary="Test")
        validate_signal(sig)

    def test_artifact_validates(self):
        from agent_sensorium.schemas import validate_signal

        sig = artifact_signal(path="/tmp/f.py", summary="Test")
        validate_signal(sig)

    def test_operator_validates(self):
        from agent_sensorium.schemas import validate_signal

        sig = operator_signal(summary="Test")
        validate_signal(sig)

    def test_session_event_ingestable(self):
        from agent_sensorium.tools import handle_sensorium_ingest_signal

        with tempfile.TemporaryDirectory() as td:
            sig = session_event_signal(
                kind="design_decision", summary="Test event", strength_hint=0.9,
            )
            raw = handle_sensorium_ingest_signal(signal=sig, instance="test", state_dir=td)
            result = json.loads(raw)
            assert result["success"] is True

    def test_artifact_ingestable(self):
        from agent_sensorium.tools import handle_sensorium_ingest_signal

        with tempfile.TemporaryDirectory() as td:
            sig = artifact_signal(
                path="/tmp/f.py", summary="New file", strength_hint=0.9,
            )
            raw = handle_sensorium_ingest_signal(signal=sig, instance="test", state_dir=td)
            result = json.loads(raw)
            assert result["success"] is True

    def test_operator_ingestable(self):
        from agent_sensorium.tools import handle_sensorium_ingest_signal

        with tempfile.TemporaryDirectory() as td:
            sig = operator_signal(summary="Correction", strength_hint=0.9)
            raw = handle_sensorium_ingest_signal(signal=sig, instance="test", state_dir=td)
            result = json.loads(raw)
            assert result["success"] is True
