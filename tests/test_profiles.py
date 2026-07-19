"""Profile model + config-seam tests for the generic Sensorium boundary.

Covers the new instance-config fields (default_actor, subconscious_profile,
tick_quiet_filename, tts), the profile-name validator, the profile helpers, and
the `sensorium_profile` admin handler.
"""

import json
from types import SimpleNamespace

import pytest

from agent_sensorium.config import (
    DEFAULT_TTS_CONFIG,
    init_profile_config,
    list_profiles,
    load_instance_config,
    profile_state_dir,
    read_active_profile,
    sanitize_profile_name,
    write_active_profile,
)
from agent_sensorium.tools import handle_sensorium_profile


class TestNewConfigDefaults:
    def test_defaults_are_generic(self):
        config, _ = load_instance_config()
        assert config["default_actor"] == "background_conscious"
        assert config["subconscious_profile"] == "serasubconscious"
        assert config["tick_quiet_filename"] == "sensorium_tick_quiet.latest.json"
        assert config["tts"]["voice"] == "warm-voice-demo"
        # No private checkout path is baked into the TTS defaults.
        assert config["tts"]["sidecar_base"] is None
        assert config["tts"]["control_command"] is None
        assert config["tts"]["pid_file"] is None
        assert DEFAULT_TTS_CONFIG["voice"] == "warm-voice-demo"

    def test_overrides_are_honored(self, tmp_path):
        cfg_path = tmp_path / "instance.config.json"
        cfg_path.write_text(json.dumps({
            "default_actor": "ops_conscious",
            "subconscious_profile": "cheap_reviewer",
            "tick_quiet_filename": "my_quiet.latest.json",
            "tts": {"voice": "studio-voice", "sidecar_base": "/opt/tts"},
        }))
        config, _ = load_instance_config(config_path=str(cfg_path))
        assert config["default_actor"] == "ops_conscious"
        assert config["subconscious_profile"] == "cheap_reviewer"
        assert config["tick_quiet_filename"] == "my_quiet.latest.json"
        assert config["tts"]["voice"] == "studio-voice"
        assert config["tts"]["sidecar_base"] == "/opt/tts"
        # Unspecified tts keys keep generic defaults.
        assert config["tts"]["model"] == DEFAULT_TTS_CONFIG["model"]

    def test_tick_quiet_filename_rejects_path_traversal(self, tmp_path):
        cfg_path = tmp_path / "instance.config.json"
        cfg_path.write_text(json.dumps({"tick_quiet_filename": "../escape.json"}))
        config, _ = load_instance_config(config_path=str(cfg_path))
        assert config["tick_quiet_filename"] == "sensorium_tick_quiet.latest.json"


class TestProfileNameValidation:
    @pytest.mark.parametrize("name", ["default", "demo", "prod", "team-a", "p_1", "v0.1"])
    def test_valid_names(self, name):
        assert sanitize_profile_name(name) == name

    @pytest.mark.parametrize("name", ["", "  ", "..", ".", "../x", "a/b", "a\\b", ".hidden", "x" * 65])
    def test_invalid_names_raise(self, name):
        with pytest.raises(ValueError):
            sanitize_profile_name(name)


class TestProfileHelpers:
    def test_init_list_and_active_roundtrip(self, tmp_path):
        base = str(tmp_path)
        result = init_profile_config("demo", base_dir=base)
        assert result["created"] is True
        assert result["profile"] == "demo"
        # Config file written with generic defaults.
        assert result["config"]["instance_name"] == "demo"
        assert result["config"]["default_actor"] == "background_conscious"

        # Idempotent: second init does not overwrite.
        again = init_profile_config("demo", base_dir=base)
        assert again["created"] is False

        init_profile_config("ops", base_dir=base)
        assert list_profiles(base) == ["demo", "ops"]

        assert read_active_profile(base) is None
        marker = write_active_profile("ops", base_dir=base)
        assert marker.exists()
        assert read_active_profile(base) == "ops"

    def test_profile_state_dir_rejects_traversal(self, tmp_path):
        with pytest.raises(ValueError):
            profile_state_dir("../evil", base_dir=str(tmp_path))


class TestDefaultInstanceBoundary:
    @pytest.mark.parametrize("source", ["../outside", "/tmp/outside", ".hidden", "bad/name"])
    def test_unsafe_environment_default_falls_back_to_generic_default(self, monkeypatch, source):
        monkeypatch.setenv("AGENT_SENSORIUM_DEFAULT_INSTANCE", source)
        monkeypatch.delenv("SENSORIUM_INSTANCE", raising=False)
        monkeypatch.setattr("agent_sensorium.config.read_active_profile", lambda: None)
        monkeypatch.setattr("importlib.import_module", lambda _name: (_ for _ in ()).throw(ImportError()))
        from agent_sensorium.config import default_instance_name

        assert default_instance_name("default") == "default"

    def test_unsafe_active_marker_is_skipped_for_safe_hermes_default(self, monkeypatch):
        monkeypatch.delenv("AGENT_SENSORIUM_DEFAULT_INSTANCE", raising=False)
        monkeypatch.delenv("SENSORIUM_INSTANCE", raising=False)
        monkeypatch.setattr("agent_sensorium.config.read_active_profile", lambda: "../outside")
        monkeypatch.setattr("importlib.import_module", lambda _name: (_ for _ in ()).throw(ImportError()))

        from agent_sensorium.config import default_instance_name

        assert default_instance_name("default") == "default"

    def test_unsafe_hermes_default_falls_back_to_generic_default(self, monkeypatch):
        monkeypatch.delenv("AGENT_SENSORIUM_DEFAULT_INSTANCE", raising=False)
        monkeypatch.delenv("SENSORIUM_INSTANCE", raising=False)
        monkeypatch.setattr("agent_sensorium.config.read_active_profile", lambda: None)
        hermes_config = SimpleNamespace(
            load_config=lambda: {},
            cfg_get=lambda *_args, **_kwargs: "../outside",
        )
        monkeypatch.setattr("importlib.import_module", lambda _name: hermes_config)
        from agent_sensorium.config import default_instance_name

        assert default_instance_name("default") == "default"


class TestProfileHandler:
    def test_list_includes_active(self, tmp_path):
        base = str(tmp_path)
        init_profile_config("demo", base_dir=base)
        write_active_profile("demo", base_dir=base)
        out = json.loads(handle_sensorium_profile(action="list", base_dir=base))
        assert out["success"] is True
        assert out["data"]["active_profile"] == "demo"
        names = {p["profile"] for p in out["data"]["profiles"]}
        assert "demo" in names

    def test_init_then_show(self, tmp_path):
        base = str(tmp_path)
        created = json.loads(handle_sensorium_profile(action="init", profile="demo", base_dir=base))
        assert created["success"] is True
        assert created["data"]["created"] is True

        shown = json.loads(handle_sensorium_profile(action="show", profile="demo", base_dir=base))
        assert shown["success"] is True
        assert shown["data"]["config"]["instance_name"] == "demo"

    def test_set_default(self, tmp_path):
        base = str(tmp_path)
        init_profile_config("ops", base_dir=base)
        out = json.loads(handle_sensorium_profile(action="set_default", profile="ops", base_dir=base))
        assert out["success"] is True
        assert read_active_profile(base) == "ops"

    def test_invalid_action_errors(self, tmp_path):
        out = json.loads(handle_sensorium_profile(action="nuke", base_dir=str(tmp_path)))
        assert out["success"] is False
        assert "Invalid action" in out["error"]

    def test_init_requires_name(self, tmp_path):
        out = json.loads(handle_sensorium_profile(action="init", profile="", base_dir=str(tmp_path)))
        assert out["success"] is False

    def test_bad_profile_name_errors(self, tmp_path):
        out = json.loads(handle_sensorium_profile(action="init", profile="../evil", base_dir=str(tmp_path)))
        assert out["success"] is False
