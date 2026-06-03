"""Tests for the demo profile seed bootstrap script."""

import json
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import sensorium_demo_seed  # noqa: E402


class TestDemoSeedDryRun:
    def test_default_dry_run_writes_nothing(self, tmp_path, capsys):
        state_dir = tmp_path / "demo"
        rc = sensorium_demo_seed.main(["--state-dir", str(state_dir), "--json"])
        assert rc == 0
        result = json.loads(capsys.readouterr().out)
        assert result["success"] is True
        assert result["applied"] is False
        assert result["dry_run"] is True
        assert result["sensors"]
        assert not (state_dir / "sensors" / "registry.json").exists()


class TestDemoSeedApply:
    def test_apply_writes_registry_with_four_kinds(self, tmp_path, capsys):
        state_dir = tmp_path / "demo"
        rc = sensorium_demo_seed.main(["--apply", "--state-dir", str(state_dir), "--json"])
        assert rc == 0
        result = json.loads(capsys.readouterr().out)
        assert result["success"] is True
        assert result["applied"] is True

        registry_path = state_dir / "sensors" / "registry.json"
        assert registry_path.exists()
        registry = json.loads(registry_path.read_text())
        assert set(registry) == {
            "runtime_heartbeat",
            "machine_body_pressure",
            "machine_network_pressure",
            "machine_process_pressure",
        }
