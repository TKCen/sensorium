"""Tests for scripts/sensorium_tick.py — deterministic tick script."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

import scripts.sensorium_tick as sensorium_tick
from agent_sensorium.store import SensoriumStore
from agent_sensorium.tools import handle_sensorium_ingest_signal

TICK_SCRIPT = str(Path(__file__).resolve().parent.parent / "scripts" / "sensorium_tick.py")


@pytest.fixture
def state_dir(tmp_path):
    return str(tmp_path / "sensorium")


def _run_tick_raw(state_dir, *, dry_run=False, instance="test", json_flag=False):
    cmd = [sys.executable, TICK_SCRIPT, "--instance", instance, "--state-dir", state_dir]
    if dry_run:
        cmd.append("--dry-run")
    if json_flag:
        cmd.append("--json")
    return subprocess.run(cmd, capture_output=True, text=True, timeout=10)


def _run_tick(state_dir, *, dry_run=False, instance="test"):
    """Run tick with --json and return parsed result dict."""
    result = _run_tick_raw(state_dir, dry_run=dry_run, instance=instance, json_flag=True)
    assert result.returncode == 0, f"tick failed: {result.stderr}"
    return json.loads(result.stdout)


def _seed_signal(state_dir, summary="Test signal"):
    handle_sensorium_ingest_signal(
        signal={
            "sensor": "test", "source": "manual", "kind": "design_decision",
            "summary": summary, "strength_hint": 0.9,
        },
        instance="test", state_dir=state_dir,
    )


class TestTickSilence:
    """Default tick produces no stdout — safe for cron."""

    def test_default_tick_silent_stdout_empty_state(self, state_dir):
        proc = _run_tick_raw(state_dir)
        assert proc.returncode == 0
        assert proc.stdout.strip() == ""

    def test_default_tick_silent_stdout_with_signal(self, state_dir):
        _seed_signal(state_dir, "Silence test")
        proc = _run_tick_raw(state_dir)
        assert proc.returncode == 0
        assert proc.stdout.strip() == ""

    def test_json_flag_produces_output(self, state_dir):
        proc = _run_tick_raw(state_dir, json_flag=True)
        assert proc.returncode == 0
        out = json.loads(proc.stdout)
        assert out["success"] is True

    def test_dry_run_also_silent(self, state_dir):
        proc = _run_tick_raw(state_dir, dry_run=True)
        assert proc.returncode == 0
        assert proc.stdout.strip() == ""


class TestTickOperations:
    """Tick runs all deterministic operations."""

    def test_runs_compact_service_status(self, state_dir):
        out = _run_tick(state_dir)
        assert out["success"] is True
        assert "compact" in out
        assert "service" in out
        assert "dispatch" not in out
        assert "status" in out

    def test_dry_run_skips_compact_and_service(self, state_dir):
        out = _run_tick(state_dir, dry_run=True)
        assert out["success"] is True
        assert out["dry_run"] is True
        assert "compact" not in out
        assert "service" not in out
        assert "dispatch" not in out
        assert "status" in out

    def test_tick_does_not_run_legacy_dispatch_preview(self, state_dir):
        _seed_signal(state_dir, "Dispatch preview test")
        out = _run_tick(state_dir)
        assert "dispatch" not in out
        assert out["status"]["counts"]["candidates"] == 1

    def test_dispatch_does_not_create_threads(self, state_dir):
        _seed_signal(state_dir, "No thread creation")
        _run_tick(state_dir)
        store = SensoriumStore(instance="test", state_dir=state_dir)
        threads = store.read_jsonl("threads")
        assert len(threads) == 0

    def test_service_included_in_normal_tick(self, state_dir):
        out = _run_tick(state_dir)
        assert "service" in out
        assert isinstance(out["service"]["serviced"], int)


class TestTickReceipts:
    """Tick writes local receipts only."""

    def test_writes_tick_receipt(self, state_dir):
        _run_tick(state_dir)
        store = SensoriumStore(instance="test", state_dir=state_dir)
        decisions = store.read_jsonl("decisions")
        tick_receipts = [d for d in decisions if d.get("type") == "tick.completed"]
        assert len(tick_receipts) == 1
        r = tick_receipts[0]
        assert r["instance"] == "test"
        assert r["dry_run"] is False
        assert "compact" in r["steps"]
        assert "service" in r["steps"]
        assert "dispatch" not in r["steps"]
        assert "status" in r["steps"]
        assert "errors" not in r

    def test_dry_run_no_receipt(self, state_dir):
        _run_tick(state_dir, dry_run=True)
        store = SensoriumStore(instance="test", state_dir=state_dir)
        decisions = store.read_jsonl("decisions")
        tick_receipts = [d for d in decisions if d.get("type") == "tick.completed"]
        assert len(tick_receipts) == 0

    def test_receipt_failure_returns_error_json_and_stderr(self, state_dir, monkeypatch, capsys):
        def boom(self, name, obj):
            raise OSError("receipt denied")

        monkeypatch.setattr(SensoriumStore, "append_jsonl", boom)
        code = sensorium_tick.main([
            "--instance", "test",
            "--state-dir", state_dir,
            "--json",
        ])
        captured = capsys.readouterr()
        assert code == 1
        out = json.loads(captured.out)
        assert out["success"] is False
        assert any("receipt" in err for err in out["errors"])
        assert "receipt" in captured.err


class TestTickErrors:
    def test_top_level_exception_with_json_prints_parseable_error(self, tmp_path):
        bad_state_dir = tmp_path / "not_a_directory"
        bad_state_dir.write_text("file blocks state dir")
        proc = _run_tick_raw(str(bad_state_dir), json_flag=True)
        assert proc.returncode == 1
        out = json.loads(proc.stdout)
        assert out["success"] is False
        assert out["errors"]
        assert "sensorium_tick:" in proc.stderr


class TestTickBodyPressure:
    """Optional body-pressure tick samples cheaply and emits only transition signals."""

    def test_body_pressure_disabled_by_default(self, state_dir, monkeypatch):
        def bad_sample(**_kwargs):
            return {"mem_available_pct": 3.0, "load_per_cpu": 0.1, "swap_used_pct": 0.0}

        monkeypatch.setattr(sensorium_tick, "machine_body_pressure_sample", bad_sample)
        _run_tick(state_dir)
        store = SensoriumStore(instance="test", state_dir=state_dir)
        assert store.read_jsonl("signals") == []

    def test_body_pressure_option_debounces_and_ingests_transition(self, state_dir, monkeypatch):
        def bad_sample(**_kwargs):
            return {"mem_available_pct": 8.0, "load_per_cpu": 0.1, "swap_used_pct": 0.0}

        monkeypatch.setattr(sensorium_tick, "machine_body_pressure_sample", bad_sample)
        for _ in range(2):
            code = sensorium_tick.main(["--instance", "test", "--state-dir", state_dir, "--body-pressure"])
            assert code == 0
            store = SensoriumStore(instance="test", state_dir=state_dir)
            assert store.read_jsonl("signals") == []

        code = sensorium_tick.main(["--instance", "test", "--state-dir", state_dir, "--body-pressure"])
        assert code == 0
        store = SensoriumStore(instance="test", state_dir=state_dir)
        signals = store.read_jsonl("signals")
        assert len(signals) == 1
        assert signals[0]["sensor"] == "sensorium.machine_body_pressure"
        assert signals[0]["transition"] == "healthy_to_degraded"
        assert signals[0]["kind"] == "body_pressure"

    def test_body_pressure_dry_run_does_not_persist_state_or_signal(self, state_dir, monkeypatch):
        def critical_sample(**_kwargs):
            return {"mem_available_pct": 3.0, "load_per_cpu": 0.1, "swap_used_pct": 0.0}

        monkeypatch.setattr(sensorium_tick, "machine_body_pressure_sample", critical_sample)
        for _ in range(3):
            code = sensorium_tick.main([
                "--instance", "test", "--state-dir", state_dir, "--body-pressure", "--dry-run",
            ])
            assert code == 0
        store = SensoriumStore(instance="test", state_dir=state_dir)
        assert store.read_jsonl("signals") == []
        assert not (Path(state_dir) / "body_pressure_state.json").exists()


class TestTickAdditionalPressureSensors:
    """Optional non-body sensors are explicit, local-only, and dry-run safe."""

    def test_all_sensors_flag_ingests_network_process_hindsight_and_kanban(self, state_dir, monkeypatch):
        monkeypatch.setattr(sensorium_tick, "machine_body_pressure_sample", lambda **_: {"mem_available_pct": 80.0, "load_per_cpu": 0.1, "swap_used_pct": 0.0})
        monkeypatch.setattr(sensorium_tick, "machine_network_pressure_sample", lambda **_: {"interfaces": ["eth0"], "non_loopback_interfaces": 1, "rx_errors": 0, "tx_errors": 0, "rx_drops": 0, "tx_drops": 0, "tcp_states": {"CLOSE_WAIT": 25}})
        monkeypatch.setattr(sensorium_tick, "machine_process_pressure_sample", lambda **_: {"process_count": 10, "zombie_count": 2, "uninterruptible_count": 0})
        monkeypatch.setattr(sensorium_tick, "hindsight_pressure_sample", lambda **_: {"api_available": True, "pending_total": 250, "processing_total": 0, "failed_total": 0})
        monkeypatch.setattr(sensorium_tick, "kanban_pressure_sample", lambda **_: {"board_count": 1, "status_counts": {"running": 1}, "failed_tasks": 0, "blocked_tasks": 0, "stale_running_tasks": 1})

        code = sensorium_tick.main(["--instance", "test", "--state-dir", state_dir, "--all-sensors"])

        assert code == 0
        store = SensoriumStore(instance="test", state_dir=state_dir)
        sensors = {sig["sensor"] for sig in store.read_jsonl("signals")}
        assert "sensorium.machine_network_pressure" in sensors
        assert "sensorium.machine_process_pressure" in sensors
        assert "sensorium.hindsight_pressure" in sensors
        assert "sensorium.kanban_pressure" in sensors

    def test_additional_sensors_dry_run_does_not_persist_state_or_signal(self, state_dir, monkeypatch):
        monkeypatch.setattr(sensorium_tick, "machine_network_pressure_sample", lambda **_: {"interfaces": ["eth0"], "non_loopback_interfaces": 1, "rx_errors": 0, "tx_errors": 0, "rx_drops": 0, "tx_drops": 0, "tcp_states": {"CLOSE_WAIT": 25}})
        code = sensorium_tick.main(["--instance", "test", "--state-dir", state_dir, "--network-pressure", "--dry-run"])

        assert code == 0
        store = SensoriumStore(instance="test", state_dir=state_dir)
        assert store.read_jsonl("signals") == []
        assert not (Path(state_dir) / "network_pressure_state.json").exists()


class TestTickNoOutbound:
    """Tick never creates outbound or user-facing records."""

    def test_no_threads_created_even_with_candidates(self, state_dir):
        _seed_signal(state_dir, "Should not become a thread via tick")
        _run_tick(state_dir)
        store = SensoriumStore(instance="test", state_dir=state_dir)
        threads = store.read_jsonl("threads")
        assert len(threads) == 0

    def test_no_outbound_decision_types(self, state_dir):
        _seed_signal(state_dir, "Outbound check")
        _run_tick(state_dir)
        store = SensoriumStore(instance="test", state_dir=state_dir)
        decisions = store.read_jsonl("decisions")
        outbound_types = {
            "message.sent", "task.created", "thread.created",
            "delivery.dispatched", "outbox.submitted",
        }
        for d in decisions:
            assert d.get("type") not in outbound_types, (
                f"Unexpected outbound decision: {d['type']}"
            )


class TestTickDryRunCompat:
    """Dry-run mode is read-only."""

    def test_dry_run_empty_state(self, state_dir):
        out = _run_tick(state_dir, dry_run=True)
        assert out["success"] is True
        assert out["dry_run"] is True
        assert "compact" not in out
        assert "dispatch" not in out
        assert out["status"]["counts"]["signals"] == 0

    def test_dry_run_with_signal(self, state_dir):
        _seed_signal(state_dir, "Tick dry-run test")
        out = _run_tick(state_dir, dry_run=True)
        assert "dispatch" not in out
        assert out["status"]["counts"]["candidates"] == 1


class TestTickNormalRun:
    """Normal tick with data."""

    def test_normal_tick_with_signal(self, state_dir):
        _seed_signal(state_dir, "Tick normal test")
        out = _run_tick(state_dir)
        assert out["success"] is True
        assert out["dry_run"] is False
        assert "dispatch" not in out
        assert out["status"]["counts"]["candidates"] == 1
        assert out["status"]["counts"]["dormant_threads"] == 0

    def test_compact_included(self, state_dir):
        out = _run_tick(state_dir)
        assert "compact" in out
        assert out["compact"]["receipts_written"] == 0
