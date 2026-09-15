import json
import subprocess
import sys
from pathlib import Path

from agent_sensorium.store import SensoriumStore


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "sensorium_create_conscious_task.py"


def test_create_conscious_task_script_creates_internal_candidate(tmp_path):
    state_dir = tmp_path / "sensorium"
    store = SensoriumStore(instance="test", state_dir=str(state_dir))
    store.ensure_dirs()
    store.append_jsonl("events", {
        "id": "evt_aperture_1",
        "ts": "2026-05-26T10:00:00Z",
        "type": "sensor.event.promoted",
        "kind": "design_decision",
        "summary": "Aperture design correction remains unresolved",
        "strength": 0.8,
        "correlation_keys": ["aperture-design"],
        "sensitivity": "private",
        "allowed_surfaces": ["local"],
    })
    store.append_jsonl("candidates", {
        "id": "cand_aperture_1",
        "status": "candidate",
        "kind": "design_decision",
        "summary": "Aperture design correction remains unresolved",
        "pressure": 0.74,
        "event_ids": ["evt_aperture_1"],
        "correlation_keys": ["aperture-design"],
        "sensitivity": "private",
        "allowed_surfaces": ["local"],
        "created_at": "2026-05-26T10:00:00Z",
        "updated_at": "2026-05-26T10:00:00Z",
    })
    record = {
        "rationale": "worth one coherent Conscious aperture pass",
        "event_ids": ["evt_aperture_1"],
        "candidate_ids": ["cand_aperture_1"],
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

    candidates = store.read_jsonl("candidates")
    assert [c["id"] for c in candidates] == ["cand_aperture_1", candidate_id]
    candidate = candidates[1]
    assert candidate["kind"] == "subconscious_advisory"
    assert candidate["conscious_task"]["request_type"] == "THINK"
    assert candidate["conscious_task"]["title"] == "Review aperture design correction"
    assert store.read_jsonl("worker_requests") == []
    assert store.read_jsonl("threads") == []
