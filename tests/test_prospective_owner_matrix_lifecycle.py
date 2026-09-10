"""Exact detached-recorder non-influence matrix for lifecycle production owners.

Each row drives the actual Kanban, pointer, or Conscious owner through its
instance-config lookup.  The recorder is activated before every ON/failure run;
no handler return, canonical owner byte, candidate selection, or receipt order
may depend on successful detached observation.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_sensorium import conscious_aperture, pointers, prospective_evidence, settlement
from agent_sensorium.conscious_aperture import open_conscious_aperture, settle_conscious_aperture_item
from agent_sensorium.pointers import record_pointer_presented
from agent_sensorium.prospective_evidence import ProspectiveEvidenceCapture, STUDY_NAME
from agent_sensorium.settlement import apply_kanban_settlement
from agent_sensorium.store import SensoriumStore


FIXED_NOW = datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc)
FIXED_ISO = "2026-01-02T12:00:00Z"
CONTROL = {
    "enabled": True,
    "start_at": "2026-01-01T00:00:00Z",
    "expires_at": "2026-01-15T00:00:00Z",
}


def _canonical_files(root: Path) -> dict[str, bytes]:
    excluded = {"instance.config.json", ".prospective-evidence-capture-v0.consumed.json"}
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and path.name not in excluded
        and STUDY_NAME not in path.relative_to(root).parts
    }


def _write_config(root: Path, control: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "instance.config.json").write_text(
        json.dumps({"prospective_evidence_capture": control}, sort_keys=True), encoding="utf-8"
    )


def _candidate(candidate_id: str, *, status: str = "candidate") -> dict:
    return {
        "id": candidate_id,
        "status": status,
        "kind": "subconscious_advisory",
        "pressure": 0.8,
        "summary": f"deterministic lifecycle candidate {candidate_id}",
        "event_ids": [],
        "source_candidate_ids": [],
        "correlation_keys": ["matrix-lifecycle"],
        "sensitivity": "private",
        "allowed_surfaces": ["local"],
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "conscious_task": {
            "id": f"ctask-{candidate_id}", "request_type": "THINK", "title": "Matrix task",
            "why": "bounded lifecycle fixture", "expected_decision": "decide",
        },
    }


def _store(root: Path) -> SensoriumStore:
    store = SensoriumStore(instance="matrix", state_dir=str(root))
    store.ensure_dirs()
    return store


def _invoke(operation: str, root: Path) -> tuple[dict, list[str]]:
    store = _store(root)
    if operation == "kanban_settlement":
        store.append_jsonl("candidates", _candidate("cand-kanban"))
        return apply_kanban_settlement(
            store, decision="SAVE", candidate_id="cand-kanban", intake_task_id="intake-matrix",
            review_task_id="review-matrix", reason="deterministic real settlement",
        ), ["settled"]
    if operation == "pointer_presented":
        candidate = _candidate("cand-pointer")
        store.append_jsonl("candidates", candidate)
        return record_pointer_presented(store, {
            "pointer_type": "candidate", "candidate_id": candidate["id"],
            "title": candidate["summary"], "surface": "local",
        }, session_id="matrix-session", surface="local", foreground_turn_index=1), ["presented"]
    if operation in {"conscious_open", "conscious_settled", "conscious_held"}:
        candidate_id = {"conscious_open": "cand-open", "conscious_settled": "cand-settled", "conscious_held": "cand-held"}[operation]
        store.append_jsonl("candidates", _candidate(candidate_id))
        opened = open_conscious_aperture(store, aperture_size=1, dry_run=False, now=FIXED_ISO)
        assert opened["candidate_ids"] == [candidate_id]
        if operation == "conscious_open":
            return opened, ["opened", "attention_snapshot"]
        decision = "SETTLED" if operation == "conscious_settled" else "HELD"
        item = opened["aperture"][0]
        result = settle_conscious_aperture_item(
            store, candidate_id=candidate_id, aperture_id=item["aperture_id"],
            consumer_id=item["consumer_id"], decision=decision,
            reason="deterministic actual Conscious decision", dry_run=False, now="2026-01-02T12:05:00Z",
            return_at="2026-01-02T13:00:00Z" if decision == "HELD" else None,
        )
        assert result["receipt"]["type"] == "conscious.aperture.settled"
        return result, ["opened", "attention_snapshot", "chosen", "settled"]
    raise AssertionError(f"unknown operation: {operation}")


def _run_mode(operation: str, mode: str, root: Path, monkeypatch) -> tuple[dict, dict[str, bytes], list[str], list[dict]]:
    _write_config(root, CONTROL if mode != "off" else {"enabled": False})
    capture = ProspectiveEvidenceCapture(root, CONTROL)
    if mode != "off":
        assert capture.activate() is True

    with monkeypatch.context() as patch:
        sequence = {"value": 0}

        def deterministic_id(prefix: str) -> str:
            sequence["value"] += 1
            return f"{prefix}_{sequence['value']:012d}"

        # Patch each lifecycle owner's imported time/id source before any owner
        # mutation; comparisons are direct, with no normalization afterwards.
        patch.setattr(conscious_aperture, "new_id", deterministic_id)
        patch.setattr(conscious_aperture, "utc_now_iso", lambda: FIXED_ISO)
        patch.setattr(pointers, "utc_now_iso", lambda: FIXED_ISO)
        patch.setattr(settlement, "utc_now_iso", lambda: FIXED_ISO)
        patch.setattr(prospective_evidence, "_now", lambda value=None: FIXED_NOW)
        if mode == "exception":
            patch.setattr(ProspectiveEvidenceCapture, "observe", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("matrix recorder failure")))
        elif mode == "capacity":
            patch.setattr(prospective_evidence, "MAX_ROWS", 0)
        elif mode == "sqlite_failure":
            patch.setattr(ProspectiveEvidenceCapture, "_connect", lambda self: (_ for _ in ()).throw(OSError("matrix sqlite failure")))
        result, stages = _invoke(operation, root)

    rows = capture._rows() if mode != "sqlite_failure" else []
    return result, _canonical_files(root), stages, rows


@pytest.mark.parametrize("operation", [
    "kanban_settlement", "pointer_presented", "conscious_open", "conscious_settled", "conscious_held",
])
def test_lifecycle_owner_matrix_exact_off_on_and_failure_equality(tmp_path, monkeypatch, operation):
    results = {
        mode: _run_mode(operation, mode, tmp_path / mode, monkeypatch)
        for mode in ("off", "on", "exception", "capacity", "sqlite_failure")
    }
    off_return, off_files, expected_stages, off_rows = results["off"]
    assert off_rows == []
    for mode, (result, files, _stages, rows) in results.items():
        assert result == off_return, f"{operation}/{mode}: detached recorder changed exact owner return"
        assert files == off_files, f"{operation}/{mode}: detached recorder changed canonical owner bytes"
        if mode == "on":
            assert [row["stage"] for row in rows] == expected_stages
            assert all(row["material"] != "yes" for row in rows), f"{operation}: lifecycle hook manufactured material=yes"
        elif mode != "off":
            assert rows == [], f"{operation}/{mode}: failed/refused recorder persisted a detached row"


def test_pointer_blocked_path_never_records_presented_stage(tmp_path, monkeypatch):
    """A false doorway may emit its guard receipt, but never detached presented."""
    root = tmp_path / "blocked"
    _write_config(root, CONTROL)
    capture = ProspectiveEvidenceCapture(root, CONTROL)
    assert capture.activate() is True
    store = _store(root)
    store.append_jsonl("candidates", _candidate("cand-blocked"))
    with monkeypatch.context() as patch:
        patch.setattr(pointers, "utc_now_iso", lambda: FIXED_ISO)
        patch.setattr(prospective_evidence, "_now", lambda value=None: FIXED_NOW)
        receipt = record_pointer_presented(store, {
            "pointer_type": "candidate", "candidate_id": "cand-blocked", "title": "mismatched doorway",
        })
    assert receipt["type"] == "pointer.presented.guard"
    assert capture._rows() == []
