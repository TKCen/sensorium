"""Tests for the unified registry-driven sensor runner (agent_sensorium.runner)."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent_sensorium import runner
from agent_sensorium.store import SensoriumStore

PY = sys.executable
TICK_SCRIPT = str(Path(__file__).resolve().parent.parent / "scripts" / "sensorium_tick.py")
DEMO_SCRIPT_SENSOR = str(Path(__file__).resolve().parent.parent / "examples" / "demo_script_sensor.py")


@pytest.fixture
def store(tmp_path):
    return SensoriumStore(instance="test", state_dir=str(tmp_path / "sensorium"))


def _script_block(command, *, timeout_seconds=5, defaults=None):
    return {
        "type": "sensor",
        "enabled": True,
        "impl": {"type": "script", "command": command},
        "schedule": {"timeout_seconds": timeout_seconds},
        "defaults": defaults or {},
    }


class TestListSensors:
    def test_builtin_names_always_present(self, store):
        listing = runner.list_sensors(store)
        assert set(listing["builtin"]) == runner.BUILTIN_SENSOR_NAMES
        assert listing["script"] == []

    def test_script_sensor_block_is_listed(self, store):
        store.write_sensor_registry({
            "version": 2,
            "blocks": {"demo_script_sensor": _script_block([PY, DEMO_SCRIPT_SENSOR])},
        })
        listing = runner.list_sensors(store)
        names = {s["name"] for s in listing["script"]}
        assert "demo_script_sensor" in names

    def test_disabled_script_sensor_is_listed_but_marked_disabled(self, store):
        block = _script_block([PY, DEMO_SCRIPT_SENSOR])
        block["enabled"] = False
        store.write_sensor_registry({"version": 2, "blocks": {"demo_script_sensor": block}})
        listing = runner.list_sensors(store)
        entry = next(s for s in listing["script"] if s["name"] == "demo_script_sensor")
        assert entry["enabled"] is False

    def test_non_sensor_blocks_are_not_listed_as_script_sensors(self, store):
        store.write_sensor_registry({
            "version": 2,
            "blocks": {"some_dampener": {"type": "dampener", "enabled": True, "mode": "cooldown"}},
        })
        listing = runner.list_sensors(store)
        assert listing["script"] == []


class TestRunSensorBuiltin:
    def test_unknown_sensor_returns_error_not_exception(self, store):
        step, err = runner.run_sensor(
            "totally_unknown_sensor",
            store=store, config={}, dry_run=True, kw={"instance": "test", "state_dir": str(store.root)},
            instance="test",
        )
        assert err is not None
        assert "unknown sensor" in err
        assert step["sampled"] is False

    def test_builtin_sensor_dispatches_through_override(self, store):
        calls = []

        def fake_sample(**_kwargs):
            calls.append("sampled")
            return {"mem_available_pct": 80.0, "load_per_cpu": 0.1, "swap_used_pct": 0.0}

        step, err = runner.run_sensor(
            "body_pressure",
            store=store, config={}, dry_run=True, kw={"instance": "test", "state_dir": str(store.root)},
            instance="test", sample_fn=fake_sample,
        )
        assert err is None
        assert calls == ["sampled"]
        assert step["sampled"] is True

    def test_builtin_sensor_writes_run_state_telemetry(self, store):
        def fake_sample(**_kwargs):
            return {"mem_available_pct": 80.0, "load_per_cpu": 0.1, "swap_used_pct": 0.0}

        runner.run_sensor(
            "body_pressure",
            store=store, config={}, dry_run=False, kw={"instance": "test", "state_dir": str(store.root)},
            instance="test", sample_fn=fake_sample,
        )
        state = store.read_block_run_state("body_pressure")
        assert state["ok"] is True
        assert "last_run_at" in state
        # Canonical aliases consumed by the T3 dashboard projection reader.
        assert state["status"] == "ok"
        assert state["last_error"] == ""

    def test_dry_run_does_not_write_run_state(self, store):
        def fake_sample(**_kwargs):
            return {"mem_available_pct": 80.0, "load_per_cpu": 0.1, "swap_used_pct": 0.0}

        runner.run_sensor(
            "body_pressure",
            store=store, config={}, dry_run=True, kw={"instance": "test", "state_dir": str(store.root)},
            instance="test", sample_fn=fake_sample,
        )
        assert store.read_block_run_state("body_pressure") == {}


class TestRunScriptBlockContract:
    def test_quiet_success_on_empty_stdout(self, store):
        block = _script_block([PY, "-c", "pass"])
        step, err = runner.run_script_block(
            "quiet_sensor", block, store=store, config={}, dry_run=False,
            kw={"instance": "test", "state_dir": str(store.root)},
        )
        assert err is None
        assert step["emitted"] is False
        state = store.read_block_run_state("quiet_sensor")
        assert state["ok"] is True
        assert state["status"] == "ok"
        assert state["last_error"] == ""

    def test_invalid_json_records_error_and_continues(self, store):
        block = _script_block([PY, "-c", "print('not json {{{')"])
        step, err = runner.run_script_block(
            "bad_json_sensor", block, store=store, config={}, dry_run=False,
            kw={"instance": "test", "state_dir": str(store.root)},
        )
        assert err is not None
        assert step["emitted"] is False
        state = store.read_block_run_state("bad_json_sensor")
        assert state["ok"] is False
        assert state["error"]
        assert state["status"] == "error"
        assert state["last_error"] == state["error"]

    def test_nonzero_exit_records_error_and_continues(self, store):
        block = _script_block([PY, "-c", "import sys; sys.exit(7)"])
        step, err = runner.run_script_block(
            "failing_sensor", block, store=store, config={}, dry_run=False,
            kw={"instance": "test", "state_dir": str(store.root)},
        )
        assert err is not None
        assert step["exit_code"] == 7
        state = store.read_block_run_state("failing_sensor")
        assert state["ok"] is False
        assert state["status"] == "error"
        assert state["last_error"]

    def test_timeout_records_error_and_continues(self, store):
        block = _script_block([PY, "-c", "import time; time.sleep(5)"], timeout_seconds=0.2)
        step, err = runner.run_script_block(
            "slow_sensor", block, store=store, config={}, dry_run=False,
            kw={"instance": "test", "state_dir": str(store.root)},
        )
        assert err is not None
        assert "timeout" in err

    def test_shell_string_command_is_rejected_not_executed(self, store):
        block = _script_block("echo hello")  # shell string, not argv list
        step, err = runner.run_script_block(
            "shell_string_sensor", block, store=store, config={}, dry_run=False,
            kw={"instance": "test", "state_dir": str(store.root)},
        )
        assert err is not None
        assert "argv list" in err

    def test_stderr_content_never_reaches_durable_run_state(self, store):
        secret = "sk-secret-token-do-not-leak"
        block = _script_block([PY, "-c", f"import sys; sys.stderr.write({secret!r}); sys.exit(1)"])
        step, err = runner.run_script_block(
            "leaky_sensor", block, store=store, config={}, dry_run=False,
            kw={"instance": "test", "state_dir": str(store.root)},
        )
        assert err is not None
        assert secret not in err
        assert secret not in json.dumps(step)
        state = store.read_block_run_state("leaky_sensor")
        assert secret not in json.dumps(state)
        assert state["status"] == "error"

    def test_demo_script_sensor_canary_ingests_signal(self, store):
        block = _script_block([PY, DEMO_SCRIPT_SENSOR])
        step, err = runner.run_script_block(
            "demo_script_sensor", block, store=store, config={}, dry_run=False,
            kw={"instance": "test", "state_dir": str(store.root)},
        )
        assert err is None
        assert step["emitted"] is True
        assert step["signals_emitted"] == 1
        signals = store.read_jsonl("signals")
        assert any(s.get("sensor") == "demo_script_sensor" for s in signals)


class TestRunSensorDispatchesScriptBlocks:
    def test_run_sensor_runs_registered_script_sensor(self, store):
        store.write_sensor_registry({
            "version": 2,
            "blocks": {"demo_script_sensor": _script_block([PY, DEMO_SCRIPT_SENSOR])},
        })
        step, err = runner.run_sensor(
            "demo_script_sensor",
            store=store, config={}, dry_run=False, kw={"instance": "test", "state_dir": str(store.root)},
            instance="test",
        )
        assert err is None
        assert step["emitted"] is True


class TestTickCliUnifiedSensorFlag:
    def _run(self, state_dir, *args):
        cmd = [PY, TICK_SCRIPT, "--instance", "test", "--state-dir", str(state_dir), "--json", *args]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10)

    def test_list_sensors_flag(self, tmp_path):
        proc = self._run(tmp_path, "--list-sensors")
        assert proc.returncode == 0
        out = json.loads(proc.stdout)
        assert "body_pressure" in out["builtin"]
        assert "heartbeat" in out["builtin"]

    def test_sensor_flag_runs_heartbeat(self, tmp_path):
        proc = self._run(tmp_path, "--sensor", "heartbeat")
        assert proc.returncode == 0
        out = json.loads(proc.stdout)
        assert out["heartbeat"]["emitted"] is True

    def test_sensor_flag_with_script_sensor_canary(self, tmp_path):
        store = SensoriumStore(instance="test", state_dir=str(tmp_path))
        store.write_sensor_registry({
            "version": 2,
            "blocks": {"demo_script_sensor": _script_block([PY, DEMO_SCRIPT_SENSOR])},
        })
        proc = self._run(tmp_path, "--sensor", "demo_script_sensor")
        assert proc.returncode == 0, proc.stderr
        out = json.loads(proc.stdout)
        assert out["demo_script_sensor"]["emitted"] is True
        signals = store.read_jsonl("signals")
        assert any(s.get("sensor") == "demo_script_sensor" for s in signals)

    def test_sensor_all_runs_builtins_and_script_sensors(self, tmp_path):
        store = SensoriumStore(instance="test", state_dir=str(tmp_path))
        store.write_sensor_registry({
            "version": 2,
            "blocks": {"demo_script_sensor": _script_block([PY, DEMO_SCRIPT_SENSOR])},
        })
        proc = self._run(tmp_path, "--sensor", "all")
        assert proc.returncode == 0, proc.stderr
        out = json.loads(proc.stdout)
        assert "heartbeat" in out
        assert "demo_script_sensor" in out
        assert out["demo_script_sensor"]["emitted"] is True

    def test_unknown_sensor_name_records_error_but_tick_continues(self, tmp_path):
        proc = self._run(tmp_path, "--sensor", "not_a_real_sensor")
        out = json.loads(proc.stdout)
        assert proc.returncode == 1
        assert any("unknown sensor" in e for e in out["errors"])
        # Status/compact/service still ran despite the unknown-sensor error.
        assert "status" in out

    def test_invalid_script_sensor_does_not_abort_tick(self, tmp_path):
        store = SensoriumStore(instance="test", state_dir=str(tmp_path))
        store.write_sensor_registry({
            "version": 2,
            "blocks": {"bad_sensor": _script_block([PY, "-c", "import sys; sys.exit(1)"])},
        })
        proc = self._run(tmp_path, "--sensor", "bad_sensor")
        out = json.loads(proc.stdout)
        assert proc.returncode == 1
        assert out["bad_sensor"]["exit_code"] == 1
        assert "status" in out

    def test_legacy_flag_alias_equivalent_to_sensor_flag(self, tmp_path):
        proc_legacy = self._run(tmp_path / "legacy", "--heartbeat")
        proc_unified = self._run(tmp_path / "unified", "--sensor", "heartbeat")
        legacy_out = json.loads(proc_legacy.stdout)
        unified_out = json.loads(proc_unified.stdout)
        assert legacy_out["heartbeat"]["emitted"] == unified_out["heartbeat"]["emitted"]
