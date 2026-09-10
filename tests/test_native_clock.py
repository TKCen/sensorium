from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from types import ModuleType

from agent_sensorium.store import SensoriumStore, atomic_rewrite_jsonl


def _load_script() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "sensorium_native_clock.py"
    spec = importlib.util.spec_from_file_location("sensorium_native_clock", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _args(tmp_path: Path, **overrides) -> argparse.Namespace:
    values = {
        "instance": "test-native-clock",
        "state_dir": str(tmp_path),
        "plugin_root": str(Path(__file__).parents[1]),
        "event_limit": 50,
        "candidate_limit": 50,
        "failure_cooldown_seconds": 1800,
        "sensor_timeout_seconds": 60,
        "hermes_timeout_seconds": 180,
        "skip_sensors": True,
        "force": False,
        "print_json": False,
        "hermes_cli": "/absolute/hermes",
        "provider": "openai-codex",
        "model": "gpt-5.6-luna",
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _seed_source(store: SensoriumStore) -> None:
    store.ensure_dirs()
    store.append_jsonl(
        "events",
        {
            "id": "evt_native_clock_source",
            "ts": "2026-08-10T00:00:00Z",
            "kind": "creative_pull",
            "summary": "A coherent private creative pull remains unresolved.",
            "strength": 0.9,
            "correlation_keys": ["native-clock-test"],
            "sensitivity": "private",
            "allowed_surfaces": ["local"],
        },
    )
    store.append_jsonl(
        "candidates",
        {
            "id": "cand_native_clock_source",
            "status": "candidate",
            "kind": "creative_pull",
            "summary": "A coherent private creative pull remains unresolved.",
            "pressure": 0.9,
            "event_ids": ["evt_native_clock_source"],
            "correlation_keys": ["native-clock-test"],
            "sensitivity": "private",
            "allowed_surfaces": ["local"],
        },
    )


def _creation_response() -> str:
    return json.dumps(
        {
            "action": "CREATE_CONSCIOUS_TASK",
            "rationale": "One coherent unresolved creative pull deserves a later foreground choice.",
            "event_ids": ["evt_native_clock_source"],
            "candidate_ids": ["cand_native_clock_source"],
            "pressure": 0.72,
            "conscious_task": {
                "request_type": "THINK",
                "title": "Choose what this creative pull wants",
                "why": "The private pattern is coherent but not yet decided.",
                "expected_decision": "Engage, hold, settle, or remain silent.",
            },
        }
    )


def test_changed_material_creates_at_most_one_internal_candidate_then_stays_silent(tmp_path):
    module = _load_script()
    store = SensoriumStore(instance="test-native-clock", state_dir=str(tmp_path))
    _seed_source(store)
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        assert command[0] == "/absolute/hermes"
        assert "kanban" not in command
        assert "CREATE_CONSCIOUS_TASK" in command[-1]
        assert command[command.index("-t") + 1] == "memory"
        assert "--ignore-user-config" not in command
        prompt = command[-1]
        assert "Hindsight recall and reflection are normal parts" in prompt
        payload = json.loads(prompt.split("\n\n", 1)[1])
        assert payload["authority"]["read_only_memory_is_normal_cognition"] is True
        assert payload["authority"]["allowed_tools"] == [
            "hindsight_recall",
            "hindsight_reflect",
        ]
        assert payload["authority"]["forbidden_tools"] == ["memory", "hindsight_retain"]
        return module.subprocess.CompletedProcess(command, 0, stdout=_creation_response(), stderr="")

    first = module.run_once(_args(tmp_path), run_command=fake_run)
    assert first["success"] is True
    assert first["action"] == "processed_changed_material"
    assert first["advisory_action"] == "CREATE_CONSCIOUS_TASK"
    assert first["candidate_id"].startswith("cand_")

    candidates = store.read_jsonl("candidates")
    advisory_candidates = [candidate for candidate in candidates if candidate.get("kind") == "subconscious_advisory"]
    assert len(advisory_candidates) == 1
    assert advisory_candidates[0]["conscious_task"]["request_type"] == "THINK"
    assert store.read_jsonl("threads") == []
    assert store.read_jsonl("worker_requests") == []

    second = module.run_once(_args(tmp_path), run_command=fake_run)
    assert second["success"] is True
    assert second["action"] == "skipped_no_eligible_source"
    assert len(calls) == 1
    assert len([c for c in store.read_jsonl("candidates") if c.get("kind") == "subconscious_advisory"]) == 1


def test_change_signature_and_model_prompt_share_the_same_material(tmp_path):
    module = _load_script()
    store = SensoriumStore(instance="test-native-clock", state_dir=str(tmp_path))
    _seed_source(store)

    material = module._source_material(store, event_limit=50, candidate_limit=50)
    prompt = module._advisory_prompt(material)
    payload = json.loads(prompt.split("\n\n", 1)[1])

    assert payload["context"] == material
    assert payload["authority"]["create_requires_exactly_one_source_candidate_id"] is True
    assert module._signature(payload["context"]) == module._signature(material)


def test_source_material_selects_highest_pressure_candidate_not_largest_id(tmp_path):
    module = _load_script()
    store = SensoriumStore(instance="test-native-clock", state_dir=str(tmp_path))
    store.ensure_dirs()
    for candidate_id, pressure in (("cand_aa_high", 0.9), ("cand_zz_low", 0.1)):
        store.append_jsonl("candidates", {
            "id": candidate_id,
            "status": "candidate",
            "kind": "creative_pull",
            "summary": candidate_id,
            "pressure": pressure,
            "event_ids": [],
            "correlation_keys": ["native-clock-ranking-test"],
            "sensitivity": "private",
            "allowed_surfaces": ["local"],
        })

    material = module._source_material(store, event_limit=1, candidate_limit=1)

    assert material["candidate_order"] == "descending_live_pressure_then_created_at_then_id"
    assert [row["id"] for row in material["candidates"]] == ["cand_aa_high"]
    assert "pressure" not in material["candidates"][0]


def test_legacy_prose_change_does_not_launch_or_append_a_paraphrase(tmp_path):
    module = _load_script()
    store = SensoriumStore(instance="test-native-clock", state_dir=str(tmp_path))
    _seed_source(store)
    calls = 0

    def fake_run(command, **kwargs):
        nonlocal calls
        calls += 1
        response = json.loads(_creation_response())
        response["conscious_task"]["title"] = (
            "Initial wording" if calls == 1 else "A paraphrase after material source change"
        )
        return module.subprocess.CompletedProcess(
            command, 0, stdout=json.dumps(response), stderr="",
        )

    module.run_once(_args(tmp_path), run_command=fake_run)
    candidates = store.read_jsonl("candidates")
    source = next(c for c in candidates if c.get("id") == "cand_native_clock_source")
    source["summary"] = "The creative pull gained materially new evidence."
    store.rewrite_jsonl("candidates", candidates)
    second = module.run_once(_args(tmp_path), run_command=fake_run)

    advisories = [
        c for c in store.read_jsonl("candidates")
        if c.get("kind") == "subconscious_advisory"
    ]
    assert calls == 1
    assert second["action"] == "skipped_no_eligible_source"
    assert len(advisories) == 1
    assert advisories[0]["conscious_task"]["title"] == "Initial wording"


def test_empty_material_is_silent_without_model_or_side_effects(tmp_path):
    module = _load_script()
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        raise AssertionError("empty material must not invoke Hermes")

    result = module.run_once(_args(tmp_path), run_command=fake_run)
    store = SensoriumStore(instance="test-native-clock", state_dir=str(tmp_path))
    assert result["success"] is True
    assert result["action"] == "skipped_empty"
    assert calls == []
    assert store.read_jsonl("candidates") == []
    assert store.read_jsonl("threads") == []
    assert store.read_jsonl("worker_requests") == []


def test_model_failure_is_bounded_and_cooldown_prevents_repeat(tmp_path):
    module = _load_script()
    store = SensoriumStore(instance="test-native-clock", state_dir=str(tmp_path))
    _seed_source(store)
    calls = []

    def failing_run(command, **kwargs):
        calls.append(command)
        return module.subprocess.CompletedProcess(command, 7, stdout="", stderr="provider unavailable")

    first = module.run_once(_args(tmp_path), run_command=failing_run)
    assert first["success"] is False
    assert first["action"] == "subconscious_session_failed"
    assert "provider unavailable" in first["reason"]
    assert store.read_jsonl("candidates")[-1]["id"] == "cand_native_clock_source"
    assert store.read_jsonl("threads") == []

    second = module.run_once(_args(tmp_path), run_command=failing_run)
    assert second["success"] is True
    assert second["action"] == "skipped_retry_cooldown"
    assert len(calls) == 1


def test_pressure_decay_does_not_change_source_signature(tmp_path):
    module = _load_script()
    store = SensoriumStore(instance="test-native-clock", state_dir=str(tmp_path))
    _seed_source(store)
    before = module._signature(module._source_material(store, event_limit=50, candidate_limit=50))
    candidates = store.read_jsonl("candidates")
    candidates[0]["pressure"] = 0.31
    atomic_rewrite_jsonl(store.paths["candidates"], candidates)
    after = module._signature(module._source_material(store, event_limit=50, candidate_limit=50))
    assert after == before


def test_deterministic_sensor_phase_never_invokes_kanban(tmp_path):
    module = _load_script()
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        assert command[0] == module._python_executable(Path(__file__).parents[1])
        assert command[1].endswith("/scripts/sensorium_tick.py")
        assert "--all-sensors" in command
        assert "--codex-usage" in command
        assert "--memory-reflection" in command
        sensor_index = command.index("--sensor")
        assert command[sensor_index + 1] == "research_source_feeds"
        assert "kanban" not in command
        return module.subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"success": True, "instance": "test-native-clock", "status": {}}),
            stderr="",
        )

    result = module._run_sensors(
        instance="test-native-clock",
        state_dir=str(tmp_path),
        plugin_root=Path(__file__).parents[1],
        timeout_seconds=60,
        run_command=fake_run,
    )
    assert result["success"] is True
    assert len(calls) == 1


def test_runtime_defaults_allow_a_full_subconscious_hindsight_window():
    module = _load_script()
    args = module._parser().parse_args([])

    assert args.sensor_timeout_seconds == 240
    assert args.hermes_timeout_seconds == 300
    assert args.total_timeout_seconds == 570
    assert args.cleanup_reserve_seconds == 30


def test_second_clock_instance_fails_soft_when_lock_is_held(tmp_path):
    module = _load_script()
    store = SensoriumStore(instance="test-native-clock", state_dir=str(tmp_path))
    store.ensure_dirs()
    held = module._lock_nonblocking(store.root / "locks" / "native_clock.lock")
    assert held is not None
    try:
        result = module.run_once(
            _args(tmp_path),
            run_command=lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("locked clock must not invoke subprocesses")
            ),
        )
    finally:
        module._release_lock(held)
    assert result["success"] is True
    assert result["action"] == "skipped_locked"
