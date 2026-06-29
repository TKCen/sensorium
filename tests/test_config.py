"""Tests for Phase 6 — instance config and policy cards."""

import json
import os

import pytest

from agent_sensorium.config import (
    SAFE_DEFAULTS,
    apply_sensitivity_policy,
    apply_surface_policy,
    load_instance_config,
    manage_attention_policy_config,
    resolve_config_path,
)
from agent_sensorium.gate import DEFAULT_CONFIG as GATE_DEFAULT_CONFIG
from agent_sensorium.store import SensoriumStore
from agent_sensorium.tools import (
    handle_sensorium_attention_policy_manage,
    handle_sensorium_dispatch_once,
    handle_sensorium_improvement_collect,
    handle_sensorium_ingest_signal,
    handle_sensorium_status,
)


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
        assert config["thresholds"]["single_signal_strength"] == 0.75
        assert config["thresholds"]["important_kind_strength"] == 0.6
        assert config["thresholds"]["candidate_pressure"] == 0.65
        assert config["thresholds"]["starvation_hours"] == 72
        assert config["thresholds"]["expiring_window_hours"] == 24
        assert "relational_salience" in config["promote_kinds"]

    def test_safe_defaults_thresholds_and_promote_kinds_match_gate_defaults(self):
        assert SAFE_DEFAULTS["thresholds"]["single_signal_strength"] == GATE_DEFAULT_CONFIG["thresholds"]["single_signal_strength"]
        assert SAFE_DEFAULTS["thresholds"]["important_kind_strength"] == GATE_DEFAULT_CONFIG["thresholds"]["important_kind_strength"]
        assert SAFE_DEFAULTS["thresholds"]["candidate_pressure"] == GATE_DEFAULT_CONFIG["thresholds"]["candidate_pressure"]
        assert SAFE_DEFAULTS["promote_kinds"] == GATE_DEFAULT_CONFIG["promote_kinds"]

    def test_safe_defaults_attention_policy_present(self):
        config, _ = load_instance_config()
        repeated = config["attention_policy"]["evidence_rules"]["repeated_suppression_or_drop"]
        assert repeated["min_count"] == 3
        assert repeated["require_actionable_key"] is True
        assert "sensorium-self-improvement-proof" in repeated["ignore_correlation_keys"]

    def test_config_file_loaded(self, tmp_path):
        cfg = tmp_path / "instance.config.json"
        cfg.write_text(json.dumps({
            "instance_name": "demo",
            "policy_card_ref": "docs/demo-policy.md",
            "allowed_surfaces": ["local", "discord"],
            "max_sensitivity": "private",
        }))
        config, diag = load_instance_config(config_path=str(cfg))
        assert config["instance_name"] == "demo"
        assert config["policy_card_ref"] == "docs/demo-policy.md"
        assert diag["source"] == "file"
        assert diag["path"] == str(cfg)
        assert diag["policy_card_ref"] == "docs/demo-policy.md"

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
        cfg.write_text(json.dumps({
            "thresholds": {
                "single_signal_strength": 0.9,
                "important_kind_strength": 0.4,
                "candidate_pressure": 0.5,
                "starvation_hours": 48,
            },
            "promote_kinds": ["note", "", "note"],
        }))
        config, _ = load_instance_config(config_path=str(cfg))
        assert config["thresholds"]["single_signal_strength"] == 0.9
        assert config["thresholds"]["important_kind_strength"] == 0.4
        assert config["thresholds"]["candidate_pressure"] == 0.5
        assert config["thresholds"]["starvation_hours"] == 48
        assert config["thresholds"]["expiring_window_hours"] == 24
        assert config["promote_kinds"] == ["note"]

    def test_ingest_signal_hotloads_instance_promotion_config(self, tmp_path):
        state = tmp_path / "state"
        state.mkdir()
        cfg = state / "instance.config.json"
        cfg.write_text(json.dumps({
            "thresholds": {
                "single_signal_strength": 0.99,
                "important_kind_strength": 0.3,
                "candidate_pressure": 0.4,
            },
            "promote_kinds": ["note"],
        }))

        first = json.loads(handle_sensorium_ingest_signal(
            instance="test",
            state_dir=str(state),
            signal={
                "sensor": "test",
                "source": "manual",
                "kind": "note",
                "summary": "first note",
                "strength_hint": 0.35,
                "sensitivity": "private",
                "allowed_surfaces": ["local"],
                "correlation_keys": ["first-note"],
            },
        ))
        assert first["data"]["promoted"] is True

        cfg.write_text(json.dumps({
            "thresholds": {
                "single_signal_strength": 0.99,
                "important_kind_strength": 0.9,
                "candidate_pressure": 0.4,
            },
            "promote_kinds": ["other"],
        }))

        second = json.loads(handle_sensorium_ingest_signal(
            instance="test",
            state_dir=str(state),
            signal={
                "sensor": "test",
                "source": "manual",
                "kind": "note",
                "summary": "second note",
                "strength_hint": 0.35,
                "sensitivity": "private",
                "allowed_surfaces": ["local"],
                "correlation_keys": ["second-note"],
            },
        ))
        assert second["data"]["promoted"] is False

    def test_dispatch_once_hotloads_instance_dispatch_threshold(self, tmp_path):
        state = tmp_path / "state"
        state.mkdir()
        cfg = state / "instance.config.json"
        cfg.write_text(json.dumps({"thresholds": {"dispatch_pressure": 0.9}}))
        store = SensoriumStore(instance="test", state_dir=str(state))
        store.ensure_dirs()
        store.append_jsonl("candidates", {
            "id": "cand_hot_dispatch",
            "status": "candidate",
            "kind": "design_decision",
            "pressure": 0.8,
            "summary": "dispatch threshold hotload candidate",
            "event_ids": ["evt_hot_dispatch"],
            "correlation_keys": ["dispatch-hotload"],
            "sensitivity": "private",
            "allowed_surfaces": ["local"],
            "created_at": "2026-06-29T07:00:00Z",
            "updated_at": "2026-06-29T07:00:00Z",
            "expires_at": "",
        })

        first = json.loads(handle_sensorium_dispatch_once(
            instance="test",
            state_dir=str(state),
            dry_run=True,
        ))
        assert first["data"]["action"] == "no_candidate"

        cfg.write_text(json.dumps({"thresholds": {"dispatch_pressure": 0.7}}))
        second = json.loads(handle_sensorium_dispatch_once(
            instance="test",
            state_dir=str(state),
            dry_run=True,
        ))
        assert second["data"]["action"] == "kanban_review_required"
        assert second["data"]["candidate_id"] == "cand_hot_dispatch"

    def test_custom_attention_policy_loaded_and_sanitized(self, tmp_path):
        cfg = tmp_path / "instance.config.json"
        cfg.write_text(json.dumps({
            "attention_policy": {
                "evidence_rules": {
                    "repeated_suppression_or_drop": {
                        "min_count": 5,
                        "require_actionable_key": False,
                        "ignore_correlation_keys": ["", "synthetic", "synthetic"],
                        "unknown_field": "ignored",
                    },
                    "unknown_rule": {"min_count": 99},
                },
                "priority_weights": {"manual_intervention": 1.5, "bad": -2},
            }
        }))
        config, _ = load_instance_config(config_path=str(cfg))
        repeated = config["attention_policy"]["evidence_rules"]["repeated_suppression_or_drop"]
        assert repeated["min_count"] == 5
        assert repeated["require_actionable_key"] is False
        assert repeated["ignore_correlation_keys"] == ["synthetic"]
        assert "unknown_field" not in repeated
        assert "unknown_rule" not in config["attention_policy"]["evidence_rules"]
        assert config["attention_policy"]["priority_weights"] == {"manual_intervention": 1.5}

    def test_diagnostics_never_expose_raw_config(self, tmp_path):
        cfg = tmp_path / "instance.config.json"
        cfg.write_text(json.dumps({
            "instance_name": "demo",
            "budgets": {"dispatch": {"capacity": 99}},
            "thresholds": {"starvation_hours": 12},
        }))
        _, diag = load_instance_config(config_path=str(cfg))
        assert "budgets" not in diag
        assert "thresholds" not in diag


class TestAttentionPolicyMutationSurface:
    def _required(self) -> dict[str, str]:
        return {
            "reason": "Tune repeated noise after conscious review.",
            "future_tendency_delta": "Repeated kanban_pressure drops require more evidence before review.",
            "verification_condition": "Three drops no longer create review; five drops still do.",
            "rollback_condition": "Restore min_count=3 if true misses increase.",
        }

    def test_manage_attention_policy_patches_evidence_rule_atomically(self, tmp_path):
        cfg = tmp_path / "instance.config.json"
        result = manage_attention_policy_config(
            action="patch_evidence_rule",
            config_path=str(cfg),
            rule="repeated_suppression_or_drop",
            patch={"min_count": 5, "unknown": "ignored"},
            actor="test-conscious",
            **self._required(),
        )

        assert result["changed"] is True
        loaded, _ = load_instance_config(config_path=str(cfg))
        repeated = loaded["attention_policy"]["evidence_rules"]["repeated_suppression_or_drop"]
        assert repeated["min_count"] == 5
        assert "unknown" not in repeated
        assert result["receipt"]["type"] == "attention_policy.config_mutation"
        assert result["receipt"]["before"] != result["receipt"]["after"]

    def test_manage_attention_policy_requires_future_tendency_fields(self, tmp_path):
        with pytest.raises(ValueError, match="future_tendency_delta"):
            manage_attention_policy_config(
                action="patch_evidence_rule",
                config_path=str(tmp_path / "instance.config.json"),
                rule="repeated_suppression_or_drop",
                patch={"min_count": 5},
                reason="missing fields",
            )

    def test_manage_attention_policy_rejects_unknown_rule_and_surface_broadening(self, tmp_path):
        with pytest.raises(ValueError, match="rule must be"):
            manage_attention_policy_config(
                action="patch_evidence_rule",
                config_path=str(tmp_path / "instance.config.json"),
                rule="allowed_surfaces",
                patch={"allowed_surfaces": ["discord"]},
                **self._required(),
            )

    def test_tool_handler_writes_receipt_and_policy_file(self, tmp_path):
        raw = handle_sensorium_attention_policy_manage(
            instance="test",
            state_dir=str(tmp_path),
            action="add_ignored_key",
            rule="repeated_suppression_or_drop",
            key="validation-residue",
            actor="test-conscious",
            **self._required(),
        )
        result = json.loads(raw)
        assert result["success"] is True
        loaded, _ = load_instance_config(state_dir=str(tmp_path))
        ignored = loaded["attention_policy"]["evidence_rules"]["repeated_suppression_or_drop"]["ignore_correlation_keys"]
        assert "validation-residue" in ignored
        store = SensoriumStore(instance="test", state_dir=str(tmp_path))
        receipts = [d for d in store.read_jsonl("decisions") if d.get("type") == "attention_policy.config_mutation"]
        assert len(receipts) == 1

    def test_improvement_collect_uses_instance_attention_policy(self, tmp_path):
        cfg = tmp_path / "instance.config.json"
        cfg.write_text(json.dumps({
            "attention_policy": {
                "evidence_rules": {
                    "repeated_suppression_or_drop": {"min_count": 4}
                }
            }
        }))
        store = SensoriumStore(instance="test", state_dir=str(tmp_path))
        store.ensure_dirs()
        for idx in range(3):
            store.append_jsonl("decisions", {
                "ts": "2026-05-29T00:00:00Z",
                "type": "kanban.settlement.applied",
                "decision": "DROP",
                "candidate_id": f"cand_{idx}",
                "candidate_kind": "kanban_pressure",
                "correlation_keys": ["kanban_pressure", "repeat-noise"],
                "reason": "noise",
            })

        raw = handle_sensorium_improvement_collect(instance="test", state_dir=str(tmp_path), dry_run=True)
        result = json.loads(raw)
        assert result["success"] is True
        assert result["data"]["action"] == "no_evidence"


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
            "instance_name": "demo",
            "policy_card_ref": "demo-policy.md",
            "allowed_surfaces": ["local", "discord"],
        }))
        raw = handle_sensorium_status(instance="test", state_dir=sd)
        result = json.loads(raw)
        cfg = result["data"]["config"]
        assert cfg["source"] == "file"
        assert cfg["instance_name"] == "demo"
        assert cfg["policy_card_ref"] == "demo-policy.md"

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
