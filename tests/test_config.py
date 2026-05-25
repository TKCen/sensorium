"""Tests for Phase 6 — instance config and policy cards."""

import json
import os

import pytest

from agent_sensorium.config import (
    SAFE_DEFAULTS,
    apply_sensitivity_policy,
    apply_surface_policy,
    load_instance_config,
    resolve_config_path,
)
from agent_sensorium.tools import handle_sensorium_status


@pytest.fixture
def state_dir(tmp_path):
    return str(tmp_path / "sensorium")


class TestConfigResolution:
    def test_no_config_returns_none(self):
        assert resolve_config_path() is None

    def test_explicit_path_found(self, tmp_path):
        cfg = tmp_path / "custom.json"
        cfg.write_text('{"instance_name": "test"}')
        assert resolve_config_path(config_path=str(cfg)) == cfg

    def test_explicit_path_missing_returns_none(self, tmp_path):
        assert resolve_config_path(config_path=str(tmp_path / "nope.json")) is None

    def test_state_dir_config_found(self, tmp_path):
        sd = tmp_path / "state"
        sd.mkdir()
        cfg = sd / "instance.config.json"
        cfg.write_text("{}")
        assert resolve_config_path(state_dir=str(sd)) == cfg

    def test_state_dir_no_config_returns_none(self, tmp_path):
        sd = tmp_path / "state"
        sd.mkdir()
        assert resolve_config_path(state_dir=str(sd)) is None

    def test_explicit_path_takes_precedence(self, tmp_path):
        sd = tmp_path / "state"
        sd.mkdir()
        (sd / "instance.config.json").write_text("{}")
        custom = tmp_path / "custom.json"
        custom.write_text("{}")
        assert resolve_config_path(config_path=str(custom), state_dir=str(sd)) == custom


class TestConfigLoading:
    def test_missing_config_returns_safe_defaults(self):
        config, diag = load_instance_config()
        assert config["instance_name"] == "default"
        assert config["allowed_surfaces"] == ["local"]
        assert config["max_sensitivity"] == "private"
        assert config["policy_card_ref"] is None
        assert diag["source"] == "defaults"
        assert diag["path"] is None

    def test_safe_defaults_thresholds_present(self):
        config, _ = load_instance_config()
        assert config["thresholds"]["starvation_hours"] == 72
        assert config["thresholds"]["expiring_window_hours"] == 24

    def test_config_file_loaded(self, tmp_path):
        cfg = tmp_path / "instance.config.json"
        cfg.write_text(json.dumps({
            "instance_name": "sera",
            "policy_card_ref": "docs/sera-policy.md",
            "allowed_surfaces": ["local", "discord"],
            "max_sensitivity": "private",
        }))
        config, diag = load_instance_config(config_path=str(cfg))
        assert config["instance_name"] == "sera"
        assert config["policy_card_ref"] == "docs/sera-policy.md"
        assert diag["source"] == "file"
        assert diag["path"] == str(cfg)
        assert diag["policy_card_ref"] == "docs/sera-policy.md"

    def test_corrupt_config_falls_back_to_defaults(self, tmp_path):
        cfg = tmp_path / "instance.config.json"
        cfg.write_text("not json {{{")
        config, diag = load_instance_config(config_path=str(cfg))
        assert config["instance_name"] == "default"
        assert diag["source"] == "defaults"
        assert diag.get("error") == "config_unreadable"

    def test_valid_json_non_object_config_falls_back_to_defaults(self, tmp_path):
        cfg = tmp_path / "instance.config.json"
        cfg.write_text(json.dumps("instance_name"))
        config, diag = load_instance_config(config_path=str(cfg))
        assert config["instance_name"] == "default"
        assert config["allowed_surfaces"] == ["local"]
        assert diag["source"] == "file"

    def test_blank_surface_values_are_ignored(self, tmp_path):
        cfg = tmp_path / "instance.config.json"
        cfg.write_text(json.dumps({"allowed_surfaces": ["discord", "", "  ", "local"]}))
        config, _ = load_instance_config(config_path=str(cfg))
        assert config["allowed_surfaces"] == ["discord", "local"]

    def test_blank_only_surface_values_fall_back_to_safe_default(self, tmp_path):
        cfg = tmp_path / "instance.config.json"
        cfg.write_text(json.dumps({"allowed_surfaces": ["", "  "]}))
        config, _ = load_instance_config(config_path=str(cfg))
        assert config["allowed_surfaces"] == ["local"]

    def test_blank_threshold_values_are_ignored(self, tmp_path):
        cfg = tmp_path / "instance.config.json"
        cfg.write_text(json.dumps({"thresholds": {"starvation_hours": 0, "expiring_window_hours": -4}}))
        config, _ = load_instance_config(config_path=str(cfg))
        assert config["thresholds"]["starvation_hours"] == 72
        assert config["thresholds"]["expiring_window_hours"] == 24

    def test_partial_config_uses_defaults_for_missing(self, tmp_path):
        cfg = tmp_path / "instance.config.json"
        cfg.write_text(json.dumps({"instance_name": "custom"}))
        config, diag = load_instance_config(config_path=str(cfg))
        assert config["instance_name"] == "custom"
        assert config["allowed_surfaces"] == ["local"]
        assert config["max_sensitivity"] == "private"

    def test_invalid_sensitivity_ignored(self, tmp_path):
        cfg = tmp_path / "instance.config.json"
        cfg.write_text(json.dumps({"max_sensitivity": "world_readable"}))
        config, _ = load_instance_config(config_path=str(cfg))
        assert config["max_sensitivity"] == "private"

    def test_invalid_surfaces_type_ignored(self, tmp_path):
        cfg = tmp_path / "instance.config.json"
        cfg.write_text(json.dumps({"allowed_surfaces": "not_a_list"}))
        config, _ = load_instance_config(config_path=str(cfg))
        assert config["allowed_surfaces"] == ["local"]

    def test_empty_surfaces_list_ignored(self, tmp_path):
        cfg = tmp_path / "instance.config.json"
        cfg.write_text(json.dumps({"allowed_surfaces": []}))
        config, _ = load_instance_config(config_path=str(cfg))
        assert config["allowed_surfaces"] == ["local"]

    def test_state_dir_config_loaded(self, tmp_path):
        sd = tmp_path / "state"
        sd.mkdir()
        cfg = sd / "instance.config.json"
        cfg.write_text(json.dumps({"instance_name": "from_state_dir"}))
        config, diag = load_instance_config(state_dir=str(sd))
        assert config["instance_name"] == "from_state_dir"
        assert diag["source"] == "file"

    def test_custom_thresholds_loaded(self, tmp_path):
        cfg = tmp_path / "instance.config.json"
        cfg.write_text(json.dumps({"thresholds": {"starvation_hours": 48}}))
        config, _ = load_instance_config(config_path=str(cfg))
        assert config["thresholds"]["starvation_hours"] == 48
        assert config["thresholds"]["expiring_window_hours"] == 24

    def test_diagnostics_never_expose_raw_config(self, tmp_path):
        cfg = tmp_path / "instance.config.json"
        cfg.write_text(json.dumps({
            "instance_name": "sera",
            "budgets": {"dispatch": {"capacity": 99}},
            "thresholds": {"starvation_hours": 12},
        }))
        _, diag = load_instance_config(config_path=str(cfg))
        assert "budgets" not in diag
        assert "thresholds" not in diag


class TestSurfacePolicy:
    def test_intersects_surfaces(self):
        result = apply_surface_policy(["local", "discord", "telegram"], ["local", "discord"])
        assert result == ["discord", "local"]

    def test_config_cannot_broaden_surfaces(self):
        result = apply_surface_policy(["local"], ["local", "discord", "telegram"])
        assert result == ["local"]

    def test_empty_item_surfaces_stays_empty(self):
        result = apply_surface_policy([], ["local", "discord"])
        assert result == []

    def test_none_item_surfaces_stays_empty(self):
        result = apply_surface_policy(None, ["local", "discord"])
        assert result == []

    def test_no_config_surfaces_returns_item_surfaces(self):
        result = apply_surface_policy(["local", "discord"], None)
        assert result == ["discord", "local"]

    def test_disjoint_surfaces_empty(self):
        result = apply_surface_policy(["telegram"], ["discord"])
        assert result == []

    def test_local_only_item_stays_local_with_broad_config(self):
        result = apply_surface_policy(["local"], ["local", "discord", "telegram", "dashboard"])
        assert result == ["local"]


class TestSensitivityPolicy:
    def test_config_narrows_sensitivity(self):
        assert apply_sensitivity_policy("public_safe", "private") == "private"

    def test_config_cannot_broaden_sensitivity(self):
        assert apply_sensitivity_policy("local_only", "public_safe") == "local_only"

    def test_same_sensitivity_unchanged(self):
        assert apply_sensitivity_policy("private", "private") == "private"

    def test_item_local_only_stays_local_only(self):
        assert apply_sensitivity_policy("local_only", "private") == "local_only"

    def test_item_private_config_local_only_narrows(self):
        assert apply_sensitivity_policy("private", "local_only") == "local_only"

    def test_public_safe_config_local_only_narrows_to_local(self):
        assert apply_sensitivity_policy("public_safe", "local_only") == "local_only"

    def test_all_combinations_never_broaden(self):
        from agent_sensorium.schemas import SENSITIVITY_RANK
        for item_s, item_r in SENSITIVITY_RANK.items():
            for cfg_s, cfg_r in SENSITIVITY_RANK.items():
                result = apply_sensitivity_policy(item_s, cfg_s)
                result_r = SENSITIVITY_RANK[result]
                assert result_r <= item_r, (
                    f"Policy broadened {item_s}({item_r}) with config {cfg_s}({cfg_r}) "
                    f"to {result}({result_r})"
                )


class TestStatusConfigDiagnostics:
    def test_status_includes_config_defaults(self, state_dir):
        raw = handle_sensorium_status(instance="test", state_dir=state_dir)
        result = json.loads(raw)
        assert result["success"] is True
        cfg = result["data"]["config"]
        assert cfg["source"] == "defaults"
        assert cfg["path"] is None
        assert cfg["instance_name"] == "default"
        assert cfg["allowed_surfaces"] == ["local"]
        assert cfg["max_sensitivity"] == "private"

    def test_status_includes_file_config(self, tmp_path):
        sd = str(tmp_path / "sensorium")
        os.makedirs(sd, exist_ok=True)
        cfg_path = tmp_path / "sensorium" / "instance.config.json"
        cfg_path.write_text(json.dumps({
            "instance_name": "sera",
            "policy_card_ref": "sera-policy.md",
            "allowed_surfaces": ["local", "discord"],
        }))
        raw = handle_sensorium_status(instance="test", state_dir=sd)
        result = json.loads(raw)
        cfg = result["data"]["config"]
        assert cfg["source"] == "file"
        assert cfg["instance_name"] == "sera"
        assert cfg["policy_card_ref"] == "sera-policy.md"

    def test_status_with_explicit_config_path(self, tmp_path):
        sd = str(tmp_path / "sensorium")
        cfg_file = tmp_path / "custom-config.json"
        cfg_file.write_text(json.dumps({
            "instance_name": "custom",
            "policy_card_ref": "custom-policy.md",
        }))
        raw = handle_sensorium_status(
            instance="test", state_dir=sd, config_path=str(cfg_file)
        )
        result = json.loads(raw)
        cfg = result["data"]["config"]
        assert cfg["source"] == "file"
        assert cfg["path"] == str(cfg_file)
        assert cfg["instance_name"] == "custom"

    def test_status_config_does_not_expose_raw_budgets(self, tmp_path):
        sd = str(tmp_path / "sensorium")
        os.makedirs(sd, exist_ok=True)
        cfg_path = tmp_path / "sensorium" / "instance.config.json"
        cfg_path.write_text(json.dumps({
            "budgets": {"dispatch": {"capacity": 99}},
        }))
        raw = handle_sensorium_status(instance="test", state_dir=sd)
        result = json.loads(raw)
        cfg = result["data"]["config"]
        assert "budgets" not in cfg
        assert "thresholds" not in cfg


class TestPluginConfigSeams:
    def test_status_schema_includes_config_path(self):
        from agent_sensorium.plugin import register
        from tests.test_plugin_registration import FakePluginContext

        ctx = FakePluginContext()
        register(ctx)
        schema = ctx.tools["sensorium_status"]["schema"]
        props = schema["parameters"]["properties"]
        assert "config_path" in props
        assert props["config_path"]["type"] == "string"

    def test_status_handler_forwards_config_path(self, tmp_path):
        from agent_sensorium.plugin import register
        from tests.test_plugin_registration import FakePluginContext

        cfg_file = tmp_path / "test-config.json"
        cfg_file.write_text(json.dumps({"instance_name": "plugin-test"}))

        ctx = FakePluginContext()
        register(ctx)
        handler = ctx.tools["sensorium_status"]["handler"]
        raw = handler({
            "instance": "test",
            "state_dir": str(tmp_path),
            "config_path": str(cfg_file),
        })
        result = json.loads(raw)
        assert result["success"] is True
        assert result["data"]["config"]["instance_name"] == "plugin-test"
