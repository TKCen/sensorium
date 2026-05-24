"""Tests for scripts/sensorium_tick.py — deterministic tick script."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent_sensorium.tools import handle_sensorium_ingest_signal

TICK_SCRIPT = str(Path(__file__).resolve().parent.parent / "scripts" / "sensorium_tick.py")


@pytest.fixture
def state_dir(tmp_path):
    return str(tmp_path / "sensorium")


def _run_tick(state_dir, *, dry_run=False, instance="test"):
    cmd = [sys.executable, TICK_SCRIPT, "--instance", instance, "--state-dir", state_dir]
    if dry_run:
        cmd.append("--dry-run")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, f"tick failed: {result.stderr}"
    return json.loads(result.stdout)


class TestTickDryRun:
    def test_dry_run_empty_state(self, state_dir):
        out = _run_tick(state_dir, dry_run=True)
        assert out["success"] is True
        assert out["dry_run"] is True
        assert "compact" not in out
        assert out["dispatch"]["action"] == "no_candidate"
        assert out["status"]["counts"]["signals"] == 0

    def test_dry_run_with_signal(self, state_dir):
        signal = {
            "sensor": "test",
            "source": "manual",
            "kind": "design_decision",
            "summary": "Tick dry-run test",
            "strength_hint": 0.9,
        }
        handle_sensorium_ingest_signal(signal=signal, instance="test", state_dir=state_dir)
        out = _run_tick(state_dir, dry_run=True)
        assert out["dispatch"]["action"] == "would_promote"
        assert out["status"]["counts"]["candidates"] == 1


class TestTickReal:
    def test_real_tick_dispatches(self, state_dir):
        signal = {
            "sensor": "test",
            "source": "manual",
            "kind": "design_decision",
            "summary": "Tick real test",
            "strength_hint": 0.9,
        }
        handle_sensorium_ingest_signal(signal=signal, instance="test", state_dir=state_dir)
        out = _run_tick(state_dir, dry_run=False)
        assert out["success"] is True
        assert out["dry_run"] is False
        assert out["dispatch"]["action"] == "promoted"
        assert out["dispatch"]["thread_id"].startswith("sth_")
        assert out["status"]["counts"]["dormant_threads"] == 1

    def test_real_tick_compact_included(self, state_dir):
        out = _run_tick(state_dir, dry_run=False)
        assert "compact" in out
        assert out["compact"]["receipts_written"] == 0
