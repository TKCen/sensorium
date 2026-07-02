import os
import shutil
from pathlib import Path
from agent_sensorium.store import SensoriumStore

def test_path_traversal():
    # Test case 1: instance with traversal when state_dir is NOT provided
    store = SensoriumStore(instance="../traversal_test_dir")
    root = str(store.root)
    print(f"Store root with traversal: {root}")

    if "agent-sensorium/../traversal_test_dir" in root or root.endswith("/traversal_test_dir") and not "agent-sensorium" in root.split("/")[-2:]:
        print("VULNERABILITY CONFIRMED: Path traversal possible in instance name!")
        return True
    else:
        print("Vulnerability not confirmed.")
        return False

if __name__ == "__main__":
    if test_path_traversal():
        exit(0)
    else:
        exit(1)
