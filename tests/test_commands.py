"""Tests for agent_sensorium.commands — /sensorium command handler."""

import json

import pytest

from agent_sensorium.commands import handle_sensorium_command
from agent_sensorium.tools import (
    handle_sensorium_dispatch_once,
    handle_sensorium_ingest_signal,
    handle_sensorium_thread_update,
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
    @pytest.mark.parametrize("instance", ["../outside", " spaced", "line\nbreak", "\x00bad"])
    def test_invalid_instance_is_rejected_before_state_creation(self, tmp_path, instance):
        state_dir = tmp_path / "must-not-exist"

        assert handle_sensorium_command("status", instance=instance, state_dir=str(state_dir)) == "Sensorium: invalid_instance"
        assert not state_dir.exists()

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
        handle_sensorium_dispatch_once(instance="test", state_dir=state_dir, dry_run=False, config={"legacy_thread_dispatch_enabled": True})
        out = handle_sensorium_command("threads", instance="test", state_dir=state_dir)
        assert "threads:" in out
        assert "[dormant]" in out
        assert "origin:" in out


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
        assert "pointer" in out
        assert "open" in out
        assert "thread" in out
        assert "dispatch" not in out
        assert "compact" in out

    def test_unknown_subcommand(self, state_dir):
        out = handle_sensorium_command("foobar", instance="test", state_dir=state_dir)
        assert "Unknown subcommand: foobar" in out
        assert "Usage:" in out


class TestStatusTerminalDisplay:
    def test_status_shows_terminal_counts(self, state_dir):
        _ingest_strong(state_dir)
        raw = handle_sensorium_dispatch_once(instance="test", state_dir=state_dir, dry_run=False, config={"legacy_thread_dispatch_enabled": True})
        thread_id = json.loads(raw)["data"]["thread_id"]
        handle_sensorium_thread_update(
            thread_id=thread_id, action="close", reason="resolved",
            instance="test", state_dir=state_dir,
        )
        out = handle_sensorium_command("status", instance="test", state_dir=state_dir)
        assert "1c" in out
        assert "0a" in out
        assert "Latest decision:" in out
        assert "thread.updated" in out
        assert "close" in out

    def test_empty_status_shows_zero_terminal_counts(self, state_dir):
        out = handle_sensorium_command("status", instance="test", state_dir=state_dir)
        assert "0c" in out
        assert "0a" in out
        assert "Latest decision:" not in out
