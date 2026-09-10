"""Regressions for the P6 prospective-study review findings."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone

from agent_sensorium import prospective_evidence
from agent_sensorium.conscious_aperture import open_conscious_aperture
from agent_sensorium.prospective_evidence import ProspectiveEvidenceCapture, observe_after_success
from agent_sensorium.store import SensoriumStore


FIXED_NOW = datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc)
CONTROL = {
    "enabled": True,
    "start_at": "2026-01-01T00:00:00Z",
    "expires_at": "2026-01-15T00:00:00Z",
}


def _active_root(tmp_path):
    root = tmp_path / "state"
    root.mkdir()
    capture = ProspectiveEvidenceCapture(root, CONTROL)
    assert capture.activate()
    return root, capture


def _candidate(candidate_id: str) -> dict:
    return {
        "id": candidate_id,
        "status": "candidate",
        "kind": "subconscious_advisory",
        "pressure": 0.8,
        "summary": "synthetic prospective lock fixture",
        "event_ids": [],
        "source_candidate_ids": [],
        "correlation_keys": [],
        "sensitivity": "private",
        "allowed_surfaces": ["local"],
        "created_at": "2026-01-02T11:00:00Z",
        "updated_at": "2026-01-02T11:00:00Z",
        "conscious_task": {
            "id": f"ctask-{candidate_id}",
            "request_type": "THINK",
            "title": "Synthetic task",
            "why": "exercise the foreground owner seam",
            "expected_decision": "decide",
        },
    }


def test_exclusive_sqlite_lock_cannot_stall_observer_or_foreground_aperture(
    tmp_path, monkeypatch
):
    root, capture = _active_root(tmp_path)
    (root / "instance.config.json").write_text(
        '{"prospective_evidence_capture":'
        '{"enabled":true,"expires_at":"2026-01-15T00:00:00Z",'
        '"start_at":"2026-01-01T00:00:00Z"}}'
    )
    store = SensoriumStore(instance="fixture", state_dir=str(root))
    store.ensure_dirs()
    store.append_jsonl("candidates", _candidate("cand-lock"))
    monkeypatch.setattr(prospective_evidence, "_now", lambda value=None: FIXED_NOW)

    blocker = sqlite3.connect(capture.db_path, timeout=1, isolation_level=None)
    blocker.execute("BEGIN EXCLUSIVE")
    calls: list[object] = []
    mutable_config = dict(CONTROL)
    mutable_classes = ["external"]
    mutable_evidence = {
        "source_receipt": "snapshot-source",
        "source_class": "manual",
        "attention_classes": mutable_classes,
    }

    observer = threading.Thread(
        target=lambda: calls.append(
            observe_after_success(
                root,
                mutable_config,
                "source_observed",
                mutable_evidence,
            )
        )
    )
    observer.start()
    observer.join(0.5)
    observer_returned_while_locked = not observer.is_alive()
    mutable_config["enabled"] = False
    mutable_evidence["source_class"] = "feedback"
    mutable_classes.append("relational")

    aperture = threading.Thread(
        target=lambda: calls.append(
            open_conscious_aperture(
                store, aperture_size=1, dry_run=False, now="2026-01-02T12:00:00Z"
            )
        )
    )
    aperture.start()
    aperture.join(0.5)
    aperture_returned_while_locked = not aperture.is_alive()

    blocker.rollback()
    blocker.close()
    observer.join(5)
    aperture.join(5)

    assert observer_returned_while_locked
    assert aperture_returned_while_locked
    assert prospective_evidence._wait_for_observation_queue(10)
    rows = capture._rows()
    assert [row["stage"] for row in rows] == [
        "source_observed",
        "opened",
        "attention_snapshot",
    ]
    # Caller-owned mutable inputs are snapshotted before the worker sees them.
    assert rows[0]["source_class"] == "manual"
    assert rows[0]["attention_classes"] == ["external"]
    assert calls[0] is None
    assert isinstance(calls[1], dict)
    assert calls[1]["action"] == "opened_aperture"


def test_observer_queue_is_bounded_drops_on_full_and_writer_survives_errors(
    tmp_path, monkeypatch
):
    assert prospective_evidence._wait_for_observation_queue(10)
    root, capture = _active_root(tmp_path)
    monkeypatch.setattr(prospective_evidence, "_now", lambda value=None: FIXED_NOW)

    original = ProspectiveEvidenceCapture.observe
    release = threading.Event()
    first_started = threading.Event()

    def blocked_then_real(self, stage, evidence, **kwargs):
        if evidence.get("source_receipt") == "block-writer":
            first_started.set()
            release.wait(5)
        if evidence.get("source_receipt") == "writer-error":
            raise OSError("synthetic writer failure")
        return original(self, stage, evidence, **kwargs)

    monkeypatch.setattr(ProspectiveEvidenceCapture, "observe", blocked_then_real)
    observe_after_success(root, CONTROL, "source_observed", {"source_receipt": "block-writer"})
    assert first_started.wait(2)

    for index in range(prospective_evidence.OBSERVATION_QUEUE_SIZE):
        observe_after_success(
            root, CONTROL, "source_observed", {"source_receipt": f"queued-{index}"}
        )
    assert prospective_evidence._OBSERVATION_QUEUE.full()
    observe_after_success(root, CONTROL, "source_observed", {"source_receipt": "dropped-full"})

    release.set()
    assert prospective_evidence._wait_for_observation_queue(15)
    assert len(capture._rows()) == prospective_evidence.OBSERVATION_QUEUE_SIZE + 1

    observe_after_success(root, CONTROL, "source_observed", {"source_receipt": "writer-error"})
    observe_after_success(root, CONTROL, "source_observed", {"source_receipt": "after-error"})
    assert prospective_evidence._wait_for_observation_queue(10)
    assert len(capture._rows()) == prospective_evidence.OBSERVATION_QUEUE_SIZE + 2
    writers = [
        thread
        for thread in threading.enumerate()
        if thread.name == "sensorium-prospective-evidence-writer" and thread.is_alive()
    ]
    assert len(writers) == 1 and writers[0].daemon


def test_case_reduction_retains_later_closed_settlement_and_allowed_vocabulary():
    base = {
        "case_ref": "case_" + "1" * 24,
        "settlement": "unknown",
        "attention_classes": ["unknown"],
    }
    for settlement in ("chosen", "settled", "held", "dropped"):
        cases = ProspectiveEvidenceCapture._cases(
            [base, {**base, "settlement": settlement}, {**base, "settlement": "unknown"}]
        )
        assert cases[0]["settlement"] == settlement
    assert ProspectiveEvidenceCapture._cases([base, {**base, "settlement": "not-closed"}])[0][
        "settlement"
    ] == "unknown"
