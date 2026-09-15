from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from agent_sensorium.conscious_aperture import (
    open_conscious_aperture,
    settle_conscious_aperture_item,
)
from agent_sensorium.conscious_doorway import handle_conscious_doorway_pre_llm
from agent_sensorium.plugin import register
from agent_sensorium.store import SensoriumStore


def _candidate(candidate_id, *, pressure=0.7):
    return {
        "id": candidate_id,
        "status": "candidate",
        "kind": "subconscious_advisory",
        "pressure": pressure,
        "summary": f"Candidate {candidate_id}",
        "fingerprint": f"fp-{candidate_id}",
        "event_ids": [f"evt_{candidate_id}"],
        "source_candidate_ids": [],
        "correlation_keys": ["test"],
        "sensitivity": "private",
        "allowed_surfaces": ["local"],
        "created_at": "2026-06-07T10:00:00Z",
        "updated_at": "2026-06-07T10:00:00Z",
        "conscious_task": {
            "id": f"ctask_{candidate_id}",
            "request_type": "THINK",
            "title": f"Task {candidate_id}",
            "why": "Needs coherent Conscious attention.",
            "expected_decision": "Decide save, hold, or external work.",
        },
        "advisory_meta": {
            "rationale": "test",
            "source_fingerprint": f"source-fp-{candidate_id}",
        },
    }


def test_stale_and_fresh_items_share_bounded_recovery_packet(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "sensorium"))
    stale = _candidate("stale", pressure=0.9)
    stale["status"] = "in_conscious_aperture"
    stale["conscious_aperture"] = {"id": "cap_old", "opened_at": "2026-06-07T08:00:00Z", "state": "open"}
    store.rewrite_jsonl("candidates", [stale, _candidate("fresh", pressure=0.8)])
    result = open_conscious_aperture(
        store, aperture_size=2, max_active_items=2, consumer_id="consumer-a",
        lease_minutes=10, stale_after_minutes=60, dry_run=False, now="2026-06-07T12:00:00Z",
    )
    assert set(result["candidate_ids"]) == {"stale", "fresh"}
    assert result["reclaimed_candidate_ids"] == ["stale"]
    assert not [d for d in store.read_jsonl("decisions") if d.get("type") == "conscious.aperture.settled"]
    assert store.read_jsonl("outbox") == store.read_jsonl("worker_requests") == []


def test_interrupted_consumer_releases_only_execution_ownership(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "sensorium"))
    store.rewrite_jsonl("candidates", [_candidate("one"), _candidate("two")])
    first = open_conscious_aperture(store, aperture_size=1, max_active_items=2, consumer_id="consumer-a", lease_minutes=5, dry_run=False, now="2026-06-07T12:00:00Z")
    unrelated = open_conscious_aperture(store, aperture_size=1, max_active_items=2, consumer_id="consumer-b", lease_minutes=5, dry_run=False, now="2026-06-07T12:01:00Z")
    recovered = open_conscious_aperture(store, aperture_size=1, max_active_items=2, consumer_id="consumer-b", lease_minutes=5, dry_run=False, now="2026-06-07T12:06:00Z")
    assert first["candidate_ids"] == ["one"]
    assert unrelated["candidate_ids"] == ["two"]
    assert recovered["candidate_ids"] == ["one"]
    assert recovered["reclaimed_candidate_ids"] == ["one"]
    assert all(row["status"] == "in_conscious_aperture" for row in store.read_jsonl("candidates"))
    assert not [d for d in store.read_jsonl("decisions") if d.get("type") == "conscious.aperture.settled"]


def test_exact_source_binding_rejects_changed_lineage_without_mutation(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "sensorium"))
    store.append_jsonl("candidates", _candidate("bound"))
    opened = open_conscious_aperture(store, aperture_size=1, max_active_items=1, consumer_id="consumer-a", lease_minutes=10, dry_run=False, now="2026-06-07T12:00:00Z")
    rows = store.read_jsonl("candidates")
    rows[0]["event_ids"] = ["evt_replaced"]
    store.rewrite_jsonl("candidates", rows)
    before = {name: store.read_jsonl(name) for name in ("candidates", "decisions", "outbox")}
    result = settle_conscious_aperture_item(
        store, candidate_id="bound", aperture_id=opened["aperture_id"], consumer_id="consumer-a",
        decision="SETTLED", reason="Exact binding changed.", dry_run=False, now="2026-06-07T12:01:00Z",
    )
    assert result["error"] == "source_binding_mismatch"
    assert before == {name: store.read_jsonl(name) for name in before}


def test_source_digest_rejects_changed_source_payload_without_mutation(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "sensorium"))
    store.append_jsonl("candidates", _candidate("digest-bound"))
    opened = open_conscious_aperture(
        store,
        aperture_size=1,
        max_active_items=1,
        consumer_id="consumer-a",
        lease_minutes=10,
        dry_run=False,
        now="2026-06-07T12:00:00Z",
    )
    binding = store.read_jsonl("candidates")[0]["conscious_aperture"]["source_binding"]
    assert len(binding["source_digest"]) == 64

    rows = store.read_jsonl("candidates")
    rows[0]["summary"] = "Changed after the item was leased."
    store.rewrite_jsonl("candidates", rows)
    before = {name: store.read_jsonl(name) for name in ("candidates", "decisions", "outbox")}

    result = settle_conscious_aperture_item(
        store,
        candidate_id="digest-bound",
        aperture_id=opened["aperture_id"],
        consumer_id="consumer-a",
        decision="SETTLED",
        reason="Do not settle changed source payload.",
        dry_run=False,
        now="2026-06-07T12:01:00Z",
    )

    assert result["error"] == "source_binding_mismatch"
    assert before == {name: store.read_jsonl(name) for name in before}


def test_competing_consumers_claim_once(tmp_path):
    state_dir = tmp_path / "sensorium"
    store = SensoriumStore(instance="test", state_dir=str(state_dir))
    store.append_jsonl("candidates", _candidate("race"))
    def claim(owner):
        local = SensoriumStore(instance="test", state_dir=str(state_dir))
        return open_conscious_aperture(local, aperture_size=1, max_active_items=1, consumer_id=owner, lease_minutes=10, dry_run=False, now="2026-06-07T12:00:00Z")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ["consumer-a", "consumer-b"]))
    assert sum(result.get("candidate_ids") == ["race"] for result in results) == 1
    assert len([d for d in store.read_jsonl("decisions") if d.get("type") == "conscious.aperture.opened"]) == 1


def test_competing_consumer_cannot_settle_another_owners_item(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "sensorium"))
    store.append_jsonl("candidates", _candidate("owned"))
    opened = open_conscious_aperture(
        store, aperture_size=1, max_active_items=1, consumer_id="consumer-a",
        lease_minutes=10, dry_run=False, now="2026-06-07T12:00:00Z",
    )
    before = {name: store.read_jsonl(name) for name in ("candidates", "decisions", "outbox")}
    result = settle_conscious_aperture_item(
        store, candidate_id="owned", aperture_id=opened["aperture_id"],
        consumer_id="consumer-b", decision="SETTLED", reason="Wrong owner.",
        dry_run=False, now="2026-06-07T12:01:00Z",
    )
    assert result["error"] == "consumer_id_mismatch"
    assert before == {name: store.read_jsonl(name) for name in before}


def test_duplicate_and_noise_rows_are_ineligible(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "sensorium"))
    canonical = _candidate("canonical")
    canonical["fingerprint"] = "same-source"
    duplicate = _candidate("duplicate")
    duplicate["fingerprint"] = "same-source"
    duplicate["status"] = "reviewed"
    noise = _candidate("noise")
    noise["status"] = "archived"
    noise["kind"] = "runtime_noise"
    noise.pop("conscious_task")
    store.rewrite_jsonl("candidates", [canonical, duplicate, noise])
    result = open_conscious_aperture(store, aperture_size=3, max_active_items=3, consumer_id="consumer-a", dry_run=False, now="2026-06-07T12:00:00Z")
    assert result["candidate_ids"] == ["canonical"]
    assert store.read_jsonl("outbox") == store.read_jsonl("worker_requests") == []


class _PluginContext:
    def __init__(self):
        self.tools = {}
        self.schemas = {}
        self.hooks = []
    def register_tool(self, *, name, handler, **kwargs):
        self.tools[name] = handler
        self.schemas[name] = kwargs.get("schema")
    def register_hook(self, name, handler):
        if name == "pre_llm_call":
            self.hooks.append(handler)
    def register_command(self, *args, **kwargs):
        pass
    def register_skill(self, *args, **kwargs):
        pass


def test_foreground_doorway_attempts_resumes_and_live_tool_settles(tmp_path, monkeypatch):
    import agent_sensorium.store as store_module
    root = tmp_path / "profiles"
    monkeypatch.setenv("AGENT_SENSORIUM_ROOT", str(root))
    monkeypatch.setenv("AGENT_SENSORIUM_DEFAULT_INSTANCE", "generic")
    monkeypatch.setattr(store_module, "_DEFAULT_BASE", str(root))
    state_dir = root / "generic"
    store = SensoriumStore(instance="generic", state_dir=str(state_dir))
    store.ensure_dirs()
    (state_dir / "instance.config.json").write_text(json.dumps({
        "instance_name": "generic", "allowed_surfaces": ["local"],
        "conscious_doorway": {"enabled": True, "aperture_size": 2, "max_active_items": 2, "lease_minutes": 10, "surfaces": ["local"], "agent_label": "Review Agent"},
    }))
    store.rewrite_jsonl("candidates", [_candidate("alpha"), _candidate("beta")])
    assert handle_conscious_doorway_pre_llm(instance="generic", platform="discord", session_id="session-a", turn_id="turn-0", state_dir=str(state_dir)) is None
    first = handle_conscious_doorway_pre_llm(instance="generic", platform="local", session_id="session-a", turn_id="turn-1", state_dir=str(state_dir))
    resumed = handle_conscious_doorway_pre_llm(instance="generic", platform="local", session_id="session-a", turn_id="turn-2", state_dir=str(state_dir))
    assert first is not None and resumed is not None
    assert "Review Agent" in first["context"]
    assert "alpha" in first["context"] and "beta" in first["context"]
    assert "sensorium(action=\"update\"" in first["context"]
    attempts = [d for d in store.read_jsonl("decisions") if d.get("type") == "conscious.aperture.presentation_attempted"]
    assert len(attempts) == 2
    assert all(row["host_consumption_confirmed"] is False for row in attempts)
    assert not [d for d in store.read_jsonl("decisions") if d.get("type") == "conscious.aperture.consumed"]
    row = store.read_jsonl("candidates")[0]
    ctx = _PluginContext()
    register(ctx)
    payload = json.loads(ctx.tools["sensorium"]({
        "action": "update", "instance": "generic", "id": "alpha",
        "aperture_id": row["conscious_aperture"]["id"],
        "consumer_id": row["conscious_aperture"]["consumer_id"], "keyword": "settle",
        "text": "Foreground review completed.", "surface": "local",
    }, session_id="session-a"))
    assert payload["success"] is True
    assert payload["data"]["action"] == "settled_aperture_item"
    assert "consumer_id" in ctx.schemas["sensorium"]["parameters"]["properties"]
    assert "prepared_external_work" not in ctx.schemas["sensorium"]["parameters"]["properties"]["keyword"]["description"]
    beta = next(item for item in store.read_jsonl("candidates") if item["id"] == "beta")
    omitted_owner = json.loads(ctx.tools["sensorium"]({
        "action": "update", "instance": "generic", "id": "beta",
        "aperture_id": beta["conscious_aperture"]["id"], "keyword": "settle",
        "text": "Missing owner token.", "surface": "local",
    }))
    assert omitted_owner["success"] is False
    assert omitted_owner["error"] == "consumer_id_required"
    unsupported = json.loads(ctx.tools["sensorium"]({
        "action": "update", "instance": "generic", "id": "beta",
        "aperture_id": beta["conscious_aperture"]["id"],
        "consumer_id": beta["conscious_aperture"]["consumer_id"],
        "keyword": "prepared_external_work", "text": "Unsupported compact decision.",
        "surface": "local",
    }))
    assert unsupported["success"] is False
    assert next(item for item in store.read_jsonl("candidates") if item["id"] == "beta")["status"] == "in_conscious_aperture"
    assert store.read_jsonl("outbox") == store.read_jsonl("worker_requests") == []


def test_plugin_pointer_receives_current_hermes_turn_fields(tmp_path, monkeypatch):
    import agent_sensorium.store as store_module
    root = tmp_path / "profiles"
    monkeypatch.setenv("AGENT_SENSORIUM_ROOT", str(root))
    monkeypatch.setenv("AGENT_SENSORIUM_DEFAULT_INSTANCE", "generic")
    monkeypatch.setattr(store_module, "_DEFAULT_BASE", str(root))
    state_dir = root / "generic"
    store = SensoriumStore(instance="generic", state_dir=str(state_dir))
    store.ensure_dirs()
    (state_dir / "instance.config.json").write_text(json.dumps({
        "instance_name": "generic", "allowed_surfaces": ["local"],
        "pointer": {"cooldown_minutes": 0, "min_turn_gap": 2},
        "conscious_doorway": {"enabled": False},
    }))
    store.append_jsonl("candidates", _candidate("pointer", pressure=0.96))
    ctx = _PluginContext()
    register(ctx)
    pointer_hook = ctx.hooks[1]
    histories = [
        [{"role": "user", "content": "general review"}],
        [{"role": "user", "content": "general review"}, {"role": "assistant", "content": "acknowledged"}, {"role": "user", "content": "another review"}],
        [{"role": "user", "content": "general review"}, {"role": "assistant", "content": "acknowledged"}, {"role": "user", "content": "another review"}, {"role": "assistant", "content": "acknowledged"}, {"role": "user", "content": "review the pending design item"}],
    ]
    outputs = [pointer_hook(session_id="session-a", turn_id=f"turn-{index}", platform="local", user_message=history[-1]["content"], conversation_history=history) for index, history in enumerate(histories, 1)]
    assert [output is not None for output in outputs] == [True, False, True]
    receipts = store.read_jsonl("decisions")
    assert [row["foreground_turn_index"] for row in receipts] == [1, 2, 3]
    assert receipts[1]["reason"] == "min_turn_gap"
