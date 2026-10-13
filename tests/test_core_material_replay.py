"""Synthetic producer material identity, replay and bounded progress proofs."""

import json

import pytest

from agent_sensorium import admission, tools
from agent_sensorium.admission_index import MAX_RECORD_BYTES
from agent_sensorium.gate import signal_fingerprint
from agent_sensorium.schemas import normalize_signal
from agent_sensorium.store import SensoriumStore


def signal(identifier="sig-a", revision: str | None = "r1", item="a"):
    row = {"id": identifier, "sensor": "research.feed", "source": "artifact",
           "kind": "creative_pull", "summary": "Synthetic material",
           "strength_hint": 0.9, "sensitivity": "private",
           "allowed_surfaces": ["local"], "correlation_keys": ["topic:synthetic"],
           "artifact_meta": {"source_id": "synthetic-feed", "item_id": item}}
    if revision is not None:
        row["source_revision"] = revision
    return row


def store_at(tmp_path):
    store = SensoriumStore(instance="core", state_dir=str(tmp_path / "state"))
    store.ensure_dirs()
    return store


def ingest(store, row):
    result = json.loads(tools.handle_sensorium_ingest_signal(
        signal=row, instance=store.instance, state_dir=str(store.root), config={}))
    assert result["success"], result
    return result["data"]


def snapshot(store):
    return {name: path.read_bytes() for name, path in store.paths.items() if path.exists()}


@pytest.mark.parametrize("alias", ["source_revision", "revision_id", "sha256"])
def test_normalized_alias_equality_and_changed_revision(tmp_path, alias):
    store = store_at(tmp_path)
    first = signal(revision=None)
    if alias == "source_revision":
        first[alias] = "r1"
    else:
        first["artifact_meta"][alias] = "r1"
    normalized = normalize_signal(first)
    assert normalized.get(alias) == first.get(alias)
    assert normalized["artifact_meta"] == first["artifact_meta"]
    original = ingest(store, first)
    replay = signal("sig-wrapper", None)
    replay["artifact_meta"]["revision_id"] = "r1"
    replay.update(summary="Different wrapper prose", ts="2026-01-02T00:00:00Z")
    assert ingest(store, replay)["duplicate"] is True
    changed = signal("sig-new", "r2")
    changed["correlation_keys"] = []
    updated = ingest(store, changed)
    assert updated["duplicate"] is False
    assert updated["coalesced"] is True
    assert updated["candidate_id"] == original["candidate_id"]


@pytest.mark.parametrize("bad", [None, "", 3, [], "r" * 513])
def test_present_invalid_revision_is_not_missing(bad):
    row = signal(revision=None)
    row["artifact_meta"]["revision_id"] = bad
    claim, error = admission._claim_for_signal("core", row)
    assert claim is None and error == "malformed_research_revision"


@pytest.mark.parametrize("family", ["research", "frontier"])
def test_conflicting_revision_aliases_and_identity_mixtures(family):
    row = signal()
    if family == "frontier":
        row["artifact_meta"] = {"entry_id": "a", "sha256": "r1"}
    row["artifact_meta"]["revision_id"] = "r2"
    assert admission._claim_for_signal("core", row)[0] is None
    mixed = signal()
    mixed["artifact_meta"].update(entry_id="a", sha256="r1")
    assert admission._claim_for_signal("core", mixed)[1] == "ambiguous_source_identity"


def test_missing_revision_remains_unversioned():
    one = signal(revision=None)
    two = signal("other", None)
    two.update(ts="2026-02-01T00:00:00Z", summary="Rephrased synthetic observation")
    assert admission.signal_source_item_identity("core", one) == admission.signal_source_item_identity("core", two)
    assert admission.signal_source_item_identity("core", one) != admission.signal_source_item_identity("core", signal())
    assert signal_fingerprint(one) != signal_fingerprint({**one, "artifact_meta": {**one["artifact_meta"], "revision_id": "r1"}})


@pytest.mark.parametrize("shape", ["dangling", "unknown", "mixed_item", "mixed_revision", "duplicate_id", "invalid_refs"])
def test_event_join_requires_complete_unique_claims(tmp_path, shape):
    store = store_at(tmp_path)
    rows = [signal()]
    refs = ["sig-a"]
    if shape == "dangling":
        refs += ["missing"]
    elif shape == "unknown":
        rows += [{**signal("unknown"), "artifact_meta": {}}]
        refs += ["unknown"]
    elif shape in {"mixed_item", "mixed_revision"}:
        rows += [signal("sig-b", "r2" if shape == "mixed_revision" else "r1", "b" if shape == "mixed_item" else "a")]
        refs += ["sig-b"]
    elif shape == "duplicate_id":
        rows += [signal(item="b")]
    else:
        refs = "sig-a"
    assert admission.event_source_item_identity(store, {"source_signal_ids": refs}, signals=rows) is None


def test_material_replay_is_domain_write_and_observer_free(tmp_path, monkeypatch):
    store = store_at(tmp_path)
    first = ingest(store, signal())
    def forbidden(*args, **kwargs):
        raise AssertionError("Replay cannot decay or observe")
    monkeypatch.setattr(tools, "_apply_candidate_decay", forbidden)
    monkeypatch.setattr(tools, "observe_after_success", forbidden)
    before = snapshot(store)
    assert ingest(store, {**signal("wrapper"), "summary": "Wrapper paraphrase"})["duplicate"]
    event = dict(store.read_jsonl("events")[0])
    event.update(id="evt-wrapper", summary="Event paraphrase")
    replay = json.loads(tools.handle_sensorium_ingest_event(
        event=event, instance=store.instance, state_dir=str(store.root), config={}))
    assert replay["data"]["duplicate"] and replay["data"]["candidate_id"] == first["candidate_id"]
    assert snapshot(store) == before


def test_unknown_same_topic_does_not_merge(tmp_path):
    store = store_at(tmp_path)
    one = signal()
    one.pop("artifact_meta")
    two = {**one, "id": "sig-two", "summary": "Different synthetic material"}
    first, second = ingest(store, one), ingest(store, two)
    assert first["candidate_id"] != second["candidate_id"]


@pytest.mark.parametrize("corruption", ["dangling_event", "dangling_signal", "unknown_signal"])
def test_changed_revision_cannot_merge_incomplete_prior(tmp_path, corruption):
    store = store_at(tmp_path)
    first = ingest(store, signal())
    if corruption == "dangling_event":
        rows = store.read_jsonl("candidates")
        rows[0]["event_ids"].append("missing")
        store.rewrite_jsonl("candidates", rows)
    else:
        events = store.read_jsonl("events")
        events[0]["source_signal_ids"].append("unknown")
        store.rewrite_jsonl("events", events)
        if corruption == "unknown_signal":
            store.append_jsonl("signals", {"id": "unknown", "sensor": "synthetic", "source": "artifact"})
    assert ingest(store, signal("changed", "r2"))["candidate_id"] != first["candidate_id"]


def test_index_consumes_multiple_large_complete_records(tmp_path):
    store = store_at(tmp_path)
    for index in range(3):
        row = signal(f"sig-{index}", item=str(index))
        row["padding"] = "x" * (MAX_RECORD_BYTES - 1024)
        store.append_jsonl("signals", row)
    result = store.prepare_admission_index(scan_bytes=4 * 1024 * 1024)
    assert result.complete and result.records_consumed == 3
    assert result.bytes_consumed <= 4 * 1024 * 1024
    assert result.progress["signals"]["complete"]


def test_index_partial_tail_and_budget_continuation(tmp_path):
    store = store_at(tmp_path)
    store.append_jsonl("signals", signal())
    path = store.paths["signals"]
    with path.open("ab") as handle:
        handle.write(json.dumps(signal("tail", "r2")).encode())
    result = store.prepare_admission_index(scan_bytes=4096)
    assert not result.complete and result.state != "invalid"
    assert result.records_consumed == 1
    with path.open("ab") as handle:
        handle.write(b"\n")
    second = store.prepare_admission_index(scan_bytes=4096)
    assert second.complete and second.records_consumed == 1
    third = store.prepare_admission_index(scan_bytes=1)
    assert not third.complete
    assert third.bytes_consumed <= 1


def test_index_budget_makes_monotone_progress(tmp_path):
    store = store_at(tmp_path)
    for index in range(9):
        row = signal(f"sig-{index}", item=str(index))
        row["padding"] = "x" * 2048
        store.append_jsonl("signals", row)
    offsets = []
    for _ in range(12):
        result = store.prepare_admission_index(scan_bytes=4096)
        assert result.bytes_consumed <= 4096
        offsets.append(result.progress["signals"]["offset"])
        if result.complete:
            break
    assert result.complete
    assert offsets == sorted(set(offsets))
