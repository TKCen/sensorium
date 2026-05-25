"""Tests for Phase 7 — deterministic compact sensor helpers."""

import json
import tempfile

import pytest

from agent_sensorium.sensors import (
    MAX_REF_CHARS,
    MAX_SUMMARY_CHARS,
    artifact_signal,
    file_content_hash,
    operator_signal,
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
