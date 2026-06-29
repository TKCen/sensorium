"""Tests for hot-reloadable actuator registry and script prepare lane."""

import json
import sys
from pathlib import Path

import pytest

from agent_sensorium.actuators import load_actuator_registry, run_actuator_prepare_artifact
from agent_sensorium.store import SensoriumStore
from agent_sensorium.tools import handle_sensorium_actuator_config, handle_sensorium_actuator_prepare


@pytest.fixture
def store(tmp_path):
    s = SensoriumStore(instance="test", state_dir=str(tmp_path / "sensorium"))
    s.ensure_dirs()
    s.append_jsonl("threads", _thread())
    return s


def _thread():
    return {
        "id": "sth_voice",
        "status": "dormant",
        "origin_candidate_id": "cand_voice",
        "conscious_task": {"id": "ct_voice", "request_type": "PRIVATE_EXPRESSION", "title": "Voice note"},
        "sensitivity": "private",
        "allowed_surfaces": ["local"],
        "created_at": "2026-06-29T07:00:00Z",
        "updated_at": "2026-06-29T07:00:00Z",
    }


def _write_script(path: Path, ref_name: str, *, authorize_delivery: bool = False, marker: Path | None = None):
    marker_line = f"Path({str(marker)!r}).write_text('ran')" if marker else "pass"
    path.write_text(
        "import json\n"
        "from pathlib import Path\n"
        "payload=json.loads(input())\n"
        f"{marker_line}\n"
        "print(json.dumps({\n"
        f"  'delivery_authorized': {str(authorize_delivery)},\n"
        f"  'artifact': {{'kind':'audio','ref_path':'/tmp/{ref_name}','delivery_state':'prepared','allowed_surfaces':['local']}},\n"
        "  'summary': 'prepared compact audio artifact'\n"
        "}))\n"
    )
    return path


def _entry(script: Path, root: Path):
    return {
        "kind": "prepare_artifact",
        "capability": "tts_voice_note",
        "impl": {"type": "script", "command": [sys.executable, str(script)]},
        "script_roots": [str(root)],
        "schedule": {"timeout_seconds": 5},
        "input_contract": {
            "allowed_request_types": ["PRIVATE_EXPRESSION", "REACH_OUT"],
            "requires_conscious_decision": True,
            "max_message_chars": 120,
        },
        "output_contract": {"artifact_kinds": ["audio"], "delivery_authorized": False},
    }


def _request(**overrides):
    req = {
        "request_type": "PRIVATE_EXPRESSION",
        "message": "private voice note text that should not be copied into receipts",
        "thread_id": "sth_voice",
        "candidate_id": "cand_voice",
        "conscious_decision_ref": "dec_voice_1",
        "surface": "discord",
    }
    req.update(overrides)
    return req


def test_actuator_registry_tool_persists_config_without_running_script(store, tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    marker = tmp_path / "ran.txt"
    script = _write_script(scripts / "voice.py", "voice-a.mp3", marker=marker)

    raw = handle_sensorium_actuator_config(
        action="register",
        name="voice_note",
        entry=_entry(script, scripts),
        instance="test",
        state_dir=str(store.root),
    )
    data = json.loads(raw)

    assert data["success"] is True
    assert data["data"]["registry"]["voice_note"]["status"] == "active"
    assert marker.exists() is False
    assert "voice_note" in load_actuator_registry(store)


def test_actuator_prepare_hotloads_registry_between_runs(store, tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    first_script = _write_script(scripts / "voice_a.py", "voice-a.mp3")
    second_script = _write_script(scripts / "voice_b.py", "voice-b.mp3")
    handle_sensorium_actuator_config(
        action="register",
        name="voice_note",
        entry=_entry(first_script, scripts),
        instance="test",
        state_dir=str(store.root),
    )

    first = run_actuator_prepare_artifact(store, name="voice_note", request=_request())
    assert first["success"] is True
    assert first["data"]["artifact"]["ref_path"].endswith("voice-a.mp3")

    handle_sensorium_actuator_config(
        action="modify",
        name="voice_note",
        entry=_entry(second_script, scripts),
        instance="test",
        state_dir=str(store.root),
    )
    second = run_actuator_prepare_artifact(store, name="voice_note", request=_request(conscious_decision_ref="dec_voice_2"))

    assert second["success"] is True
    assert second["data"]["artifact"]["ref_path"].endswith("voice-b.mp3")


def test_actuator_requires_conscious_decision_before_script_runs(store, tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    marker = tmp_path / "ran.txt"
    script = _write_script(scripts / "voice.py", "voice-a.mp3", marker=marker)
    handle_sensorium_actuator_config(
        action="register",
        name="voice_note",
        entry=_entry(script, scripts),
        instance="test",
        state_dir=str(store.root),
    )

    result = run_actuator_prepare_artifact(
        store,
        name="voice_note",
        request=_request(conscious_decision_ref="", decision_ref=""),
    )

    assert result["success"] is False
    assert result["error"] == "missing_conscious_decision_ref"
    assert marker.exists() is False


def test_actuator_rejects_script_direct_delivery_claim(store, tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = _write_script(scripts / "voice.py", "voice-a.mp3", authorize_delivery=True)
    handle_sensorium_actuator_config(
        action="register",
        name="voice_note",
        entry=_entry(script, scripts),
        instance="test",
        state_dir=str(store.root),
    )

    result = run_actuator_prepare_artifact(store, name="voice_note", request=_request())

    assert result["success"] is False
    assert result["error"] == "direct_delivery_not_allowed"
    assert store.read_jsonl("artifacts") == []


def test_actuator_prepare_tool_does_not_copy_message_into_receipts(store, tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = _write_script(scripts / "voice.py", "voice-a.mp3")
    handle_sensorium_actuator_config(
        action="register",
        name="voice_note",
        entry=_entry(script, scripts),
        instance="test",
        state_dir=str(store.root),
    )

    raw = handle_sensorium_actuator_prepare(
        name="voice_note",
        request=_request(),
        instance="test",
        state_dir=str(store.root),
    )
    data = json.loads(raw)
    serialized_decisions = json.dumps(store.read_jsonl("decisions"))

    assert data["success"] is True
    assert data["data"]["data"]["receipt"]["outbound_delivery"] is False
    assert "private voice note text" not in serialized_decisions
