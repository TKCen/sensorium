from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

import agent_sensorium.desktop_presentation as presentation
from agent_sensorium.desktop_projection_index import DesktopProjectionIndex
from agent_sensorium.outbox import (
    OUTBOX_INDEX_CATCHUP_BYTES,
    _indexed_outbox_request,
)
from agent_sensorium.store import SensoriumStore, atomic_rewrite_jsonl

NOW = "2026-09-15T10:00:00Z"


def _store(tmp_path: Path, name: str = "state") -> SensoriumStore:
    store = SensoriumStore(instance="desktop-test", state_dir=str(tmp_path / name))
    store.ensure_dirs()
    return store


def _candidate(identifier: str, status: str = "archived", *, padding: int = 0) -> dict:
    return {
        "id": identifier,
        "status": status,
        "kind": "subconscious_advisory",
        "created_at": "2026-09-15T09:00:00Z",
        "updated_at": "2026-09-15T09:00:00Z",
        "padding": "x" * padding,
    }


def _prepared(identifier: str = "prepared", *, body: str = "current exact words") -> dict:
    return {
        "id": identifier,
        "status": "prepared",
        "created_at": "2026-09-15T09:00:00Z",
        "origin_thread_id": "",
        "origin_candidate_id": "candidate-current",
        "surface": "local",
        "delivery_mode": "context_pointer",
        "target": {},
        "allowed_surfaces": ["local"],
        "message_preview": body,
        "content_hash": hashlib.sha256(body.encode()).hexdigest()[:16],
        "content_length": len(body),
    }


def _finish(store: SensoriumStore, *, budget: int = 1024 * 1024) -> list:
    results = []
    for _ in range(100):
        result = store.prepare_desktop_projection_index(
            scan_bytes=budget, scan_records=2000
        )
        results.append(result)
        assert result.bytes_consumed <= budget
        assert result.records_consumed <= 2000
        if result.complete:
            return results
    raise AssertionError("Desktop projection did not become ready")


def _tree_identity(root: Path) -> dict[str, tuple[int, int, int]]:
    return {
        str(path.relative_to(root)): (path.stat().st_size, path.stat().st_mtime_ns, path.stat().st_ino)
        for path in root.rglob("*") if path.is_file()
    }


@pytest.mark.parametrize(
    ("expiry", "opened", "now", "expected"),
    [
        ("2026-09-15T10:01:00Z", "2026-09-15T07:00:00Z", NOW, 1),
        (NOW, "2026-09-15T07:00:00Z", NOW, 0),
        ("2026-09-15T09:59:59Z", "2026-09-15T07:00:00Z", NOW, 0),
        (None, "2026-09-15T07:01:00Z", NOW, 1),
        (None, "2026-09-15T07:00:00Z", NOW, 0),
        ("not-a-time", "2026-09-15T07:00:00Z", NOW, 0),
        ("not-a-time", "2026-09-15T07:01:00Z", NOW, 1),
    ],
)
def test_desktop_open_aperture_uses_canonical_expiry(
    tmp_path, expiry, opened, now, expected
):
    store = _store(tmp_path)
    aperture = {"id": "aperture", "state": "open", "opened_at": opened}
    if expiry is not None:
        aperture["lease_expires_at"] = expiry
    candidate = _candidate("owned", "in_conscious_aperture")
    candidate["conscious_aperture"] = aperture
    store.append_jsonl("candidates", candidate)
    before = _tree_identity(store.root)

    result = presentation.project_desktop_presentation(store.root, now=now)

    assert result["counts"]["open_apertures"] == expected
    assert result["latest"]["state"] == ("open" if expected else "unknown")
    assert _tree_identity(store.root) == before


def test_held_and_renewed_controls_remain_current(tmp_path):
    store = _store(tmp_path)
    held = _candidate("held", "held")
    held["conscious_aperture"] = {"id": "held-aperture", "state": "held"}
    renewed = _candidate("renewed", "in_conscious_aperture")
    renewed["conscious_aperture"] = {
        "id": "renewed-aperture", "state": "open",
        "opened_at": "2026-09-15T06:00:00Z",
        "lease_expires_at": "2026-09-15T10:05:00Z",
    }
    store.rewrite_jsonl("candidates", [held, renewed])
    result = presentation.project_desktop_presentation(store.root, now=NOW)
    assert result["counts"]["held_apertures"] == 1
    assert result["counts"]["open_apertures"] == 1


def test_large_candidate_history_matches_small_current_projection_and_is_bounded(tmp_path):
    small = _store(tmp_path, "small")
    large = _store(tmp_path, "large")
    active = _candidate("candidate-current", "candidate")
    active["conscious_task"] = {"id": "task"}
    small.append_jsonl("candidates", active)
    large.rewrite_jsonl(
        "candidates",
        [active] + [_candidate(f"archived-{i:05d}", padding=900) for i in range(10000)],
    )
    assert large.paths["candidates"].stat().st_size > presentation.MAX_CANDIDATES_BYTES
    passes = _finish(large)
    assert len(passes) > 1

    small_result = presentation.project_desktop_presentation(small.root, now=NOW)
    before = _tree_identity(large.root)
    large_result = presentation.project_desktop_presentation(large.root, now=NOW)
    after = _tree_identity(large.root)
    indexed = DesktopProjectionIndex(large).read()

    for key in ("counts", "posture", "headline_code", "detail_code", "latest"):
        assert large_result[key] == small_result[key]
    assert indexed.complete and indexed.rows is not None
    assert sum(len(rows) for rows in indexed.rows.values()) == 1
    assert indexed.sqlite_vm_steps < 100
    assert before == after


def test_append_and_atomic_rewrite_require_bounded_owner_catchup(tmp_path):
    store = _store(tmp_path)
    active = _candidate("candidate-current", "candidate")
    active["conscious_task"] = {"id": "task"}
    store.append_jsonl("candidates", active)
    _finish(store, budget=128)
    assert presentation.project_desktop_presentation(store.root, now=NOW)["counts"]["unresolved_candidates"] == 1

    store.append_jsonl("candidates", _candidate("candidate-current", "archived"))
    stale = presentation.project_desktop_presentation(store.root, now=NOW)
    assert stale["ok"] is False and stale["posture"] == "unavailable"
    _finish(store, budget=128)
    assert presentation.project_desktop_presentation(store.root, now=NOW)["counts"]["unresolved_candidates"] == 0

    rewritten = _candidate("candidate-current", "candidate")
    rewritten["conscious_task"] = {"id": "task-new"}
    atomic_rewrite_jsonl(store.paths["candidates"], [rewritten])
    assert presentation.project_desktop_presentation(store.root, now=NOW)["ok"] is False
    _finish(store, budget=128)
    assert presentation.project_desktop_presentation(store.root, now=NOW)["counts"]["unresolved_candidates"] == 1


def test_missing_corrupt_and_incomplete_projection_are_unavailable_and_nonmutating(tmp_path):
    missing = _store(tmp_path, "missing")
    missing.rewrite_jsonl(
        "candidates", [_candidate(f"archived-{i}", padding=900) for i in range(10000)]
    )
    before = _tree_identity(missing.root)
    assert presentation.project_desktop_presentation(missing.root, now=NOW)["ok"] is False
    assert _tree_identity(missing.root) == before

    incomplete = _store(tmp_path, "incomplete")
    incomplete.rewrite_jsonl(
        "candidates", [_candidate(f"archived-{i}", padding=900) for i in range(10000)]
    )
    first = incomplete.prepare_desktop_projection_index(scan_bytes=4096, scan_records=10)
    assert not first.complete
    before = _tree_identity(incomplete.root)
    assert presentation.project_desktop_presentation(incomplete.root, now=NOW)["ok"] is False
    assert _tree_identity(incomplete.root) == before

    corrupt = _store(tmp_path, "corrupt")
    corrupt.append_jsonl("candidates", _candidate("one", "candidate"))
    _finish(corrupt)
    (corrupt.root / "inner_life" / "desktop_projection.sqlite3").write_bytes(b"not sqlite")
    before = _tree_identity(corrupt.root)
    assert presentation.project_desktop_presentation(corrupt.root, now=NOW)["ok"] is False
    assert _tree_identity(corrupt.root) == before


def test_large_outbox_history_preserves_exact_current_artifact_without_private_output(tmp_path):
    store = _store(tmp_path)
    sentinel = "PRIVATE-BODY-MUST-NOT-LEAK"
    store.rewrite_jsonl(
        "outbox",
        [_prepared(body=sentinel)] + [
            {"id": f"done-{i:05d}", "status": "dispatched",
             "created_at": "2026-09-14T09:00:00Z", "padding": "x" * 900}
            for i in range(2000)
        ],
    )
    assert store.paths["outbox"].stat().st_size > presentation.MAX_OUTBOX_BYTES
    _finish(store)
    result = presentation.project_desktop_presentation(store.root, now=NOW)
    encoded = json.dumps(result, sort_keys=True)
    assert result["counts"]["verified_prepared_reachouts"] == 1
    assert result["latest"]["artifact"]["verified"] is True
    assert result["latest"]["artifact"]["content_length"] == len(sentinel)
    assert sentinel not in encoded
    for forbidden in ("message_preview", "summary", "source_candidate_ids", "owner_token", "hold_reason"):
        assert forbidden not in encoded


def test_outbox_index_enforces_bytes_records_partial_and_oversized_policy(tmp_path):
    oversized = _store(tmp_path, "oversized")
    oversized.append_jsonl("outbox", {
        "id": "large", "idempotency_key": "large-key",
        "message_preview": "x" * (2 * OUTBOX_INDEX_CATCHUP_BYTES),
    })
    metrics: dict[str, int] = {}
    with oversized.outbox_transaction():
        found, error = _indexed_outbox_request(
            oversized, "large-key", metrics=metrics
        )
    with sqlite3.connect(oversized.root / "inner_life" / "outbox_index.sqlite3") as conn:
        offset = conn.execute("SELECT offset FROM metadata WHERE singleton=1").fetchone()[0]
    assert found is None and error == "outbox_index_record_too_large"
    assert metrics["source_bytes"] <= OUTBOX_INDEX_CATCHUP_BYTES
    assert offset <= OUTBOX_INDEX_CATCHUP_BYTES

    partial = _store(tmp_path, "partial")
    rows = [
        {"id": f"row-{i}", "idempotency_key": f"key-{i}", "padding": "x" * 400}
        for i in range(4000)
    ]
    partial.rewrite_jsonl("outbox", rows)
    calls = []
    with partial.outbox_transaction():
        for _ in range(20):
            current: dict[str, int] = {}
            found, error = _indexed_outbox_request(
                partial, "key-0", scan_bytes=128 * 1024, metrics=current
            )
            calls.append(current)
            assert current["source_bytes"] <= 128 * 1024
            assert current["records_consumed"] <= 2000
            if error is None:
                break
            assert error == "outbox_index_catching_up"
    assert error is None and found is not None and found["id"] == "row-0"
    assert len(calls) > 1


def test_outbox_generation_reset_has_no_history_sized_delete(tmp_path):
    store = _store(tmp_path)
    store.rewrite_jsonl("outbox", [
        {"id": f"old-{i}", "idempotency_key": f"old-key-{i}"}
        for i in range(5000)
    ])
    with store.outbox_transaction():
        for _ in range(20):
            _, error = _indexed_outbox_request(store, "missing")
            if error is None:
                break
    assert error is None
    atomic_rewrite_jsonl(store.paths["outbox"], [
        {"id": "new", "idempotency_key": "new-key"}
    ])
    metrics: dict[str, int] = {}
    with store.outbox_transaction():
        found, error = _indexed_outbox_request(store, "new-key", metrics=metrics)
    assert error is None and found is not None
    assert metrics["generation"] == 2
    assert metrics["source_bytes"] < 1024
    assert metrics["sqlite_vm_steps"] < 1000


def _load_script(name: str):
    path = Path(__file__).parents[1] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"desktop_owner_{name}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ordinary_native_clock_invocation_publishes_projection(tmp_path, monkeypatch):
    store = _store(tmp_path)
    active = _candidate("candidate-current", "candidate")
    active["conscious_task"] = {"id": "task"}
    store.append_jsonl("candidates", active)
    module = _load_script("sensorium_native_clock")
    monkeypatch.setattr(
        module,
        "_source_material",
        lambda *args, **kwargs: {
            "selection": None,
            "events": [],
            "candidates": [],
            "candidate_source_count": 1,
            "projected_candidate_count": 0,
            "admission_state": "ready",
            "admission_reason": None,
            "admission_progress": {},
            "admission_work": {},
        },
    )
    args = module._parser().parse_args([
        "--instance", "desktop-test", "--state-dir", str(store.root),
        "--skip-sensors", "--json",
    ])
    result = module.run_once(args)
    assert result["success"] is True
    assert DesktopProjectionIndex(store).read().complete
    projected = presentation.project_desktop_presentation(store.root, now=NOW)
    assert projected["counts"]["unresolved_candidates"] == 1


def test_ordinary_native_conscious_invocation_publishes_projection(tmp_path):
    store = _store(tmp_path)
    store.append_jsonl("outbox", _prepared())
    module = _load_script("sensorium_native_conscious")
    args = module._parser().parse_args([
        "--instance", "desktop-test", "--state-dir", str(store.root), "--json"
    ])
    result = module.run_once(args, now=NOW)
    assert result["success"] is True
    index = DesktopProjectionIndex(store).read()
    assert index.complete and index.rows is not None
    assert len(index.rows["outbox"]) == 1
    projected = presentation.project_desktop_presentation(store.root, now=NOW)
    assert projected["counts"]["verified_prepared_reachouts"] == 1
