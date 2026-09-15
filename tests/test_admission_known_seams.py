from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import subprocess
from pathlib import Path

from agent_sensorium.admission import _claim_for_signal, build_admission_plan
from agent_sensorium.store import SensoriumStore

ROOT = Path(__file__).parents[1]


def _store(tmp_path: Path, name: str) -> SensoriumStore:
    store = SensoriumStore(instance=name, state_dir=str(tmp_path / name))
    store.ensure_dirs()
    return store


def _frontier(signal_id: str, revision: str) -> dict:
    return {
        "id": signal_id,
        "sensor": "research.frontier",
        "source": "artifact",
        "kind": "creative_pull",
        "summary": revision,
        "artifact_meta": {"entry_id": "same-item", "sha256": revision},
    }


def _finish(store: SensoriumStore, budget: int = 4 * 1024 * 1024):
    for _ in range(200):
        result = store.prepare_admission_index(scan_bytes=budget)
        if result.complete:
            return result
    raise AssertionError("admission owner did not reach ready")


def _clock():
    spec = importlib.util.spec_from_file_location(
        "known_seam_clock", ROOT / "scripts" / "sensorium_native_clock.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _args(store: SensoriumStore) -> argparse.Namespace:
    return argparse.Namespace(
        instance=store.instance,
        state_dir=str(store.root),
        plugin_root=str(ROOT),
        event_limit=1,
        candidate_limit=1,
        admission_scan_bytes=4 * 1024 * 1024,
        failure_cooldown_seconds=1800,
        sensor_timeout_seconds=60,
        hermes_timeout_seconds=60,
        total_timeout_seconds=120,
        cleanup_reserve_seconds=10,
        skip_sensors=True,
        force=False,
        print_json=False,
        hermes_cli="/must-not-run/hermes",
        provider="fixture",
        model="fixture",
    )


def _seed_one(store: SensoriumStore, *, status: str = "candidate") -> None:
    store.append_jsonl("signals", _frontier("signal-one", "revision-one"))
    store.append_jsonl(
        "events",
        {
            "id": "event",
            "kind": "creative_pull",
            "summary": "event",
            "source_signal_ids": ["signal-one"],
        },
    )
    store.append_jsonl(
        "candidates",
        {
            "id": "candidate",
            "status": status,
            "kind": "creative_pull",
            "summary": "candidate",
            "pressure": 0.8,
            "created_at": "2026-01-01T00:00:00Z",
            "event_ids": ["event"],
        },
    )


def test_ready_prepare_and_selection_sql_work_do_not_scale_with_archive(tmp_path):
    clock = _clock()
    measurements = []
    for count in (10, 2000):
        store = _store(tmp_path, f"archive-sql-{count}")
        for index in range(count):
            store.append_jsonl(
                "candidates",
                {
                    "id": f"archived-{index}",
                    "status": "archived",
                    "kind": "creative_pull",
                    "event_ids": [],
                },
            )
        _finish(store)
        material = clock._source_material(
            store,
            event_limit=1,
            candidate_limit=1,
            admission_scan_bytes=4 * 1024 * 1024,
        )
        measurements.append(material["admission_work"])
        assert material["candidate_source_count"] == count
        assert material["selection"] is None
        assert material["admission_work"]["source_records"] == 0
    small, large = measurements
    assert large["prepare_sqlite_vm_steps"] <= small["prepare_sqlite_vm_steps"] * 2
    assert large["selection_sqlite_vm_steps"] <= small["selection_sqlite_vm_steps"] * 2


def test_disposition_reverse_item_lookup_is_indexed_and_bounded(tmp_path):
    measurements = []
    explain = None
    for count in (10, 1000):
        store = _store(tmp_path, f"item-index-{count}")
        for index in range(count):
            signal_id = f"signal-{index}"
            event_id = f"event-{index}"
            candidate_id = f"candidate-{index}"
            signal = _frontier(signal_id, f"revision-{index}")
            signal["artifact_meta"]["entry_id"] = f"item-{index}"
            store.append_jsonl("signals", signal)
            store.append_jsonl(
                "events", {"id": event_id, "source_signal_ids": [signal_id]}
            )
            store.append_jsonl(
                "candidates",
                {
                    "id": candidate_id,
                    "status": "candidate",
                    "kind": "creative_pull",
                    "pressure": 0.5,
                    "created_at": "2026-01-01T00:00:00Z",
                    "event_ids": [event_id],
                },
            )
        _finish(store)
        binding = build_admission_plan(store, candidate_limit=1)["selection"]
        assert binding is not None
        store.append_jsonl(
            "decisions",
            {
                "id": "settled",
                "type": "subconscious.advisory",
                "dry_run": False,
                "action": "drop",
                "output_action": "DROP",
                "admission_binding": binding,
            },
        )
        result = _finish(store)
        measurements.append(result.sqlite_vm_steps)
        conn = sqlite3.connect(store.root / "indexes" / "admission-v2.sqlite3")
        if explain is None:
            explain = conn.execute(
                "EXPLAIN QUERY PLAN SELECT candidate_id FROM candidate_state WHERE item_key=?",
                (binding["item_key"],),
            ).fetchall()
        conn.close()
    assert measurements[1] <= measurements[0] * 2
    assert explain is not None
    assert any("ix_candidate_state_item" in row[3] for row in explain)


def test_archive_only_native_tick_uses_index_metadata_not_full_jsonl(monkeypatch, tmp_path):
    store = _store(tmp_path, "archive-only")
    _seed_one(store, status="archived")
    _finish(store)
    clock = _clock()
    original = clock.SensoriumStore.read_jsonl

    def bounded_only(self, name, limit=None):
        if name == "candidates" and limit is None:
            raise AssertionError("unbounded candidate read")
        return original(self, name, limit)

    monkeypatch.setattr(clock.SensoriumStore, "read_jsonl", bounded_only)
    result = clock.run_once(_args(store))
    assert result["action"] == "skipped_no_eligible_source"
    assert result["admission_index"]["work"]["source_records"] == 0


def test_corrupt_prepare_is_authoritative_for_native_entrypoint(tmp_path):
    store = _store(tmp_path, "corrupt-native")
    _seed_one(store)
    _finish(store)
    (store.root / "indexes" / "admission-v2.sqlite3").write_bytes(b"not sqlite")
    clock = _clock()
    calls = []

    def forbidden_runner(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="must not run")

    result = clock.run_once(_args(store), run_command=forbidden_runner)
    assert result["action"] == "skipped_admission_index_invalid"
    assert result["admission_index"]["reason"] == "file is not a database"
    assert calls == []


def test_append_winner_refreshes_projection_without_candidate_rewrite(tmp_path):
    store = _store(tmp_path, "append-winner")
    _seed_one(store)
    _finish(store)
    old = build_admission_plan(store)["selection"]
    assert old is not None
    second = _frontier("signal-two", "revision-two")
    store.append_jsonl("signals", second)
    store.append_jsonl(
        "events",
        {
            "id": "event",
            "kind": "creative_pull",
            "summary": "replacement",
            "source_signal_ids": ["signal-two"],
        },
    )
    prepared = _finish(store)
    current = build_admission_plan(store)["selection"]
    claim, error = _claim_for_signal(store.instance, second)
    assert prepared.dependent_refreshes == 1
    assert error is None and claim is not None and current is not None
    assert current["revision_key"] == claim["revision_key"]
    assert current["admission_key"] != old["admission_key"]
    for decision_id, binding in (("old-settlement", old), ("exact-settlement", current)):
        store.append_jsonl(
            "decisions",
            {
                "id": decision_id,
                "type": "subconscious.advisory",
                "dry_run": False,
                "action": "drop",
                "output_action": "DROP",
                "admission_binding": binding,
            },
        )
        _finish(store)
        plan = build_admission_plan(store)
        if decision_id == "old-settlement":
            assert plan["selection"] == current
        else:
            assert plan["selection"] is None
            assert plan["suppressed_counts"] == {"prior_disposition": 1}


def test_dependency_fanout_is_queued_and_fail_closed_until_complete(tmp_path):
    store = _store(tmp_path, "fanout")
    store.append_jsonl("signals", _frontier("signal-one", "revision-one"))
    store.append_jsonl("events", {"id": "shared", "source_signal_ids": ["signal-one"]})
    for index in range(100):
        store.append_jsonl(
            "candidates",
            {
                "id": f"candidate-{index:03d}",
                "status": "candidate",
                "kind": "creative_pull",
                "pressure": 0.5,
                "created_at": "2026-01-01T00:00:00Z",
                "event_ids": ["shared"],
            },
        )
    _finish(store)
    second = _frontier("signal-two", "revision-two")
    store.append_jsonl("signals", second)
    store.append_jsonl("events", {"id": "shared", "source_signal_ids": ["signal-two"]})
    first = store.prepare_admission_index(scan_bytes=4 * 1024 * 1024)
    assert not first.complete
    assert first.reason == "dependent_refresh_pending"
    assert first.dependent_refreshes == 64
    incomplete = build_admission_plan(store)
    assert incomplete["state"] == "catching_up" and incomplete["selection"] is None
    final = _finish(store)
    assert final.dependent_refreshes == 36
    claim, error = _claim_for_signal(store.instance, second)
    assert error is None and claim is not None
    conn = sqlite3.connect(store.root / "indexes" / "admission-v2.sqlite3")
    revisions = {
        json.loads(row[0])["revision_key"]
        for row in conn.execute("SELECT binding_json FROM candidate_state")
    }
    conn.close()
    assert revisions == {claim["revision_key"]}
