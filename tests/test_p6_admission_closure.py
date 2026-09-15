from __future__ import annotations

import importlib.util
import os
import sqlite3
from pathlib import Path

from agent_sensorium.admission import binding_for_candidate, build_admission_plan
from agent_sensorium.store import SensoriumStore


def _store(tmp_path: Path, name: str = "admission") -> SensoriumStore:
    store = SensoriumStore(instance=name, state_dir=str(tmp_path / name))
    store.ensure_dirs()
    return store


def _source(store: SensoriumStore, suffix: str, *, pressure: float, created: str) -> str:
    signal_id = f"signal-{suffix}"
    event_id = f"event-{suffix}"
    candidate_id = f"candidate-{suffix}"
    store.append_jsonl("signals", {
        "id": signal_id, "sensor": "research.source_feed", "source": "artifact",
        "kind": "creative_pull", "summary": f"source {suffix}",
        "artifact_meta": {"source_id": "feed", "item_id": suffix},
    })
    store.append_jsonl("events", {
        "id": event_id, "kind": "creative_pull", "summary": f"event {suffix}",
        "source_signal_ids": [signal_id], "correlation_keys": [f"item:{suffix}"],
    })
    store.append_jsonl("candidates", {
        "id": candidate_id, "status": "candidate", "kind": "creative_pull",
        "summary": f"candidate {suffix}", "pressure": pressure,
        "event_ids": [event_id], "correlation_keys": [f"item:{suffix}"],
        "created_at": created,
    })
    return candidate_id


def _memory_source(store: SensoriumStore, suffix: str, members: list[str]) -> str:
    signal_id = f"memory-signal-{suffix}"
    event_id = f"memory-event-{suffix}"
    candidate_id = f"memory-candidate-{suffix}"
    store.append_jsonl("signals", {
        "id": signal_id, "sensor": "sensorium.memory_reflection", "source": "memory",
        "kind": "memory_reflection", "summary": f"memory {suffix}",
        "memory_provenance": {
            "provider": "hindsight", "bank_id": "synthetic", "item_ids": members,
        },
    })
    store.append_jsonl("events", {
        "id": event_id, "kind": "memory_reflection", "summary": f"memory {suffix}",
        "source_signal_ids": [signal_id],
    })
    store.append_jsonl("candidates", {
        "id": candidate_id, "status": "candidate", "kind": "memory_reflection",
        "summary": f"memory {suffix}", "pressure": 0.8,
        "event_ids": [event_id], "created_at": "2026-01-01T00:00:00Z",
    })
    return candidate_id


def _frontier_revision(store: SensoriumStore, revision: str, *, create: bool) -> str:
    signal_id = f"frontier-signal-{revision}"
    store.append_jsonl("signals", {
        "id": signal_id, "sensor": "research.frontier", "source": "artifact",
        "kind": "creative_pull", "summary": "frontier source",
        "artifact_meta": {"entry_id": "entry", "sha256": revision},
    })
    store.append_jsonl("events", {
        "id": "frontier-event", "kind": "creative_pull", "summary": "frontier event",
        "source_signal_ids": [signal_id],
    })
    if create:
        store.append_jsonl("candidates", {
            "id": "frontier-candidate", "status": "candidate", "kind": "creative_pull",
            "summary": "frontier candidate", "pressure": 0.8,
            "event_ids": ["frontier-event"], "created_at": "2026-01-01T00:00:00Z",
        })
    return "frontier-candidate"


def _finish_index(store: SensoriumStore, budget: int, limit: int = 100) -> list:
    results = []
    for _ in range(limit):
        result = store.prepare_admission_index(scan_bytes=budget)
        results.append(result)
        assert result.bytes_consumed <= budget
        if result.complete:
            return results
        assert result.state in {"catching_up", "rebuilding"}
    raise AssertionError("index did not complete")


def test_malformed_advisory_metadata_and_binding_do_not_abort_valid_source(tmp_path):
    store = _store(tmp_path, "malformed")
    wanted = _source(store, "wanted", pressure=0.7, created="2026-01-01T00:00:00Z")
    store.append_jsonl("candidates", {
        "id": "malformed-advisory", "kind": "subconscious_advisory", "status": "candidate",
        "advisory_meta": ["not", "a", "mapping"], "admission_binding": "also-not-a-mapping",
    })
    _finish_index(store, 4096)
    plan = build_admission_plan(store, candidate_limit=1)
    assert plan["state"] == "ready"
    assert plan["selection"]["source_candidate_id"] == wanted


def test_global_priority_and_oldest_tie_are_not_physical_tail_limited(tmp_path):
    store = _store(tmp_path, "priority")
    highest_oldest = _source(
        store, "old-high", pressure=0.95, created="2020-01-01T00:00:00Z",
    )
    _source(store, "new-low", pressure=0.1, created="2026-01-01T00:00:00Z")
    _source(store, "new-high", pressure=0.95, created="2025-01-01T00:00:00Z")
    _finish_index(store, 4096)
    plan = build_admission_plan(store, candidate_limit=1)
    assert plan["selection"]["source_candidate_id"] == highest_oldest
    assert plan["eligible_count"] == 3
    assert plan["projected_candidate_count"] == 1


def test_old_applying_disposition_remains_authoritative_but_dry_run_does_not(tmp_path):
    store = _store(tmp_path, "old-disposition")
    settled_id = _source(store, "settled", pressure=1.0, created="2020-01-01T00:00:00Z")
    live_id = _source(store, "live", pressure=0.5, created="2021-01-01T00:00:00Z")
    settled, error = binding_for_candidate(store, settled_id)
    assert error is None and settled
    live, error = binding_for_candidate(store, live_id)
    assert error is None and live
    store.append_jsonl("decisions", {
        "id": "old-applied", "type": "subconscious.advisory", "dry_run": False,
        "action": "drop", "output_action": "DROP", "admission_binding": settled,
    })
    store.append_jsonl("decisions", {
        "id": "old-dry", "type": "subconscious.advisory", "dry_run": True,
        "action": "drop", "output_action": "DROP", "admission_binding": live,
    })
    for index in range(100):
        store.append_jsonl("decisions", {"id": f"noise-{index}", "dry_run": True})
    _finish_index(store, 4096)
    plan = build_admission_plan(store, candidate_limit=1)
    assert plan["selection"]["source_candidate_id"] == live_id
    assert plan["suppressed_counts"] == {"prior_disposition": 1}


def test_incremental_aggregate_budget_restart_and_no_mixed_completeness(tmp_path):
    store = _store(tmp_path, "catch-up")
    expected = None
    for index in range(18):
        candidate = _source(
            store, str(index), pressure=index / 20,
            created=f"2026-01-{index + 1:02d}T00:00:00Z",
        )
        expected = candidate
    first = store.prepare_admission_index(scan_bytes=700)
    assert not first.complete and first.bytes_consumed <= 700
    plan = build_admission_plan(store, candidate_limit=2, admission_scan_bytes=700)
    assert plan["selection"] is None
    assert plan["eligible_count"] is None
    # A new store object proves progress is process-independent.
    restarted = SensoriumStore(instance=store.instance, state_dir=str(store.root))
    passes = _finish_index(restarted, 700)
    assert len(passes) > 1
    ready = build_admission_plan(restarted, candidate_limit=2, admission_scan_bytes=700)
    assert ready["selection"]["source_candidate_id"] == expected
    assert ready["projected_candidate_count"] == 2


def test_index_preserves_memory_v1_v2_member_overlap_compatibility(tmp_path):
    store = _store(tmp_path, "memory-compat")
    ab_id = _memory_source(store, "ab", ["A", "B"])
    ab, error = binding_for_candidate(store, ab_id)
    assert error is None and ab
    legacy = {
        key: value for key, value in ab.items()
        if key not in {"envelope_key", "member_keys", "selected_member_keys"}
    }
    legacy["policy_version"] = "source-admission-v1"
    store.append_jsonl("decisions", {
        "id": "legacy-ab", "type": "subconscious.advisory", "dry_run": False,
        "action": "drop", "output_action": "DROP", "admission_binding": legacy,
    })
    bc_id = _memory_source(store, "bc", ["B", "C"])
    _finish_index(store, 4096)
    first = build_admission_plan(store, candidate_limit=1)
    assert first["selection"]["source_candidate_id"] == bc_id
    assert len(first["selection"]["selected_member_keys"]) == 1
    assert first["source_decisions"][-1].get("attribution_limitation") != (
        "legacy_lineage_unavailable"
    )
    store.append_jsonl("decisions", {
        "id": "v2-c", "type": "subconscious.advisory", "dry_run": False,
        "action": "drop", "output_action": "DROP",
        "admission_binding": first["selection"],
    })
    _finish_index(store, 4096)
    assert build_admission_plan(store)["selection"] is None


def test_old_nonterminal_advisory_allows_revision_but_terminal_closure_suppresses(tmp_path):
    store = _store(tmp_path, "terminal-authority")
    candidate_id = _frontier_revision(store, "revision-one", create=True)
    old, error = binding_for_candidate(store, candidate_id)
    assert error is None and old
    store.append_jsonl("candidates", {
        "id": "old-advisory", "kind": "subconscious_advisory", "status": "held",
        "admission_binding": old, "advisory_meta": {"action": "CREATE_CONSCIOUS_TASK"},
    })
    _frontier_revision(store, "revision-two", create=False)
    _finish_index(store, 4096)
    assert build_admission_plan(store)["selection"] is not None
    rows = store.read_jsonl("candidates")
    next(row for row in rows if row.get("id") == "old-advisory")["status"] = "archived"
    store.rewrite_jsonl("candidates", rows)
    _finish_index(store, 4096)
    closed = build_admission_plan(store)
    assert closed["selection"] is None
    assert closed["suppressed_counts"] == {"explicit_item_closure": 1}


def test_candidate_replacement_and_truncation_fail_closed_then_rebuild(tmp_path):
    store = _store(tmp_path, "replacement")
    _source(store, "first", pressure=0.5, created="2026-01-01T00:00:00Z")
    _finish_index(store, 4096)
    candidates = store.read_jsonl("candidates")
    candidates[0]["pressure"] = 0.8
    store.rewrite_jsonl("candidates", candidates)
    stale = build_admission_plan(store)
    assert stale["selection"] is None and stale["state"] == "catching_up"
    _finish_index(store, 4096)
    assert build_admission_plan(store)["selection"] is not None
    store.paths["signals"].write_bytes(b"{\"broken\":")
    assert build_admission_plan(store)["selection"] is None
    invalid = store.prepare_admission_index(scan_bytes=4096)
    assert invalid.state == "invalid" and invalid.complete is False


def test_preview_cacheless_read_is_domain_and_cache_write_free(tmp_path):
    store = _store(tmp_path, "read-only")
    _source(store, "one", pressure=0.5, created="2026-01-01T00:00:00Z")
    before = {
        str(path.relative_to(store.root)): (path.stat().st_mtime_ns, path.read_bytes())
        for path in store.root.rglob("*") if path.is_file()
    }
    plan = build_admission_plan(store)
    after = {
        str(path.relative_to(store.root)): (path.stat().st_mtime_ns, path.read_bytes())
        for path in store.root.rglob("*") if path.is_file()
    }
    assert plan["selection"] is not None
    assert before == after
    assert not (store.root / "indexes").exists()


def test_cache_is_private_safe_mode_0600_and_symlink_fails_closed(tmp_path):
    store = _store(tmp_path, "privacy")
    _source(store, "secret", pressure=0.5, created="2026-01-01T00:00:00Z")
    _finish_index(store, 4096)
    cache = store.root / "indexes" / "admission-v2.sqlite3"
    assert cache.stat().st_mode & 0o777 == 0o600
    conn = sqlite3.connect(cache)
    signal_payload = " ".join(
        row[0] for row in conn.execute("SELECT projection_json FROM signal_claim")
    )
    conn.close()
    assert "source secret" not in signal_payload

    other = tmp_path / "outside"
    other.mkdir()
    linked = _store(tmp_path, "symlink")
    (linked.root / "indexes").symlink_to(other, target_is_directory=True)
    result = linked.prepare_admission_index(scan_bytes=4096)
    assert result.complete is False and result.state == "invalid"
    assert list(other.iterdir()) == []


def test_corrupt_cache_fails_closed_and_next_prepare_rebuilds(tmp_path):
    store = _store(tmp_path, "corrupt")
    _source(store, "one", pressure=0.5, created="2026-01-01T00:00:00Z")
    _finish_index(store, 4096)
    cache = store.root / "indexes" / "admission-v2.sqlite3"
    cache.write_bytes(b"not sqlite")
    plan = build_admission_plan(store)
    assert plan["selection"] is None and plan["state"] == "invalid"
    failed = store.prepare_admission_index(scan_bytes=4096)
    assert not failed.complete
    # Corruption is removed safely by the failed preparation; next pass rebuilds.
    rebuilt = store.prepare_admission_index(scan_bytes=4096)
    assert rebuilt.complete
    assert build_admission_plan(store)["selection"] is not None


def test_ready_native_entrypoint_ignores_large_archived_history_footprint(tmp_path):
    clock = importlib.util.spec_from_file_location(
        "bounded_clock", Path(__file__).parents[1] / "scripts" / "sensorium_native_clock.py",
    )
    assert clock and clock.loader
    module = importlib.util.module_from_spec(clock)
    clock.loader.exec_module(module)
    store = _store(tmp_path, "archived-footprint")
    wanted = _source(store, "old-unresolved", pressure=0.9, created="2020-01-01T00:00:00Z")
    for index in range(300):
        store.append_jsonl("candidates", {
            "id": f"archived-{index}", "status": "archived", "kind": "creative_pull",
            "pressure": 1.0, "created_at": "2019-01-01T00:00:00Z", "event_ids": [],
        })
        store.append_jsonl("decisions", {"id": f"audit-{index}", "dry_run": True})
    first = None
    for _ in range(200):
        first = module._source_material(
            store, event_limit=1, candidate_limit=1, admission_scan_bytes=4096,
        )
        if first.get("admission_state", "ready") == "ready":
            break
    assert first is not None
    assert first["selection"]["source_candidate_id"] == wanted
    assert first["eligible_count"] == 1
    assert first["admission_work"]["materialized_rows"] == 1
    assert first["admission_work"]["query_rows"] <= 2
    # A steady read charges only four fixed-size boundary checks. Neither query
    # nor materialized work scales with the 600 archived/audit rows.
    second = module._source_material(
        store, event_limit=1, candidate_limit=1, admission_scan_bytes=4096,
    )
    assert second["selection"] == first["selection"]
    assert second["admission_work"]["source_records"] == 0
    assert second["admission_work"]["source_bytes"] <= 4 * 2 * 64
    assert second["admission_work"]["query_rows"] <= 2
    assert second["admission_work"]["materialized_rows"] == 1


def test_same_stat_boundary_drift_is_detected_read_only(tmp_path):
    store = _store(tmp_path, "boundary")
    _source(store, "first", pressure=0.5, created="2026-01-01T00:00:00Z")
    _finish_index(store, 4096)
    path = store.paths["signals"]
    before = path.stat()
    payload = path.read_bytes()
    index = payload.rfind(b"first")
    assert index >= 0
    path.write_bytes(payload[:index] + b"other" + payload[index + len(b"first"):])
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    plan = build_admission_plan(store)
    assert plan["selection"] is None
    assert plan["state"] == "invalid"
    assert plan["reason"] == "source_boundary_changed"
