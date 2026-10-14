"""Fictional production-path replay collisions and valid inverse controls."""
import copy
import json

import pytest

from agent_sensorium import admission, tools
from agent_sensorium.gate import promote_signal_to_event, signal_fingerprint
from tests.test_core_material_replay import ingest, signal, snapshot, store_at


def call(store, row, event=False):
    handler = tools.handle_sensorium_ingest_event if event else tools.handle_sensorium_ingest_signal
    return json.loads(handler(**{"event" if event else "signal": row}, instance=store.instance,
                              state_dir=str(store.root), config={}))


def forbid_effects(store, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Rejected ingress must not write, decay, prune or observe")
    monkeypatch.setattr(tools, "observe_after_success", forbidden)
    monkeypatch.setattr(tools, "_apply_candidate_decay", forbidden)
    monkeypatch.setattr(tools, "_read_pruned_sensor_policy", forbidden)
    monkeypatch.setattr(type(store), "append_jsonl", forbidden)
    monkeypatch.setattr(type(store), "rewrite_jsonl", forbidden)
    monkeypatch.setattr(type(store), "write_state", forbidden)
    monkeypatch.setattr(type(store), "write_sensor_policy", forbidden)


# Ported original B RED cases, preserving their collision and memory oracles.
@pytest.mark.parametrize("change", ["revision", "item", "legacy_to_explicit", "memory_member",
                                    "malformed_revision", "source_to_legacy", "alias", "evidence"])
def test_signal_wrapper_collision_is_not_replay(tmp_path, monkeypatch, change):
    store = store_at(tmp_path)
    first = signal("same")
    first.update(sensitivity="public_safe", allowed_surfaces=["local", "telegram"])
    incoming = signal("same", "r2")
    if change == "item":
        incoming = signal("same", "r1", "b")
    elif change == "legacy_to_explicit":
        first.pop("artifact_meta")
        first.pop("source_revision")
    elif change == "memory_member":
        first.update(sensor="sensorium.memory_reflection", source="memory",
                     memory_provenance={"provider": "fiction", "bank_id": "bank", "item_ids": ["a"]})
        first.pop("artifact_meta")
        first.pop("source_revision")
        incoming = {**first, "memory_provenance": {"provider": "fiction", "bank_id": "bank", "item_ids": ["b"]}}
    elif change == "malformed_revision":
        incoming["source_revision"] = None
    elif change == "source_to_legacy":
        incoming.pop("artifact_meta")
        incoming.pop("source_revision")
    elif change == "alias":
        incoming["artifact_meta"]["revision_id"] = "r1"
    elif change == "evidence":
        incoming.update(sensor="body.synthetic", source="telemetry", kind="body_pressure",
                        source_revision="1", metric_family="fiction")
        incoming.pop("artifact_meta")
    ingest(store, first)
    before = snapshot(store)
    incoming.update(sensitivity="local_only", allowed_surfaces=[])
    forbid_effects(store, monkeypatch)
    assert not call(store, incoming)["success"]
    assert snapshot(store) == before


def test_event_wrapper_collision_is_not_replay(tmp_path, monkeypatch):
    store = store_at(tmp_path)
    ingest(store, signal())
    event = dict(store.read_jsonl("events")[0])
    store.append_jsonl("signals", signal("sig-b", "r2"))
    event.update(source_signal_ids=["sig-b"], allowed_surfaces=[], sensitivity="local_only")
    before = snapshot(store)
    forbid_effects(store, monkeypatch)
    assert not call(store, event, event=True)["success"]
    with pytest.raises(ValueError, match="collision"):
        tools._append_event_and_create_or_update_candidate(store, event=event, config={})
    assert snapshot(store) == before


def test_memory_fingerprint_does_not_override_member_identity(tmp_path):
    store = store_at(tmp_path)
    first = signal("m1", None)
    first.pop("artifact_meta")
    first.update(sensor="sensorium.memory_reflection", source="memory",
                 memory_provenance={"provider": "fiction", "bank_id": "bank", "item_ids": ["a"]})
    incoming = {**first, "id": "m2", "memory_provenance": {"provider": "fiction", "bank_id": "bank", "item_ids": ["b"]}}
    assert signal_fingerprint(first) == signal_fingerprint(incoming)
    ingest(store, first)
    result = ingest(store, incoming)
    assert not result["duplicate"] and len(store.read_jsonl("signals")) == 2


@pytest.mark.parametrize("stream", ["signals", "events", "candidates", "threads"])
@pytest.mark.parametrize("reverse", [False, True])
def test_replay_rejects_ambiguous_descendants_without_narrowing(tmp_path, monkeypatch, stream, reverse):
    store = store_at(tmp_path)
    ingest(store, signal())
    if stream == "threads":
        candidate = store.read_jsonl("candidates")[0]
        store.append_jsonl("threads", {"id": "thread-fiction", "origin_candidate_id": candidate["id"]})
    rows = store.read_jsonl(stream)
    duplicate = {**rows[0], "summary": "Other fictional wrapper"}
    rows = [duplicate, rows[0]] if reverse else [rows[0], duplicate]
    store.rewrite_jsonl(stream, rows)
    before = snapshot(store)
    forbid_effects(store, monkeypatch)
    assert not call(store, {**signal(), "sensitivity": "local_only"})["success"]
    assert snapshot(store) == before


@pytest.mark.parametrize("shape", ["missing", "ambiguous", "malformed", "mixed_legacy", "mixed_revision", "bad_refs"])
def test_trusted_join_rejected_before_wrapper_replay(tmp_path, monkeypatch, shape):
    store = store_at(tmp_path)
    ingest(store, signal())
    event = dict(store.read_jsonl("events")[0])
    extra = signal("extra", "r2")
    if shape == "missing":
        event["source_signal_ids"].append("absent")
    elif shape == "ambiguous":
        store.append_jsonl("signals", signal())
    elif shape == "malformed":
        rows = store.read_jsonl("signals")
        rows[0]["source_revision"] = None
        store.rewrite_jsonl("signals", rows)
    elif shape in {"mixed_legacy", "mixed_revision"}:
        if shape == "mixed_legacy":
            extra.pop("artifact_meta")
            extra.pop("source_revision")
        store.append_jsonl("signals", extra)
        event["source_signal_ids"].append("extra")
    else:
        event["source_signal_ids"] = "sig-a"
    before = snapshot(store)
    forbid_effects(store, monkeypatch)
    assert not call(store, event, event=True)["success"]
    with pytest.raises(ValueError):
        tools._append_event_and_create_or_update_candidate(store, event=event, config={})
    assert snapshot(store) == before


def test_ordinary_signal_preflights_event_collision_before_signal_write(tmp_path, monkeypatch):
    store = store_at(tmp_path)
    ingest(store, signal())
    old = store.read_jsonl("events")[0]
    promote = tools.promote_signal_to_event
    def colliding(row, config):
        return {**promote(row, config), "id": old["id"]}
    monkeypatch.setattr(tools, "promote_signal_to_event", colliding)
    before = snapshot(store)
    forbid_effects(store, monkeypatch)
    assert not call(store, signal("fresh", "r2"))["success"]
    assert snapshot(store) == before


@pytest.mark.parametrize("legacy", [False, True])
def test_literal_ids_valid_replay_and_stricter_only_narrowing(tmp_path, monkeypatch, legacy):
    store = store_at(tmp_path)
    row = signal(" literal wrapper ", " r1 ", " item ")
    if legacy:
        row.pop("artifact_meta")
        row.pop("source_revision")
    row.update(sensitivity="public_safe", allowed_surfaces=["local", "telegram"])
    created = ingest(store, row)
    assert created["signal_id"] == " literal wrapper "
    before = snapshot(store)
    def forbidden(*args, **kwargs):
        raise AssertionError("Replay must not observe or decay")
    monkeypatch.setattr(tools, "observe_after_success", forbidden)
    monkeypatch.setattr(tools, "_apply_candidate_decay", forbidden)
    assert call(store, row)["data"]["duplicate"]
    assert snapshot(store) == before
    narrower = {**row, "sensitivity": "local_only"}
    assert call(store, narrower)["data"]["duplicate"]
    after = snapshot(store)
    assert after != before
    assert call(store, row)["data"]["duplicate"]
    assert snapshot(store) == after
    event = dict(store.read_jsonl("events")[0])
    assert call(store, event, event=True)["data"]["duplicate"]
    assert snapshot(store) == after


def test_fresh_revision_coalesces_but_telemetry_does_not(tmp_path):
    store = store_at(tmp_path)
    one = ingest(store, signal())
    two = ingest(store, signal("fresh", "r2"))
    assert not two["duplicate"] and two["coalesced"]
    assert two["candidate_id"] == one["candidate_id"]
    telemetry = signal("t1")
    telemetry.pop("artifact_meta")
    telemetry.update(sensor="body.synthetic", source="telemetry", kind="body_pressure",
                     source_revision="1", metric_family="fiction")
    first = ingest(store, telemetry)
    second = ingest(store, {**telemetry, "id": "t2", "source_revision": "2"})
    assert not second["duplicate"] and not second["coalesced"]
    assert first["candidate_id"] != second["candidate_id"]


@pytest.mark.parametrize("stream", ["events", "candidates"])
def test_fresh_revision_cannot_coalesce_through_duplicate_owner(tmp_path, stream):
    store = store_at(tmp_path)
    first = ingest(store, signal())
    store.append_jsonl(stream, copy.deepcopy(store.read_jsonl(stream)[0]))
    result = ingest(store, signal("fresh", "r2"))
    assert not result["coalesced"] and result["candidate_id"] != first["candidate_id"]


def test_typed_join_preserves_legacy_and_full_memory_members(tmp_path):
    store = store_at(tmp_path)
    assert admission.classify_event_claim(store.instance, {}, signals=[]).kind == "legacy"
    row = signal()
    claim = admission.classify_signal_claim(store.instance, row)
    event = promote_signal_to_event(row)
    assert admission.classify_event_claim(store.instance, event, signals=[row]) == claim
    assert admission.classify_event_claim(store.instance, event, signals=[row, row]).kind == "malformed"


@pytest.mark.parametrize("corruption", ["malformed", "legacy_changed", "injected_cache"])
def test_retained_wrapper_and_raw_projection_cannot_forge_replay(tmp_path, monkeypatch, corruption):
    store = store_at(tmp_path)
    incoming = signal()
    if corruption == "legacy_changed":
        incoming.pop("artifact_meta")
        incoming.pop("source_revision")
    ingest(store, incoming)
    rows = store.read_jsonl("signals")
    if corruption == "malformed":
        rows[0]["source_revision"] = None
    elif corruption == "legacy_changed":
        incoming["summary"] = "Changed fictional legacy material"
    else:
        rows[0]["artifact_meta"]["item_id"] = "different"
        rows[0]["_admission_claim"] = admission._claim_for_signal(store.instance, incoming)[0]
    store.rewrite_jsonl("signals", rows)
    before = snapshot(store)
    forbid_effects(store, monkeypatch)
    assert not call(store, incoming)["success"]
    assert snapshot(store) == before


@pytest.mark.parametrize("direction", ["source_to_legacy", "legacy_to_source", "duplicate"])
def test_event_wrapper_class_collision_and_ambiguity(tmp_path, monkeypatch, direction):
    store = store_at(tmp_path)
    row = signal()
    store.append_jsonl("signals", row)
    event = promote_signal_to_event(row)
    if direction == "legacy_to_source":
        event["source_signal_ids"] = []
    assert call(store, event, event=True)["success"]
    incoming = dict(event)
    if direction == "duplicate":
        store.append_jsonl("events", event)
    else:
        incoming["source_signal_ids"] = [row["id"]] if direction == "legacy_to_source" else []
    before = snapshot(store)
    forbid_effects(store, monkeypatch)
    assert not call(store, incoming, event=True)["success"]
    with pytest.raises(ValueError):
        tools._append_event_and_create_or_update_candidate(store, event=incoming, config={})
    assert snapshot(store) == before


def test_standalone_legacy_event_inverse_and_collision(tmp_path, monkeypatch):
    store = store_at(tmp_path)
    event = {**promote_signal_to_event(signal()), "source_signal_ids": [], "id": " literal event "}
    assert call(store, event, event=True)["success"]
    before = snapshot(store)
    forbid_effects(store, monkeypatch)
    assert call(store, event, event=True)["data"]["duplicate"]
    assert not call(store, {**event, "summary": "Different legacy material"}, event=True)["success"]
    assert snapshot(store) == before


def test_memory_members_order_is_equivalent_but_not_new_evidence(tmp_path, monkeypatch):
    store = store_at(tmp_path)
    row = signal("memory-one", None)
    row.pop("artifact_meta")
    row.update(sensor="sensorium.memory_reflection", source="memory",
               memory_provenance={"provider": "fiction", "bank_id": "bank", "item_ids": ["a", "b"]})
    ingest(store, row)
    before = snapshot(store)
    replay = {**row, "id": "memory-two", "summary": "Other representation",
              "memory_provenance": {"provider": "fiction", "bank_id": "bank", "item_ids": ["b", "a"]}}
    forbid_effects(store, monkeypatch)
    assert call(store, replay)["data"]["duplicate"]
    assert snapshot(store) == before


@pytest.mark.parametrize("stream", ["signals", "events"])
def test_distinct_equivalent_retained_wrappers_do_not_select_first(tmp_path, monkeypatch, stream):
    store = store_at(tmp_path)
    ingest(store, signal())
    row = dict(store.read_jsonl(stream)[0])
    store.append_jsonl(stream, {**row, "id": "second-wrapper"})
    incoming = {**row, "id": "third-wrapper"}
    before = snapshot(store)
    forbid_effects(store, monkeypatch)
    assert not call(store, incoming, event=stream == "events")["success"]
    assert snapshot(store) == before


def test_malformed_fresh_claim_does_not_fall_back_to_legacy(tmp_path, monkeypatch):
    store = store_at(tmp_path)
    row = signal("fresh")
    row["source_revision"] = None
    row["_admission_claim"] = admission._claim_for_signal(store.instance, signal())[0]
    before = snapshot(store)
    forbid_effects(store, monkeypatch)
    assert not call(store, row)["success"]
    assert snapshot(store) == before
