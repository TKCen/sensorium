from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

from agent_sensorium import attempts
from agent_sensorium.admission import _claim_for_signal, binding_for_candidate, build_admission_plan
from agent_sensorium.store import SensoriumStore
from agent_sensorium.subconscious import run_subconscious_advisory
from agent_sensorium.tools import handle_sensorium_ingest_signal

ROOT = Path(__file__).parents[1]


def _store(tmp_path, name: str) -> SensoriumStore:
    store = SensoriumStore(instance=name, state_dir=str(tmp_path / name))
    store.ensure_dirs()
    return store


def _research(signal_id: str, item: str, *, strength: float = 0.9, key: str = "subject:p2") -> dict:
    return {
        "id": signal_id,
        "sensor": "research.source_feed",
        "source": "artifact",
        "kind": "creative_pull",
        "summary": f"Synthetic {item}",
        "actor": "tool",
        "strength_hint": strength,
        "sensitivity": "private",
        "allowed_surfaces": ["local"],
        "correlation_keys": [key],
        "artifact_meta": {"source_id": "feed-A", "item_id": item},
    }


def _frontier(signal_id: str, item: str, *, strength: float = 0.4, key: str = "subject:p2") -> dict:
    return {
        "id": signal_id,
        "sensor": "research.frontier",
        "source": "artifact",
        "kind": "creative_pull",
        "summary": f"Synthetic frontier {item}",
        "actor": "tool",
        "strength_hint": strength,
        "sensitivity": "private",
        "allowed_surfaces": ["local"],
        "correlation_keys": [key],
        "artifact_meta": {"entry_id": item, "sha256": f"revision-{item}"},
    }


def _ingest(store: SensoriumStore, signal: dict) -> dict:
    payload = json.loads(handle_sensorium_ingest_signal(
        signal=signal, instance=store.instance, state_dir=str(store.root), config={}
    ))
    assert payload["success"] is True, payload
    return payload["data"]


def _output(action: str, candidate_id: str) -> dict:
    output = {
        "action": action,
        "rationale": "Synthetic P2 review regression.",
        "event_ids": [],
        "candidate_ids": [candidate_id],
    }
    if action == "CREATE_CONSCIOUS_TASK":
        output["conscious_task"] = {
            "request_type": "THINK",
            "title": "Synthetic review",
            "why": "Exercise source-bound validation.",
            "expected_decision": "Reject malformed provenance.",
        }
    return output


def _manual_candidate(store: SensoriumStore, *, candidate_id: str, event_ids: list[str]) -> None:
    store.append_jsonl("candidates", {
        "id": candidate_id,
        "status": "candidate",
        "kind": "creative_pull",
        "summary": candidate_id,
        "pressure": 0.8,
        "event_ids": event_ids,
        "correlation_keys": ["subject:p2"],
    })


def _ordered_frontier(signal_id: str, revision: str, ts: str) -> dict:
    signal = _frontier(
        signal_id, "same-ordered-item", strength=0.9, key="subject:p2-order")
    signal["artifact_meta"]["sha256"] = revision
    signal["summary"] = f"Synthetic ordered frontier {revision}"
    signal["ts"] = ts
    return signal


def _native_clock():
    path = ROOT / "scripts" / "sensorium_native_clock.py"
    spec = importlib.util.spec_from_file_location("p2_order_native_clock", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert Path(module.__file__).resolve() == path
    return module


def _native_args(store: SensoriumStore) -> argparse.Namespace:
    return argparse.Namespace(
        instance=store.instance, state_dir=str(store.root), plugin_root=str(ROOT),
        event_limit=50, candidate_limit=50, failure_cooldown_seconds=1800,
        sensor_timeout_seconds=60, hermes_timeout_seconds=60,
        total_timeout_seconds=120, cleanup_reserve_seconds=10,
        skip_sensors=True, force=False, print_json=False,
        hermes_cli="/must-not-run/hermes", provider="fixture", model="fixture",
    )


def _clear_native_clock_outputs(store: SensoriumStore) -> None:
    for name in ("native_clock_state.json", "last_native_clock.json"):
        (store.root / name).unlink(missing_ok=True)


def _seed_ordered_revisions(store: SensoriumStore) -> tuple[str, dict, dict]:
    first = _ingest(store, _ordered_frontier(
        "sig-order-A", "opaque-z-first-A", "2026-09-09T14:29:00Z"))
    second = _ingest(store, _ordered_frontier(
        "sig-order-B", "opaque-a-second-B", "2026-09-09T14:28:00Z"))
    assert first["candidate_id"] == second["candidate_id"]
    return first["candidate_id"], first, second


@pytest.mark.parametrize("permutation", [
    "event_ids", "source_signal_ids", "duplicate_references", "combined",
])
def test_received_signal_order_not_membership_order_controls_native_quietness(tmp_path, permutation):
    store = _store(tmp_path, f"order-{permutation}")
    candidate_id, first, second = _seed_ordered_revisions(store)
    events = store.read_jsonl("events")
    candidates = store.read_jsonl("candidates")
    candidate = next(row for row in candidates if row.get("id") == candidate_id)
    event_a = next(row for row in events if row.get("id") == first["event_id"])
    event_b = next(row for row in events if row.get("id") == second["event_id"])

    if permutation == "source_signal_ids":
        event_a["source_signal_ids"] = ["sig-order-A", "sig-order-B"]
        candidate["event_ids"] = [event_a["id"]]
    elif permutation == "combined":
        event_a["source_signal_ids"] = ["sig-order-A", "sig-order-B"]
        event_b["source_signal_ids"] = ["sig-order-B"]
        candidate["event_ids"] = [event_a["id"], event_b["id"], event_b["id"], event_a["id"], event_b["id"]]
    store.rewrite_jsonl("events", events)
    store.rewrite_jsonl("candidates", candidates)

    binding_b, error = binding_for_candidate(store, candidate_id)
    assert error is None and binding_b is not None
    assert run_subconscious_advisory(
        store, advisory_output=_output("DROP", candidate_id),
        admission_binding=binding_b, enabled=True, dry_run=False,
    )["action"] == "drop"

    module = _native_clock()
    args = _native_args(store)
    calls = []

    def counted_runner(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(
            command, 0, stdout=json.dumps(_output("DROP", candidate_id)), stderr="")

    control = module.run_once(args, run_command=counted_runner)
    assert control["action"] == "skipped_no_eligible_source"
    assert calls == []
    _clear_native_clock_outputs(store)

    events = store.read_jsonl("events")
    candidates = store.read_jsonl("candidates")
    candidate = next(row for row in candidates if row.get("id") == candidate_id)
    event_a = next(row for row in events if row.get("id") == first["event_id"])
    if permutation == "event_ids":
        candidate["event_ids"] = list(reversed(candidate["event_ids"]))
    elif permutation == "source_signal_ids":
        event_a["source_signal_ids"] = list(reversed(event_a["source_signal_ids"]))
    elif permutation == "duplicate_references":
        candidate["event_ids"].append(first["event_id"])
    else:
        candidate["event_ids"] = list(reversed(candidate["event_ids"]))
        event_a["source_signal_ids"] = list(reversed(event_a["source_signal_ids"]))
    store.rewrite_jsonl("events", events)
    store.rewrite_jsonl("candidates", candidates)

    rebound, error = binding_for_candidate(store, candidate_id)
    assert error is None and rebound is not None
    assert rebound["admission_key"] == binding_b["admission_key"]
    permuted = module.run_once(args, run_command=counted_runner)
    assert permuted["action"] == "skipped_no_eligible_source"
    assert calls == []


def test_genuine_latest_received_revision_is_admitted_once_then_quiet(tmp_path):
    store = _store(tmp_path, "order-positive")
    candidate_id, _, _ = _seed_ordered_revisions(store)
    binding_b, error = binding_for_candidate(store, candidate_id)
    assert error is None and binding_b is not None
    assert run_subconscious_advisory(
        store, advisory_output=_output("DROP", candidate_id),
        admission_binding=binding_b, enabled=True, dry_run=False,
    )["action"] == "drop"

    signal_c = _ordered_frontier(
        "sig-order-C", "opaque-m-third-C", "2026-09-09T14:27:00Z")
    third = _ingest(store, signal_c)
    assert third["candidate_id"] == candidate_id
    binding_c, error = binding_for_candidate(store, candidate_id)
    claim_c, claim_error = _claim_for_signal(store.instance, store.read_jsonl("signals")[-1])
    assert error is None and claim_error is None and binding_c is not None and claim_c is not None
    assert binding_c["revision_key"] == claim_c["revision_key"]
    assert binding_c["admission_key"] != binding_b["admission_key"]

    module = _native_clock()
    args = _native_args(store)
    calls = []
    prompts = []

    def applying_runner(command, **kwargs):
        calls.append(command)
        prompts.append(command[-1])
        return subprocess.CompletedProcess(
            command, 0, stdout=json.dumps(_output("DROP", candidate_id)), stderr="")

    admitted = module.run_once(args, run_command=applying_runner)
    assert admitted["action"] == "processed_changed_material"
    assert admitted["source_signature"] == binding_c["admission_key"]
    assert len(calls) == 1 and binding_c["admission_key"] in prompts[0]
    assert "opaque-m-third-C" in prompts[0]

    signal_count = len(store.read_jsonl("signals"))
    duplicate = _ingest(store, signal_c)
    assert duplicate["duplicate"] is True
    assert len(store.read_jsonl("signals")) == signal_count
    candidates = store.read_jsonl("candidates")
    candidate = next(row for row in candidates if row.get("id") == candidate_id)
    candidate["event_ids"] = list(reversed(candidate["event_ids"])) + [candidate["event_ids"][0]]
    store.rewrite_jsonl("candidates", candidates)
    events = store.read_jsonl("events")
    for event in events:
        event["source_signal_ids"] = list(reversed(event.get("source_signal_ids") or [])) * 2
    store.rewrite_jsonl("events", events)

    quiet = module.run_once(args, run_command=applying_runner)
    assert quiet["action"] == "skipped_no_eligible_source"
    assert len(calls) == 1


def test_nonapplying_receipts_remain_audit_only_and_project_authority(tmp_path):
    store = _store(tmp_path, "receipt-authority")
    created = _ingest(store, _research("sig-receipt", "receipt-item"))
    binding, error = binding_for_candidate(store, created["candidate_id"])
    assert error is None and binding is not None

    preview = run_subconscious_advisory(
        store,
        advisory_output=_output("DROP", created["candidate_id"]),
        admission_binding=binding,
        enabled=True,
        dry_run=True,
        record_receipt=True,
    )
    assert preview["action"] == "drop" and preview["dry_run"] is True
    disabled = run_subconscious_advisory(
        store,
        advisory_output=_output("SAVE", created["candidate_id"]),
        admission_binding=binding,
        enabled=False,
        dry_run=False,
        record_receipt=True,
    )
    assert disabled["action"] == "disabled"

    # Existing fields model validation fallback and failed audit rows without
    # creating a second persistence owner.
    for row in (
        {
            "type": "subconscious.advisory", "dry_run": False,
            "action": "save", "output_action": "CREATE_CONSCIOUS_TASK",
            "reason_code": "stable_source_candidate_required",
            "admission_binding": deepcopy(binding),
        },
        {
            "type": "subconscious.advisory", "dry_run": False,
            "action": "model_unavailable", "output_action": None,
            "admission_binding": deepcopy(binding),
        },
    ):
        store.append_jsonl("decisions", row)

    plan = build_admission_plan(store)
    assert plan["selection"] == binding
    assert plan["suppressed_counts"] == {}
    assert len(plan["source_decisions"]) == 4
    projected = {(row["action"], row["output_action"], row["dry_run"]) for row in plan["source_decisions"]}
    assert ("drop", "DROP", True) in projected
    assert ("disabled", "SAVE", False) in projected
    assert ("save", "CREATE_CONSCIOUS_TASK", False) in projected
    assert ("model_unavailable", None, False) in projected


def test_successful_applying_receipt_still_suppresses_unchanged_revision(tmp_path):
    store = _store(tmp_path, "receipt-positive")
    created = _ingest(store, _research("sig-positive", "positive-item"))
    binding, error = binding_for_candidate(store, created["candidate_id"])
    assert error is None and binding is not None
    applied = run_subconscious_advisory(
        store, advisory_output=_output("DROP", created["candidate_id"]),
        admission_binding=binding, enabled=True, dry_run=False,
    )
    assert applied["action"] == "drop"
    plan = build_admission_plan(store)
    assert plan["selection"] is None
    assert plan["suppressed_counts"] == {"prior_disposition": 1}


@pytest.mark.parametrize("shape", ["partial_event", "partial_signal", "mixed_items"])
def test_partial_or_mixed_historical_joins_are_candidate_scoped_unknown(tmp_path, shape):
    store = _store(tmp_path, f"join-{shape}")
    store.append_jsonl("signals", _research("sig-valid", "item-A"))
    if shape == "partial_event":
        store.append_jsonl("events", {
            "id": "evt-valid", "kind": "creative_pull", "summary": "valid",
            "source_signal_ids": ["sig-valid"],
        })
        event_ids = ["evt-valid", "evt-missing"]
    elif shape == "partial_signal":
        store.append_jsonl("events", {
            "id": "evt-partial", "kind": "creative_pull", "summary": "partial",
            "source_signal_ids": ["sig-valid", "sig-missing"],
        })
        event_ids = ["evt-partial"]
    else:
        store.append_jsonl("signals", _research("sig-other", "item-B"))
        store.append_jsonl("events", {
            "id": "evt-mixed", "kind": "creative_pull", "summary": "mixed",
            "source_signal_ids": ["sig-valid", "sig-other"],
        })
        event_ids = ["evt-mixed"]
    _manual_candidate(store, candidate_id=f"cand-{shape}", event_ids=event_ids)

    binding, error = binding_for_candidate(store, f"cand-{shape}")
    assert error is None and binding is not None
    assert binding["identity_mode"] == "legacy"
    assert binding["evidence_class"] == "unknown"
    assert build_admission_plan(store)["selection"] == binding


@pytest.mark.parametrize("action", ["DROP", "SAVE", "CREATE_CONSCIOUS_TASK"])
def test_explicit_malformed_identity_is_rejected_before_any_mutation(tmp_path, action):
    store = _store(tmp_path, f"malformed-{action.lower()}")
    store.append_jsonl("signals", {
        "id": "sig-malformed", "sensor": "research.frontier", "source": "artifact",
        "kind": "creative_pull", "summary": "Malformed frontier",
        "artifact_meta": {"entry_id": "entry-A", "sha256": ""},
    })
    store.append_jsonl("events", {
        "id": "evt-malformed", "kind": "creative_pull", "summary": "Malformed frontier",
        "source_signal_ids": ["sig-malformed"],
    })
    _manual_candidate(store, candidate_id="cand-malformed", event_ids=["evt-malformed"])
    before_candidates = deepcopy(store.read_jsonl("candidates"))
    before_decisions = deepcopy(store.read_jsonl("decisions"))

    with pytest.raises(ValueError, match="malformed_frontier_identity"):
        run_subconscious_advisory(
            store, advisory_output=_output(action, "cand-malformed"),
            enabled=True, dry_run=False,
        )
    assert store.read_jsonl("candidates") == before_candidates
    assert store.read_jsonl("decisions") == before_decisions


def _retry_entry(revision: str, ordinal: int = 3, status: str = "failed") -> dict:
    return {
        "source_revision": revision,
        "last_ordinal": ordinal,
        "retry_not_before": None,
        "last_status": status,
    }


def _active(revision: str) -> dict:
    return {
        "attempt_id": "synthetic", "session_purpose": "autonomous",
        "source_revision": revision, "owner_pid": 1, "owner_start_token": "synthetic",
        "started_at": "2026-09-09T00:00:00Z", "deadline_at": "2026-09-09T00:01:00Z",
        "ordinal": 3, "stage": "reasoning", "status": "active",
    }


def test_retry_gate_fails_closed_for_present_corrupt_container_and_target():
    top = {"retry_states": "corrupt", "attempt_history": []}
    assert attempts.retry_gate(top, "exhausted")[:2] == (False, "retry_state_malformed")
    target = {
        "retry_states": {"exhausted": {"last_ordinal": 3}},
        "retry_state": _retry_entry("other"),
        "attempt_history": [],
    }
    assert attempts.retry_gate(target, "exhausted")[:2] == (False, "retry_state_malformed")


def test_retry_updates_do_not_overwrite_corrupt_durable_owner_state():
    start_state = {"retry_states": "corrupt", "attempt_history": []}
    start_before = deepcopy(start_state)
    with pytest.raises(ValueError, match="retry_states_malformed"):
        attempts.start_attempt(
            start_state, session_purpose="autonomous", source_revision="rev",
            ordinal=1, stage="reasoning", deadline_seconds=60,
        )
    assert start_state == start_before

    for operation in ("bind", "terminalize"):
        state = {
            "retry_states": {"rev": {"last_ordinal": "corrupt"}},
            "active_attempt": _active("rev"),
            "attempt_history": [],
        }
        before = deepcopy(state)
        with pytest.raises(ValueError, match="retry_state_malformed"):
            if operation == "bind":
                attempts.bind_attempt(state, source_revision="rev", ordinal=3, stage="applying")
            else:
                attempts.terminalize_attempt(state, success=False, failure_class="provider_failure")
        assert state == before

    reconcile_state = {
        "retry_states": {"rev": {"last_ordinal": "corrupt"}},
        "attempt_history": [{
            **_active("rev"), "status": "failed", "failure_class": "interrupted_outer_kill",
            "stage": "applying",
        }],
    }
    reconcile_before = deepcopy(reconcile_state)
    with pytest.raises(ValueError, match="retry_state_malformed"):
        attempts.reconcile_applied_attempt(
            reconcile_state, source_revision="rev", disposition_ref="decision:1",
        )
    assert reconcile_state == reconcile_before


@pytest.mark.parametrize("prior", [
    {
        "id": "telemetry", "sensor": "sensorium.runtime_heartbeat", "source": "machine",
        "kind": "runtime_heartbeat", "summary": "Telemetry", "strength_hint": 0.4,
        "correlation_keys": ["subject:support"], "sensitivity": "local_only",
        "allowed_surfaces": ["local"],
    },
    {
        "id": "memory", "sensor": "sensorium.memory_reflection", "source": "memory",
        "kind": "creative_pull", "summary": "Interpretation", "strength_hint": 0.4,
        "correlation_keys": ["subject:support"], "sensitivity": "private",
        "allowed_surfaces": ["local"],
    },
    {
        "id": "feedback", "sensor": "sensorium.worker_result", "source": "feedback",
        "kind": "task_result", "summary": "Copied lifecycle feedback", "strength_hint": 0.4,
        "correlation_keys": ["subject:support"], "sensitivity": "private",
        "allowed_surfaces": ["local"], "outcome": "completed",
        "feedback_scope": "system_action", "caused_by": {"candidate_id": "cand_synthetic"},
    },
    {
        "id": "malformed", "sensor": "research.frontier", "source": "artifact",
        "kind": "creative_pull", "summary": "Malformed claim", "strength_hint": 0.4,
        "correlation_keys": ["subject:support"], "sensitivity": "private",
        "allowed_surfaces": ["local"],
        "artifact_meta": {"entry_id": "entry", "sha256": ""},
    },
    {
        "id": "lifecycle", "sensor": "sensorium.thread_update", "source": "internal",
        "kind": "task_result", "summary": "Lifecycle activity", "strength_hint": 0.4,
        "correlation_keys": ["subject:support"], "sensitivity": "private",
        "allowed_surfaces": ["local"],
    },
])
def test_nonqualifying_prior_support_cannot_promote_genuine_weak_source(tmp_path, prior):
    store = _store(tmp_path, f"negative-support-{prior['id']}")
    first = _ingest(store, prior)
    assert first["promoted"] is False
    incoming = _research("genuine", "genuine-item", strength=0.4, key="subject:support")
    result = _ingest(store, incoming)
    assert result["promoted"] is False
    assert result["reason"] == "below threshold (strength=0.4, kind='creative_pull')"
    assert store.read_jsonl("events") == []


def test_genuine_source_and_foreground_residue_support_remain_positive(tmp_path):
    residue_store = _store(tmp_path, "positive-residue")
    residue = {
        "id": "residue", "sensor": "sensorium.live_turn", "source": "hermes_session",
        "kind": "creative_pull", "summary": "Deliberate unresolved residue",
        "actor": "agent", "strength_hint": 0.4, "sensitivity": "private",
        "allowed_surfaces": ["local"],
        "correlation_keys": ["live-residue:synthetic", "subject:support"],
    }
    assert _ingest(residue_store, residue)["promoted"] is False
    supported = _ingest(
        residue_store,
        _research("source-after-residue", "supported-item", strength=0.4, key="subject:support"),
    )
    assert supported["promoted"] is True
    assert supported["reason"] == "distinct_source_support_crossed_salience_threshold"

    source_store = _store(tmp_path, "positive-sources")
    assert _ingest(
        source_store, _research("source-one", "item-one", strength=0.4, key="subject:support")
    )["promoted"] is False
    second = _ingest(
        source_store, _frontier("source-two", "item-two", strength=0.4, key="subject:support")
    )
    assert second["promoted"] is True
    assert second["reason"] == "distinct_source_support_crossed_salience_threshold"

    reverse_store = _store(tmp_path, "positive-residue-reverse")
    assert _ingest(
        reverse_store, _research("source-first", "item-first", strength=0.4, key="subject:support")
    )["promoted"] is False
    reverse = _ingest(reverse_store, residue)
    assert reverse["promoted"] is True
    assert reverse["reason"] == "distinct_source_support_crossed_salience_threshold"


def test_review_regressions_import_candidate_modules():
    from agent_sensorium import admission, gate, subconscious

    assert Path(admission.__file__).resolve() == ROOT / "agent_sensorium" / "admission.py"
    assert Path(attempts.__file__).resolve() == ROOT / "agent_sensorium" / "attempts.py"
    assert Path(gate.__file__).resolve() == ROOT / "agent_sensorium" / "gate.py"
    assert Path(subconscious.__file__).resolve() == ROOT / "agent_sensorium" / "subconscious.py"
