"""Synthetic retained mutation authority, exercised through real ingress owners."""
import copy
import json

import pytest

from agent_sensorium import tools
from agent_sensorium.dispatcher import candidate_to_thread
from agent_sensorium.gate import promote_signal_to_event
from tests.test_canonical_ingress_replay import call, create_role_advisory
from tests.test_core_material_replay import ingest, signal, store_at


def seed(tmp_path):
    store = store_at(tmp_path)
    row = signal(" literal signal ")
    row.update(sensitivity="public_safe", allowed_surfaces=["local", "telegram"])
    result = ingest(store, row)
    root = store.read_jsonl("candidates")[0]
    derived = create_role_advisory(store, root)
    for candidate in [root, derived]:
        store.append_jsonl("threads", candidate_to_thread(candidate))
    with store.candidate_transaction():
        pass
    return store, row, result


def snapshot(store):
    return {str(p.relative_to(store.root)): p.read_bytes()
            for p in store.root.rglob("*") if p.is_file()}


def corrupt(store, stream, field, value, descendant=False):
    rows = store.read_jsonl(stream)
    target = rows[-1] if descendant else rows[0]
    if value == "ABSENT":
        target.pop(field, None)
    else:
        target[field] = value
    store.rewrite_jsonl(stream, rows)


def attempt(store, row, owner, mode):
    incoming = copy.deepcopy(row)
    if mode == "fresh":
        incoming["id"], incoming["source_revision"] = "fresh-wrapper", "r2"
    elif mode == "stricter":
        incoming.update(sensitivity="local_only", allowed_surfaces=[])
    if owner == "signal":
        return call(store, incoming)
    event = promote_signal_to_event(incoming)
    if mode != "fresh":
        event["id"] = store.read_jsonl("events")[0]["id"]
    if owner == "event":
        return call(store, event, event=True)
    try:
        data = tools._append_event_and_create_or_update_candidate(store, event=event, config={})
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    return {"success": True, "data": data}


def denied_unchanged(store, row, owner, mode, monkeypatch):
    effects = []
    monkeypatch.setattr(tools, "observe_after_success", lambda *a, **k: effects.append("observer"))
    before = snapshot(store)
    response = attempt(store, row, owner, mode)
    after = snapshot(store)
    assert not response["success"], response
    assert after == before
    assert effects == []


def test_causal_dictionary_membership_fresh_revision(tmp_path, monkeypatch):
    store, row, result = seed(tmp_path)
    corrupt(store, "candidates", "event_ids", {result["event_id"]: True})
    denied_unchanged(store, row, "signal", "fresh", monkeypatch)


def test_causal_invalid_visibility_wider_replay(tmp_path, monkeypatch):
    store, row, result = seed(tmp_path)
    corrupt(store, "candidates", "sensitivity", "bogus")
    kw = dict(instance=store.instance, state_dir=str(store.root), surface="local")
    assert not json.loads(tools.handle_sensorium_candidate_open(
        candidate_id=result["candidate_id"], **kw))["success"]
    status = json.loads(tools.handle_sensorium_status(**kw))["data"]
    assert result["candidate_id"] not in [c["id"] for c in status["top_candidates"]]
    denied_unchanged(store, row, "signal", "wider", monkeypatch)
    assert not json.loads(tools.handle_sensorium_candidate_open(
        candidate_id=result["candidate_id"], **kw))["success"]


def test_causal_string_signal_membership_stricter_replay(tmp_path, monkeypatch):
    store, row, _ = seed(tmp_path)
    corrupt(store, "events", "source_signal_ids", row["id"])
    denied_unchanged(store, row, "signal", "stricter", monkeypatch)


SHAPES = ["mapping", "scalar", "string", "none", "nested",
          "nonstring", "duplicate", "empty_id"]


def bad_refs(shape, identifier):
    return {"mapping": {identifier: True}, "scalar": 7, "string": identifier,
            "tuple": (identifier,), "none": None, "nested": [identifier, []],
            "nonstring": [identifier, 7], "duplicate": [identifier, identifier],
            "empty_id": [identifier, ""]}[shape]


@pytest.mark.parametrize("owner", ["signal", "event", "internal"])
@pytest.mark.parametrize("mode", ["fresh", "wider", "stricter"])
@pytest.mark.parametrize("stream,field", [("candidates", "event_ids"),
    ("events", "source_signal_ids"), ("candidates", "source_candidate_ids")])
@pytest.mark.parametrize("shape", SHAPES)
def test_reference_owner_matrix(tmp_path, monkeypatch, owner, mode, stream, field, shape):
    store, row, result = seed(tmp_path)
    identifier = {"event_ids": result["event_id"], "source_signal_ids": row["id"],
                  "source_candidate_ids": result["candidate_id"]}[field]
    if owner != "signal" and mode == "fresh":
        store.append_jsonl("signals", {**row, "id": "fresh-wrapper", "source_revision": "r2"})
    value = bad_refs(shape, identifier)
    corrupt(store, stream, field, value, descendant=field == "source_candidate_ids")
    denied_unchanged(store, row, owner, mode, monkeypatch)


@pytest.mark.parametrize("stream,descendant", [("signals", False), ("events", False),
    ("candidates", False), ("candidates", True), ("threads", False), ("threads", True)])
@pytest.mark.parametrize("field,value", [("sensitivity", "bogus"), ("sensitivity", "ABSENT"),
    ("allowed_surfaces", "local"), ("allowed_surfaces", ["local", 7]),
    ("allowed_surfaces", "ABSENT")])
@pytest.mark.parametrize("mode", ["fresh", "wider", "stricter"])
def test_visibility_closure_before_any_effect(tmp_path, monkeypatch, stream, descendant, field, value, mode):
    store, row, _ = seed(tmp_path)
    corrupt(store, stream, field, value, descendant)
    if mode == "fresh" and (stream != "candidates" or descendant):
        # Only the coalescing candidate's policy is merged on fresh ingress.
        before = copy.deepcopy(store.read_jsonl(stream))
        result = attempt(store, row, "signal", mode)
        assert result["success"] and result["data"]["coalesced"]
        target = before[-1] if descendant else before[0]
        retained = next(r for r in store.read_jsonl(stream) if r["id"] == target["id"])
        assert (field in retained) == (field in target)
        assert retained.get(field) == target.get(field)
        if stream in {"signals", "events", "threads"}:
            assert retained == target
        return
    denied_unchanged(store, row, "signal", mode, monkeypatch)


@pytest.mark.parametrize("owner", ["signal", "event", "internal"])
def test_valid_replay_closure_and_unrelated_visibility(tmp_path, owner):
    store, row, result = seed(tmp_path)
    store.append_jsonl("candidates", {"id": "unconnected", "event_ids": [],
                                      "source_candidate_ids": [], "sensitivity": "bogus"})
    before = snapshot(store)
    assert attempt(store, row, owner, "wider")["success"]
    assert snapshot(store) == before
    assert attempt(store, row, owner, "stricter")["success"]
    after = snapshot(store)
    assert after != before
    for stream in ["signals", "events", "candidates", "threads"]:
        for retained in store.read_jsonl(stream):
            if retained["id"] != "unconnected":
                if stream == "signals" and owner != "signal":
                    assert retained["allowed_surfaces"] == ["local", "telegram"]
                    continue
                assert retained["allowed_surfaces"] == []
                assert retained["sensitivity"] == "local_only"
    kw = dict(instance=store.instance, state_dir=str(store.root), surface="local")
    assert not json.loads(tools.handle_sensorium_candidate_open(
        candidate_id=result["candidate_id"], **kw))["success"]
    assert attempt(store, row, owner, "wider")["success"]
    assert snapshot(store) == after


@pytest.mark.parametrize("field", ["event_ids", "source_candidate_ids"])
def test_tuple_reference_not_coerced_at_internal_resolution(field):
    candidates = [{"id": "root", "event_ids": ["event"]},
                  {"id": "derived", "event_ids": ["event"], "source_candidate_ids": ["root"]}]
    candidates[1][field] = ("event" if field == "event_ids" else "root",)
    with pytest.raises(ValueError, match="ambiguous_ingress_lineage"):
        tools._candidate_for_event_id(candidates, "event")


@pytest.mark.parametrize("absent", [False, True])
def test_standalone_legacy_empty_refs_and_missing_historical_sources(tmp_path, absent):
    store = store_at(tmp_path)
    event = {**promote_signal_to_event(signal()), "source_signal_ids": [], "id": " legacy event "}
    if absent:
        event.pop("source_signal_ids")
    assert call(store, event, event=True)["success"]
    before = snapshot(store)
    assert call(store, event, event=True)["data"]["duplicate"]
    assert snapshot(store) == before


@pytest.mark.parametrize("owner", ["signal", "event", "internal"])
@pytest.mark.parametrize("stream,descendant", [("signals", False), ("events", False),
    ("candidates", True), ("threads", False), ("threads", True)])
@pytest.mark.parametrize("invalid", ["bogus", "ABSENT"])
def test_fresh_identity_only_policy_inverse(tmp_path, owner, stream, descendant, invalid):
    store, row, result = seed(tmp_path)
    candidates = store.read_jsonl("candidates")
    candidates[-1]["status"] = "held"
    store.rewrite_jsonl("candidates", candidates)
    corrupt(store, stream, "sensitivity", invalid, descendant)
    if owner != "signal":
        store.append_jsonl("signals", {**row, "id": "fresh-wrapper", "source_revision": "r2"})
    before = copy.deepcopy(store.read_jsonl(stream))
    target = before[-1] if descendant else before[0]
    response = attempt(store, row, owner, "fresh")
    assert response["success"] and response["data"]["coalesced"]
    assert response["data"]["candidate_id"] == result["candidate_id"]
    retained = next(r for r in store.read_jsonl(stream) if r["id"] == target["id"])
    assert json.dumps(retained, sort_keys=True) == json.dumps(target, sort_keys=True)


@pytest.mark.parametrize("owner", ["signal", "event", "internal"])
@pytest.mark.parametrize("field,value", [("sensitivity", "bogus"), ("sensitivity", "ABSENT"),
    ("allowed_surfaces", "local"), ("allowed_surfaces", "ABSENT")])
def test_fresh_coalescing_owner_policy_denied(tmp_path, monkeypatch, owner, field, value):
    store, row, _ = seed(tmp_path)
    corrupt(store, "candidates", field, value)
    if owner != "signal":
        store.append_jsonl("signals", {**row, "id": "fresh-wrapper", "source_revision": "r2"})
    denied_unchanged(store, row, owner, "fresh", monkeypatch)


@pytest.mark.parametrize("owner", ["event", "internal"])
@pytest.mark.parametrize("mode", ["wider", "stricter"])
@pytest.mark.parametrize("field,value", [("sensitivity", "bogus"), ("sensitivity", "ABSENT"),
    ("allowed_surfaces", "local"), ("allowed_surfaces", "ABSENT")])
def test_event_replay_identity_only_signal_policy_inverse(tmp_path, owner, mode, field, value):
    store, row, _ = seed(tmp_path)
    corrupt(store, "signals", field, value)
    before = store.paths["signals"].read_bytes()
    response = attempt(store, row, owner, mode)
    assert response["success"] and response["data"]["duplicate"]
    assert store.paths["signals"].read_bytes() == before
