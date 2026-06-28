"""Security tests for SensoriumStore."""

import pytest
from pathlib import Path
from agent_sensorium.store import SensoriumStore

def test_store_init_sanitizes_instance():
    # Valid instance name
    store = SensoriumStore(instance="valid_name")
    assert store.instance == "valid_name"

    # Path traversal attempt
    with pytest.raises(ValueError, match="invalid profile name"):
        SensoriumStore(instance="../evil")

    # Leading dot attempt
    with pytest.raises(ValueError, match="invalid profile name"):
        SensoriumStore(instance=".hidden")

    # Separator attempt
    with pytest.raises(ValueError, match="invalid profile name"):
        SensoriumStore(instance="sub/dir")

def test_store_init_with_explicit_state_dir_still_sanitizes_instance(tmp_path):
    # Even if state_dir is provided, instance should be safe (it might be used elsewhere)
    store = SensoriumStore(instance="valid", state_dir=str(tmp_path))
    assert store.instance == "valid"
    assert store.root == tmp_path

    with pytest.raises(ValueError, match="invalid profile name"):
        SensoriumStore(instance="../evil", state_dir=str(tmp_path))
