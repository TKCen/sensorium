import json
import subprocess
import sys
from pathlib import Path

from agent_sensorium.store import SensoriumStore


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "sensorium_create_conscious_task.py"


def test_create_conscious_task_script_creates_internal_candidate(tmp_path):
    state_dir = tmp_path / "sensorium"
    record = {
        "rationale": "worth one coherent Conscious aperture pass",
        "event_ids": ["evt_aperture_1"],
        "candidate_ids": [],
        "pressure": 0.74,
        "conscious_task": {
            "request_type": "THINK",
            "title": "Review aperture design correction",
            "why": "Subconscious promoted this for later coherent Conscious attention",
            "expected_decision": "Decide whether to save, hold, or prepare external work",
        },
    }

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--instance",
            "test",
            "--state-dir",
            str(state_dir),
            "--record",
            json.dumps(record),
            "--json",
        ],
        text=True,
        capture_output=True,
        check=True,
    )

    payload = json.loads(proc.stdout)
    assert payload["success"] is True
    data = payload["data"]
    assert data["action"] == "created_conscious_task_candidate"
    candidate_id = data["candidate_id"]

    store = SensoriumStore(instance="test", state_dir=str(state_dir))
    candidates = store.read_jsonl("candidates")
    assert [c["id"] for c in candidates] == [candidate_id]
    candidate = candidates[0]
    assert candidate["kind"] == "subconscious_advisory"
    assert candidate["conscious_task"]["request_type"] == "THINK"
    assert candidate["conscious_task"]["title"] == "Review aperture design correction"
    assert store.read_jsonl("worker_requests") == []
    assert store.read_jsonl("threads") == []
