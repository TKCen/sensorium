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


@pytest.mark.parametrize("owner", ["signal", "trusted_event", "internal"])
def test_new_revision_survives_prior_owner_extinction(tmp_path, monkeypatch, owner):
    store = store_at(tmp_path)
    first = ingest(store, signal())
    rows = store.read_jsonl("candidates")
    rows[0].update(created_at="2000-01-01T00:00:00Z", updated_at="2000-01-01T00:00:00Z")
    old_event_ids = list(rows[0]["event_ids"])
    store.rewrite_jsonl("candidates", rows)
    observers = []
    monkeypatch.setattr(tools, "observe_after_success",
                        lambda *args, **kwargs: observers.append((args[2], args[3])))
    fresh = signal("new-wrapper", "r2")
    if owner == "signal":
        response = call(store, fresh)
        assert response["success"]
        result = response["data"]
    else:
        store.append_jsonl("signals", fresh)
        event = promote_signal_to_event(fresh)
        if owner == "trusted_event":
            response = call(store, event, event=True)
            assert response["success"]
            result = response["data"]
        else:
            result = tools._append_event_and_create_or_update_candidate(store, event=event, config={})
    after = store.read_jsonl("candidates")
    actual = dict(owner=owner, result=result, candidates=after, observers=observers)
    print("EXTINCTION_OBSERVATION " + json.dumps(actual, sort_keys=True))
    old = next(row for row in after if row["id"] == first["candidate_id"])
    assert old["extinct"] and old["status"] == "suppressed"
    assert not result["coalesced"], actual
    assert result["candidate_id"] != first["candidate_id"], actual
    new = next(row for row in after if row["id"] == result["candidate_id"])
    assert new["status"] == "candidate" and not new.get("extinct")
    assert len(after) == 2
    assert result["event_id"] != first["event_id"]
    assert old["event_ids"] == old_event_ids
    assert new["event_ids"] == [result["event_id"]]
    assert not store.read_jsonl("decisions")
    expected_stages = {"signal": ["source_observed", "candidate_updated"],
                       "trusted_event": ["candidate_updated"], "internal": []}
    assert [stage for stage, _ in observers] == expected_stages[owner]
    if observers:
        stage, evidence = observers[-1]
        assert stage == "candidate_updated"
        assert evidence["change_state"] == "new"
        assert evidence["candidate_id"] == new["id"]
        assert evidence["event_id"] == result["event_id"]


def create_role_advisory(store, candidate):
    response = json.loads(tools.handle_sensorium_subconscious_advisory(
        instance=store.instance, state_dir=str(store.root), enabled=True, dry_run=False,
        advisory_output={"action": "CREATE_CONSCIOUS_TASK", "rationale": "Synthetic role evidence",
                         "candidate_ids": [candidate["id"]], "event_ids": candidate["event_ids"],
                         "conscious_task": {"request_type": "THINK", "title": "Synthetic task",
                                            "why": "Evaluate", "expected_decision": "Hold or engage"}},
        config={"model_enabled": False}, record_receipt=True))
    assert response["success"], response
    return store.read_jsonl("candidates")[-1]


@pytest.mark.parametrize("event", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
def test_independent_replay_root_rejected(tmp_path, monkeypatch, event, reverse):
    store = store_at(tmp_path)
    ingest(store, signal())
    root = store.read_jsonl("candidates")[0]
    competing = {**root, "id": "independent-root"}
    rows = [root, competing]
    store.rewrite_jsonl("candidates", rows[::-1] if reverse else rows)
    incoming = dict(store.read_jsonl("events")[0]) if event else signal()
    incoming.update(sensitivity="local_only", allowed_surfaces=[])
    before = snapshot(store)
    forbid_effects(store, monkeypatch)
    assert not call(store, incoming, event=event)["success"]
    assert snapshot(store) == before


@pytest.mark.parametrize("event", [False, True])
@pytest.mark.parametrize("restriction", ["surface", "sensitivity"])
def test_real_advisory_replay_returns_source_and_only_narrows(tmp_path, monkeypatch, event, restriction):
    from agent_sensorium.dispatcher import candidate_to_thread

    store = store_at(tmp_path)
    first = signal()
    first.update(sensitivity="public_safe", allowed_surfaces=["local", "telegram"])
    original = ingest(store, first)
    root = store.read_jsonl("candidates")[0]
    derived = create_role_advisory(store, root)
    assert derived["event_ids"] == root["event_ids"]
    assert derived["source_candidate_ids"] == [root["id"]]
    for row in [root, derived]:
        store.append_jsonl("threads", candidate_to_thread(row))
    # Reverse retention order: role is not append-position authority.
    store.rewrite_jsonl("candidates", [derived, root])
    incoming = dict(store.read_jsonl("events")[0]) if event else dict(first)
    before = snapshot(store)
    with monkeypatch.context() as effects:
        forbid_effects(store, effects)
        replay = call(store, incoming, event=event)
        assert replay["success"] and replay["data"]["duplicate"]
        assert replay["data"]["candidate_id"] == original["candidate_id"]
        assert replay["data"]["event_id"] == original["event_id"]
        if not event:
            assert replay["data"]["signal_id"] == original["signal_id"]
        assert snapshot(store) == before
    def forbidden(*args, **kwargs):
        raise AssertionError("Narrowing must not append, observe, decay or prune")
    for name in ["observe_after_success", "_apply_candidate_decay", "_read_pruned_sensor_policy"]:
        monkeypatch.setattr(tools, name, forbidden)
    monkeypatch.setattr(type(store), "append_jsonl", forbidden)
    narrower = {**incoming, "allowed_surfaces": ["local"]} if restriction == "surface" else {
        **incoming, "sensitivity": "local_only"}
    result = call(store, narrower, event=event)
    assert result["success"] and result["data"]["candidate_id"] == root["id"]
    after = snapshot(store)
    assert after != before
    for stream in ["events", "candidates", "threads"]:
        for row in store.read_jsonl(stream):
            assert row["allowed_surfaces"] == ["local"]
            if restriction == "sensitivity":
                assert row["sensitivity"] == "local_only"
    assert {row["id"] for row in store.read_jsonl("candidates")} == {root["id"], derived["id"]}
    assert len(store.read_jsonl("events")) == len(store.read_jsonl("signals")) == 1
    with monkeypatch.context() as effects:
        forbid_effects(store, effects)
        wider = call(store, incoming, event=event)
        assert wider["success"] and wider["data"]["candidate_id"] == root["id"]
        assert snapshot(store) == after


@pytest.mark.parametrize("event", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("shape", ["duplicate", "dangling", "conflicting", "cycle", "membership",
                                   "malformed_binding", "malformed_sources", "nested_conflict",
                                   "pseudo_derived", "descendant_binding"])
def test_structural_replay_lineage_rejected_without_effects(tmp_path, monkeypatch, event, reverse, shape):
    store = store_at(tmp_path)
    ingest(store, signal())
    root = store.read_jsonl("candidates")[0]
    derived = create_role_advisory(store, root)
    rows = [root, derived]
    if shape == "duplicate":
        rows.append(copy.deepcopy(derived))
    elif shape == "dangling":
        derived["source_candidate_ids"] = ["absent"]
    elif shape == "conflicting":
        derived["admission_binding"]["source_candidate_id"] = "absent"
    elif shape == "cycle":
        root["source_candidate_ids"] = [derived["id"]]
    elif shape == "membership":
        root["event_ids"] = []
    elif shape == "malformed_binding":
        derived["admission_binding"] = []
    elif shape == "malformed_sources":
        derived["source_candidate_ids"] = root["id"]
    elif shape == "nested_conflict":
        derived["advisory_meta"]["admission_binding"]["item_key"] = "contradictory"
    elif shape == "pseudo_derived":
        derived.pop("source_candidate_ids")
    else:
        derived["event_ids"] = []
        derived["admission_binding"] = {}
    store.rewrite_jsonl("candidates", rows[::-1] if reverse else rows)
    incoming = dict(store.read_jsonl("events")[0]) if event else signal()
    incoming.update(sensitivity="local_only", allowed_surfaces=[])
    before = snapshot(store)
    forbid_effects(store, monkeypatch)
    assert not call(store, incoming, event=event)["success"]
    with pytest.raises(ValueError, match="ambiguous_ingress_lineage"):
        tools._preflight_event(store, store.read_jsonl("events")[0],
                               events=store.read_jsonl("events"),
                               signals=store.read_jsonl("signals"),
                               candidates=store.read_jsonl("candidates"))
    assert snapshot(store) == before


@pytest.mark.parametrize("reverse", [False, True])
def test_multi_source_legacy_evidence_has_one_root_per_event(reverse):
    one = {"id": "one", "event_ids": ["event-one"]}
    two = {"id": "two", "event_ids": ["event-two"]}
    derived = {"id": "derived", "event_ids": ["event-one", "event-two"],
               "source_candidate_ids": ["one", "two"]}
    rows = [one, two, derived]
    if reverse:
        rows.reverse()
    assert tools._candidate_for_event_id(rows, "event-one") is one
    assert tools._candidate_for_event_id(rows, "event-two") is two
