"""Tests for pre_llm_salience hook."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

workspace = Path(__file__).resolve().parents[1]
if str(workspace) not in sys.path:
    sys.path.insert(0, str(workspace))

from agent_sensorium.pre_llm_salience import (  # noqa: E402
    EXAMPLE_SALIENCE_KINDS,
    handle_salience_pre_llm,
    salience_context_for_llm,
)
from agent_sensorium.store import SensoriumStore  # noqa: E402


@pytest.fixture
def tmp_state_dir(tmp_path):
    """A clean Sensorium state directory."""
    d = tmp_path / "sensorium_state"
    d.mkdir()
    return str(d)


class TestSalienceContextForLl:
    def test_returns_string(self):
        ctx = salience_context_for_llm()
        assert isinstance(ctx, str)
        assert len(ctx) > 0

    def test_contains_sensorium_label(self):
        ctx = salience_context_for_llm()
        assert "Sensorium Salience Hook" in ctx

    def test_mentions_example_signal_kinds_without_boundary_language(self):
        ctx = salience_context_for_llm()
        for kind in EXAMPLE_SALIENCE_KINDS:
            assert kind in ctx, f"Missing example kind: {kind}"
        lower = ctx.lower()
        assert "examples" in lower
        assert "not exhaustive" in lower
        assert "use a concise kind" in lower
        assert "handle the live turn normally first" in lower
        assert "appropriate kind (" not in ctx
        assert "supported kinds" not in lower

    def test_mentions_compact_sensorium_ingest(self):
        ctx = salience_context_for_llm()
        assert "sensorium(action='ingest')" in ctx
        assert "sensorium_ingest_signal" not in ctx

    def test_uses_generic_user_language(self):
        """The reusable hook should not hard-code Sera/operator wording."""
        ctx = salience_context_for_llm()
        assert "your user" in ctx
        assert "you or your user" in ctx
        assert "operator" not in ctx.lower()
        assert "Sera" not in ctx

    def test_context_is_small(self):
        ctx = salience_context_for_llm()
        assert len(ctx) < 1000, f"Context too large: {len(ctx)} chars"

    def test_context_does_not_leak_transcript(self):
        ctx = salience_context_for_llm()
        forbidden = ["transcript", "capsule", "raw message", "full conversation"]
        for term in forbidden:
            assert term.lower() not in ctx.lower(), f"Context leaks: {term}"


class TestHandleSaliencePreLl:
    def test_returns_context_when_enabled(self, tmp_state_dir):
        store = SensoriumStore(instance="test", state_dir=tmp_state_dir)
        store.ensure_dirs()

        result = handle_salience_pre_llm(
            instance="test",
            state_dir=tmp_state_dir,
            platform="local",
        )
        assert result is not None
        assert "context" in result
        assert "sensorium(action='ingest')" in result["context"]
        assert "sensorium_ingest_signal" not in result["context"]

    def test_returns_context_with_salience_policy_disabled(self, tmp_state_dir):
        """Policy gates downstream processing, not live salience awareness."""
        store = SensoriumStore(instance="test", state_dir=tmp_state_dir)
        store.ensure_dirs()
        cfg_path = store.root / "instance.config.json"
        cfg_path.write_text(json.dumps({"salience_hook": {"enabled": False}}))

        result = handle_salience_pre_llm(
            instance="test",
            state_dir=tmp_state_dir,
            platform="local",
        )
        assert result is not None
        assert "sensorium(action='ingest')" in result["context"]
        assert "sensorium_ingest_signal" not in result["context"]

    def test_does_not_crash_on_missing_config(self, tmp_state_dir):
        """Missing config should not crash; hook is always-on and resilient."""
        store = SensoriumStore(instance="test", state_dir=tmp_state_dir)
        store.ensure_dirs()

        result = handle_salience_pre_llm(
            instance="test",
            state_dir=tmp_state_dir,
            platform="local",
        )
        assert result is not None
        assert "context" in result

    def test_returns_context_when_instance_not_found(self, tmp_path):
        """A new instance dir should be created without crashing the turn."""
        result = handle_salience_pre_llm(
            instance="nonexistent-instance-xyz",
            state_dir=str(tmp_path / "nada"),
            platform="local",
        )
        assert result is not None
        assert "context" in result

    def test_platform_defaults_to_local(self, tmp_state_dir):
        store = SensoriumStore(instance="test", state_dir=tmp_state_dir)
        store.ensure_dirs()

        result = handle_salience_pre_llm(
            instance="test",
            state_dir=tmp_state_dir,
        )
        assert result is not None
        assert "context" in result
