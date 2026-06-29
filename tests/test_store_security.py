"""Security tests for SensoriumStore."""

import pytest
from agent_sensorium.store import SensoriumStore

def test_store_init_rejects_absolute_path():
    with pytest.raises(ValueError, match="invalid profile name"):
        SensoriumStore(instance="/tmp/evil")

def test_store_init_rejects_path_traversal():
    with pytest.raises(ValueError, match="invalid profile name"):
        SensoriumStore(instance="../../evil")

def test_store_init_rejects_empty_name():
    with pytest.raises(ValueError, match="profile name must not be blank"):
        SensoriumStore(instance="")

def test_store_init_accepts_valid_name():
    store = SensoriumStore(instance="valid_name")
    assert store.instance == "valid_name"
    assert "agent-sensorium/valid_name" in str(store.root)

def test_store_init_with_explicit_state_dir_bypasses_validation():
    # If state_dir is provided, instance name is still sanitized but _root uses state_dir
    store = SensoriumStore(instance="valid", state_dir="/tmp/explicit")
    assert store.instance == "valid"
    assert str(store.root) == "/tmp/explicit"

def test_store_init_with_explicit_state_dir_sanitizes_instance():
    with pytest.raises(ValueError, match="invalid profile name"):
        SensoriumStore(instance="../evil", state_dir="/tmp/explicit")
