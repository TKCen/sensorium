import json

import pytest

from agent_sensorium.config import load_instance_config
from agent_sensorium.world_model_provider import (
    FixtureWorldModelProvider,
    WORLD_MODEL_PROTOCOL_VERSION,
    WorldModelProviderAdapter,
    sanitized_world_model_provider_config,
    validate_world_model_provider_config,
)


def envelope(operation="search"):
    return {
        "contract_version": WORLD_MODEL_PROTOCOL_VERSION,
        "accepted_knowledge": "accepted-slice1-only",
        "corpus": {"source_build_name": "fixture", "bundle_name": "fixture", "source_current_digest": "x"},
        "privacy_label": "private",
        "degradation_state": "available",
        "receipt": {"operation": operation, "elapsed_ms": 1, "budget_ms": 300},
        "data": {"results": []},
    }


def test_default_is_disabled_and_has_no_installation_path():
    config = validate_world_model_provider_config({})
    assert config.enabled is False
    assert config.bundle_root is None
    assert config.protocol_version == WORLD_MODEL_PROTOCOL_VERSION


def test_config_is_strict_bounded_and_generic():
    configured = sanitized_world_model_provider_config({
        "enabled": True,
        "protocol_version": WORLD_MODEL_PROTOCOL_VERSION,
        "bundle_root": "/private-install/bundle",
        "timeout_ms": 250,
        "max_search_results": 7,
    })
    assert configured["max_search_results"] == 7
    with pytest.raises(ValueError, match="unknown_field"):
        validate_world_model_provider_config({"enabled": True, "unexpected": True})
    with pytest.raises(ValueError, match="config_rejected"):
        validate_world_model_provider_config({"timeout_ms": 1001})


def test_adapter_is_read_only_and_rejects_unknown_request_fields(tmp_path):
    fixture = FixtureWorldModelProvider({"search": envelope()})
    config = validate_world_model_provider_config({"enabled": True})
    adapter = WorldModelProviderAdapter(fixture, config)
    before = list(tmp_path.iterdir())
    response = adapter.read({"operation": "search", "query": "bounded", "limit": 1})
    assert response["data"]["results"] == []
    assert fixture.calls == [{"operation": "search", "query": "bounded", "limit": 1}]
    assert list(tmp_path.iterdir()) == before
    with pytest.raises(ValueError, match="request_rejected"):
        adapter.read({"operation": "search", "query": "bounded", "path": "/anywhere"})
    with pytest.raises(ValueError, match="request_rejected"):
        adapter.read({"operation": "page", "object_id": "kb:v1:canonical:canonical_page:0123456789abcdef0123", "query": "not allowed"})


def test_adapter_rejects_protocol_or_operation_mismatch():
    bad = envelope()
    bad["contract_version"] = "other"
    adapter = WorldModelProviderAdapter(
        FixtureWorldModelProvider({"search": bad}), validate_world_model_provider_config({"enabled": True})
    )
    with pytest.raises(ValueError, match="response_rejected"):
        adapter.read({"operation": "search", "query": "bounded"})


def test_instance_config_preserves_valid_seam_and_fails_closed_invalid(tmp_path):
    config_path = tmp_path / "instance.config.json"
    config_path.write_text(json.dumps({"world_model_provider": {"enabled": True, "max_search_results": 4}}))
    loaded, diagnostics = load_instance_config(config_path=str(config_path))
    assert loaded["world_model_provider"]["enabled"] is True
    assert loaded["world_model_provider"]["max_search_results"] == 4
    assert "world_model_provider" not in diagnostics
    config_path.write_text(json.dumps({"world_model_provider": {"unknown": True}}))
    loaded, _ = load_instance_config(config_path=str(config_path))
    assert loaded["world_model_provider"]["enabled"] is False
