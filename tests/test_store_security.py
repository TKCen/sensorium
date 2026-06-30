import pytest
from agent_sensorium.store import SensoriumStore

def test_store_valid_instance_name():
    """Verify that a normal alphanumeric instance name is accepted."""
    store = SensoriumStore(instance="valid_name-123")
    assert store.instance == "valid_name-123"

def test_store_rejects_path_traversal():
    """Verify that SensoriumStore prevents path traversal in the instance name."""
    traversal_names = [
        "../../../etc/passwd",
        "../evil",
        "/absolute/path",
        "nested/path",
        ".hidden",
        "..",
        ".",
    ]
    for name in traversal_names:
        with pytest.raises(ValueError) as excinfo:
            SensoriumStore(instance=name)
        assert "invalid profile name" in str(excinfo.value) or "profile name" in str(excinfo.value)

def test_store_rejects_blank_or_large_name():
    """Verify that SensoriumStore rejects blank or overly long instance names."""
    with pytest.raises(ValueError, match="profile name must not be blank"):
        SensoriumStore(instance="   ")

    with pytest.raises(ValueError, match="profile name must be at most 64 characters"):
        SensoriumStore(instance="a" * 65)

def test_store_rejects_invalid_characters():
    """Verify that SensoriumStore rejects instance names with shell/path metacharacters."""
    invalid_names = [
        "name; rm -rf /",
        "name$(whoami)",
        "name`id`",
        "name*",
        "name?",
        "name with space",
        "name\x00hidden",
    ]
    for name in invalid_names:
        with pytest.raises(ValueError):
            SensoriumStore(instance=name)
