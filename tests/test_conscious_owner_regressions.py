from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import multiprocessing
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_sensorium.conscious_aperture import (
    MAX_PRESENTATION_INDEX_RECORDS,
    claim_conscious_aperture_for_presentation,
    open_conscious_aperture,
    record_conscious_aperture_presentation_attempt,
)
from agent_sensorium.conscious_consumer import consume_conscious_advisory
from agent_sensorium.conscious_doorway import handle_conscious_doorway_pre_llm
from agent_sensorium.outbox import _indexed_outbox_request, prepare_local_outbox_request
from agent_sensorium.store import SensoriumStore

ROOT = Path(__file__).parents[1]
START = "2026-09-15T11:00:00Z"
BODY = "The blue lantern by the window made me think of you."


def _load_runner():
    path = ROOT / "scripts" / "sensorium_native_conscious.py"
    spec = importlib.util.spec_from_file_location("conscious_owner_native", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _store(root: Path, instance: str = "test") -> SensoriumStore:
    store = SensoriumStore(instance=instance, state_dir=str(root))
    store.ensure_dirs()
    return store


def _advisory(candidate_id: str = "advisory_1", fingerprint: str = "revision-1") -> dict:
    return {
        "id": candidate_id,
        "status": "candidate",
        "kind": "subconscious_advisory",
        "pressure": 0.9,
        "summary": "A bounded source deserves one conscious choice.",
        "event_ids": ["event_1"],
        "source_candidate_ids": ["source_1"],
        "source_candidate_fingerprint": fingerprint,
        "sensitivity": "private",
        "allowed_surfaces": ["local"],
        "created_at": "2026-09-15T10:00:00Z",
        "updated_at": "2026-09-15T10:00:00Z",
        "conscious_task": {
            "id": "task_1",
            "request_type": "THINK",
            "title": "Choose",
            "why": "The source remains unresolved.",
            "expected_decision": "Choose one bounded disposition.",
        },
    }


def _prepare(store: SensoriumStore, message: str = BODY) -> dict:
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
        source_candidate_fingerprint="revision-1",
        dry_run=False,
    )


def _process_prepare(root: str, start, queue) -> None:
    store = SensoriumStore(instance="test", state_dir=root)
    start.wait()
    result = _prepare(store)
    queue.put((result.get("success"), result.get("idempotent_hit", False), result.get("data", {}).get("id")))


def _runner_args(root: Path) -> argparse.Namespace:
    return argparse.Namespace(
        instance="test",
        state_dir=str(root),
        plugin_root=str(ROOT),
        hermes_cli="/synthetic/hermes",
        provider="openai-codex",
        model="gpt-5.6-sol",
        timeout_seconds=30,
        total_timeout_seconds=60,
        cleanup_reserve_seconds=5,
        failure_cooldown_seconds=1800,
        stale_after_minutes=180,
        force=False,
        emit_reachout=False,
        print_json=False,
    )


def _model_response(decision: str, **extra) -> str:
    return json.dumps(
        {
            "decision": decision,
            "candidate_id": "advisory_1",
            "source_candidate_fingerprint": "revision-1",
            "reason": "One exact bounded choice.",
            **extra,
        }
    )


def _ledger_bytes(store: SensoriumStore) -> dict[str, bytes | None]:
    return {
        name: path.read_bytes() if path.exists() else None
        for name, path in {
            "candidates": store.paths["candidates"],
            "decisions": store.paths["decisions"],
            "outbox": store.paths["outbox"],
        }.items()
    }


def test_same_profile_local_prepare_is_atomic_across_threads_and_profiles_are_independent(tmp_path):
    root = tmp_path / "shared"
    barrier = multiprocessing.Barrier(2)

    def contender():
        barrier.wait()
        return _prepare(_store(root))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result() for future in [pool.submit(contender), pool.submit(contender)]]
    store = _store(root)
    rows = store.read_jsonl("outbox")
    receipts = [row for row in store.read_jsonl("decisions") if row.get("type") == "outbox.prepared"]
    assert len(rows) == len(receipts) == 1
    assert {result["data"]["id"] for result in results} == {rows[0]["id"]}
    assert sorted(result.get("idempotent_hit", False) for result in results) == [False, True]

    other = _store(tmp_path / "other", instance="other")
    assert _prepare(other)["success"] is True
    assert len(other.read_jsonl("outbox")) == 1
    assert len(store.read_jsonl("outbox")) == 1


def test_same_profile_local_prepare_is_atomic_across_processes(tmp_path):
    root = tmp_path / "shared-process"
    _store(root)
    context = multiprocessing.get_context("fork")
    start = context.Event()
    queue = context.Queue()
    processes = [context.Process(target=_process_prepare, args=(str(root), start, queue)) for _ in range(2)]
    for process in processes:
        process.start()
    start.set()
    results = [queue.get(timeout=10) for _ in processes]
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0
    store = _store(root)
    assert len(store.read_jsonl("outbox")) == 1
    assert len([row for row in store.read_jsonl("decisions") if row.get("type") == "outbox.prepared"]) == 1
    assert len({row[2] for row in results}) == 1
    assert sorted(row[1] for row in results) == [False, True]


def test_local_prepare_refuses_same_key_with_mismatched_exact_source(tmp_path):
    store = _store(tmp_path / "state")
    first = _prepare(store)
    rows = store.read_jsonl("outbox")
    rows[0]["origin_candidate_id"] = "tampered-source"
    store.rewrite_jsonl("outbox", rows)
    refused = _prepare(store)
    assert first["success"] is True
    assert refused["success"] is False
    assert refused["error"] == "idempotency_content_mismatch"
    assert len(store.read_jsonl("outbox")) == 1


def test_corrupt_outbox_tail_fails_closed_and_lock_order_rejects_candidate_nesting(tmp_path):
    store = _store(tmp_path / "state")
    assert _prepare(store)["success"] is True
    with open(store.paths["outbox"], "a", encoding="utf-8") as stream:
        stream.write("{corrupt-tail\n")
    before = store.paths["outbox"].read_bytes()
    refused = _prepare(store, "Different exact wording.")
    assert refused["success"] is False
    assert refused["error"] == "outbox_index_source_corrupt"
    assert store.paths["outbox"].read_bytes() == before

    with store.candidate_transaction():
        with pytest.raises(RuntimeError, match="cannot nest inside candidate"):
            _prepare(store, "Another exact wording.")


def test_outbox_transactions_reject_cross_profile_nesting(tmp_path):
    first = _store(tmp_path / "first", instance="first")
    second = _store(tmp_path / "second", instance="second")
    with first.outbox_transaction():
        with pytest.raises(RuntimeError, match="cannot nest across profile roots"):
            second.append_jsonl("outbox", {"id": "forbidden-cross-profile"})
    assert second.read_jsonl("outbox") == []


def test_real_doorway_refuses_full_presentation_index_before_resume_or_new_claim(tmp_path):
    root = tmp_path / "doorway"
    root.mkdir()
    (root / "instance.config.json").write_text(json.dumps({"conscious_doorway": {"enabled": True}}))
    store = _store(root)
    store.append_jsonl("candidates", _advisory("active"))
    first = handle_conscious_doorway_pre_llm(
        instance="test", platform="local", session_id="same-session", turn_id="turn-0", state_dir=str(root)
    )
    assert first is not None
    row = store.read_jsonl("candidates")[0]
    item = {
        "candidate_id": row["id"],
        "aperture_id": row["conscious_aperture"]["id"],
    }
    for index in range(1, MAX_PRESENTATION_INDEX_RECORDS):
        result = record_conscious_aperture_presentation_attempt(
            store,
            aperture=[item],
            consumer_id=row["conscious_aperture"]["consumer_id"],
            turn_id=f"turn-{index}",
            surface="local",
        )
        assert result["success"] is True
    store.append_jsonl("candidates", _advisory("fresh", "revision-2"))
    before_candidates = store.paths["candidates"].read_bytes()
    before_decisions = store.paths["decisions"].read_bytes()
    prior_expiry = store.read_jsonl("candidates")[0]["conscious_aperture"]["lease_expires_at"]

    resumed = handle_conscious_doorway_pre_llm(
        instance="test", platform="local", session_id="same-session", turn_id="overflow-resume", state_dir=str(root)
    )
    fresh = handle_conscious_doorway_pre_llm(
        instance="test", platform="local", session_id="other-session", turn_id="overflow-new", state_dir=str(root)
    )
    assert resumed is fresh is None
    assert store.paths["candidates"].read_bytes() == before_candidates
    assert store.paths["decisions"].read_bytes() == before_decisions
    assert store.read_jsonl("candidates")[0]["conscious_aperture"]["lease_expires_at"] == prior_expiry


def test_presentation_render_failure_and_same_turn_replay_do_not_mutate_ownership(tmp_path):
    store = _store(tmp_path / "render")
    store.append_jsonl("candidates", _advisory())
    before = _ledger_bytes(store)
    failed = claim_conscious_aperture_for_presentation(
        store,
        consumer_id="foreground:test",
        turn_id="turn-fail",
        surface="local",
        platform="local",
        render_context=lambda packet: (_ for _ in ()).throw(ValueError("render failed")),
        aperture_size=1,
        max_active_items=1,
        instance_config={"allowed_surfaces": ["local"]},
        now=START,
    )
    assert failed == {"success": False, "error": "presentation_render_failed"}
    assert _ledger_bytes(store) == before

    first = claim_conscious_aperture_for_presentation(
        store,
        consumer_id="foreground:test",
        turn_id="turn-ok",
        surface="local",
        platform="local",
        render_context=lambda packet: "context",
        aperture_size=1,
        max_active_items=1,
        instance_config={"allowed_surfaces": ["local"]},
        now=START,
    )
    after_first = _ledger_bytes(store)
    replay = claim_conscious_aperture_for_presentation(
        store,
        consumer_id="foreground:test",
        turn_id="turn-ok",
        surface="local",
        platform="local",
        render_context=lambda packet: "context",
        aperture_size=1,
        max_active_items=1,
        instance_config={"allowed_surfaces": ["local"]},
        now="2026-09-15T11:01:00Z",
    )
    assert first["presentation"]["action"] == "presentation_attempt_recorded"
    assert replay["presentation"]["action"] == "presentation_already_attempted"
    assert _ledger_bytes(store) == after_first


@pytest.mark.parametrize("return_at", ["broken", "2026-09-15T10:59:59Z", START])
@pytest.mark.parametrize("dry_run", [True, False])
def test_invalid_hold_is_denied_before_dry_or_apply_ownership(return_at, dry_run, tmp_path):
    store = _store(tmp_path / f"hold-{dry_run}-{return_at.replace(':', '-')}")
    store.append_jsonl("candidates", _advisory())
    before = _ledger_bytes(store)
    result = consume_conscious_advisory(
        store,
        decision={"decision": "HOLD", "reason": "Wait.", "return_at": return_at},
        dry_run=dry_run,
        now=START,
    )
    assert result["success"] is False
    assert result["error"] == "invalid_return_at"
    assert _ledger_bytes(store) == before


def test_future_hold_control_still_settles(tmp_path):
    store = _store(tmp_path / "hold-future")
    store.append_jsonl("candidates", _advisory())
    result = consume_conscious_advisory(
        store,
        decision={"decision": "HOLD", "reason": "Wait.", "return_at": "2026-09-15T12:00:00Z"},
        dry_run=False,
        now=START,
    )
    assert result["success"] is True
    assert store.read_jsonl("candidates")[0]["status"] == "held"


def _seed_resumable(store: SensoriumStore) -> str:
    store.append_jsonl("candidates", _advisory())
    opened = open_conscious_aperture(
        store,
        aperture_size=1,
        max_active_sessions=1,
        consumer_id="conscious-session",
        lease_minutes=1,
        dry_run=False,
        now=START,
    )
    return opened["aperture_id"]


def test_native_runner_commits_resume_before_model_and_applies_at_current_time(tmp_path):
    module = _load_runner()
    store = _store(tmp_path / "resume")
    aperture_id = _seed_resumable(store)
    observed = {}

    def fake_run(command, **kwargs):
        current = store.read_jsonl("candidates")[0]["conscious_aperture"]
        observed.update(current)
        return subprocess.CompletedProcess(command, 0, stdout=_model_response("SILENCE"), stderr="")

    result = module.run_once(
        _runner_args(store.root),
        run_command=fake_run,
        now=START,
        clock=lambda: "2026-09-15T11:02:00Z",
    )
    assert observed["id"] == aperture_id
    assert observed["lease_expires_at"] == "2026-09-15T11:15:00Z"
    assert result["success"] is True
    assert result["applied_at"] == "2026-09-15T11:02:00Z"
    settlement = next(row for row in store.read_jsonl("decisions") if row.get("type") == "conscious.aperture.settled")
    assert settlement["ts"] == "2026-09-15T11:02:00Z"


def test_native_runner_cannot_settle_after_real_expiry_and_other_owner_reclaim(tmp_path):
    module = _load_runner()
    store = _store(tmp_path / "reclaim")
    original_aperture = _seed_resumable(store)

    def fake_run(command, **kwargs):
        assert store.read_jsonl("candidates")[0]["conscious_aperture"]["lease_expires_at"] == "2026-09-15T11:15:00Z"
        reclaimed = open_conscious_aperture(
            store,
            aperture_size=1,
            max_active_sessions=1,
            consumer_id="other-owner",
            dry_run=False,
            now="2026-09-15T11:15:00Z",
        )
        assert reclaimed["aperture_id"] != original_aperture
        return subprocess.CompletedProcess(command, 0, stdout=_model_response("REACH_OUT", message=BODY), stderr="")

    result = module.run_once(
        _runner_args(store.root),
        run_command=fake_run,
        now=START,
        clock=lambda: "2026-09-15T11:16:00Z",
    )
    assert result["success"] is False
    assert result["action"] == "conscious_session_failed"
    assert store.read_jsonl("outbox") == []
    assert not [row for row in store.read_jsonl("decisions") if row.get("type") == "conscious.aperture.settled"]
    assert store.read_jsonl("candidates")[0]["conscious_aperture"]["consumer_id"] == "other-owner"


def test_current_prepared_row_flows_to_emission_without_lifetime_outbox_read(tmp_path, monkeypatch):
    module = _load_runner()
    store = _store(tmp_path / "history")
    history = store.paths["outbox"]
    rows = [
        {"id": f"historic-{index}", "status": "dispatched", "idempotency_key": f"historic-key-{index}"}
        for index in range(18000)
    ]
    history.write_text("".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows))
    with store.outbox_transaction():
        while True:
            _, error = _indexed_outbox_request(store, "missing-current")
            if error != "outbox_index_catching_up":
                break
    assert error is None
    with sqlite3.connect(store.root / "inner_life" / "outbox_index.sqlite3") as conn:
        indexed_before = conn.execute("SELECT offset FROM metadata WHERE singleton=1").fetchone()[0]
    archive_bytes = history.stat().st_size
    store.append_jsonl("candidates", _advisory())
    original_read = SensoriumStore.read_jsonl

    def reject_lifetime_outbox(self, name, limit=None):
        if name == "outbox":
            raise AssertionError("lifetime outbox read called")
        return original_read(self, name, limit=limit)

    monkeypatch.setattr(SensoriumStore, "read_jsonl", reject_lifetime_outbox)
    result = module.run_once(
        _runner_args(store.root),
        run_command=lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, stdout=_model_response("REACH_OUT", message=BODY), stderr=""
        ),
        now=START,
        clock=lambda: "2026-09-15T11:01:00Z",
    )
    assert result["success"] is True
    assert module.scheduler_output(result) == BODY
    assert BODY not in json.dumps(result)
    assert BODY not in (store.root / "last_conscious_clock.json").read_text()
    with sqlite3.connect(store.root / "inner_life" / "outbox_index.sqlite3") as conn:
        indexed_after = conn.execute("SELECT offset FROM metadata WHERE singleton=1").fetchone()[0]
    assert indexed_after == indexed_before
    assert archive_bytes > 1024 * 1024
    assert history.stat().st_size - archive_bytes < 2048
