
import pytest
from agent_sensorium.store import SensoriumStore

def test_sensorium_store_traversal_rejected():
    # Verify that SensoriumStore now rejects traversal attempts in the instance name
    # when no explicit state_dir is provided.
    with pytest.raises(ValueError) as excinfo:
        SensoriumStore(instance="../traversal_test")
    assert "invalid profile name" in str(excinfo.value)

def test_sensorium_store_explicit_path_allowed():
    # Explicit state_dir should still be allowed (useful for tests)
    # even if instance name is weird, because state_dir is the authority.
    store = SensoriumStore(instance="unused", state_dir="/tmp/sensorium_test")
    assert str(store.root) == "/tmp/sensorium_test"
    assert store.instance == "unused"

def test_sensorium_store_default_valid():
    store = SensoriumStore(instance="default")
    assert store.instance == "default"
    assert "default" in str(store.root)
