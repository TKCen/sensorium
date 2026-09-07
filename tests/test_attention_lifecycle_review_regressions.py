from __future__ import annotations

import importlib.util
import json
import multiprocessing
from pathlib import Path

import pytest

from agent_sensorium.conscious_aperture import (
    MAX_PRESENTATION_INDEX_RECORDS,
    open_conscious_aperture,
    record_conscious_aperture_presentation_attempt,
    settle_conscious_aperture_item,
)
from agent_sensorium.conscious_doorway import (
    conscious_doorway_context,
    handle_conscious_doorway_pre_llm,
)
from agent_sensorium.plugin import register
from agent_sensorium.settlement import _derived_stale_aperture_ids, apply_kanban_settlement
from agent_sensorium.store import SensoriumStore
from agent_sensorium.tools import (
    handle_sensorium_candidate_update,
    handle_sensorium_compact,
    handle_sensorium_ingest_event,
)


def _candidate(candidate_id, *, pressure=0.7, created_at="2026-06-07T10:00:00Z"):
    return {
        "id": candidate_id, "status": "candidate", "kind": "subconscious_advisory",
        "pressure": pressure, "summary": f"Candidate {candidate_id}",
        "fingerprint": f"fp-{candidate_id}", "event_ids": [f"evt_{candidate_id}"],
        "source_candidate_ids": [], "correlation_keys": ["test"],
        "sensitivity": "private", "allowed_surfaces": ["local"],
        "created_at": created_at, "updated_at": created_at,
        "conscious_task": {"id": f"task-{candidate_id}", "request_type": "THINK",
                            "title": candidate_id, "why": "review", "expected_decision": "decide"},
        "advisory_meta": {"source_fingerprint": f"source-{candidate_id}"},
    }


def _expired(candidate_id):
    row = _candidate(candidate_id, pressure=0.1)
    row["status"] = "in_conscious_aperture"
    row["conscious_aperture"] = {
        "id": f"cap-{candidate_id}", "consumer_id": "dead-owner", "generation": 1,
        "opened_at": "2026-06-07T10:00:00Z", "lease_expires_at": "2026-06-07T10:05:00Z",
        "state": "open",
    }
    return row


def _paused_aperture_process(state_dir, read_ready, release_read, result_queue):
    store = SensoriumStore(instance="test", state_dir=state_dir)
    original_read = store.read_jsonl
    paused = False

    def read_jsonl(name, limit=None):
        nonlocal paused
        rows = original_read(name, limit=limit)
        if name == "candidates" and not paused:
            paused = True
            read_ready.set()
            if not release_read.wait(10):
                raise TimeoutError("test did not release paused aperture read")
        return rows

    store.read_jsonl = read_jsonl
    result_queue.put(open_conscious_aperture(
        store,
        aperture_size=1,
        max_active_items=1,
        consumer_id="aperture-process",
        dry_run=False,
        now="2026-06-07T12:00:00Z",
    ))


def _concurrent_candidate_writer_process(state_dir, mode, started, done, result_queue):
    started.set()
    try:
        if mode == "settlement":
            result = apply_kanban_settlement(
                SensoriumStore(instance="test", state_dir=state_dir),
                decision="DROP",
                candidate_id="settlement-target",
                reason="Concurrent Kanban settlement.",
            )
        else:
            event = {
                "id": f"evt-{mode}",
                "ts": "2026-06-07T12:00:00Z",
                "type": "sensor.event.promoted",
                "kind": "task_result" if mode == "coalesce" else "new_event_kind",
                "summary": f"Concurrent {mode} event",
                "strength": 0.9,
                "correlation_keys": ["coalesce-target"] if mode == "coalesce" else ["new"],
                "sensitivity": "private",
                "allowed_surfaces": ["local"],
            }
            result = json.loads(handle_sensorium_ingest_event(
                event=event,
                instance="test",
                state_dir=state_dir,
                config={"silence_ttl_hours": 1_000_000},
            ))
        result_queue.put(result)
    finally:
        done.set()


class _LivePluginContext:
    def __init__(self):
        self.tools = {}

    def register_tool(self, *, name, handler, **kwargs):
        self.tools[name] = handler

    def register_hook(self, *args, **kwargs):
        pass

    def register_command(self, *args, **kwargs):
        pass

    def register_skill(self, *args, **kwargs):
        pass


def _load_dashboard_plugin():
    path = Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("attention_lifecycle_dashboard", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_current_lease_requires_exact_tokens_and_rejects_stale_generation(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "state"))
    store.append_jsonl("candidates", _candidate("owned"))
    first = open_conscious_aperture(
        store, aperture_size=1, max_active_items=1, consumer_id="owner-a",
        lease_minutes=5, dry_run=False, now="2026-06-07T12:00:00Z",
    )
    cases = [
        ({"consumer_id": "owner-a"}, "aperture_id_required"),
        ({"aperture_id": first["aperture_id"]}, "consumer_id_required"),
        ({"aperture_id": "cap-wrong", "consumer_id": "owner-a"}, "aperture_id_mismatch"),
        ({"aperture_id": first["aperture_id"], "consumer_id": "owner-b"}, "consumer_id_mismatch"),
    ]
    for tokens, expected in cases:
        before = store.read_jsonl("candidates")
        result = settle_conscious_aperture_item(
            store, candidate_id="owned", decision="SETTLED", reason="Exact owner only.",
            dry_run=False, now="2026-06-07T12:01:00Z", **tokens,
        )
        assert result["error"] == expected
        assert store.read_jsonl("candidates") == before
    expired = settle_conscious_aperture_item(
        store, candidate_id="owned", aperture_id=first["aperture_id"], consumer_id="owner-a",
        decision="SETTLED", reason="Expired.", dry_run=False, now="2026-06-07T12:05:00Z",
    )
    assert expired["error"] == "aperture_lease_expired"
    second = open_conscious_aperture(
        store, aperture_size=1, max_active_items=1, consumer_id="owner-b",
        lease_minutes=5, dry_run=False, now="2026-06-07T12:06:00Z",
    )
    stale = settle_conscious_aperture_item(
        store, candidate_id="owned", aperture_id=first["aperture_id"], consumer_id="owner-a",
        decision="SETTLED", reason="Stale generation.", dry_run=False, now="2026-06-07T12:07:00Z",
    )
    assert second["aperture_id"] != first["aperture_id"]
    assert stale["error"] == "aperture_id_mismatch"


def test_legacy_ownerless_path_is_explicit_and_batch_wrong_owner_fails_closed(tmp_path):
    legacy_store = SensoriumStore(instance="legacy", state_dir=str(tmp_path / "legacy"))
    legacy = _candidate("legacy")
    legacy["status"] = "in_conscious_aperture"
    legacy["conscious_aperture"] = {"opened_at": "2026-06-07T12:00:00Z", "state": "open"}
    legacy_store.append_jsonl("candidates", legacy)
    blocked = settle_conscious_aperture_item(
        legacy_store, candidate_id="legacy", decision="SETTLED", reason="Legacy.",
        dry_run=False, now="2026-06-07T12:01:00Z",
    )
    allowed = settle_conscious_aperture_item(
        legacy_store, candidate_id="legacy", decision="SETTLED", reason="Legacy.",
        allow_legacy_ownerless=True, dry_run=False, now="2026-06-07T12:01:00Z",
    )
    assert blocked["error"] == "aperture_id_required"
    assert allowed["success"] is True

    store = SensoriumStore(instance="batch", state_dir=str(tmp_path / "batch"))
    store.rewrite_jsonl("candidates", [_candidate("alpha"), _candidate("beta")])
    packet = open_conscious_aperture(
        store, aperture_size=2, max_active_items=2, consumer_id="owner-a",
        dry_run=False, now="2026-06-07T12:00:00Z",
    )
    results = [settle_conscious_aperture_item(
        store, candidate_id=item["candidate_id"], aperture_id=item["aperture_id"],
        consumer_id="owner-a" if index == 0 else "owner-b", decision="SETTLED",
        reason="Mixed batch.", dry_run=False, now="2026-06-07T12:01:00Z",
    ) for index, item in enumerate(packet["aperture"])]
    assert results[0]["success"] is True
    assert results[1]["error"] == "consumer_id_mismatch"


def test_packet_attempt_is_all_or_none_and_host_drop_never_claims_consumption(tmp_path):
    store = SensoriumStore(instance="packet", state_dir=str(tmp_path / "packet"))
    store.rewrite_jsonl("candidates", [_candidate("alpha"), _candidate("beta")])
    packet = open_conscious_aperture(
        store, aperture_size=2, max_active_items=2, consumer_id="owner-a",
        dry_run=False, now="2026-06-07T12:00:00Z",
    )
    invalid = [dict(item) for item in packet["aperture"]]
    invalid[1]["aperture_id"] = "cap-stale"
    failed = record_conscious_aperture_presentation_attempt(
        store, aperture=invalid, consumer_id="owner-a", turn_id="turn-a",
        surface="local", now="2026-06-07T12:01:00Z",
    )
    assert failed["error"] == "aperture_id_mismatch" and failed["item_index"] == 1
    assert not [d for d in store.read_jsonl("decisions") if d.get("type") == "conscious.aperture.presentation_attempted"]

    doorway_dir = tmp_path / "doorway"
    doorway = SensoriumStore(instance="generic", state_dir=str(doorway_dir))
    doorway.ensure_dirs()
    (doorway_dir / "instance.config.json").write_text(json.dumps({
        "instance_name": "generic", "allowed_surfaces": ["local"],
        "conscious_doorway": {"enabled": True, "aperture_size": 1,
                              "max_active_items": 1, "surfaces": ["local"]},
    }))
    doorway.append_jsonl("candidates", _candidate("host-drop"))
    proposed = handle_conscious_doorway_pre_llm(
        instance="generic", platform="local", session_id="session-a",
        turn_id="turn-a", state_dir=str(doorway_dir),
    )
    assert proposed is not None  # Simulated host drops this proposed context.
    decisions = doorway.read_jsonl("decisions")
    attempts = [d for d in decisions if d.get("type") == "conscious.aperture.presentation_attempted"]
    assert len(attempts) == 1 and attempts[0]["host_consumption_confirmed"] is False
    assert not [d for d in decisions if d.get("type") == "conscious.aperture.consumed"]


def test_prepared_external_work_requires_validated_core_spec(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "state"))
    store.append_jsonl("candidates", _candidate("prepared"))
    packet = open_conscious_aperture(
        store, aperture_size=1, max_active_items=1, consumer_id="owner-a",
        dry_run=False, now="2026-06-07T12:00:00Z",
    )
    before = store.read_jsonl("candidates")
    for external_work, error in ((None, "external_work_spec_required"), ({"title": "Only"}, "invalid_external_work_spec")):
        result = settle_conscious_aperture_item(
            store, candidate_id="prepared", aperture_id=packet["aperture_id"],
            consumer_id="owner-a", decision="PREPARED_EXTERNAL_WORK", reason="Prepare.",
            external_work=external_work, dry_run=False, now="2026-06-07T12:01:00Z",
        )
        assert result["error"] == error
        assert store.read_jsonl("candidates") == before
    assert store.read_jsonl("worker_requests") == store.read_jsonl("outbox") == []


def test_capacity_one_persists_alternation_so_neither_lane_starves(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "state"))
    store.append_jsonl("candidates", _expired("stale"))
    picks, lanes = [], []
    for index in range(4):
        rows = store.read_jsonl("candidates")
        rows.append(_candidate(f"fresh-{index}", pressure=0.99))
        if index:
            rows.append(_expired(f"recovery-{index}"))
        store.rewrite_jsonl("candidates", rows)
        packet = open_conscious_aperture(
            store, aperture_size=1, max_active_items=1, consumer_id=f"owner-{index}",
            dry_run=False, now=f"2026-06-07T12:0{index}:00Z",
        )
        picks += packet["candidate_ids"]
        lanes += packet["fairness_service_lanes"]
        item = packet["aperture"][0]
        settle_conscious_aperture_item(
            store, candidate_id=item["candidate_id"], aperture_id=item["aperture_id"],
            consumer_id=f"owner-{index}", decision="SETTLED", reason="Clear.",
            dry_run=False, now=f"2026-06-07T12:0{index}:30Z",
        )
    assert picks[:2] == ["stale", "fresh-0"]
    assert lanes == ["recovery", "fresh", "recovery", "fresh"]


def test_foreground_open_and_presentation_decode_zero_decision_rows(tmp_path, monkeypatch):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "state"))
    for index in range(600):
        store.append_jsonl("decisions", {"type": "unrelated.audit", "index": index})
    store.append_jsonl("candidates", _candidate("bounded"))
    original_read = store.read_jsonl
    decision_reads = 0

    def reject_decision_reads(name, limit=None):
        nonlocal decision_reads
        if name == "decisions":
            decision_reads += 1
            raise AssertionError("foreground decoded the lifetime decision log")
        return original_read(name, limit=limit)

    monkeypatch.setattr(store, "read_jsonl", reject_decision_reads)
    packet = open_conscious_aperture(
        store, aperture_size=1, max_active_items=1, consumer_id="owner",
        dry_run=False, now="2026-06-07T12:00:00Z",
    )
    attempted = record_conscious_aperture_presentation_attempt(
        store, aperture=packet["aperture"], consumer_id="owner", turn_id="turn-1",
        surface="local", now="2026-06-07T12:01:00Z",
    )
    settled = settle_conscious_aperture_item(
        store, candidate_id="bounded", aperture_id=packet["aperture_id"],
        consumer_id="owner", decision="SETTLED", reason="No audit lookup.",
        dry_run=False, now="2026-06-07T12:02:00Z",
    )
    assert packet["success"] is attempted["success"] is settled["success"] is True
    assert attempted["action"] == "presentation_attempt_recorded"
    assert decision_reads == 0


def test_fairness_state_survives_unrelated_audit_volume(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "state"))
    for index in range(600):
        store.append_jsonl("decisions", {"type": "unrelated.audit", "index": index})
    store.rewrite_jsonl("candidates", [_expired("recovery-0"), _candidate("fresh-0")])
    first = open_conscious_aperture(
        store, aperture_size=1, max_active_items=1, consumer_id="owner-0",
        dry_run=False, now="2026-06-07T12:00:00Z",
    )
    first_item = first["aperture"][0]
    settle_conscious_aperture_item(
        store, candidate_id=first_item["candidate_id"], aperture_id=first_item["aperture_id"],
        consumer_id="owner-0", decision="SETTLED", reason="Advance fairness.",
        dry_run=False, now="2026-06-07T12:00:30Z",
    )
    rows = store.read_jsonl("candidates")
    rows.append(_expired("recovery-1"))
    store.rewrite_jsonl("candidates", rows)
    second = open_conscious_aperture(
        store, aperture_size=1, max_active_items=1, consumer_id="owner-1",
        dry_run=False, now="2026-06-07T12:01:00Z",
    )
    assert first["fairness_service_lanes"] == ["recovery"]
    assert second["fairness_service_lanes"] == ["fresh"]


def test_presentation_duplicate_is_idempotent_and_new_turn_is_recordable(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "state"))
    store.append_jsonl("candidates", _candidate("presented"))
    packet = open_conscious_aperture(
        store, aperture_size=1, max_active_items=1, consumer_id="owner",
        dry_run=False, now="2026-06-07T12:00:00Z",
    )
    first = record_conscious_aperture_presentation_attempt(
        store, aperture=packet["aperture"], consumer_id="owner", turn_id="turn-1",
        surface="local", now="2026-06-07T12:01:00Z",
    )
    duplicate = record_conscious_aperture_presentation_attempt(
        store, aperture=packet["aperture"], consumer_id="owner", turn_id="turn-1",
        surface="local", now="2026-06-07T12:02:00Z",
    )
    next_turn = record_conscious_aperture_presentation_attempt(
        store, aperture=packet["aperture"], consumer_id="owner", turn_id="turn-2",
        surface="local", now="2026-06-07T12:03:00Z",
    )
    attempts = [
        row for row in store.read_jsonl("decisions")
        if row.get("type") == "conscious.aperture.presentation_attempted"
    ]
    assert first["action"] == "presentation_attempt_recorded"
    assert duplicate["action"] == "presentation_already_attempted"
    assert duplicate["receipt"] == first["receipt"]
    assert next_turn["action"] == "presentation_attempt_recorded"
    assert [row["turn_id"] for row in attempts] == ["turn-1", "turn-2"]


def test_bounded_presentation_index_fails_closed_instead_of_evicting_replay_keys(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "state"))
    store.append_jsonl("candidates", _candidate("bounded-index"))
    packet = open_conscious_aperture(
        store, aperture_size=1, max_active_items=1, consumer_id="owner",
        dry_run=False, now="2026-06-07T12:00:00Z",
    )
    for index in range(MAX_PRESENTATION_INDEX_RECORDS):
        result = record_conscious_aperture_presentation_attempt(
            store, aperture=packet["aperture"], consumer_id="owner",
            turn_id=f"turn-{index}", surface="local", now="2026-06-07T12:01:00Z",
        )
        assert result["action"] == "presentation_attempt_recorded"
    before = store.read_jsonl("decisions")
    refused = record_conscious_aperture_presentation_attempt(
        store, aperture=packet["aperture"], consumer_id="owner", turn_id="turn-overflow",
        surface="local", now="2026-06-07T12:02:00Z",
    )
    replay = record_conscious_aperture_presentation_attempt(
        store, aperture=packet["aperture"], consumer_id="owner", turn_id="turn-0",
        surface="local", now="2026-06-07T12:03:00Z",
    )
    assert refused["error"] == "presentation_retention_window_exhausted"
    assert replay["action"] == "presentation_already_attempted"
    assert store.read_jsonl("decisions") == before


def test_aperture_state_absence_is_distinct_from_initialized_empty(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "state"))
    with pytest.raises(FileNotFoundError):
        store.read_conscious_aperture_state()

    empty_state = {
        "version": 1,
        "fairness_last_served_lane": None,
        "presentation_attempts": [],
    }
    store.write_conscious_aperture_state(empty_state)
    assert store.read_conscious_aperture_state() == empty_state


def test_missing_state_exact_presentation_replay_fails_closed_without_mutation(
    tmp_path, monkeypatch
):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "state"))
    store.append_jsonl("candidates", _candidate("missing-replay"))
    packet = open_conscious_aperture(
        store, aperture_size=1, max_active_items=1, consumer_id="owner",
        dry_run=False, now="2026-06-07T12:00:00Z",
    )
    first = record_conscious_aperture_presentation_attempt(
        store, aperture=packet["aperture"], consumer_id="owner", turn_id="turn-1",
        surface="local", now="2026-06-07T12:01:00Z",
    )
    assert first["action"] == "presentation_attempt_recorded"
    state_path = store.conscious_aperture_state_path
    state_path.unlink()
    before_candidates = (store.root / "candidates.jsonl").read_bytes()
    before_decisions = (store.root / "decisions.jsonl").read_bytes()
    original_read = store.read_jsonl

    def reject_decision_reads(name, limit=None):
        if name == "decisions":
            raise AssertionError("missing-state replay scanned audit history")
        return original_read(name, limit=limit)

    monkeypatch.setattr(store, "read_jsonl", reject_decision_reads)
    replay = record_conscious_aperture_presentation_attempt(
        store, aperture=packet["aperture"], consumer_id="owner", turn_id="turn-1",
        surface="local", now="2026-06-07T12:02:00Z",
    )
    assert replay == {"success": False, "error": "missing_aperture_state"}
    assert not state_path.exists()
    assert (store.root / "candidates.jsonl").read_bytes() == before_candidates
    assert (store.root / "decisions.jsonl").read_bytes() == before_decisions


def test_missing_state_open_refuses_active_lease_without_mutation(tmp_path, monkeypatch):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "state"))
    active = _expired("active-boundary")
    active["conscious_aperture"]["consumer_id"] = "owner"
    active["conscious_aperture"]["lease_expires_at"] = "2026-06-07T12:05:00Z"
    store.append_jsonl("candidates", active)
    before_candidates = (store.root / "candidates.jsonl").read_bytes()
    original_read = store.read_jsonl

    def reject_decision_reads(name, limit=None):
        if name == "decisions":
            raise AssertionError("missing-state open scanned audit history")
        return original_read(name, limit=limit)

    monkeypatch.setattr(store, "read_jsonl", reject_decision_reads)
    result = open_conscious_aperture(
        store, aperture_size=1, max_active_items=1, consumer_id="owner",
        dry_run=False, now="2026-06-07T12:04:59Z",
    )
    assert result == {"success": False, "error": "missing_aperture_state"}
    assert not store.conscious_aperture_state_path.exists()
    assert (store.root / "candidates.jsonl").read_bytes() == before_candidates
    assert not (store.root / "decisions.jsonl").exists()


def test_missing_state_allows_virgin_initialization_without_current_lease(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "state"))
    store.append_jsonl("candidates", _candidate("virgin"))
    assert not store.conscious_aperture_state_path.exists()

    result = open_conscious_aperture(
        store, aperture_size=1, max_active_items=1, consumer_id="owner",
        dry_run=False, now="2026-06-07T12:00:00Z",
    )
    assert result["action"] == "opened_aperture"
    assert result["candidate_ids"] == ["virgin"]
    assert store.conscious_aperture_state_path.exists()


def test_missing_state_recovers_lease_at_exact_expiry_without_settlement(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "state"))
    expired = _expired("expired-boundary")
    expired["conscious_aperture"]["lease_expires_at"] = "2026-06-07T12:05:00Z"
    store.append_jsonl("candidates", expired)

    result = open_conscious_aperture(
        store, aperture_size=1, max_active_items=1, consumer_id="new-owner",
        dry_run=False, now="2026-06-07T12:05:00Z",
    )
    assert result["action"] == "opened_aperture"
    assert result["reclaimed_candidate_ids"] == ["expired-boundary"]
    assert result["candidate_ids"] == ["expired-boundary"]
    assert store.conscious_aperture_state_path.exists()
    assert not [
        row for row in store.read_jsonl("decisions")
        if row.get("type") == "conscious.aperture.settled"
    ]
    assert store.read_jsonl("worker_requests") == store.read_jsonl("outbox") == []


def test_corrupt_aperture_state_fails_closed_without_audit_scan_or_mutation(
    tmp_path, monkeypatch
):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "state"))
    store.append_jsonl("candidates", _candidate("corrupt-open"))
    state_path = store.root / "inner_life" / "conscious_aperture_state.json"
    state_path.write_text("{broken", encoding="utf-8")
    before_candidates = (store.root / "candidates.jsonl").read_bytes()
    before_state = state_path.read_bytes()
    original_read = store.read_jsonl

    def reject_decision_reads(name, limit=None):
        if name == "decisions":
            raise AssertionError("corrupt-state fallback scanned audit history")
        return original_read(name, limit=limit)

    monkeypatch.setattr(store, "read_jsonl", reject_decision_reads)
    result = open_conscious_aperture(
        store, aperture_size=1, max_active_items=1, consumer_id="owner",
        dry_run=False, now="2026-06-07T12:00:00Z",
    )
    assert result == {"success": False, "error": "corrupt_aperture_state"}
    assert (store.root / "candidates.jsonl").read_bytes() == before_candidates
    assert state_path.read_bytes() == before_state
    assert not (store.root / "decisions.jsonl").exists()

    presentation_store = SensoriumStore(
        instance="presentation", state_dir=str(tmp_path / "presentation")
    )
    presentation_store.append_jsonl("candidates", _candidate("corrupt-presentation"))
    packet = open_conscious_aperture(
        presentation_store, aperture_size=1, max_active_items=1, consumer_id="owner",
        dry_run=False, now="2026-06-07T12:00:00Z",
    )
    presentation_store.conscious_aperture_state_path.write_text("[]", encoding="utf-8")
    presentation_state_before = presentation_store.conscious_aperture_state_path.read_bytes()
    before_candidates = (presentation_store.root / "candidates.jsonl").read_bytes()
    before_decisions = (presentation_store.root / "decisions.jsonl").read_bytes()
    presentation_read = presentation_store.read_jsonl

    def reject_presentation_decision_reads(name, limit=None):
        if name == "decisions":
            raise AssertionError("corrupt presentation state scanned audit history")
        return presentation_read(name, limit=limit)

    monkeypatch.setattr(presentation_store, "read_jsonl", reject_presentation_decision_reads)
    attempted = record_conscious_aperture_presentation_attempt(
        presentation_store, aperture=packet["aperture"], consumer_id="owner",
        turn_id="turn-corrupt", surface="local", now="2026-06-07T12:01:00Z",
    )
    assert attempted == {"success": False, "error": "corrupt_aperture_state"}
    assert presentation_store.conscious_aperture_state_path.read_bytes() == presentation_state_before
    assert (presentation_store.root / "candidates.jsonl").read_bytes() == before_candidates
    assert (presentation_store.root / "decisions.jsonl").read_bytes() == before_decisions


def test_resumed_lease_renews_atomically_and_settles_after_old_expiry(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "state"))
    store.append_jsonl("candidates", _candidate("resumed"))
    first = open_conscious_aperture(
        store, aperture_size=1, max_active_items=1, consumer_id="owner", lease_minutes=5,
        dry_run=False, now="2026-06-07T12:00:00Z",
    )
    original_item = first["aperture"][0]
    resumed = open_conscious_aperture(
        store, aperture_size=1, max_active_items=1, consumer_id="owner", lease_minutes=5,
        dry_run=False, now="2026-06-07T12:04:59Z",
    )
    resumed_item = resumed["aperture"][0]
    persisted = store.read_jsonl("candidates")[0]["conscious_aperture"]
    assert resumed["action"] == "resumed_aperture"
    assert resumed_item["aperture_id"] == original_item["aperture_id"]
    assert resumed_item["lease_expires_at"] == "2026-06-07T12:09:59Z"
    assert persisted["lease_expires_at"] == resumed_item["lease_expires_at"]
    attempted = record_conscious_aperture_presentation_attempt(
        store, aperture=resumed["aperture"], consumer_id="owner", turn_id="turn-after-old",
        surface="local", now="2026-06-07T12:05:01Z",
    )
    settled = settle_conscious_aperture_item(
        store, candidate_id="resumed", aperture_id=resumed_item["aperture_id"],
        consumer_id="owner", decision="SETTLED", reason="Renewed lease remains truthful.",
        dry_run=False, now="2026-06-07T12:05:02Z",
    )
    assert attempted["success"] is settled["success"] is True


def test_mixed_resumed_and_fresh_packet_has_full_processing_interval(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "state"))
    store.append_jsonl("candidates", _candidate("resumed"))
    first = open_conscious_aperture(
        store, aperture_size=1, max_active_items=2, consumer_id="owner", lease_minutes=5,
        dry_run=False, now="2026-06-07T12:00:00Z",
    )
    store.append_jsonl("candidates", _candidate("fresh"))
    mixed = open_conscious_aperture(
        store, aperture_size=2, max_active_items=2, consumer_id="owner", lease_minutes=10,
        dry_run=False, now="2026-06-07T12:04:59Z",
    )
    expiries = {item["candidate_id"]: item["lease_expires_at"] for item in mixed["aperture"]}
    assert set(expiries) == {"resumed", "fresh"}
    assert set(expiries.values()) == {"2026-06-07T12:14:59Z"}
    assert next(item for item in mixed["aperture"] if item["candidate_id"] == "resumed")[
        "aperture_id"
    ] == first["aperture_id"]


def test_resume_never_shortens_dry_run_is_immutable_and_wrong_owner_is_fenced(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "state"))
    store.append_jsonl("candidates", _candidate("long"))
    first = open_conscious_aperture(
        store, aperture_size=1, max_active_items=1, consumer_id="owner", lease_minutes=30,
        dry_run=False, now="2026-06-07T12:00:00Z",
    )
    before_candidates = (store.root / "candidates.jsonl").read_bytes()
    state_path = store.root / "inner_life" / "conscious_aperture_state.json"
    before_state = state_path.read_bytes()
    dry = open_conscious_aperture(
        store, aperture_size=1, max_active_items=1, consumer_id="owner", lease_minutes=5,
        dry_run=True, now="2026-06-07T12:10:00Z",
    )
    wrong = open_conscious_aperture(
        store, aperture_size=1, max_active_items=1, consumer_id="other", lease_minutes=60,
        dry_run=False, now="2026-06-07T12:11:00Z",
    )
    resumed = open_conscious_aperture(
        store, aperture_size=1, max_active_items=1, consumer_id="owner", lease_minutes=5,
        dry_run=False, now="2026-06-07T12:10:00Z",
    )
    assert dry["aperture"][0]["lease_expires_at"] == "2026-06-07T12:30:00Z"
    assert (store.root / "candidates.jsonl").read_bytes() == before_candidates
    assert state_path.read_bytes() == before_state
    assert wrong["action"] == "active_aperture_exists"
    assert wrong["candidate_ids"] == []
    assert resumed["aperture"][0]["lease_expires_at"] == "2026-06-07T12:30:00Z"
    assert resumed["aperture_id"] == first["aperture_id"]


def test_mixed_lane_order_is_stable_by_time_then_id_under_reordering(tmp_path):
    expired = _expired("expired")
    expired["conscious_aperture"]["lease_expires_at"] = "2026-06-07T08:00:00Z"
    held = _candidate("held")
    held["status"] = "held"
    held["held_return"] = {"not_before": "2026-06-07T09:00:00Z", "reason_code": "time_checkpoint"}
    fresh = _candidate("fresh", pressure=0.99, created_at="2026-06-07T11:00:00Z")
    outputs = []
    for name, rows in (("forward", [expired, held, fresh]), ("reverse", [fresh, held, expired])):
        store = SensoriumStore(instance=name, state_dir=str(tmp_path / name))
        store.rewrite_jsonl("candidates", rows)
        result = open_conscious_aperture(
            store, aperture_size=3, max_active_items=3, consumer_id="owner",
            stale_after_minutes=60, dry_run=False, now="2026-06-07T12:00:00Z",
        )
        outputs.append((result["candidate_ids"], result["fairness_service_lanes"]))
        assert not [d for d in store.read_jsonl("decisions") if d.get("type") == "conscious.aperture.settled"]
        assert store.read_jsonl("worker_requests") == store.read_jsonl("outbox") == []
    expected = (["expired", "fresh", "held"], ["recovery", "fresh", "recovery"])
    assert outputs == [expected, expected]


@pytest.mark.parametrize("writer_mode", ["append", "coalesce", "settlement"])
def test_candidate_mutations_serialize_across_processes(writer_mode, tmp_path):
    """A paused aperture rewrite cannot erase any other candidate writer."""
    state_dir = str(tmp_path / writer_mode)
    store = SensoriumStore(instance="test", state_dir=state_dir)
    rows = [_candidate("aperture-target")]
    if writer_mode == "settlement":
        rows.append({
            "id": "settlement-target",
            "status": "candidate",
            "kind": "task_result",
            "pressure": 0.6,
            "summary": "Settle me",
            "fingerprint": "settlement-target",
            "event_ids": ["evt-settlement-target"],
            "correlation_keys": ["settlement-target"],
            "sensitivity": "private",
            "allowed_surfaces": ["local"],
            "created_at": "2026-06-07T10:00:00Z",
            "updated_at": "2026-06-07T10:00:00Z",
        })
    elif writer_mode == "coalesce":
        rows.append({
            "id": "coalesce-target",
            "status": "candidate",
            "kind": "task_result",
            "pressure": 0.6,
            "summary": "Coalesce me",
            "fingerprint": "coalesce-target",
            "event_ids": ["evt-original"],
            "correlation_keys": ["coalesce-target"],
            "sensitivity": "private",
            "allowed_surfaces": ["local"],
            "created_at": "2026-06-07T10:00:00Z",
            "updated_at": "2026-06-07T10:00:00Z",
        })
    store.rewrite_jsonl("candidates", rows)

    ctx = multiprocessing.get_context("spawn")
    read_ready = ctx.Event()
    release_read = ctx.Event()
    writer_started = ctx.Event()
    writer_done = ctx.Event()
    result_queue = ctx.Queue()
    aperture = ctx.Process(
        target=_paused_aperture_process,
        args=(state_dir, read_ready, release_read, result_queue),
    )
    writer = ctx.Process(
        target=_concurrent_candidate_writer_process,
        args=(state_dir, writer_mode, writer_started, writer_done, result_queue),
    )
    aperture.start()
    assert read_ready.wait(5)
    writer.start()
    assert writer_started.wait(5)
    writer_completed_while_aperture_held = writer_done.wait(0.5)
    release_read.set()
    aperture.join(10)
    writer.join(10)
    assert aperture.exitcode == writer.exitcode == 0
    assert writer_completed_while_aperture_held is False
    assert result_queue.get(timeout=2)
    assert result_queue.get(timeout=2)

    final = {row["id"]: row for row in store.read_jsonl("candidates")}
    assert final["aperture-target"]["status"] == "in_conscious_aperture"
    if writer_mode == "append":
        assert any(row.get("event_ids") == ["evt-append"] for row in final.values())
    elif writer_mode == "coalesce":
        assert final["coalesce-target"]["event_ids"] == ["evt-original", "evt-coalesce"]
    else:
        assert final["settlement-target"]["status"] == "suppressed"


def test_candidate_transaction_is_same_root_reentrant_and_rejects_cross_root_nesting(tmp_path):
    first = SensoriumStore(instance="first", state_dir=str(tmp_path / "first"))
    same_root = SensoriumStore(instance="first", state_dir=str(tmp_path / "first"))
    other = SensoriumStore(instance="other", state_dir=str(tmp_path / "other"))
    with first.candidate_transaction():
        with same_root.candidate_transaction():
            same_root.append_jsonl("candidates", {"id": "nested"})
        with pytest.raises(RuntimeError, match="cannot nest across profile roots"):
            with other.candidate_transaction():
                pass
    assert first.read_jsonl("candidates") == [{"id": "nested"}]


@pytest.mark.parametrize("action", ["suppress", "hold", "resume", "cancel", "mark_reviewed"])
def test_every_generic_candidate_action_rejects_a_leased_row(action, tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / action))
    store.append_jsonl("candidates", _candidate("leased"))
    packet = open_conscious_aperture(
        store,
        aperture_size=1,
        max_active_items=1,
        consumer_id="owner",
        dry_run=False,
        now="2026-06-07T12:00:00Z",
    )
    before = store.read_jsonl("candidates")
    result = json.loads(handle_sensorium_candidate_update(
        candidate_id="leased",
        action=action,
        reason="Generic mutation must not bypass ownership.",
        instance="test",
        state_dir=str(store.root),
    ))
    assert result["success"] is False
    assert result["error"] == "candidate_leased_requires_exact_settlement"
    assert store.read_jsonl("candidates") == before
    assert packet["aperture"][0]["candidate_id"] == "leased"


@pytest.mark.parametrize("decision", ["DROP", "SAVE", "PROMOTE_CONSCIOUS"])
def test_every_kanban_settlement_rejects_a_leased_row(decision, tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / decision))
    store.append_jsonl("candidates", _candidate("leased-kanban"))
    open_conscious_aperture(
        store,
        aperture_size=1,
        max_active_items=1,
        consumer_id="owner",
        dry_run=False,
        now="2026-06-07T12:00:00Z",
    )
    before = store.read_jsonl("candidates")
    result = apply_kanban_settlement(
        store,
        decision=decision,
        candidate_id="leased-kanban",
        reason="Kanban must not bypass the active owner.",
    )
    assert result["action"] == "leased_candidate_requires_exact_settlement"
    assert result["updated_candidate_ids"] == []
    assert result["receipts"] == []
    assert store.read_jsonl("candidates") == before


def test_compaction_cannot_archive_a_leased_row(tmp_path):
    state_dir = tmp_path / "compact"
    store = SensoriumStore(instance="test", state_dir=str(state_dir))
    candidate = _candidate("leased-compact")
    candidate["expires_at"] = "2000-01-01T00:00:00Z"
    store.append_jsonl("candidates", candidate)
    packet = open_conscious_aperture(
        store,
        aperture_size=1,
        max_active_items=1,
        consumer_id="owner",
        dry_run=False,
        now="2099-06-07T12:00:00Z",
    )
    before = store.read_jsonl("candidates")
    result = json.loads(handle_sensorium_compact(instance="test", state_dir=str(state_dir)))
    assert result["success"] is True
    assert result["data"]["archived_candidates"] == []
    assert store.read_jsonl("candidates") == before
    assert packet["aperture"][0]["candidate_id"] == "leased-compact"


@pytest.mark.parametrize("keyword", ["suppress", "cancel", "resume"])
def test_live_generic_update_keywords_cannot_bypass_exact_lease(keyword, tmp_path):
    state_dir = tmp_path / keyword
    store = SensoriumStore(instance="test", state_dir=str(state_dir))
    store.append_jsonl("candidates", _candidate("cand_live_leased"))
    packet = open_conscious_aperture(
        store,
        aperture_size=1,
        max_active_items=1,
        consumer_id="foreground:owner",
        dry_run=False,
        now="2026-06-07T12:00:00Z",
    )
    item = packet["aperture"][0]
    ctx = _LivePluginContext()
    register(ctx)
    result = json.loads(ctx.tools["sensorium"]({
        "action": "update",
        "instance": "test",
        "id": "cand_live_leased",
        "keyword": keyword,
        "text": "No generic ownership bypass.",
        "aperture_id": item["aperture_id"],
        "consumer_id": item["consumer_id"],
    }, state_dir=str(state_dir)))
    assert result["success"] is False
    assert result["error"] == "candidate_leased_requires_exact_settlement"
    assert store.read_jsonl("candidates")[0]["status"] == "in_conscious_aperture"


def test_doorway_context_caps_display_lists_and_total_bytes():
    huge = "display-" + ("x" * 20_000)
    packet = {
        "aperture": [{
            "candidate_id": "cand_exact",
            "aperture_id": "cap_exact",
            "consumer_id": "foreground:exact",
            "lease_expires_at": "2026-06-07T12:15:00Z",
            "summary": huge,
            "conscious_task": {
                "id": huge,
                "request_type": huge,
                "title": huge,
                "why": huge,
                "expected_decision": huge,
            },
            "source_binding": {
                "candidate_id": "cand_exact",
                "conscious_task_id": huge,
                "candidate_fingerprint": huge,
                "source_fingerprint": huge,
                "source_revision": huge,
                "event_ids": [f"event-{index}-{huge}" for index in range(500)],
                "source_candidate_ids": [f"source-{index}-{huge}" for index in range(500)],
                "source_digest": "a" * 64,
            },
        }],
    }
    context = conscious_doorway_context(packet, agent_label=huge)
    assert len(context.encode("utf-8")) <= 8192
    assert huge not in context
    assert '"candidate_id":"cand_exact"' in context
    assert '"aperture_id":"cap_exact"' in context
    assert '"consumer_id":"foreground:exact"' in context


def test_oversized_authority_id_is_not_truncated_or_leased(tmp_path):
    state_dir = tmp_path / "oversized-authority"
    store = SensoriumStore(instance="test", state_dir=str(state_dir))
    store.ensure_dirs()
    (state_dir / "instance.config.json").write_text(json.dumps({
        "instance_name": "test",
        "allowed_surfaces": ["local"],
        "conscious_doorway": {"enabled": True, "surfaces": ["local"]},
    }))
    oversized_id = "cand_" + ("x" * 1000)
    store.append_jsonl("candidates", _candidate(oversized_id))
    assert handle_conscious_doorway_pre_llm(
        instance="test",
        platform="local",
        session_id="session",
        state_dir=str(state_dir),
    ) is None
    row = store.read_jsonl("candidates")[0]
    assert row["id"] == oversized_id
    assert row["status"] == "candidate"


def test_live_settlement_uses_the_hook_state_dir_override(tmp_path, monkeypatch):
    import agent_sensorium.store as store_module

    implicit_root = tmp_path / "implicit"
    override = tmp_path / "override"
    monkeypatch.setattr(store_module, "_DEFAULT_BASE", str(implicit_root))
    store = SensoriumStore(instance="test", state_dir=str(override))
    store.append_jsonl("candidates", _candidate("override-owned"))
    packet = open_conscious_aperture(
        store,
        aperture_size=1,
        max_active_items=1,
        consumer_id="foreground:override",
        dry_run=False,
        now="2099-06-07T12:00:00Z",
    )
    item = packet["aperture"][0]
    ctx = _LivePluginContext()
    register(ctx)
    result = json.loads(ctx.tools["sensorium"]({
        "action": "update",
        "instance": "test",
        "id": "override-owned",
        "keyword": "settle",
        "text": "Settle in exact overridden root.",
        "aperture_id": item["aperture_id"],
        "consumer_id": item["consumer_id"],
    }, state_dir=str(override)))
    assert result["success"] is True
    assert store.read_jsonl("candidates")[0]["status"] == "reviewed"
    assert SensoriumStore(instance="test").read_jsonl("candidates") == []


def test_conscious_doorway_is_local_only_and_local_desktop_surface_still_works(tmp_path):
    state_dir = tmp_path / "local-only-doorway"
    store = SensoriumStore(instance="test", state_dir=str(state_dir))
    store.ensure_dirs()
    (state_dir / "instance.config.json").write_text(json.dumps({
        "instance_name": "test",
        "allowed_surfaces": ["local", "discord"],
        "conscious_doorway": {
            "enabled": True,
            "aperture_size": 1,
            "max_active_items": 1,
            "surfaces": ["local", "discord"],
        },
    }))
    candidate = _candidate("local-only")
    candidate["allowed_surfaces"] = ["local", "discord"]
    store.append_jsonl("candidates", candidate)

    remote = handle_conscious_doorway_pre_llm(
        instance="test",
        platform="discord",
        session_id="remote-session",
        state_dir=str(state_dir),
    )
    assert remote is None
    assert store.read_jsonl("candidates")[0]["status"] == "candidate"

    local = handle_conscious_doorway_pre_llm(
        instance="test",
        platform="local",
        session_id="desktop-session",
        state_dir=str(state_dir),
    )
    assert local is not None
    assert "[Sensorium Conscious Aperture]" in local["context"]
    assert store.read_jsonl("candidates")[0]["status"] == "in_conscious_aperture"


def test_liveness_prefers_explicit_lease_expiry_with_legacy_fallback():
    def leased(candidate_id, *, opened_at, lease_expires_at=None):
        row = _candidate(candidate_id)
        row["status"] = "in_conscious_aperture"
        row["conscious_aperture"] = {
            "id": f"cap-{candidate_id}",
            "consumer_id": "owner",
            "generation": 1,
            "opened_at": opened_at,
            "state": "open",
        }
        if lease_expires_at is not None:
            row["conscious_aperture"]["lease_expires_at"] = lease_expires_at
        return row

    expired = leased(
        "expired-explicit",
        opened_at="2026-06-07T11:59:00Z",
        lease_expires_at="2026-06-07T11:59:30Z",
    )
    future = leased(
        "future-explicit",
        opened_at="2026-06-07T08:00:00Z",
        lease_expires_at="2026-06-07T13:00:00Z",
    )
    legacy = leased("legacy-timeout", opened_at="2026-06-07T08:00:00Z")
    stale_ids = _derived_stale_aperture_ids(
        [expired, future, legacy],
        now="2026-06-07T12:00:00Z",
    )
    assert stale_ids == {"expired-explicit", "legacy-timeout"}

    dashboard = _load_dashboard_plugin()
    dashboard_expired = leased(
        "dashboard-expired",
        opened_at="2999-01-01T00:00:00Z",
        lease_expires_at="2000-01-01T00:00:00Z",
    )
    dashboard_future = leased(
        "dashboard-future",
        opened_at="2000-01-01T00:00:00Z",
        lease_expires_at="2999-01-01T00:00:00Z",
    )
    dashboard_legacy = leased("dashboard-legacy", opened_at="2000-01-01T00:00:00Z")
    assert dashboard._candidate_liveness(dashboard_expired)["reason_code"] == "stale_aperture"
    assert (
        dashboard._candidate_liveness(dashboard_future)["reason_code"]
        == "reviewing_open_aperture"
    )
    assert dashboard._candidate_liveness(dashboard_legacy)["reason_code"] == "stale_aperture"


@pytest.mark.parametrize(
    ("field", "malformed_value"),
    [("conscious_task", "not-a-task-object"), ("advisory_meta", ["not", "an", "object"])],
)
def test_malformed_advisory_shapes_do_not_block_unrelated_valid_candidate(
    field, malformed_value, tmp_path
):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / field))
    malformed = _candidate(f"malformed-{field}", pressure=0.99)
    malformed[field] = malformed_value
    valid = _candidate("valid-neighbor", pressure=0.5)
    store.rewrite_jsonl("candidates", [malformed, valid])

    packet = open_conscious_aperture(
        store,
        aperture_size=1,
        max_active_items=1,
        consumer_id="owner",
        dry_run=False,
        now="2026-06-07T12:00:00Z",
    )

    assert packet["success"] is True
    assert packet["candidate_ids"] == ["valid-neighbor"]
    rows = {row["id"]: row for row in store.read_jsonl("candidates")}
    assert rows[f"malformed-{field}"]["status"] == "candidate"
    assert rows["valid-neighbor"]["status"] == "in_conscious_aperture"
