"""Tests for agent_sensorium.commands — /sensorium command handler."""

import json

import pytest

from agent_sensorium.commands import handle_sensorium_command
from agent_sensorium.tools import (
    handle_sensorium_dispatch_once,
    handle_sensorium_ingest_signal,
)


@pytest.fixture
def state_dir(tmp_path):
    return str(tmp_path / "sensorium")


def _ingest_strong(state_dir):
    signal = {
        "sensor": "test",
        "source": "manual",
        "kind": "design_decision",
        "summary": "Test signal for commands",
        "strength_hint": 0.9,
    }
    return handle_sensorium_ingest_signal(signal=signal, instance="test", state_dir=state_dir)


class TestStatusCommand:
    def test_empty_status(self, state_dir):
        out = handle_sensorium_command("status", instance="test", state_dir=state_dir)
        assert "Sensorium [test]" in out
        assert "signals: 0" in out

    def test_default_is_status(self, state_dir):
        out = handle_sensorium_command("", instance="test", state_dir=state_dir)
        assert "Sensorium [test]" in out

    def test_status_with_data(self, state_dir):
        _ingest_strong(state_dir)
        out = handle_sensorium_command("status", instance="test", state_dir=state_dir)
        assert "signals: 1" in out
        assert "Top candidates:" in out
        assert "design_decision" in out


class TestThreadsCommand:
    def test_no_threads(self, state_dir):
        out = handle_sensorium_command("threads", instance="test", state_dir=state_dir)
        assert "no visible threads" in out

    def test_threads_after_dispatch(self, state_dir):
        _ingest_strong(state_dir)
        handle_sensorium_dispatch_once(instance="test", state_dir=state_dir, dry_run=False)
        out = handle_sensorium_command("threads", instance="test", state_dir=state_dir)
        assert "threads:" in out
        assert "[dormant]" in out
        assert "origin:" in out


class TestDispatchCommand:
    def test_dispatch_no_candidates(self, state_dir):
        out = handle_sensorium_command("dispatch", instance="test", state_dir=state_dir)
        assert "no eligible candidate" in out

    def test_dispatch_preview(self, state_dir):
        _ingest_strong(state_dir)
        out = handle_sensorium_command("dispatch", instance="test", state_dir=state_dir)
        assert "would promote" in out
        assert "pressure" in out


class TestCompactCommand:
    def test_compact_empty(self, state_dir):
        out = handle_sensorium_command("compact", instance="test", state_dir=state_dir)
        assert "0 candidates" in out
        assert "0 threads archived" in out


class TestHelpCommand:
    def test_help(self, state_dir):
        out = handle_sensorium_command("help", instance="test", state_dir=state_dir)
        assert "Usage:" in out
        assert "status" in out
        assert "threads" in out
        assert "dispatch" in out
        assert "compact" in out

    def test_unknown_subcommand(self, state_dir):
        out = handle_sensorium_command("foobar", instance="test", state_dir=state_dir)
        assert "Unknown subcommand: foobar" in out
        assert "Usage:" in out
