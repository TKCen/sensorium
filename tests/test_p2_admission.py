from __future__ import annotations

import argparse
import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType

from agent_sensorium import admission, attempts, subconscious
from agent_sensorium.admission import (
    binding_for_candidate, build_admission_plan, validate_admission_binding,
)
from agent_sensorium.conscious_aperture import open_conscious_aperture, settle_conscious_aperture_item
from agent_sensorium.live_turn import normalize_live_turn_intent, should_ingest_live_residue
from agent_sensorium.sensors import classify_hindsight_pressure, classify_machine_body_pressure
from agent_sensorium.store import SensoriumStore
from agent_sensorium.subconscious import run_subconscious_advisory
from agent_sensorium.tools import handle_sensorium_ingest_signal

ROOT = Path(__file__).parents[1]


def _store(tmp_path, instance="p2") -> SensoriumStore:
    store = SensoriumStore(instance=instance, state_dir=str(tmp_path / instance))
    store.ensure_dirs()
    return store


def _signal(*, signal_id: str, item: str, revision: str | None = None,
            family: str = "research", summary: str = "Shared topic", strength: float = 0.9) -> dict:
    meta = (
        {"source_id": "feed-A", "item_id": item}
        if family == "research" else
        {"entry_id": item, "sha256": revision}
    )
    return {
        "id": signal_id,
        "sensor": "research.source_feed" if family == "research" else "research.frontier",
        "source": "artifact",
        "kind": "creative_pull",
        "summary": summary,
        "actor": "tool",
        "strength_hint": strength,
        "sensitivity": "private",
        "allowed_surfaces": ["local"],
        "correlation_keys": ["topic:shared", "channel:research"],
        "artifact_meta": meta,
    }


def _ingest(store: SensoriumStore, signal: dict) -> dict:
    return json.loads(handle_sensorium_ingest_signal(
        signal=signal, instance=store.instance, state_dir=str(store.root),
    ))["data"]


def _memory_signal(signal_id: str, summary: str, item_ids: list[str]) -> dict:
    return {
        "id": signal_id,
        "sensor": "sensorium.memory_reflection",
        "source": "memory",
        "kind": "memory_reflection",
        "summary": summary,
        "actor": "tool",
        "strength_hint": 0.9,
        "sensitivity": "private",
        "allowed_surfaces": ["local"],
        "correlation_keys": ["memory-reflection:synthetic"],
        "memory_provenance": {
            "provider": "hindsight",
            "bank_id": "synthetic-bank",
            "item_ids": item_ids,
        },
        "unverified": True,
    }


def _drop(candidate_id: str | None = None) -> dict:
    return {
        "action": "DROP",
        "rationale": "Synthetic fixture is settled.",
        "event_ids": [],
        "candidate_ids": [candidate_id] if candidate_id else [],
    }


def _create(candidate_id: str) -> dict:
    return {
        "action": "CREATE_CONSCIOUS_TASK",
        "rationale": "Synthetic unresolved source merits one choice.",
        "event_ids": [],
        "candidate_ids": [candidate_id],
        "pressure": 0.7,
        "conscious_task": {
            "request_type": "THINK",
            "title": "Synthetic source review",
            "why": "One source-owned revision remains unresolved.",
            "expected_decision": "Settle, save, or hold.",
        },
    }


def _load_clock() -> ModuleType:
    path = ROOT / "scripts" / "sensorium_native_clock.py"
    spec = importlib.util.spec_from_file_location("p2_native_clock", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _clock_args(store: SensoriumStore, **overrides) -> argparse.Namespace:
    values = {
        "instance": store.instance, "state_dir": str(store.root),
        "plugin_root": str(ROOT), "event_limit": 50, "candidate_limit": 1,
        "failure_cooldown_seconds": 1800, "sensor_timeout_seconds": 60,
        "hermes_timeout_seconds": 60, "total_timeout_seconds": 120,
        "cleanup_reserve_seconds": 5, "skip_sensors": True, "force": False,
        "print_json": False, "hermes_cli": "/synthetic/hermes",
        "provider": "fake", "model": "fake",
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_01_hundred_decided_ticks_survive_order_pressure_window_wrappers_and_heartbeat(tmp_path):
    clock = _load_clock()
    store = _store(tmp_path, "p2-hundred")
    candidate_ids = []
    for item in ("item-A", "item-B"):
        result = _ingest(store, _signal(signal_id=f"sig-{item}", item=item))
        candidate_ids.append(result["candidate_id"])
    for candidate_id in candidate_ids:
        binding, error = binding_for_candidate(store, candidate_id)
        assert error is None and binding
        assert run_subconscious_advisory(
            store, advisory_output=_drop(candidate_id), admission_binding=binding,
            enabled=True, dry_run=False,
        )["action"] == "drop"

    # Direct-Conscious heartbeat noise is deliberately outside Subconscious.
    store.append_jsonl("candidates", {
        "id": "cand-heartbeat", "status": "candidate", "kind": "body_pressure",
        "pressure": 1.0, "summary": "mechanical heartbeat", "event_ids": [],
        "correlation_keys": ["machine"], "created_at": "2026-09-09T00:00:00Z",
    })
    model_calls = 0

    def forbidden(command, **kwargs):
        nonlocal model_calls
        model_calls += 1
        raise AssertionError("decided revisions must not launch a model")

    decisions_before = len([d for d in store.read_jsonl("decisions") if d.get("type") == "subconscious.advisory"])
    for tick in range(100):
        rows = store.read_jsonl("candidates")
        source_rows = [row for row in rows if row.get("id") in candidate_ids]
        other_rows = [row for row in rows if row.get("id") not in candidate_ids]
        for index, row in enumerate(source_rows):
            row["pressure"] = 0.9 if (tick + index) % 2 else 0.1
        store.rewrite_jsonl("candidates", list(reversed(source_rows)) + other_rows)
        if tick % 10 == 0:
            replay = _signal(signal_id=f"wrapper-{tick}", item="item-A")
            assert _ingest(store, replay)["duplicate"] is True
        result = clock.run_once(_clock_args(store), run_command=forbidden)
        assert result["action"] == "skipped_no_eligible_source"
    assert model_calls == 0
    assert len([c for c in store.read_jsonl("candidates") if c.get("kind") == "subconscious_advisory"]) == 0
    assert len([d for d in store.read_jsonl("decisions") if d.get("type") == "subconscious.advisory"]) == decisions_before == 2


def test_02_unseen_material_revision_calls_once_then_quiet_with_prior_disposition_in_prompt(tmp_path):
    clock = _load_clock()
    store = _store(tmp_path, "p2-unseen")
    first = _ingest(store, _signal(signal_id="frontier-1", item="entry-A", revision="digest-v1", family="frontier"))
    binding1, _ = binding_for_candidate(store, first["candidate_id"])
    run_subconscious_advisory(store, advisory_output=_drop(first["candidate_id"]),
                              admission_binding=binding1, enabled=True, dry_run=False)
    changed = _ingest(store, _signal(signal_id="frontier-2", item="entry-A", revision="digest-v2", family="frontier"))
    assert changed["candidate_id"] == first["candidate_id"]
    calls = 0

    def fake(command, **kwargs):
        nonlocal calls
        calls += 1
        payload = json.loads(command[-1].split("\n\n", 1)[1])
        context = payload["context"]
        assert context["selection"]["revision_key"] != binding1["revision_key"]
        assert context["source_decisions"]
        return clock.subprocess.CompletedProcess(command, 0, stdout=json.dumps(_drop()), stderr="")

    first_tick = clock.run_once(_clock_args(store), run_command=fake)
    second_tick = clock.run_once(_clock_args(store), run_command=fake)
    assert first_tick["action"] == "processed_changed_material"
    assert first_tick["prompt_sha256"] and len(first_tick["prompt_sha256"]) == 64
    assert second_tick["action"] == "skipped_no_eligible_source"
    assert calls == 1


def test_03_distinct_items_wrapper_replay_material_revision_and_transactional_concurrency(tmp_path):
    store = _store(tmp_path, "p2-identity")
    a = _ingest(store, _signal(signal_id="a-1", item="item-A"))
    b = _ingest(store, _signal(signal_id="b-1", item="item-B"))
    assert a["candidate_id"] != b["candidate_id"]
    assert _ingest(store, _signal(signal_id="a-wrapper", item="item-A"))["duplicate"] is True
    assert len([c for c in store.read_jsonl("candidates") if c.get("kind") == "creative_pull"]) == 2

    frontier = _ingest(store, _signal(signal_id="f-1", item="entry-X", revision="rev-one", family="frontier", summary="Frontier"))
    binding1, _ = binding_for_candidate(store, frontier["candidate_id"])
    run_subconscious_advisory(store, advisory_output=_drop(frontier["candidate_id"]),
                              admission_binding=binding1, enabled=True, dry_run=False)
    _ingest(store, _signal(signal_id="f-2", item="entry-X", revision="rev-two", family="frontier", summary="Frontier changed"))
    binding2, _ = binding_for_candidate(store, frontier["candidate_id"])
    assert binding2["item_key"] == binding1["item_key"]
    assert binding2["revision_key"] != binding1["revision_key"]
    assert build_admission_plan(store)["selection"]["admission_key"] == binding2["admission_key"]

    # A fresh source is applied twice concurrently; one canonical representation wins.
    fresh = _ingest(store, _signal(signal_id="c-1", item="item-C", summary="Concurrent"))
    binding_c, _ = binding_for_candidate(store, fresh["candidate_id"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run_subconscious_advisory(
            store, advisory_output=_create(fresh["candidate_id"]),
            admission_binding=binding_c, enabled=True, dry_run=False,
        ), range(2)))
    assert sorted(result["action"] for result in results) == ["already_exists", "created_conscious_task_candidate"]
    advisories = [c for c in store.read_jsonl("candidates") if c.get("kind") == "subconscious_advisory"]
    assert len(advisories) == 1
    assert advisories[0]["admission_binding"]["admission_key"] == binding_c["admission_key"]


def test_03b_material_revision_reuses_one_canonical_item_transactionally(tmp_path):
    store = _store(tmp_path, "p2-revision-reuse")
    first = _ingest(store, _signal(
        signal_id="rev-1", item="entry-reuse", revision="opaque-one",
        family="frontier", summary="Initial frontier state",
    ))
    binding1, error = binding_for_candidate(store, first["candidate_id"])
    assert error is None and binding1 is not None
    created = run_subconscious_advisory(
        store, advisory_output=_create(first["candidate_id"]),
        admission_binding=binding1, enabled=True, dry_run=False,
    )
    _ingest(store, _signal(
        signal_id="rev-2", item="entry-reuse", revision="opaque-two",
        family="frontier", summary="Changed frontier state",
    ))
    binding2, error = binding_for_candidate(store, first["candidate_id"])
    assert error is None and binding2 is not None
    assert binding2["item_key"] == binding1["item_key"]
    assert binding2["admission_key"] != binding1["admission_key"]
    updated = run_subconscious_advisory(
        store, advisory_output=_create(first["candidate_id"]),
        admission_binding=binding2, enabled=True, dry_run=False,
    )
    assert updated["action"] == "updated_conscious_task_candidate"
    assert updated["candidate_id"] == created["candidate_id"]
    advisories = [c for c in store.read_jsonl("candidates") if c.get("kind") == "subconscious_advisory"]
    assert len(advisories) == 1
    assert advisories[0]["admission_binding"]["admission_key"] == binding2["admission_key"]


def test_04_source_owned_support_is_distinct_from_copied_internal_interpretation(tmp_path):
    store = _store(tmp_path, "p2-evidence")
    source = _ingest(store, _signal(signal_id="source", item="supported-item"))
    source_binding, _ = binding_for_candidate(store, source["candidate_id"])
    assert source_binding is not None
    assert source_binding["evidence_class"] == "source_owned"

    reflection = {
        "id": "reflection-1", "sensor": "sensorium.memory_reflection", "source": "memory",
        "kind": "creative_pull", "summary": "Unverified internal interpretation",
        "actor": "agent", "strength_hint": 0.9, "sensitivity": "private",
        "allowed_surfaces": ["local"], "correlation_keys": ["topic:shared"],
        "reflection_fingerprint": "generated-copy", "unverified": True,
    }
    first = _ingest(store, reflection)
    copied = dict(reflection, id="reflection-wrapper")
    assert _ingest(store, copied)["duplicate"] is True
    second = dict(reflection, id="reflection-2", summary="A different ambiguous interpretation")
    second_result = _ingest(store, second)
    assert first["candidate_id"] != second_result["candidate_id"]
    internal_binding, _ = binding_for_candidate(store, first["candidate_id"])
    assert internal_binding is not None
    assert internal_binding["identity_mode"] == "legacy"
    assert internal_binding["evidence_class"] == "internal_interpretation"
    assert internal_binding["item_key"] != source_binding["item_key"]
    assert all(len(source_binding[key]) == 64 for key in ("item_key", "revision_key", "admission_key"))


def test_04b_present_malformed_identity_is_suppressed_not_laundered_as_legacy(tmp_path):
    store = _store(tmp_path, "p2-malformed")
    store.append_jsonl("signals", {
        "id": "bad-signal", "sensor": "research.frontier", "source": "artifact",
        "kind": "creative_pull", "summary": "Malformed synthetic claim",
        "artifact_meta": {"entry_id": "entry", "sha256": ""},
    })
    store.append_jsonl("events", {
        "id": "bad-event", "kind": "creative_pull", "summary": "Malformed synthetic claim",
        "source_signal_ids": ["bad-signal"],
    })
    store.append_jsonl("candidates", {
        "id": "bad-candidate", "status": "candidate", "kind": "creative_pull",
        "summary": "Malformed synthetic claim", "pressure": 0.9,
        "event_ids": ["bad-event"], "correlation_keys": ["topic:bad"],
    })
    plan = build_admission_plan(store)
    assert plan["selection"] is None
    assert plan["suppressed_counts"] == {"malformed_frontier_identity": 1}


def test_04c_native_memory_identity_survives_rewording_without_new_evidence(tmp_path):
    store = _store(tmp_path, "p4-memory-reword")
    first = _ingest(store, _memory_signal(
        "memory-signal-1", "Initial generated synthesis", ["memory-b", "memory-a"],
    ))
    binding1, error = binding_for_candidate(store, first["candidate_id"])
    assert error is None and binding1 is not None
    assert binding1["identity_mode"] == "source"
    assert binding1["evidence_class"] == "internal_interpretation"
    assert run_subconscious_advisory(
        store, advisory_output=_drop(first["candidate_id"]), admission_binding=binding1,
        enabled=True, dry_run=False,
    )["action"] == "drop"

    second = _ingest(store, _memory_signal(
        "memory-signal-2", "Reworded generated synthesis", ["memory-a", "memory-b"],
    ))
    assert second["candidate_id"] == first["candidate_id"]
    binding2, error = binding_for_candidate(store, second["candidate_id"])
    assert error is None and binding2 is not None
    for key in ("item_key", "envelope_key", "revision_key", "member_keys", "evidence_class"):
        assert binding2[key] == binding1[key]
    assert binding1["selected_member_keys"] == binding1["member_keys"]
    assert binding2["selected_member_keys"] == []
    assert binding2["admission_key"] != binding1["admission_key"]
    assert build_admission_plan(store)["selection"] is None


def test_04d_distinct_native_memory_item_set_preserves_first_consideration(tmp_path):
    store = _store(tmp_path, "p4-memory-distinct")
    first = _ingest(store, _memory_signal("memory-a", "First synthesis", ["memory-a"]))
    binding1, error = binding_for_candidate(store, first["candidate_id"])
    assert error is None and binding1 is not None
    assert run_subconscious_advisory(
        store, advisory_output=_drop(first["candidate_id"]), admission_binding=binding1,
        enabled=True, dry_run=False,
    )["action"] == "drop"

    distinct = _ingest(store, _memory_signal("memory-b", "Distinct synthesis", ["memory-b"]))
    binding2, error = binding_for_candidate(store, distinct["candidate_id"])
    assert error is None and binding2 is not None
    assert binding2["item_key"] != binding1["item_key"]
    assert build_admission_plan(store)["selection"] == binding2


def test_04e_malformed_or_forged_native_memory_identity_is_suppressed(tmp_path):
    store = _store(tmp_path, "p4-memory-malformed")
    malformed = _memory_signal("bad-memory", "Malformed generated identity", ["memory-a", "memory-a"])
    _ingest(store, malformed)
    plan = build_admission_plan(store)
    assert plan["selection"] is None
    assert plan["suppressed_counts"] == {"malformed_memory_identity": 1}


def test_06_weak_foreground_residue_is_signal_only_and_replay_non_amplifying(tmp_path):
    store = _store(tmp_path, "p2-residue")
    intent = normalize_live_turn_intent(
        foreground_action_taken=True, foreground_resolution="partial",
        residue="watch", durable_capture="none",
    )
    assert should_ingest_live_residue(intent) == (True, "residue_present")
    weak = {
        "id": "weak-residue", "sensor": "sensorium.live_turn", "source": "hermes_session",
        "kind": "creative_pull", "summary": "Small deliberate synthetic residue",
        "actor": "agent", "strength_hint": 0.4, "sensitivity": "private",
        "allowed_surfaces": ["local"], "correlation_keys": ["live-residue:synthetic", "subject:alpha"],
    }
    first = _ingest(store, weak)
    assert first["promoted"] is False
    assert store.read_jsonl("events") == [] and store.read_jsonl("candidates") == []
    assert _ingest(store, dict(weak, id="weak-wrapper"))["duplicate"] is True

    support_signal = _signal(
        signal_id="supported-later", item="independent-item",
        summary="Source-owned support later matters", strength=0.4,
    )
    support_signal["correlation_keys"] = ["subject:alpha", "channel:research"]
    supported = _ingest(store, support_signal)
    assert supported["promoted"] is True
    assert supported["reason"] == "distinct_source_support_crossed_salience_threshold"
    assert len(store.read_jsonl("signals")) == 2
    handled = normalize_live_turn_intent(
        foreground_action_taken=True, foreground_resolution="full",
        residue="none", durable_capture="docs",
    )
    assert should_ingest_live_residue(handled) == (False, "foreground_owned_no_residue")


def test_07_transition_sequence_and_real_ingest_distinguish_later_episode_without_heartbeat_novelty(tmp_path):
    store = _store(tmp_path, "p2-transitions")
    state = {}
    bad = {"api_available": True, "pending_total": 250, "processing_total": 0, "failed_total": 0}
    good = {"api_available": True, "pending_total": 0, "processing_total": 0, "failed_total": 0}
    first, state = classify_hindsight_pressure(bad, state=state)
    steady, state = classify_hindsight_pressure(bad, state=state)
    recovery, state = classify_hindsight_pressure(good, state=state)
    later, state = classify_hindsight_pressure(bad, state=state)
    assert steady is None
    assert [first["source_revision"], recovery["source_revision"], later["source_revision"]] == ["1", "2", "3"]
    for signal in (first, recovery, later):
        assert _ingest(store, signal)["duplicate"] is False
    assert state["transition_sequence"] == 3

    body_state = {}
    cfg = {"degraded_samples": 1, "sustained_samples": 2, "recovery_samples": 1}
    body_bad = {"mem_available_pct": 8.0, "load_per_cpu": 0.2, "swap_used_pct": 0.0}
    body_good = {"mem_available_pct": 80.0, "load_per_cpu": 0.2, "swap_used_pct": 0.0}
    transition, body_state = classify_machine_body_pressure(body_bad, state=body_state, config=cfg)
    quiet, body_state = classify_machine_body_pressure(body_bad, state=body_state, config=cfg)
    heartbeat, body_state = classify_machine_body_pressure(body_bad, state=body_state, config=cfg)
    recovered, body_state = classify_machine_body_pressure(body_good, state=body_state, config=cfg)
    later_fault, body_state = classify_machine_body_pressure(body_bad, state=body_state, config=cfg)
    assert quiet is None
    assert transition["source_revision"] == heartbeat["source_revision"] == "1"
    assert recovered["source_revision"] == "2" and later_fault["source_revision"] == "3"
    for signal in (transition, heartbeat, recovered, later_fault):
        assert _ingest(store, signal)["duplicate"] is False


def test_08_retry_exhaustion_survives_unrelated_success_and_quiet_history_churn():
    state = {}
    for ordinal, when in ((1, "2026-09-09T00:00:00Z"), (2, "2026-09-09T01:00:00Z"), (3, "2026-09-09T04:00:00Z")):
        attempts.start_attempt(state, session_purpose="autonomous", source_revision="exhausted",
                               ordinal=ordinal, stage="reasoning", deadline_seconds=60, now=when)
        attempts.terminalize_attempt(state, success=False, failure_class="provider_failure", now=when)
    for index in range(attempts.MAX_ATTEMPT_HISTORY + 20):
        revision = f"unrelated-{index}"
        attempts.start_attempt(state, session_purpose="autonomous", source_revision=revision,
                               ordinal=1, stage="reasoning", deadline_seconds=60,
                               now=f"2026-09-10T{index % 24:02d}:00:00Z")
        attempts.terminalize_attempt(state, success=True, now=f"2026-09-10T{index % 24:02d}:00:01Z")
        attempts.start_attempt(state, session_purpose="autonomous", source_revision=None,
                               ordinal=0, stage="sensing", deadline_seconds=60,
                               now=f"2026-09-11T{index % 24:02d}:00:00Z")
        attempts.terminalize_attempt(state, success=True, now=f"2026-09-11T{index % 24:02d}:00:01Z")
    assert not any(row.get("source_revision") == "exhausted" for row in state["attempt_history"])
    assert state["retry_states"]["exhausted"]["last_ordinal"] == 3
    assert attempts.retry_gate(state, "exhausted", now="2026-09-12T00:00:00Z")[:2] == (False, "retry_exhausted")
    state["retry_states"]["malformed"] = {"last_ordinal": 0}
    assert attempts.retry_gate(state, "malformed")[:2] == (False, "retry_state_malformed")


def test_09_candidate_import_identity_is_exact():
    assert Path(admission.__file__).resolve() == ROOT / "agent_sensorium" / "admission.py"
    assert Path(attempts.__file__).resolve() == ROOT / "agent_sensorium" / "attempts.py"
    assert Path(subconscious.__file__).resolve() == ROOT / "agent_sensorium" / "subconscious.py"


# Frozen P4 member-vs-envelope acceptance matrix. Each numbered test maps one
# design row and uses only synthetic store state.

def _save(candidate_id: str) -> dict:
    return {
        "action": "SAVE", "rationale": "Synthetic selected member retained.",
        "event_ids": [], "candidate_ids": [candidate_id],
    }


def _settle(store: SensoriumStore, candidate_id: str, output: dict | None = None) -> dict:
    binding, error = binding_for_candidate(store, candidate_id)
    assert error is None and binding is not None
    result = run_subconscious_advisory(
        store, advisory_output=output or _drop(candidate_id), admission_binding=binding,
        enabled=True, dry_run=False,
    )
    assert result["action"] in {"drop", "save", "created_conscious_task_candidate"}
    return binding


def test_matrix_01_subset_quiet_actual_clock(tmp_path):
    clock = _load_clock()
    store = _store(tmp_path, "matrix-subset")
    first = _ingest(store, _memory_signal("ab", "A B", ["A", "B"]))
    settled = _settle(store, first["candidate_id"])
    subset = _ingest(store, _memory_signal("a", "A", ["A"]))
    subset_binding, error = binding_for_candidate(store, subset["candidate_id"])
    assert error is None and subset_binding is not None
    assert subset_binding["selected_member_keys"] == []
    calls = 0
    def forbidden(command, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("settled subset must not launch")
    result = clock.run_once(_clock_args(store), run_command=forbidden)
    assert result["action"] == "skipped_no_eligible_source" and calls == 0
    assert set(subset_binding["member_keys"]).issubset(set(settled["member_keys"]))


def test_matrix_02_overlap_selects_only_new_member_and_carries_prior(tmp_path):
    clock = _load_clock()
    store = _store(tmp_path, "matrix-overlap")
    ab = _ingest(store, _memory_signal("ab", "A B", ["A", "B"]))
    ab_binding = _settle(store, ab["candidate_id"])
    bc = _ingest(store, _memory_signal("bc", "B C", ["B", "C"]))
    plan = build_admission_plan(store)
    binding = plan["selection"]
    assert binding and binding["source_candidate_id"] == bc["candidate_id"]
    assert len(binding["member_keys"]) == 2 and len(binding["selected_member_keys"]) == 1
    overlap = set(binding["member_keys"]) & set(ab_binding["member_keys"])
    assert len(overlap) == 1
    assert any(set(row["overlap_member_keys"]) == overlap for row in plan["source_decisions"])
    calls = 0
    def fake(command, **kwargs):
        nonlocal calls
        calls += 1
        return clock.subprocess.CompletedProcess(command, 0, stdout=json.dumps(_drop(bc["candidate_id"])), stderr="")
    result = clock.run_once(_clock_args(store), run_command=fake)
    assert result["action"] == "processed_changed_material" and calls == 1
    for ids in (["B"], ["C"], ["B", "C"]):
        _ingest(store, _memory_signal("quiet-" + "-".join(ids), "quiet", list(ids)))
    assert build_admission_plan(store)["selection"] is None


def test_matrix_03_disjoint_first_consideration_once(tmp_path):
    clock = _load_clock()
    store = _store(tmp_path, "matrix-disjoint")
    ab = _ingest(store, _memory_signal("ab", "A B", ["A", "B"]))
    _settle(store, ab["candidate_id"])
    c = _ingest(store, _memory_signal("c", "C", ["C"]))
    binding, _ = binding_for_candidate(store, c["candidate_id"])
    assert binding and binding["selected_member_keys"] == binding["member_keys"]
    calls = 0
    def fake(command, **kwargs):
        nonlocal calls
        calls += 1
        return clock.subprocess.CompletedProcess(command, 0, stdout=json.dumps(_drop(c["candidate_id"])), stderr="")
    assert clock.run_once(_clock_args(store), run_command=fake)["action"] == "processed_changed_material"
    assert clock.run_once(_clock_args(store), run_command=fake)["action"] == "skipped_no_eligible_source"
    assert calls == 1


def test_matrix_04_order_and_wording_neutrality(tmp_path):
    store = _store(tmp_path, "matrix-neutral")
    first = _ingest(store, _memory_signal("one", "first wording", ["A", "B"]))
    binding1, _ = binding_for_candidate(store, first["candidate_id"])
    replay = _ingest(store, _memory_signal("two", "different wording", ["B", "A"]))
    binding2, _ = binding_for_candidate(store, replay["candidate_id"])
    assert replay["candidate_id"] == first["candidate_id"]
    assert binding1 is not None and binding2 is not None
    for key in ("item_key", "envelope_key", "revision_key", "member_keys", "selected_member_keys", "admission_key"):
        assert binding1[key] == binding2[key]
    _settle(store, first["candidate_id"])
    assert build_admission_plan(store)["selection"] is None


def test_matrix_05_competing_envelopes_revalidate_one_unseen_member(tmp_path):
    store = _store(tmp_path, "matrix-competing")
    ab = _ingest(store, _memory_signal("ab", "A B", ["A", "B"]))
    _settle(store, ab["candidate_id"])
    bc = _ingest(store, _memory_signal("bc", "B C", ["B", "C"]))
    c = _ingest(store, _memory_signal("c", "C", ["C"]))
    bc_binding, _ = binding_for_candidate(store, bc["candidate_id"])
    c_binding, _ = binding_for_candidate(store, c["candidate_id"])
    assert bc_binding and c_binding
    assert bc_binding["selected_member_keys"] == c_binding["selected_member_keys"]
    assert bc_binding["admission_key"] == c_binding["admission_key"]
    assert run_subconscious_advisory(
        store, advisory_output=_drop(bc["candidate_id"]), admission_binding=bc_binding,
        enabled=True, dry_run=False,
    )["action"] == "drop"
    assert validate_admission_binding(store, c_binding) == (False, "stale_admission_binding")
    assert build_admission_plan(store)["selection"] is None


def test_matrix_06_overlap_disposition_does_not_overwrite_prior_member(tmp_path):
    store = _store(tmp_path, "matrix-no-overwrite")
    b = _ingest(store, _memory_signal("b", "B", ["B"]))
    b_binding = _settle(store, b["candidate_id"])
    bc = _ingest(store, _memory_signal("bc", "B C", ["B", "C"]))
    bc_binding, _ = binding_for_candidate(store, bc["candidate_id"])
    assert bc_binding and len(bc_binding["selected_member_keys"]) == 1
    assert run_subconscious_advisory(
        store, advisory_output=_save(bc["candidate_id"]), admission_binding=bc_binding,
        enabled=True, dry_run=False,
    )["action"] == "save"
    applying = [row for row in store.read_jsonl("decisions") if row.get("action") in {"drop", "save"}]
    assert applying[-2]["admission_binding"]["selected_member_keys"] == b_binding["selected_member_keys"]
    assert applying[-1]["admission_binding"]["selected_member_keys"] == bc_binding["selected_member_keys"]
    assert set(applying[-2]["admission_binding"]["selected_member_keys"]).isdisjoint(
        applying[-1]["admission_binding"]["selected_member_keys"]
    )


def test_matrix_07_legacy_v1_read_attribution_without_mutation(tmp_path):
    store = _store(tmp_path, "matrix-v1-lineage")
    ab = _ingest(store, _memory_signal("ab", "A B", ["A", "B"]))
    current, _ = binding_for_candidate(store, ab["candidate_id"])
    assert current
    legacy = {k: v for k, v in current.items() if k not in {"envelope_key", "member_keys", "selected_member_keys"}}
    legacy["policy_version"] = "source-admission-v1"
    assert validate_admission_binding(store, legacy) == (False, "invalid_admission_binding")
    store.append_jsonl("decisions", {
        "type": "subconscious.advisory", "dry_run": False, "action": "drop",
        "output_action": "DROP", "admission_binding": legacy,
    })
    historical = store.read_jsonl("decisions")
    a = _ingest(store, _memory_signal("a", "A", ["A"]))
    a_binding, _ = binding_for_candidate(store, a["candidate_id"])
    assert a_binding and a_binding["selected_member_keys"] == []
    bc = _ingest(store, _memory_signal("bc", "B C", ["B", "C"]))
    bc_binding, _ = binding_for_candidate(store, bc["candidate_id"])
    assert bc_binding and len(bc_binding["selected_member_keys"]) == 1
    assert store.read_jsonl("decisions") == historical

    absent = _store(tmp_path, "matrix-v1-absent")
    old_ab = _ingest(absent, _memory_signal("ab", "A B", ["A", "B"]))
    old_binding, _ = binding_for_candidate(absent, old_ab["candidate_id"])
    assert old_binding
    dangling = {k: v for k, v in old_binding.items() if k not in {"envelope_key", "member_keys", "selected_member_keys"}}
    dangling.update(policy_version="source-admission-v1", source_candidate_id="missing")
    absent.append_jsonl("decisions", {
        "type": "subconscious.advisory", "dry_run": False, "action": "drop",
        "output_action": "DROP", "admission_binding": dangling,
    })
    exact, _ = binding_for_candidate(absent, old_ab["candidate_id"])
    assert exact and exact["selected_member_keys"] == []
    new_a = _ingest(absent, _memory_signal("a", "A", ["A"]))
    new_a_binding, _ = binding_for_candidate(absent, new_a["candidate_id"])
    assert new_a_binding and new_a_binding["selected_member_keys"] == new_a_binding["member_keys"]


def test_matrix_08_malformed_partial_ids_never_claim_member_identity(tmp_path):
    malformed_sets = [["A", "A"], [""], ["x" * 513], [" A"], ["A", 7]]
    for index, item_ids in enumerate(malformed_sets):
        store = _store(tmp_path, f"matrix-malformed-{index}")
        _ingest(store, _memory_signal(f"bad-{index}", "bad", item_ids))
        plan = build_admission_plan(store)
        assert plan["selection"] is None
        assert plan["suppressed_counts"] == {"malformed_memory_identity": 1}
    legacy_store = _store(tmp_path, "matrix-absent")
    signal = _memory_signal("absent", "unidentified synthesis", ["A"])
    signal.pop("memory_provenance")
    result = _ingest(legacy_store, signal)
    binding, _ = binding_for_candidate(legacy_store, result["candidate_id"])
    assert binding and binding["identity_mode"] == "legacy"
    assert "member_keys" not in binding


def test_matrix_09_non_memory_policy_v2_invariance(tmp_path):
    store = _store(tmp_path, "matrix-non-memory")
    research = _ingest(store, _signal(signal_id="r1", item="item"))
    r1, _ = binding_for_candidate(store, research["candidate_id"])
    assert r1 and r1["policy_version"] == "source-admission-v2" and "member_keys" not in r1
    _settle(store, research["candidate_id"])
    assert _ingest(store, _signal(signal_id="r2", item="item"))["duplicate"] is True
    frontier = _ingest(store, _signal(signal_id="f1", item="entry", revision="v1", family="frontier"))
    f1 = _settle(store, frontier["candidate_id"])
    _ingest(store, _signal(signal_id="f2", item="entry", revision="v2", family="frontier"))
    f2, _ = binding_for_candidate(store, frontier["candidate_id"])
    assert f2 and f2["item_key"] == f1["item_key"] and f2["admission_key"] != f1["admission_key"]


def test_matrix_10_memory_overlap_never_becomes_independent_support(tmp_path):
    store = _store(tmp_path, "matrix-no-independence")
    first = _memory_signal("weak-ab", "A B", ["A", "B"])
    second = _memory_signal("weak-bc", "B C", ["B", "C"])
    first["strength_hint"] = second["strength_hint"] = 0.4
    assert _ingest(store, first)["promoted"] is False
    assert _ingest(store, second)["promoted"] is False
    assert store.read_jsonl("candidates") == []
    strong = _ingest(store, _memory_signal("strong", "A", ["A"]))
    binding, _ = binding_for_candidate(store, strong["candidate_id"])
    assert binding and binding["evidence_class"] == "internal_interpretation"


def test_matrix_11_held_return_binding_survives_overlap(tmp_path):
    store = _store(tmp_path, "matrix-held")
    ab = _ingest(store, _memory_signal("ab", "A B", ["A", "B"]))
    binding = _settle(store, ab["candidate_id"], _create(ab["candidate_id"]))
    advisory = next(row for row in store.read_jsonl("candidates") if row.get("kind") == "subconscious_advisory")
    opened = open_conscious_aperture(store, aperture_size=1, dry_run=False, now="2026-09-10T12:00:00Z")
    assert opened["candidate_ids"] == [advisory["id"]]
    held = settle_conscious_aperture_item(
        store, candidate_id=advisory["id"], aperture_id=opened["aperture_id"],
        consumer_id="conscious-session", decision="HELD", reason="deliberate return",
        return_at="2026-09-10T13:00:00Z", dry_run=False, now="2026-09-10T12:01:00Z",
    )
    assert held["new_status"] == "held"
    _ingest(store, _memory_signal("bc", "B C", ["B", "C"]))
    held_after = next(row for row in store.read_jsonl("candidates") if row.get("id") == advisory["id"])
    assert held_after["admission_binding"] == binding
    assert advisory["id"] not in open_conscious_aperture(
        store, aperture_size=10, dry_run=True, now="2026-09-10T12:59:00Z",
    )["candidate_ids"]
    returned = open_conscious_aperture(store, aperture_size=10, dry_run=False, now="2026-09-10T13:00:00Z")
    assert advisory["id"] in returned["candidate_ids"]
    returned_row = next(row for row in store.read_jsonl("candidates") if row.get("id") == advisory["id"])
    assert returned_row["admission_binding"] == binding


def test_matrix_12_actual_caller_binding_is_exact_and_prompt_hash_separate(tmp_path):
    clock = _load_clock()
    store = _store(tmp_path, "matrix-caller")
    ab = _ingest(store, _memory_signal("ab", "A B", ["A", "B"]))
    _settle(store, ab["candidate_id"])
    bc = _ingest(store, _memory_signal("bc", "B C", ["B", "C"]))
    plan = build_admission_plan(store)
    binding = plan["selection"]
    assert binding
    captured = {}
    def fake(command, **kwargs):
        payload = json.loads(command[-1].split("\n\n", 1)[1])
        captured["selection"] = payload["context"]["selection"]
        return clock.subprocess.CompletedProcess(command, 0, stdout=json.dumps(_create(bc["candidate_id"])), stderr="")
    result = clock.run_once(_clock_args(store), run_command=fake)
    assert result["action"] == "processed_changed_material"
    assert captured["selection"] == binding
    assert result["source_signature"] == binding["admission_key"]
    assert result["prompt_sha256"] != binding["admission_key"]
    receipt = [row for row in store.read_jsonl("decisions") if row.get("type") == "subconscious.advisory"][-1]
    advisory = next(row for row in store.read_jsonl("candidates") if row.get("kind") == "subconscious_advisory")
    assert receipt["admission_binding"] == advisory["admission_binding"] == binding
    assert receipt["admission_binding"]["selected_member_keys"] == binding["selected_member_keys"]
