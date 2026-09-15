"""Focused contract tests for the disabled private Conscious doorway canary."""

from __future__ import annotations

import fcntl
import json
import multiprocessing
from pathlib import Path

from agent_sensorium.conscious_aperture import (
    open_conscious_aperture,
    settle_conscious_aperture_item,
)
from agent_sensorium.conscious_doorway import handle_conscious_doorway_pre_llm
from agent_sensorium.plugin import register
from agent_sensorium.store import SensoriumStore


class FakePluginContext:
    def __init__(self):
        self.tools, self.commands, self.skills, self.hooks = {}, {}, {}, {}

    def register_tool(self, name, toolset, schema, handler, **kwargs):
        self.tools[name] = {"toolset": toolset, "schema": schema, "handler": handler, **kwargs}

    def register_command(self, name, handler, **kwargs):
        self.commands[name] = {"handler": handler, **kwargs}

    def register_skill(self, name, path, **kwargs):
        self.skills[name] = {"path": path, **kwargs}

    def register_hook(self, name, handler, **kwargs):
        self.hooks[f"{name}:{len(self.hooks)}"] = {"handler": handler, **kwargs}


def _candidate(identifier: str, *, kind="subconscious_advisory", status="candidate", valid=True):
    row = {
        "id": identifier,
        "status": status,
        "kind": kind,
        "pressure": 0.9,
        "summary": f"private {identifier}; raw transcript must not leak",
        "event_ids": [],
        "source_candidate_ids": [],
        "correlation_keys": [],
        "sensitivity": "private",
        "allowed_surfaces": ["local"],
        "created_at": "2026-07-26T10:00:00Z",
        "updated_at": "2026-07-26T10:00:00Z",
        "raw_task_body": "task body must not leak",
        "platform_content": "platform content must not leak",
        "files": ["private-file-must-not-leak.txt"],
        "advisory_meta": {"rationale": "A concise advisory rationale."},
    }
    if valid:
        row["conscious_task"] = {
            "id": f"task-{identifier}",
            "request_type": "THINK",
            "title": "Private task",
            "why": "Requires a bounded choice",
            "expected_decision": "Choose one settlement.",
        }
    return row


def _store(tmp_path: Path, enabled=True):
    root = tmp_path / "state"
    root.mkdir(parents=True)
    (root / "instance.config.json").write_text(
        json.dumps({"conscious_doorway": {"enabled": enabled}})
    )
    store = SensoriumStore(instance="test", state_dir=str(root))
    store.ensure_dirs()
    return store


def _forbidden_ledgers(store):
    return {
        name: store.read_jsonl(name)
        for name in ("outbox", "thread_actions", "worker_requests", "threads")
    }


def test_off_is_exactly_quiet_without_store_creation(tmp_path):
    root = tmp_path / "absent"
    assert handle_conscious_doorway_pre_llm(instance="test", state_dir=str(root)) is None
    assert not root.exists()


def test_implicit_root_config_activates_while_disabled_and_absent_remain_no_create(
    tmp_path, monkeypatch
):
    import agent_sensorium.store as store_module

    monkeypatch.setattr(store_module, "_DEFAULT_BASE", str(tmp_path / "implicit"))
    enabled = SensoriumStore(instance="enabled")
    enabled.root.mkdir(parents=True)
    (enabled.root / "instance.config.json").write_text(
        json.dumps({"conscious_doorway": {"enabled": True}})
    )
    enabled.append_jsonl("candidates", _candidate("cand_implicit"))

    packet = handle_conscious_doorway_pre_llm(instance="enabled", state_dir=None)
    assert packet and "cand_implicit" in packet["context"]
    assert enabled.read_jsonl("candidates")[0]["status"] == "in_conscious_aperture"

    disabled = SensoriumStore(instance="disabled")
    disabled.root.mkdir(parents=True)
    (disabled.root / "instance.config.json").write_text(
        json.dumps({"conscious_doorway": {"enabled": False}})
    )
    assert handle_conscious_doorway_pre_llm(instance="disabled", state_dir=None) is None
    assert sorted(path.name for path in disabled.root.iterdir()) == ["instance.config.json"]

    absent = SensoriumStore(instance="absent")
    assert handle_conscious_doorway_pre_llm(instance="absent", state_dir=None) is None
    assert not absent.root.exists()


def test_on_without_typed_advisory_is_quiet_and_nonmutating(tmp_path):
    store = _store(tmp_path)
    store.append_jsonl("candidates", _candidate("cand_raw", kind="design_insight"))
    before = (store.root / "candidates.jsonl").read_bytes()
    assert handle_conscious_doorway_pre_llm(instance="test", state_dir=str(store.root)) is None
    assert (store.root / "candidates.jsonl").read_bytes() == before
    assert store.read_jsonl("decisions") == []


def test_opens_one_private_packet_and_never_mutates_forbidden_ledgers(tmp_path):
    store = _store(tmp_path)
    store.append_jsonl("candidates", _candidate("cand_one"))
    packet = handle_conscious_doorway_pre_llm(instance="test", state_dir=str(store.root))
    assert packet and "[Sensorium Conscious Aperture]" in packet["context"]
    assert "cand_one" in packet["context"]
    assert '"title":"Private task"' in packet["context"]
    assert '"why":"Requires a bounded choice"' in packet["context"]
    assert '"expected_decision":"Choose one settlement."' in packet["context"]
    assert '"aperture_id":"cap_' in packet["context"]
    assert '"consumer_id":"foreground:' in packet["context"]
    assert 'keyword="settle"' in packet["context"]
    assert 'keyword="hold"' in packet["context"]
    for forbidden in (
        "task body must not leak",
        "platform content must not leak",
        "private-file-must-not-leak.txt",
    ):
        assert forbidden not in packet["context"]
    assert store.read_jsonl("candidates")[0]["status"] == "in_conscious_aperture"
    assert (
        len([d for d in store.read_jsonl("decisions") if d["type"] == "conscious.aperture.opened"])
        == 1
    )
    assert _forbidden_ledgers(store) == {name: [] for name in _forbidden_ledgers(store)}


def test_same_owner_resumes_and_expired_lease_is_reclaimed(tmp_path):
    store = _store(tmp_path)
    store.append_jsonl("candidates", _candidate("cand_one"))
    store.append_jsonl("candidates", _candidate("cand_two"))
    first = handle_conscious_doorway_pre_llm(
        instance="test", state_dir=str(store.root), session_id="same-session"
    )
    second = handle_conscious_doorway_pre_llm(
        instance="test", state_dir=str(store.root), session_id="same-session"
    )
    assert first and second
    assert [r["status"] for r in store.read_jsonl("candidates")] == [
        "in_conscious_aperture",
        "in_conscious_aperture",
    ]
    rows = store.read_jsonl("candidates")
    rows[0]["conscious_aperture"]["lease_expires_at"] = "2026-01-01T00:00:00Z"
    store.rewrite_jsonl("candidates", rows)
    stale = open_conscious_aperture(
        store,
        aperture_size=1,
        max_active_items=2,
        consumer_id="new-owner",
        dry_run=False,
        now="2026-07-26T10:00:00Z",
        stale_after_minutes=1,
    )
    assert stale["action"] == "opened_aperture"
    assert stale["reclaimed_candidate_ids"] == ["cand_one"]


def test_raw_saved_and_malformed_candidates_remain_ineligible(tmp_path):
    store = _store(tmp_path)
    store.append_jsonl("candidates", _candidate("cand_raw", kind="design_insight"))
    store.append_jsonl("candidates", _candidate("cand_saved", status="archived"))
    store.append_jsonl("candidates", _candidate("cand_bad", valid=False))
    assert handle_conscious_doorway_pre_llm(instance="test", state_dir=str(store.root)) is None
    assert [r["status"] for r in store.read_jsonl("candidates")] == [
        "candidate",
        "archived",
        "candidate",
    ]


def test_compact_tool_routes_active_item_to_canonical_settlement(tmp_path, monkeypatch):
    import agent_sensorium.store as store_module

    monkeypatch.setattr(store_module, "_DEFAULT_BASE", str(tmp_path / "implicit"))
    instance = "doorway-tool"
    store = SensoriumStore(instance=instance)
    store.ensure_dirs()
    (store.root / "instance.config.json").write_text(
        json.dumps({"conscious_doorway": {"enabled": True}})
    )
    store.append_jsonl("candidates", _candidate("cand_settle"))
    assert handle_conscious_doorway_pre_llm(
        instance=instance, state_dir=str(store.root), session_id="tool-session"
    )
    ownership = store.read_jsonl("candidates")[0]["conscious_aperture"]
    ctx = FakePluginContext()
    register(ctx)
    result = json.loads(
        ctx.tools["sensorium"]["handler"](
            {
                "action": "update",
                "id": "cand_settle",
                "keyword": "no_action",
                "text": "No foreground action is needed.",
                "instance": instance,
                "aperture_id": ownership["id"],
                "consumer_id": ownership["consumer_id"],
            }
        )
    )
    assert result["success"] and result["data"]["action"] == "settled_aperture_item"
    candidate = store.read_jsonl("candidates")[0]
    assert (
        candidate["status"] == "reviewed"
        and candidate["conscious_aperture"]["decision"] == "REVIEWED"
    )
    assert store.read_jsonl("decisions")[-1]["type"] == "conscious.aperture.settled"
    assert _forbidden_ledgers(store) == {name: [] for name in _forbidden_ledgers(store)}


def test_observer_failure_path_cannot_change_canonical_open(tmp_path, monkeypatch):
    from agent_sensorium import conscious_aperture

    baseline = _store(tmp_path / "baseline")
    failed = _store(tmp_path / "failed")
    for store in (baseline, failed):
        store.append_jsonl("candidates", _candidate("cand_same"))
    normal = open_conscious_aperture(
        baseline, aperture_size=1, dry_run=False, now="2026-07-26T10:00:00Z"
    )
    monkeypatch.setattr(conscious_aperture, "observe_after_success", lambda *args, **kwargs: None)
    guarded = open_conscious_aperture(
        failed, aperture_size=1, dry_run=False, now="2026-07-26T10:00:00Z"
    )
    assert {
        key: normal[key] for key in ("success", "action", "candidate_ids", "selected_count")
    } == {key: guarded[key] for key in ("success", "action", "candidate_ids", "selected_count")}
    assert [r["status"] for r in baseline.read_jsonl("candidates")] == [
        r["status"] for r in failed.read_jsonl("candidates")
    ]


def _doorway_open_contender(state_dir, ready, release, results):
    ready.put("ready")
    release.wait()
    results.put(handle_conscious_doorway_pre_llm(instance="test", state_dir=state_dir))


def _settlement_contender(
    state_dir, candidate_id, aperture_id, consumer_id, ready, release, results
):
    ready.put("ready")
    release.wait()
    results.put(
        settle_conscious_aperture_item(
            SensoriumStore(instance="test", state_dir=state_dir),
            candidate_id=candidate_id,
            aperture_id=aperture_id,
            consumer_id=consumer_id,
            decision="REVIEWED",
            reason="Concurrent settlement.",
            dry_run=False,
            now="2026-07-26T10:05:00Z",
        )
    )


def test_doorway_recovers_due_held_but_excludes_malformed_task(tmp_path):
    store = _store(tmp_path)
    due = _candidate("due", status="held")
    bad_due = _candidate("bad_due", status="held", valid=False)
    bad_due["conscious_task"] = {"request_type": "INVALID"}
    for candidate in (due, bad_due):
        candidate["held_return"] = {
            "not_before": "2026-01-01T00:00:00Z",
            "reason_code": "time_checkpoint",
        }
        store.append_jsonl("candidates", candidate)
    packet = handle_conscious_doorway_pre_llm(instance="test", state_dir=str(store.root))
    assert packet and "due" in packet["context"] and "bad_due" not in packet["context"]
    assert [row["status"] for row in store.read_jsonl("candidates")] == [
        "in_conscious_aperture",
        "held",
    ]
    assert any(
        row.get("type") == "conscious.aperture.returned" for row in store.read_jsonl("decisions")
    )


def test_registered_doorway_hook_accepts_documented_kwargs_without_state_dir(tmp_path, monkeypatch):
    import agent_sensorium.plugin as plugin_module
    import agent_sensorium.store as store_module

    monkeypatch.setattr(store_module, "_DEFAULT_BASE", str(tmp_path / "implicit"))
    monkeypatch.setattr(plugin_module, "_default_instance", lambda: "default")
    store = SensoriumStore(instance="default")
    store.root.mkdir(parents=True)
    (store.root / "instance.config.json").write_text(
        json.dumps({"conscious_doorway": {"enabled": True}})
    )
    store.append_jsonl("candidates", _candidate("registered"))
    ctx = FakePluginContext()
    register(ctx)
    packet = list(ctx.hooks.values())[0]["handler"](
        session_id="session",
        user_message="hello",
        conversation_history=[],
        is_first_turn=True,
        model="test",
        platform="local",
    )
    assert packet and "registered" in packet["context"]


def test_concurrent_same_owner_doorway_reuses_one_open_receipt_and_aperture_id(tmp_path):
    store = _store(tmp_path)
    store.append_jsonl("candidates", _candidate("concurrent"))
    ctx = multiprocessing.get_context("fork")
    ready, results, release = ctx.Queue(), ctx.Queue(), ctx.Barrier(3)
    processes = [
        ctx.Process(target=_doorway_open_contender, args=(str(store.root), ready, release, results))
        for _ in range(2)
    ]
    with open(store.root / "locks" / "conscious-aperture.lock", "a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        for process in processes:
            process.start()
        assert [ready.get(timeout=5), ready.get(timeout=5)] == ["ready", "ready"]
        release.wait(timeout=5)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    packets = [results.get(timeout=5), results.get(timeout=5)]
    for process in processes:
        process.join(timeout=5)
        assert process.exitcode == 0
    opened = [packet for packet in packets if packet is not None]
    receipts = [
        row
        for row in store.read_jsonl("decisions")
        if row.get("type") == "conscious.aperture.opened"
    ]
    candidate = store.read_jsonl("candidates")[0]
    assert len(opened) == 2
    assert (
        len(receipts) == 1 and candidate["conscious_aperture"]["id"] == receipts[0]["aperture_id"]
    )
    assert all(receipts[0]["aperture_id"] in packet["context"] for packet in opened)


def test_concurrent_settlement_is_idempotent_without_receipt_or_state_overwrite(tmp_path):
    store = _store(tmp_path)
    store.append_jsonl("candidates", _candidate("settlement"))
    opened = open_conscious_aperture(
        store, aperture_size=1, dry_run=False, now="2026-07-26T10:00:00Z"
    )
    ctx = multiprocessing.get_context("fork")
    ready, results, release = ctx.Queue(), ctx.Queue(), ctx.Barrier(3)
    processes = [
        ctx.Process(
            target=_settlement_contender,
            args=(
                str(store.root),
                "settlement",
                opened["aperture_id"],
                opened["aperture"][0]["consumer_id"],
                ready,
                release,
                results,
            ),
        )
        for _ in range(2)
    ]
    with open(store.root / "locks" / "conscious-aperture.lock", "a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        for process in processes:
            process.start()
        assert [ready.get(timeout=5), ready.get(timeout=5)] == ["ready", "ready"]
        release.wait(timeout=5)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    outcomes = [results.get(timeout=5), results.get(timeout=5)]
    for process in processes:
        process.join(timeout=5)
        assert process.exitcode == 0
    receipts = [
        row
        for row in store.read_jsonl("decisions")
        if row.get("type") == "conscious.aperture.settled"
    ]
    candidate = store.read_jsonl("candidates")[0]
    assert sorted(outcome["action"] for outcome in outcomes) == [
        "already_settled",
        "settled_aperture_item",
    ]
    assert len(receipts) == 1 and candidate["status"] == receipts[0]["new_status"] == "reviewed"
    assert candidate["conscious_aperture"]["decision"] == receipts[0]["decision"] == "REVIEWED"
