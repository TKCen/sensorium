"""Exact detached-recorder non-influence matrix for production tool owners.

Each row drives the real handler through its configured instance-config lookup.
The detached study is deliberately activated before every ON/failure run; no
canonical state or response is allowed to depend on whether it records.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_sensorium import gate, prospective_evidence, sensors, tools
from agent_sensorium.prospective_evidence import ProspectiveEvidenceCapture, STUDY_NAME
from agent_sensorium.store import SensoriumStore


FIXED_NOW = datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc)
CONTROL = {
    "enabled": True,
    "start_at": "2026-01-01T00:00:00Z",
    "expires_at": "2026-01-15T00:00:00Z",
}


def _canonical_files(root: Path) -> dict[str, bytes]:
    """All owner files, excluding only the configured detached-study boundary."""
    excluded = {
        "instance.config.json",
        ".prospective-evidence-capture-v0.consumed.json",
    }
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
        json.dumps({"prospective_evidence_capture": control}, sort_keys=True),
        encoding="utf-8",
    )


def _signal(
    signal_id: str, *, kind: str = "design_decision", strength: float = 0.91, summary: str = "deterministic production owner fixture"
) -> dict:
    return {
        "id": signal_id,
        "ts": "2026-01-02T11:00:00Z",
        "sensor": "matrix",
        "source": "manual",
        "kind": kind,
        "summary": summary,
        "strength_hint": strength,
        "correlation_keys": ["matrix-correlation"],
    }


def _event(event_id: str, *, correlation: str = "matrix-trusted") -> dict:
    return {
        "id": event_id,
        "ts": "2026-01-02T11:00:00Z",
        "type": "sensor.event.promoted",
        "kind": "task_result",
        "summary": "deterministic trusted event fixture",
        "source_signal_ids": [f"sig-{event_id}"],
        "signal_count": 1,
        "strength": 0.91,
        "correlation_keys": [correlation],
        "sensitivity": "private",
        "allowed_surfaces": ["local"],
    }


def _invoke(operation: str, root: Path) -> tuple[str, list[str]]:
    """Run one real production operation and return expected detached stages."""
    state_dir = str(root)
    if operation == "accepted_nonpromoted":
        return tools.handle_sensorium_ingest_signal(
            signal=_signal("sig-nonpromoted", kind="note", strength=0.2), instance="matrix", state_dir=state_dir
        ), ["source_observed"]
    if operation == "promoted_create":
        return tools.handle_sensorium_ingest_signal(
            signal=_signal("sig-promoted-create"), instance="matrix", state_dir=state_dir
        ), ["source_observed", "candidate_updated"]
    if operation == "promoted_coalesce":
        first = json.loads(tools.handle_sensorium_ingest_signal(
            signal=_signal("sig-coalesce-a"), instance="matrix", state_dir=state_dir
        ))
        second = tools.handle_sensorium_ingest_signal(
            signal=_signal("sig-coalesce-b", summary="deterministic production owner fixture second source"), instance="matrix", state_dir=state_dir
        )
        second_data = json.loads(second)["data"]
        # This proves the production coalescer selected the same candidate, not
        # merely that each mode happened to create an equal-shaped record.
        assert second_data["coalesced"] is True
        assert second_data["candidate_id"] == first["data"]["candidate_id"]
        assert len(SensoriumStore(instance="matrix", state_dir=state_dir).read_jsonl("candidates")) == 1
        return second, ["source_observed", "candidate_updated", "source_observed", "candidate_updated"]
    if operation == "trusted_event_create_update":
        first = json.loads(tools.handle_sensorium_ingest_event(
            event=_event("evt-trusted-a"), instance="matrix", state_dir=state_dir
        ))
        second = tools.handle_sensorium_ingest_event(
            event=_event("evt-trusted-b"), instance="matrix", state_dir=state_dir
        )
        second_data = json.loads(second)["data"]
        assert second_data["coalesced"] is True
        assert second_data["candidate_id"] == first["data"]["candidate_id"]
        return second, ["candidate_updated", "candidate_updated"]
    if operation == "candidate_update":
        created = json.loads(tools.handle_sensorium_ingest_signal(
            signal=_signal("sig-candidate-update"), instance="matrix", state_dir=state_dir
        ))
        return tools.handle_sensorium_candidate_update(
            candidate_id=created["data"]["candidate_id"], action="hold", reason="bounded test action",
            instance="matrix", state_dir=state_dir,
        ), ["source_observed", "candidate_updated", "candidate_updated"]
    if operation == "exact_user_correction":
        return tools.handle_sensorium_ingest_signal(
            signal=_signal("sig-user-correction", kind="user_correction", strength=0.2),
            instance="matrix", state_dir=state_dir,
        ), ["source_observed", "correction"]
    if operation == "exact_retraction":
        return tools.handle_sensorium_ingest_signal(
            signal=_signal("sig-retraction", kind="retraction", strength=0.2),
            instance="matrix", state_dir=state_dir,
        ), ["source_observed", "retraction"]
    raise AssertionError(f"unknown operation: {operation}")


def _run_mode(operation: str, mode: str, root: Path, monkeypatch) -> tuple[str, dict[str, bytes], list[str], int]:
    control = CONTROL if mode != "off" else {"enabled": False}
    _write_config(root, control)
    capture = ProspectiveEvidenceCapture(root, CONTROL)
    if mode != "off":
        assert capture.activate() is True

    # These are the handlers' actual imported time sources.  The recorder's
    # clock is patched before observation, never normalized after the fact.
    with monkeypatch.context() as patch:
        sequence = {"value": 0}

        def deterministic_id(prefix: str) -> str:
            sequence["value"] += 1
            return f"{prefix}_{sequence['value']:012d}"

        # `gate` imported these helpers at its production import site; patching
        # them makes generated event/candidate IDs and pitch timestamps exact.
        patch.setattr(gate, "new_id", deterministic_id)
        patch.setattr(gate, "utc_now_iso", lambda: "2026-01-02T12:00:00Z")
        patch.setattr(gate, "_utcnow", lambda: FIXED_NOW)
        patch.setattr(sensors, "_utc_now", lambda: FIXED_NOW)
        patch.setattr(tools, "utc_now_iso", lambda: "2026-01-02T12:00:00Z")
        patch.setattr(prospective_evidence, "_now", lambda value=None: FIXED_NOW)
        if mode == "exception":
            patch.setattr(ProspectiveEvidenceCapture, "observe", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("matrix recorder failure")))
        elif mode == "capacity":
            # A real recorder capacity refusal, after successful activation.
            patch.setattr(prospective_evidence, "MAX_ROWS", 0)
        elif mode == "sqlite_failure":
            # Activation used the real connection. Only observation loses SQLite.
            patch.setattr(ProspectiveEvidenceCapture, "_connect", lambda self: (_ for _ in ()).throw(OSError("matrix sqlite failure")))
        result, stages = _invoke(operation, root)
        assert prospective_evidence._wait_for_observation_queue(10)

    rows = capture._rows()
    return result, _canonical_files(root), stages, len(rows)


@pytest.mark.parametrize(
    "operation",
    [
        "accepted_nonpromoted",
        "promoted_create",
        "promoted_coalesce",
        "trusted_event_create_update",
        "candidate_update",
        "exact_user_correction",
        "exact_retraction",
    ],
)
def test_tool_owner_matrix_exact_off_on_and_failure_equality(tmp_path, monkeypatch, operation):
    results = {
        mode: _run_mode(operation, mode, tmp_path / mode, monkeypatch)
        for mode in ("off", "on", "exception", "capacity", "sqlite_failure")
    }
    off_return, off_files, expected_stages, off_rows = results["off"]
    assert off_rows == 0
    for mode, (result, files, _stages, row_count) in results.items():
        assert result == off_return, f"{operation}/{mode}: detached recorder changed handler return"
        assert files == off_files, f"{operation}/{mode}: detached recorder changed canonical owner bytes"
        if mode == "on":
            capture = ProspectiveEvidenceCapture(tmp_path / mode, CONTROL)
            assert [row["stage"] for row in capture._rows()] == expected_stages
            assert row_count == len(expected_stages)
        elif mode != "off":
            assert row_count == 0, f"{operation}/{mode}: failed/refused recorder influenced detached count"
