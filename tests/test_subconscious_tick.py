"""Tick wiring tests for Phase 8 Subconscious advisory."""

import json
import subprocess
import sys
from pathlib import Path

from agent_sensorium.store import SensoriumStore

TICK_SCRIPT = str(Path(__file__).resolve().parent.parent / "scripts" / "sensorium_tick.py")


def _event():
    return {
        "id": "evt_tick_advisory",
        "ts": "2026-05-26T10:00:00Z",
        "type": "sensor.event.promoted",
        "kind": "hindsight_pressure",
        "summary": "hindsight pressure healthy_to_degraded: failed=9",
        "strength": 0.8,
        "correlation_keys": ["hindsight-pressure"],
        "sensitivity": "local_only",
        "allowed_surfaces": ["local"],
    }


def test_subconscious_flag_runs_disabled_dry_run_advisory(tmp_path):
    state_dir = str(tmp_path / "sensorium")
    store = SensoriumStore(instance="test", state_dir=state_dir)
    store.ensure_dirs()
    store.append_jsonl("events", _event())

    proc = subprocess.run(
        [sys.executable, TICK_SCRIPT, "--instance", "test", "--state-dir", state_dir, "--subconscious-advisory", "--json"],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["success"] is True
    assert out["subconscious_advisory"]["action"] == "disabled"
    assert out["subconscious_advisory"]["dry_run"] is True
    assert "context" in out["subconscious_advisory"]
    assert store.read_jsonl("threads") == []


def test_subconscious_model_flag_enables_model_lane_without_external_side_effects(tmp_path):
    state_dir = str(tmp_path / "sensorium")
    store = SensoriumStore(instance="test", state_dir=state_dir)
    store.ensure_dirs()
    store.append_jsonl("events", _event())
    env = dict(**__import__("os").environ, SENSORIUM_SUBCONSCIOUS_API_KEY_ENV="SENSORIUM_MISSING_TEST_KEY")

    proc = subprocess.run(
        [
            sys.executable,
            TICK_SCRIPT,
            "--instance", "test",
            "--state-dir", state_dir,
            "--subconscious-advisory",
            "--subconscious-model",
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )

    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["success"] is True
    assert out["subconscious_advisory"]["action"] == "model_unavailable"
    assert out["subconscious_advisory"]["model"] == "deepseek/deepseek-v4-flash"
    assert store.read_jsonl("candidates") == []
    assert store.read_jsonl("threads") == []


def test_subconscious_advisory_dry_run_does_not_write_tick_receipt(tmp_path):
    state_dir = str(tmp_path / "sensorium")
    proc = subprocess.run(
        [sys.executable, TICK_SCRIPT, "--instance", "test", "--state-dir", state_dir, "--subconscious-advisory", "--dry-run", "--json"],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert proc.returncode == 0, proc.stderr
    store = SensoriumStore(instance="test", state_dir=state_dir)
    tick_receipts = [d for d in store.read_jsonl("decisions") if d.get("type") == "tick.completed"]
    assert tick_receipts == []
