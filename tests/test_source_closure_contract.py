"""Canonical disclosure and new-thread production contracts with synthetic stores."""
import copy
import json

import pytest

from agent_sensorium import config, tools
from agent_sensorium.artifacts import store_artifact
from agent_sensorium.dispatcher import dispatch_once
from agent_sensorium.volunteer_cards import build_volunteer_cards
from tests.test_closed_visibility import _consumer_rows, _consumer_snapshot, _prepare_consumers
from tests.test_core_material_replay import store_at


def retain_candidate_parent(store, identifier):
    """Complete an exact fictional parent without changing retained metadata."""
    if not any(row.get("id") == identifier for row in store.read_jsonl("candidates")):
        store.append_jsonl("candidates", {
            "id": identifier, "status": "archived", "pressure": 0,
            "kind": "design_decision", "summary": "Synthetic retained parent",
            "sensitivity": "private", "allowed_surfaces": ["local", "discord", "dashboard"],
        })


def retain_thread_parents(store):
    for row in store.read_jsonl("threads"):
        if "origin_candidate_id" in row:
            retain_candidate_parent(store, row["origin_candidate_id"])


def reject_then_retain_historical(store, signal, *, promoted=True):
    """Separate rejected producer ingress from malformed historical admission."""
    from unittest.mock import patch
    from agent_sensorium.gate import event_to_candidate, promote_signal_to_event
    from agent_sensorium.schemas import normalize_signal
    before = _consumer_snapshot(store)
    with patch.object(tools, "observe_after_success", side_effect=AssertionError("No observer")), \
         patch.object(tools, "_apply_candidate_decay", side_effect=AssertionError("No decay")):
        rejected = json.loads(tools.handle_sensorium_ingest_signal(
            signal=signal, instance=store.instance, state_dir=str(store.root)))
    assert rejected["success"] is False
    assert rejected["error"] == "invalid_ingress_claim"
    assert _consumer_snapshot(store) == before
    assert all(store.read_jsonl(name) == [] for name in ("signals", "events", "candidates", "decisions"))
    retained = normalize_signal(signal)
    store.append_jsonl("signals", retained)
    if promoted:
        event = promote_signal_to_event(retained)
        store.append_jsonl("events", event)
        store.append_jsonl("candidates", event_to_candidate(event))


def retain_event_signals(store, event):
    from agent_sensorium.schemas import normalize_signal
    store.ensure_dirs()
    for identifier in event["source_signal_ids"]:
        store.append_jsonl("signals", normalize_signal({
            "id": identifier, "sensor": "synthetic.local", "source": "local",
            "kind": event["kind"], "summary": event["summary"],
            "strength_hint": event["strength"], "correlation_keys": event["correlation_keys"],
            "sensitivity": event["sensitivity"], "allowed_surfaces": event["allowed_surfaces"],
        }))


@pytest.mark.parametrize("shape", ["root", "nested", "list"])
@pytest.mark.parametrize("action", ["DROP", "SAVE", "CREATE_CONSCIOUS_TASK"])
def test_empty_link_does_not_authorize_rationale(tmp_path, monkeypatch, shape, action):
    store = store_at(tmp_path)
    marker = "Synthetic withheld rationale"
    empty = {"source_candidate_ids": []}
    receipt = {"type": "subconscious.advisory", "output_action": action, "reason": marker}
    receipt.update(empty if shape == "root" else {"metadata": empty if shape == "nested" else [empty]})
    store.append_jsonl("decisions", receipt)
    store.write_state({"last_dispatch_result": receipt})
    _prepare_consumers(store, monkeypatch, config._validate_config({}))
    before = _consumer_snapshot(store)
    result = tools.handle_sensorium_status(instance=store.instance, state_dir=str(store.root))
    assert marker not in result
    assert _consumer_snapshot(store) == before


@pytest.mark.parametrize("mode", ["absent", "corrupt", "nonmapping", "directory", "unreadable"])
def test_config_presence_controls_real_disclosure(tmp_path, monkeypatch, mode):
    store = store_at(tmp_path)
    marker = "Synthetic retained subject"
    candidate, thread = _consumer_rows(marker)
    store.append_jsonl("candidates", candidate)
    store.append_jsonl("threads", thread)
    path = store.root / "instance.config.json"
    if mode == "directory":
        path.mkdir()
    elif mode != "absent":
        path.write_text("{broken" if mode == "corrupt" else "[]" if mode == "nonmapping" else "{}")
    if mode == "unreadable":
        original = type(path).read_text
        def denied(self, *args, **kwargs):
            if self == path:
                raise PermissionError("Synthetic denial")
            return original(self, *args, **kwargs)
        monkeypatch.setattr(type(path), "read_text", denied)
    with store.candidate_transaction():
        pass
    before = _consumer_snapshot(store)
    policy, diagnostic = config.load_instance_config(state_dir=str(store.root))
    result = tools.handle_sensorium_status(instance=store.instance, state_dir=str(store.root))
    assert (marker in result) == (mode == "absent")
    assert policy.get("_visibility_invalid", False) == (mode != "absent")
    assert diagnostic["allowed_surfaces"] == ["local"]
    assert "Synthetic denial" not in json.dumps(diagnostic)
    assert _consumer_snapshot(store) == before


@pytest.mark.parametrize("parent", ["visible", "hidden", "missing", "duplicate"])
def test_real_candidate_artifact_preserves_placeholders(tmp_path, monkeypatch, parent):
    store = store_at(tmp_path)
    candidate, _ = _consumer_rows("Synthetic artifact parent")
    candidate["status"] = "archived"
    if parent == "hidden":
        candidate["allowed_surfaces"] = []
    if parent != "missing":
        store.append_jsonl("candidates", candidate)
    if parent == "duplicate":
        store.append_jsonl("candidates", copy.deepcopy(candidate))
    result = store_artifact(store, kind="text", ref_path="synthetic-note.txt",
                            source_candidate_id=candidate["id"], why_created="Synthetic note")
    assert result["success"]
    artifact = store.read_jsonl("artifacts")[0]
    assert artifact["source_refs"] == {"thread_id": "", "candidate_id": candidate["id"], "action_id": ""}
    _prepare_consumers(store, monkeypatch, config._validate_config({}))
    before = _consumer_snapshot(store)
    cards = build_volunteer_cards(store, surface="local", limit=10)
    assert any(c["card_type"] == "artifact" for c in cards) == (parent == "visible")
    assert _consumer_snapshot(store) == before


@pytest.mark.parametrize("bad", [None, False, 0, [], {}, "nonexistent", ""])
def test_arbitrary_thread_identifiers_are_not_placeholders(tmp_path, bad):
    candidate, _ = _consumer_rows("Synthetic parent")
    disclosure = config.DisclosureVisibility([candidate], [], "local", config._validate_config({}))
    if bad is not None:
        assert not disclosure.linked({"candidate_id": candidate["id"], "thread_id": bad})
    if bad != "":
        assert not disclosure.linked({"source_refs": {"candidate_id": candidate["id"], "thread_id": bad}})


@pytest.mark.parametrize("mode", ["complete", "missing_visibility", "hidden", "duplicate", "dangling", "deny_all",
                                  "missing_id", "missing_sensitivity", "invalid_config", "invalid_operational"])
def test_dispatcher_produces_only_disclosable_threads(tmp_path, mode):
    store = store_at(tmp_path)
    candidate, _ = _consumer_rows("Synthetic production source")
    candidate.update(status="candidate", pressure=0.9)
    if mode == "missing_visibility":
        candidate.pop("allowed_surfaces")
    elif mode == "hidden":
        candidate["allowed_surfaces"] = []
    elif mode == "dangling":
        candidate["source_candidate_ids"] = ["nonexistent"]
    elif mode == "missing_id":
        candidate.pop("id")
    elif mode == "missing_sensitivity":
        candidate.pop("sensitivity")
    store.append_jsonl("candidates", candidate)
    if mode == "duplicate":
        store.append_jsonl("candidates", {**candidate, "status": "archived"})
    if mode == "deny_all":
        (store.root / "instance.config.json").write_text(json.dumps({"allowed_surfaces": []}))
    elif mode == "invalid_config":
        (store.root / "instance.config.json").write_text("{broken")
    before = store.read_jsonl("candidates")
    dispatch_config: dict = {"legacy_thread_dispatch_enabled": True}
    if mode == "invalid_operational":
        dispatch_config["operational_pointer"] = {"enabled": True, "sensitivity": "invalid"}
    result = dispatch_once(store, dry_run=False, config=dispatch_config)
    assert store.read_jsonl("candidates") == before
    assert (result["action"] == "promoted") == (mode == "complete")
    assert len(store.read_jsonl("threads")) == (1 if mode == "complete" else 0)
    assert len(store.read_jsonl("decisions")) == (1 if mode == "complete" else 0)
    assert store.read_state()["budgets"]["dispatch"]["remaining"] == (9 if mode == "complete" else 10)
    if mode == "complete":
        opened = json.loads(tools.handle_sensorium_thread_open(
            thread_id=result["thread_id"], instance=store.instance, state_dir=str(store.root)))
        assert opened["success"]
        assert "Synthetic production source" in json.dumps(opened)
        retained = store.read_jsonl("threads")[0]
        assert retained["origin_candidate_id"] == candidate["id"]
        store.rewrite_jsonl("candidates", [{**candidate, "status": "archived"}])
        assert json.loads(tools.handle_sensorium_thread_open(
            thread_id=result["thread_id"], instance=store.instance, state_dir=str(store.root)))["success"]
        store.rewrite_jsonl("candidates", [{k: v for k, v in candidate.items() if k != "sensitivity"}])
        assert not json.loads(tools.handle_sensorium_thread_open(
            thread_id=result["thread_id"], instance=store.instance, state_dir=str(store.root)))["success"]
        assert store.read_jsonl("threads")[0] == retained


def test_dispatcher_skips_ineligible_pressure_and_cannot_broaden_source(tmp_path):
    store = store_at(tmp_path)
    candidate, _ = _consumer_rows("Synthetic low-pressure source")
    candidate.update(status="candidate", pressure=0.7, allowed_surfaces=["local"])
    high = {**candidate, "id": "candidate-high", "pressure": 0.99, "allowed_surfaces": []}
    store.append_jsonl("candidates", high)
    store.append_jsonl("candidates", candidate)
    before = store.read_jsonl("candidates")
    result = dispatch_once(store, dry_run=False, config={
        "legacy_thread_dispatch_enabled": True,
        "operational_pointer": {"enabled": True, "surfaces": ["discord"], "sensitivity": "private"},
        "allowed_surfaces": ["local", "discord"], "max_sensitivity": "private"})
    assert result["action"] == "promoted"
    assert result["candidate_id"] == candidate["id"]
    assert store.read_jsonl("candidates") == before
    (store.root / "instance.config.json").write_text(json.dumps({"allowed_surfaces": ["local", "discord"]}))
    assert json.loads(tools.handle_sensorium_thread_open(thread_id=result["thread_id"],
        instance=store.instance, state_dir=str(store.root), surface="local"))["success"]
    assert not json.loads(tools.handle_sensorium_thread_open(thread_id=result["thread_id"],
        instance=store.instance, state_dir=str(store.root), surface="discord"))["success"]
