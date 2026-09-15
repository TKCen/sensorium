from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

from agent_sensorium import attempts, subconscious
from agent_sensorium.admission import _claim_for_signal, build_admission_plan
from agent_sensorium.admission_index import AdmissionIndex
from agent_sensorium.store import SensoriumStore

ROOT = Path(__file__).parents[1]


def _clock():
    spec = importlib.util.spec_from_file_location(
        "attention_owner_clock", ROOT / "scripts" / "sensorium_native_clock.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _finish(store: SensoriumStore):
    for _ in range(20):
        result = store.prepare_admission_index(scan_bytes=4 * 1024 * 1024)
        if result.complete:
            return result
    raise AssertionError("admission index did not become ready")


def _frontier(signal_id: str, item: str, revision: str) -> dict:
    return {
        "id": signal_id,
        "sensor": "research.frontier",
        "source": "artifact",
        "kind": "creative_pull",
        "summary": revision,
        "artifact_meta": {"entry_id": item, "sha256": revision},
    }


def _candidate(candidate_id: str, event_id: str, pressure: float) -> dict:
    return {
        "id": candidate_id,
        "status": "candidate",
        "kind": "creative_pull",
        "summary": candidate_id,
        "pressure": pressure,
        "created_at": "2026-01-01T00:00:00Z",
        "event_ids": [event_id],
        "correlation_keys": ["attention-owner"],
        "sensitivity": "private",
        "allowed_surfaces": ["local"],
    }


def _args(store: SensoriumStore) -> argparse.Namespace:
    return argparse.Namespace(
        instance=store.instance,
        state_dir=str(store.root),
        plugin_root=str(ROOT),
        event_limit=50,
        candidate_limit=50,
        admission_scan_bytes=4 * 1024 * 1024,
        failure_cooldown_seconds=1800,
        sensor_timeout_seconds=60,
        hermes_timeout_seconds=60,
        total_timeout_seconds=120,
        cleanup_reserve_seconds=10,
        skip_sensors=True,
        force=False,
        print_json=False,
        hermes_cli="/synthetic/hermes",
        provider="fixture",
        model="fixture",
    )


def test_malformed_advisory_metadata_is_ignored_by_lifecycle_owner(tmp_path):
    store = SensoriumStore(instance="metadata", state_dir=str(tmp_path / "metadata"))
    malformed = _candidate("malformed", "none", 0.1)
    malformed.update(
        kind="subconscious_advisory",
        status="archived",
        admission_binding=None,
        advisory_meta=["truthy", "not-a-mapping"],
        source_candidate_ids=["unrelated"],
        source_candidate_fingerprint="old",
    )
    incoming = dict(malformed)
    incoming.update(
        id="incoming",
        admission_binding={"item_key": "wanted", "admission_key": "revision"},
        advisory_meta={"admission_binding": {"item_key": "wanted", "admission_key": "revision"}},
        source_candidate_ids=["source"],
        source_candidate_fingerprint="new",
    )
    before = json.dumps(malformed, sort_keys=True)

    assert subconscious._find_existing_candidate([malformed], incoming) is None
    store.append_jsonl("candidates", malformed)
    refreshed, changed, reason = subconscious._refresh_existing_advisory(
        store, candidates=[malformed], existing=malformed, incoming=incoming
    )

    assert refreshed == malformed and changed is False
    assert reason == "source_changed_but_explicitly_closed"
    assert json.dumps(store.read_jsonl("candidates")[0], sort_keys=True) == before


def test_success_retry_entries_retire_but_live_exhaustion_survives_1001_successes():
    state: dict = {}
    for ordinal in (1, 2, 3):
        attempts.start_attempt(
            state,
            session_purpose="autonomous",
            source_revision="live-exhausted",
            ordinal=ordinal,
            stage="reasoning",
            deadline_seconds=60,
            now=f"2026-01-01T00:0{ordinal}:00Z",
        )
        attempts.terminalize_attempt(
            state, success=False, failure_class="provider_failure",
            now=f"2026-01-01T00:0{ordinal}:01Z",
        )
    for index in range(1001):
        revision = f"successful-{index:04d}"
        attempts.start_attempt(
            state,
            session_purpose="autonomous",
            source_revision=revision,
            ordinal=1,
            stage="reasoning",
            deadline_seconds=60,
            now="2026-01-02T00:00:00Z",
        )
        attempts.terminalize_attempt(state, success=True, disposition_ref="canonical", now="2026-01-02T00:00:01Z")

    allowed, reason, ordinal = attempts.retry_gate(
        state, "live-exhausted", now="2026-01-03T00:00:00Z"
    )
    serialized = (json.dumps(state, indent=2, sort_keys=True) + "\n").encode("utf-8")
    assert (allowed, reason, ordinal) == (False, "retry_exhausted", 4)
    assert set(state["retry_states"]) == {"live-exhausted"}
    assert len(serialized) < 64 * 1024


def test_native_sensor_phase_omits_absent_optional_registry_sensor(tmp_path):
    clock = _clock()
    result = clock._run_sensors(
        instance="absent-registry",
        state_dir=str(tmp_path / "absent"),
        plugin_root=ROOT,
        timeout_seconds=60,
        run_command=subprocess.run,
    )
    assert result["success"] is True


def test_native_sensor_phase_runs_enabled_optional_sensor_once(tmp_path):
    clock = _clock()
    state_root = tmp_path / "enabled"
    marker = tmp_path / "research-source-runs.txt"
    store = SensoriumStore(instance="enabled-registry", state_dir=str(state_root))
    store.write_sensor_registry(
        {
            "version": 2,
            "blocks": {
                "research_source_feeds": {
                    "type": "sensor",
                    "enabled": True,
                    "emits_signals": False,
                    "impl": {
                        "type": "script",
                        "command": [sys.executable, "-c", f"open({str(marker)!r}, 'a').write('run\\n')"],
                    },
                    "schedule": {"timeout_seconds": 10},
                }
            },
        }
    )
    result = clock._run_sensors(
        instance="enabled-registry",
        state_dir=str(state_root),
        plugin_root=ROOT,
        timeout_seconds=60,
        run_command=subprocess.run,
    )
    assert result["success"] is True
    assert marker.read_text(encoding="utf-8").splitlines() == ["run"]


def test_native_sensor_phase_omits_disabled_optional_sensor(tmp_path):
    clock = _clock()
    state_root = tmp_path / "disabled"
    marker = tmp_path / "disabled-runs.txt"
    store = SensoriumStore(instance="disabled-registry", state_dir=str(state_root))
    store.write_sensor_registry(
        {
            "version": 2,
            "blocks": {
                "research_source_feeds": {
                    "type": "sensor",
                    "enabled": False,
                    "emits_signals": False,
                    "impl": {
                        "type": "script",
                        "command": [sys.executable, "-c", f"open({str(marker)!r}, 'a').write('run\\n')"],
                    },
                }
            },
        }
    )
    result = clock._run_sensors(
        instance="disabled-registry",
        state_dir=str(state_root),
        plugin_root=ROOT,
        timeout_seconds=60,
        run_command=subprocess.run,
    )
    assert result["success"] is True
    assert not marker.exists()


def test_native_prompt_bounds_huge_correlation_fields_without_changing_identity(tmp_path):
    clock = _clock()
    store = SensoriumStore(instance="huge-prompt", state_dir=str(tmp_path / "huge"))
    signal = _frontier("signal-huge", "item-huge", "revision-huge")
    event = {
        "id": "event-huge",
        "kind": "creative_pull",
        "summary": "summary" * 1000,
        "source_signal_ids": [signal["id"]],
        "correlation_keys": [("key" * 1000) + str(index) for index in range(100)],
    }
    store.append_jsonl("signals", signal)
    store.append_jsonl("events", event)
    store.append_jsonl("candidates", _candidate("candidate-huge", event["id"], 0.9))
    _finish(store)
    material = clock._source_material(store, event_limit=50, candidate_limit=50)
    identity = material["selection"]["admission_key"]
    prompt = clock._advisory_prompt(material)
    displayed = json.loads(prompt.split("\n\n", 1)[1])["context"]

    assert len(prompt.encode("utf-8")) < 64 * 1024
    assert displayed["source_identity"]["admission_key"] == identity
    assert max(len(value.encode("utf-8")) for value in displayed["events"][0]["correlation_keys"]) <= 512
    assert material["selection"]["admission_key"] == identity


def test_exhausted_top_source_does_not_starve_next_eligible_identity(tmp_path):
    clock = _clock()
    store = SensoriumStore(instance="starvation", state_dir=str(tmp_path / "starvation"))
    for suffix, pressure in (("top", 0.9), ("next", 0.8)):
        signal = _frontier(f"signal-{suffix}", f"item-{suffix}", f"revision-{suffix}")
        store.append_jsonl("signals", signal)
        store.append_jsonl(
            "events",
            {"id": f"event-{suffix}", "source_signal_ids": [signal["id"]], "kind": "creative_pull"},
        )
        store.append_jsonl("candidates", _candidate(f"candidate-{suffix}", f"event-{suffix}", pressure))
    _finish(store)
    top = build_admission_plan(store, candidate_limit=2)["selection"]
    assert top and top["source_candidate_id"] == "candidate-top"
    state_path = store.root / "native_clock_state.json"
    state_path.write_text(
        json.dumps(
            {
                "retry_states": {
                    top["admission_key"]: {
                        "source_revision": top["admission_key"],
                        "last_ordinal": 3,
                        "last_status": "failed",
                        "retry_not_before": None,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    seen_prompts: list[str] = []

    def fake_run(command, **kwargs):
        seen_prompts.append(command[-1])
        return subprocess.CompletedProcess(
            command, 0,
            stdout=json.dumps({"action": "DROP", "rationale": "fixture", "event_ids": [], "candidate_ids": []}),
            stderr="",
        )

    result = clock.run_once(_args(store), run_command=fake_run)
    final_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert result["action"] == "processed_changed_material"
    assert result["source_revision"] != top["admission_key"]
    assert "candidate-next" in seen_prompts[0]
    assert attempts.retry_gate(final_state, top["admission_key"])[1] == "retry_exhausted"


def test_newly_visible_event_refreshes_existing_candidate_without_candidate_rewrite(tmp_path):
    store = SensoriumStore(instance="late-event", state_dir=str(tmp_path / "late-event"))
    candidate = _candidate("candidate", "event-late", 0.8)
    store.append_jsonl("candidates", candidate)
    _finish(store)
    before_binding = build_admission_plan(store)["selection"]
    candidate_bytes = store.paths["candidates"].read_bytes()

    signal = _frontier("signal-late", "item-late", "revision-late")
    store.append_jsonl("signals", signal)
    store.append_jsonl(
        "events",
        {"id": "event-late", "source_signal_ids": ["signal-late"], "kind": "creative_pull"},
    )
    prepared = _finish(store)
    after_binding = build_admission_plan(store)["selection"]
    claim, error = _claim_for_signal(store.instance, signal)

    assert error is None and claim is not None
    assert before_binding and after_binding
    assert before_binding["admission_key"] != after_binding["admission_key"]
    assert after_binding["revision_key"] == claim["revision_key"]
    assert store.paths["candidates"].read_bytes() == candidate_bytes
    assert prepared.dependent_refreshes >= 1


def test_event_candidate_snapshot_interleave_queues_first_insert_dependents(monkeypatch, tmp_path):
    store = SensoriumStore(instance="snapshot-interleave", state_dir=str(tmp_path / "interleave"))
    store.append_jsonl("candidates", _candidate("candidate", "event-late", 0.8))
    candidate_bytes = store.paths["candidates"].read_bytes()
    signal = _frontier("signal-late", "item-late", "revision-late")
    original_open = AdmissionIndex._open_source
    injected = False

    def interleaved_open(index, stream):
        nonlocal injected
        if stream == "candidates" and not injected:
            injected = True
            store.append_jsonl("signals", signal)
            store.append_jsonl(
                "events",
                {"id": "event-late", "source_signal_ids": ["signal-late"], "kind": "creative_pull"},
            )
        return original_open(index, stream)

    monkeypatch.setattr(AdmissionIndex, "_open_source", interleaved_open)
    first = store.prepare_admission_index(scan_bytes=4 * 1024 * 1024)
    assert not first.complete and first.reason == "source_snapshot_changed"
    monkeypatch.setattr(AdmissionIndex, "_open_source", original_open)
    final = _finish(store)
    binding = build_admission_plan(store)["selection"]
    claim, error = _claim_for_signal(store.instance, signal)

    assert final.complete and error is None and claim and binding
    assert binding["revision_key"] == claim["revision_key"]
    assert store.paths["candidates"].read_bytes() == candidate_bytes
