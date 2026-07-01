"""Security tests for SensoriumStore and profile name validation."""

import pytest
from agent_sensorium.store import SensoriumStore
from agent_sensorium.schemas import sanitize_profile_name

def test_sanitize_profile_name_basic():
    assert sanitize_profile_name("default") == "default"
    assert sanitize_profile_name("demo-123") == "demo-123"
    assert sanitize_profile_name("my.profile_name") == "my.profile_name"

def test_sanitize_profile_name_traversal():
    bad_names = [
        "../secrets",
        "..\\windows",
        "/etc/passwd",
        "../../../../etc/passwd",
        "sub/dir",
        ".hidden",
        "..",
        ".",
    ]
    for name in bad_names:
        with pytest.raises(ValueError, match="invalid profile name"):
            sanitize_profile_name(name)

def test_sanitize_profile_name_invalid_chars():
    bad_names = [
        "name with spaces",
        "name!",
        "name@domain",
        "name\x00null",
    ]
    for name in bad_names:
        with pytest.raises(ValueError):
            sanitize_profile_name(name)

def test_sensorium_store_enforces_validation(tmp_path):
    # Valid name works
    store = SensoriumStore(instance="valid-name", state_dir=str(tmp_path / "sensorium"))
    assert store.instance == "valid-name"

    # Path traversal name fails even if state_dir is provided
    # (because it validates the instance name regardless)
    with pytest.raises(ValueError, match="invalid profile name"):
        SensoriumStore(instance="../malicious", state_dir=str(tmp_path / "sensorium"))

def test_sensorium_store_default_base_traversal(tmp_path, monkeypatch):
    # Mock _DEFAULT_BASE to a temp dir
    base = tmp_path / "base"
    base.mkdir()
    import agent_sensorium.store
    monkeypatch.setattr(agent_sensorium.store, "_DEFAULT_BASE", str(base))

    # Attempting to use a traversal name to escape _DEFAULT_BASE
    with pytest.raises(ValueError, match="invalid profile name"):
        SensoriumStore(instance="../escaped")

    # Normal name stays within base
    store = SensoriumStore(instance="safe")
    assert store.root == base / "safe"
