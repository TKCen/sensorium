from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

import agent_sensorium.conscious_consumer as consumer
import agent_sensorium.desktop_presentation as presentation
from agent_sensorium.conscious_consumer import consume_conscious_advisory
from agent_sensorium.outbox import prepare_local_outbox_request, source_revision_key
from agent_sensorium.store import SensoriumStore

NOW = "2026-09-10T10:00:00Z"


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _foreign_row(relative: str) -> dict:
    message = "Foreign authored words must never cross the selected root."
    if relative == "candidates.jsonl":
        return {
            "id": "foreign_candidate",
            "status": "candidate",
            "kind": "subconscious_advisory",
            "updated_at": NOW,
            "conscious_task": {},
        }
    if relative == "outbox.jsonl":
        return {
            "id": "foreign_outbox",
            "status": "prepared",
            "created_at": NOW,
            "origin_thread_id": "",
            "origin_candidate_id": "foreign_candidate",
            "surface": "local",
            "delivery_mode": "context_pointer",
            "target": {},
            "allowed_surfaces": ["local"],
            "message_preview": message,
            "content_hash": hashlib.sha256(message.encode()).hexdigest()[:16],
            "content_length": len(message),
        }
    if relative.endswith("clock.json"):
        return {"ts": NOW, "action": "foreign_clock"}
    return {"ts": NOW, "sensor": "foreign_sensor"}


@pytest.mark.parametrize(
    ("relative", "intermediate"),
    [
        ("candidates.jsonl", False),
        ("outbox.jsonl", False),
        ("last_native_clock.json", False),
        ("last_conscious_clock.json", False),
        ("signals/inbox.jsonl", True),
    ],
)
def test_presentation_rejects_leaf_and_intermediate_symlink_escapes(
    tmp_path, monkeypatch, relative, intermediate
):
    root = tmp_path / "state" / "demo"
    outside = tmp_path / "outside"
    root.mkdir(parents=True)
    outside.mkdir()
    _write_jsonl(root / "decisions.jsonl", [])
    outside_leaf = outside / Path(relative).name
    _write_jsonl(outside_leaf, [_foreign_row(relative)])
    if intermediate:
        (root / "signals").symlink_to(outside, target_is_directory=True)
    else:
        (root / relative).symlink_to(outside_leaf)

    forbidden = (outside_leaf.stat().st_dev, outside_leaf.stat().st_ino)
    original_fdopen = os.fdopen
    outside_reads: list[int] = []

    def guarded_fdopen(fd, *args, **kwargs):
        metadata = os.fstat(fd)
        if (metadata.st_dev, metadata.st_ino) == forbidden:
            outside_reads.append(fd)
            raise AssertionError("outside-root descriptor read")
        return original_fdopen(fd, *args, **kwargs)

    monkeypatch.setattr(presentation.os, "fdopen", guarded_fdopen)
    result = presentation.project_desktop_presentation(
        root, instance="demo", profile="default", now=NOW
    )

    assert result["ok"] is False
    assert result["posture"] == "unavailable"
    assert result["counts"] == {
        "unresolved_candidates": 0,
        "open_apertures": 0,
        "held_apertures": 0,
        "verified_prepared_reachouts": 0,
        "blocked_items": 0,
    }
    assert result["latest"]["opaque_ref"] is None
    assert "foreign" not in json.dumps(result).lower()
    assert outside_reads == []


def test_presentation_regular_file_control_still_projects_owned_state(tmp_path):
    root = tmp_path / "state" / "demo"
    _write_jsonl(root / "candidates.jsonl", [_foreign_row("candidates.jsonl") | {"id": "owned"}])

    result = presentation.project_desktop_presentation(
        root, instance="demo", profile="default", now=NOW
    )

    assert result["ok"] is True
    assert result["posture"] == "awaiting_review"
    assert result["counts"]["unresolved_candidates"] == 1
    assert result["latest"]["opaque_ref"].startswith("candidate#")


def _store(tmp_path) -> SensoriumStore:
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "sensorium"))
    store.ensure_dirs()
    return store


def _advisory() -> dict:
    return {
        "id": "advisory_1",
        "status": "candidate",
        "kind": "subconscious_advisory",
        "pressure": 0.9,
        "summary": "A bounded source deserves one conscious choice.",
        "event_ids": ["event_1"],
        "source_candidate_ids": ["source_1"],
        "source_candidate_fingerprint": "source-revision-1",
        "sensitivity": "private",
        "allowed_surfaces": ["local"],
        "created_at": "2026-08-26T10:00:00Z",
        "updated_at": "2026-08-26T10:00:00Z",
        "conscious_task": {
            "id": "ctask_1",
            "request_type": "THINK",
            "title": "Choose whether to reach out",
            "why": "The source remains meaningfully unresolved.",
            "expected_decision": "Silence, hold, or author one local message.",
        },
    }


def _prepare(store: SensoriumStore, message: str) -> dict:
    return prepare_local_outbox_request(
        store,
        origin_candidate_id="advisory_1",
        request_type="REACH_OUT",
        surface="local",
        delivery_mode="context_pointer",
        target={},
        title="Conscious reach-out",
        message_preview=message,
        content_hash=hashlib.sha256(message.encode()).hexdigest()[:16],
        source_candidate_ids=["source_1"],
        source_candidate_fingerprint="source-revision-1",
        dry_run=False,
    )


def test_local_idempotency_is_bound_to_source_revision_and_authored_body(tmp_path):
    store = _store(tmp_path)
    first_body = "The blue lantern by the window made me think of you."
    second_body = "The blue lantern returned differently, so I wrote newer words."

    first = _prepare(store, first_body)
    same = _prepare(store, first_body)
    different = _prepare(store, second_body)
    rows = store.read_jsonl("outbox")

    assert first["success"] is same["success"] is different["success"] is True
    assert same["idempotent_hit"] is True
    assert same["data"]["id"] == first["data"]["id"]
    assert different["data"]["id"] != first["data"]["id"]
    assert [row["message_preview"] for row in rows] == [first_body, second_body]
    assert len({row["idempotency_key"] for row in rows}) == 2
    assert all(
        row["content_hash"] == hashlib.sha256(row["message_preview"].encode()).hexdigest()[:16]
        for row in rows
    )


def test_full_prepare_retry_does_not_substitute_old_body_after_settlement_failure(
    tmp_path, monkeypatch
):
    store = _store(tmp_path)
    store.append_jsonl("candidates", _advisory())
    original_settle = consumer._settle
    failures = 0

    def fail_first_final_settlement(*args, **kwargs):
        nonlocal failures
        if kwargs.get("decision") == "SETTLED" and failures == 0:
            failures += 1
            return {"success": False, "error": "injected_settlement_failure"}
        return original_settle(*args, **kwargs)

    monkeypatch.setattr(consumer, "_settle", fail_first_final_settlement)
    first_body = "The blue lantern by the window made me think of you."
    second_body = "The blue lantern returned differently, so I wrote newer words."
    first = consume_conscious_advisory(
        store,
        decision={"decision": "REACH_OUT", "reason": "Use exact first wording.", "message": first_body},
        dry_run=False,
        now="2026-08-26T11:00:00Z",
    )
    second = consume_conscious_advisory(
        store,
        decision={"decision": "REACH_OUT", "reason": "Use exact newer wording.", "message": second_body},
        dry_run=False,
        now="2026-08-26T11:01:00Z",
    )
    rows = store.read_jsonl("outbox")

    assert first["action"] == "prepared_reach_out_unsettled"
    assert second["success"] is True
    assert second["action"] == "prepared_reach_out"
    assert len(rows) == 2
    selected = next(row for row in rows if row["id"] == second["outbox_id"])
    assert selected["message_preview"] == second_body
    assert selected["content_hash"] == second["message_hash"]
    assert selected["source_candidate_ids"] == ["source_1"]
    assert selected["source_candidate_fingerprint"] == "source-revision-1"
    assert store.read_jsonl("candidates")[0]["status"] == "reviewed"
    assert failures == 1


def test_full_prepare_retry_with_same_body_reuses_exact_outbox(tmp_path, monkeypatch):
    store = _store(tmp_path)
    store.append_jsonl("candidates", _advisory())
    original_settle = consumer._settle
    failed = False

    def fail_once(*args, **kwargs):
        nonlocal failed
        if kwargs.get("decision") == "SETTLED" and not failed:
            failed = True
            return {"success": False, "error": "injected_settlement_failure"}
        return original_settle(*args, **kwargs)

    monkeypatch.setattr(consumer, "_settle", fail_once)
    message = "The blue lantern by the window made me think of you."
    decision = {
        "decision": "REACH_OUT",
        "reason": "Keep the exact authored wording.",
        "message": message,
    }
    first = consume_conscious_advisory(
        store, decision=decision, dry_run=False, now="2026-08-26T11:00:00Z"
    )
    second = consume_conscious_advisory(
        store, decision=decision, dry_run=False, now="2026-08-26T11:01:00Z"
    )

    assert first["action"] == "prepared_reach_out_unsettled"
    assert second["action"] == "prepared_reach_out"
    assert second["outbox_id"] == first["outbox_id"]
    assert len(store.read_jsonl("outbox")) == 1


@pytest.mark.parametrize("tamper", ["body", "source_identity"])
def test_consumer_rejects_prepared_row_that_does_not_match_authored_choice(
    tmp_path, monkeypatch, tamper
):
    store = _store(tmp_path)
    store.append_jsonl("candidates", _advisory())
    chosen = "The blue lantern by the window made me think of you."

    def tampered_prepare(*args, **kwargs):
        body = "Older words from a prior attempt." if tamper == "body" else chosen
        source_ids = ["other_source"] if tamper == "source_identity" else ["source_1"]
        row = {
            "id": "obx_tampered",
            "status": "prepared",
            "origin_thread_id": "",
            "origin_candidate_id": "advisory_1",
            "request_type": "REACH_OUT",
            "surface": "local",
            "delivery_mode": "context_pointer",
            "target": {},
            "allowed_surfaces": ["local"],
            "message_preview": body,
            "content_hash": hashlib.sha256(body.encode()).hexdigest()[:16],
            "content_length": len(body),
            "source_candidate_ids": source_ids,
            "source_candidate_fingerprint": "source-revision-1",
            "source_revision_key": source_revision_key(
                candidate_id="advisory_1",
                source_candidate_ids=source_ids,
                source_candidate_fingerprint="source-revision-1",
            ),
        }
        store.append_jsonl("outbox", row)
        return {"success": True, "receipt": {"outbox_id": row["id"]}, "outbox": row}

    monkeypatch.setattr(consumer, "apply_conscious_reachout_decision", tampered_prepare)
    result = consume_conscious_advisory(
        store,
        decision={"decision": "REACH_OUT", "reason": "Use this exact wording.", "message": chosen},
        dry_run=False,
        now="2026-08-26T11:00:00Z",
    )

    assert result["success"] is False
    assert result["action"] == "reach_out_denied_held"
    assert result["error"] == "outbox_not_openable"
    assert store.read_jsonl("candidates")[0]["status"] == "held"
    assert not any(
        row.get("new_status") == "reviewed" for row in store.read_jsonl("decisions")
    )
