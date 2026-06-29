"""Tests for hot-reloadable actuator registry and script prepare lane."""

import json
import os
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


def _write_script(
    path: Path,
    ref_name: str,
    *,
    authorize_delivery: bool = False,
    marker: Path | None = None,
    extra_records: list[dict] | None = None,
):
    marker_line = f"Path({str(marker)!r}).write_text('ran')" if marker else "pass"
    extra_records_py = repr(extra_records or [])
    path.write_text(
        "import json\n"
        "from pathlib import Path\n"
        "payload=json.loads(input())\n"
        f"{marker_line}\n"
        "records = [{\n"
        f"  'delivery_authorized': {str(authorize_delivery)},\n"
        f"  'artifact': {{'kind':'audio','ref_path':'/tmp/{ref_name}','delivery_state':'prepared','allowed_surfaces':['local']}},\n"
        "  'summary': 'prepared compact audio artifact'\n"
        "}]\n"
        f"records.extend({extra_records_py})\n"
        "print(json.dumps(records[0] if len(records) == 1 else records))\n"
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


def test_actuator_empty_or_invalid_request_types_pause_entry(store, tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = _write_script(scripts / "voice.py", "voice-a.mp3")
    entry = _entry(script, scripts)
    entry["input_contract"]["allowed_request_types"] = []

    handle_sensorium_actuator_config(
        action="register",
        name="voice_note",
        entry=entry,
        instance="test",
        state_dir=str(store.root),
    )
    loaded = load_actuator_registry(store)["voice_note"]

    assert loaded["status"] == "paused"
    assert loaded["enabled"] is False
    assert loaded["input_contract"]["allowed_request_types"] == []


def test_actuator_invalid_conscious_gate_defaults_to_required(store, tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    marker = tmp_path / "ran.txt"
    script = _write_script(scripts / "voice.py", "voice-a.mp3", marker=marker)
    entry = _entry(script, scripts)
    entry["input_contract"]["requires_conscious_decision"] = None
    handle_sensorium_actuator_config(
        action="register",
        name="voice_note",
        entry=entry,
        instance="test",
        state_dir=str(store.root),
    )

    result = run_actuator_prepare_artifact(store, name="voice_note", request=_request(conscious_decision_ref=""))

    assert result["success"] is False
    assert result["error"] == "missing_conscious_decision_ref"
    assert marker.exists() is False


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


def test_actuator_expands_user_paths_before_script_execution(store, tmp_path, monkeypatch):
    home = tmp_path / "home"
    scripts = home / "scripts"
    scripts.mkdir(parents=True)
    script = _write_script(scripts / "voice.py", "voice-home.mp3")
    monkeypatch.setenv("HOME", str(home))
    entry = _entry(script, scripts)
    entry["impl"]["command"] = [sys.executable, "~/scripts/voice.py"]
    entry["script_roots"] = ["~/scripts"]
    handle_sensorium_actuator_config(
        action="register",
        name="voice_note",
        entry=entry,
        instance="test",
        state_dir=str(store.root),
    )

    result = run_actuator_prepare_artifact(store, name="voice_note", request=_request())

    assert result["success"] is True
    assert result["data"]["artifact"]["ref_path"].endswith("voice-home.mp3")


def test_actuator_executes_resolved_bare_script_not_path_search(store, tmp_path, monkeypatch):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "prepare_note"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "json.loads(sys.stdin.read())\n"
        "print(json.dumps({'artifact': {'kind': 'audio', 'ref_path': '/tmp/resolved.mp3', 'delivery_state': 'prepared', 'allowed_surfaces': ['local']}}))\n"
    )
    script.chmod(0o755)
    hijack = tmp_path / "hijack"
    hijack.mkdir()
    (hijack / "prepare_note").write_text("#!/bin/sh\necho '{bad json'\n")
    (hijack / "prepare_note").chmod(0o755)
    monkeypatch.chdir(scripts)
    monkeypatch.setenv("PATH", f"{hijack}:{os.environ.get('PATH', '')}")
    entry = _entry(script, scripts)
    entry["impl"]["command"] = ["prepare_note"]
    handle_sensorium_actuator_config(
        action="register",
        name="voice_note",
        entry=entry,
        instance="test",
        state_dir=str(store.root),
    )

    result = run_actuator_prepare_artifact(store, name="voice_note", request=_request())

    assert result["success"] is True
    assert result["data"]["artifact"]["ref_path"].endswith("resolved.mp3")


def test_actuator_partial_modify_preserves_nested_restrictions(store, tmp_path):
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

    handle_sensorium_actuator_config(
        action="modify",
        name="voice_note",
        entry={"input_contract": {"max_message_chars": 40}},
        instance="test",
        state_dir=str(store.root),
    )
    loaded = load_actuator_registry(store)["voice_note"]

    assert loaded["input_contract"]["max_message_chars"] == 40
    assert loaded["input_contract"]["allowed_request_types"] == ["PRIVATE_EXPRESSION", "REACH_OUT"]
    assert loaded["output_contract"]["artifact_kinds"] == ["audio"]


def test_actuator_script_hash_tracks_script_content(store, tmp_path):
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
    first = run_actuator_prepare_artifact(store, name="voice_note", request=_request(conscious_decision_ref="dec_hash_1"))
    first_hash = first["data"]["receipt"]["script_hash"]

    _write_script(script, "voice-b.mp3")
    second = run_actuator_prepare_artifact(store, name="voice_note", request=_request(conscious_decision_ref="dec_hash_2"))

    assert second["success"] is True
    assert second["data"]["receipt"]["script_hash"] != first_hash


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


def test_actuator_rejects_delivery_claim_in_later_script_record(store, tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = _write_script(
        scripts / "voice.py",
        "voice-a.mp3",
        extra_records=[{"outbound_delivery": True, "summary": "must be rejected"}],
    )
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


def test_actuator_rejects_multiple_script_records_without_delivery_claim(store, tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = _write_script(scripts / "voice.py", "voice-a.mp3", extra_records=[{"summary": "extra"}])
    handle_sensorium_actuator_config(
        action="register",
        name="voice_note",
        entry=_entry(script, scripts),
        instance="test",
        state_dir=str(store.root),
    )

    result = run_actuator_prepare_artifact(store, name="voice_note", request=_request())

    assert result["success"] is False
    assert result["error"] == "invalid_script_result_count"
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


def test_actuator_bounds_malformed_decision_ref_before_receipt(store, tmp_path):
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
    secret_ref = "full private decision note with sk-secret-token and raw prompt text" * 4

    result = run_actuator_prepare_artifact(
        store,
        name="voice_note",
        request=_request(conscious_decision_ref=secret_ref),
    )
    decisions = store.read_jsonl("decisions")
    serialized_decisions = json.dumps(decisions)
    receipt = result["data"]["receipt"]

    assert result["success"] is True
    assert receipt["conscious_decision_ref"].startswith("decision_")
    assert secret_ref not in serialized_decisions
    assert "sk-secret-token" not in serialized_decisions
