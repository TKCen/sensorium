"""Retry denial must not reset producer-owned current-state catch-up."""
from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import subprocess
from pathlib import Path

from agent_sensorium.desktop_presentation import project_desktop_presentation
from agent_sensorium.store import SensoriumStore

ROOT = Path(__file__).resolve().parents[1]


def _runner():
    spec = importlib.util.spec_from_file_location(
        "retry_before_claim_runner", ROOT / "scripts" / "sensorium_native_conscious.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    terminalize = module.terminalize_attempt

    def finish_at_synthetic_clock(state, **kwargs):
        # The synthetic provider returns instantly; keep completion on the same
        # injected clock instead of inheriting the real wall clock's cooldown.
        return terminalize(state, **{**kwargs, "now": state["active_attempt"]["started_at"]})

    module.terminalize_attempt = finish_at_synthetic_clock
    return module


def _fixture(tmp_path):
    store = SensoriumStore(instance="retry-order", state_dir=str(tmp_path / "state"))
    store.ensure_dirs()
    current = {
        "id": "advisory-current", "status": "candidate", "kind": "subconscious_advisory",
        "pressure": 0.9, "summary": "Synthetic current advisory.",
        "event_ids": ["event-current"], "source_candidate_ids": ["source-current"],
        "source_candidate_fingerprint": "generation-churn-v1", "sensitivity": "private",
        "allowed_surfaces": ["local"], "created_at": "2026-09-15T10:00:00Z",
        "updated_at": "2026-09-15T10:00:00Z",
        "conscious_task": {"id": "task-current", "request_type": "THINK",
                           "title": "Synthetic choice", "why": "Exercise producer catch-up.",
                           "expected_decision": "Remain bounded."},
    }
    archived = [
        {"id": f"archived-{i:05d}", "status": "archived", "kind": "subconscious_advisory",
         "created_at": "2026-09-14T00:00:00Z", "updated_at": "2026-09-14T00:00:00Z",
         "padding": "x" * 900}
        for i in range(2600)
    ]
    store.rewrite_jsonl("candidates", [current, *archived])
    args = argparse.Namespace(
        instance=store.instance, state_dir=str(store.root), plugin_root=str(ROOT),
        hermes_cli="/synthetic/hermes", provider="fixture", model="fixture",
        timeout_seconds=30, total_timeout_seconds=60, cleanup_reserve_seconds=5,
        failure_cooldown_seconds=1800, stale_after_minutes=180, force=False,
        emit_reachout=False, print_json=False,
    )
    return store, args


def _cursor(store):
    with sqlite3.connect(store.root / "inner_life" / "desktop_projection.sqlite3") as conn:
        row = conn.execute(
            "SELECT generation,offset,eof_size,complete FROM source_cursor WHERE stream='candidates'"
        ).fetchone()
    assert row is not None
    return row


def _identity(path):
    stat = path.stat()
    return stat.st_ino, stat.st_mtime_ns, path.read_bytes()


def test_exhausted_native_ticks_preserve_source_and_finish_projection(tmp_path):
    runner = _runner()
    store, args = _fixture(tmp_path)
    calls = []

    def failed_model(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 7, stdout="", stderr="synthetic provider unavailable")

    for timestamp in ("2026-09-15T11:00:00Z", "2026-09-15T12:00:00Z", "2026-09-15T15:00:00Z"):
        result = runner.run_once(args, run_command=failed_model, now=timestamp, clock=lambda: timestamp)
        assert result["action"] == "conscious_session_failed"
    assert len(calls) == 3
    before = _identity(store.paths["candidates"])
    receipt_before = store.paths["decisions"].read_bytes() if store.paths["decisions"].exists() else b""
    cursors = [_cursor(store)]
    assert cursors[0][2] > 2 * 1024 * 1024
    for timestamp in ("2026-09-15T16:00:00Z", "2026-09-15T17:00:00Z", "2026-09-15T18:00:00Z"):
        result = runner.run_once(args, run_command=failed_model, now=timestamp, clock=lambda: timestamp)
        assert result["action"] == "skipped_retry_exhausted"
        assert _identity(store.paths["candidates"]) == before
        assert store.paths["decisions"].read_bytes() == receipt_before
        cursors.append(_cursor(store))
    assert len(calls) == 3
    assert len({row[0] for row in cursors}) == 1
    assert all(a[1] <= b[1] for a, b in zip(cursors, cursors[1:]))
    assert cursors[-1][3] == 1
    current = [row for row in store.read_jsonl("candidates") if row["status"] != "archived"]
    assert len(current) == 1 and current[0]["id"] == "advisory-current"
    assert current[0]["status"] == "in_conscious_aperture"
    assert not current[0].get("conscious_settlements")
    tree_before = {str(p): (p.stat().st_mtime_ns, p.read_bytes()) for p in store.root.rglob("*") if p.is_file()}
    projection = project_desktop_presentation(store.root, now="2026-09-15T18:00:00Z")
    assert projection["ok"] is True
    assert projection["counts"]["open_apertures"] == 0
    assert projection["posture"] != "settled"
    assert {str(p): (p.stat().st_mtime_ns, p.read_bytes()) for p in store.root.rglob("*") if p.is_file()} == tree_before


def test_cooldown_native_tick_does_not_renew_same_owner(tmp_path):
    runner = _runner()
    store, args = _fixture(tmp_path)

    def failed_model(command, **kwargs):
        return subprocess.CompletedProcess(command, 7, stdout="", stderr="synthetic provider unavailable")

    result = runner.run_once(args, run_command=failed_model, now="2026-09-15T11:00:00Z")
    assert result["action"] == "conscious_session_failed"
    before = _identity(store.paths["candidates"])

    def forbidden_model(*args, **kwargs):
        raise AssertionError("retry cooldown must not invoke the model")

    result = runner.run_once(args, run_command=forbidden_model, now="2026-09-15T11:01:00Z")
    assert result["action"] == "skipped_retry_cooldown"
    assert _identity(store.paths["candidates"]) == before
    state = json.loads((store.root / "conscious_clock_state.json").read_text())
    assert state["retry_state"]["last_ordinal"] == 1
