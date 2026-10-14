"""Synthetic public-base cache migration and raw/projection trust prevention."""
import json
from pathlib import Path
import sqlite3
import subprocess
import types

import pytest
from agent_sensorium import admission, admission_index
from agent_sensorium.admission_index import AdmissionIndex
from agent_sensorium.subconscious import run_subconscious_advisory
from tests.test_core_material_replay import signal, store_at, ingest

BASE = "6ae45f8b0882c6f92770deb98259946c9316013e"

def public_base():
    source = subprocess.run(["git", "show", BASE + ":agent_sensorium/admission.py"],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, check=True).stdout
    module = types.ModuleType("agent_sensorium.synthetic_base_admission")
    module.__package__ = "agent_sensorium"
    exec(compile(source, "<public-base-admission>", "exec"), module.__dict__)
    return module

def canonical(store):
    # All canonical owners, including SAVE/accounting outputs; disposable
    # indexes live outside store.paths and are the only permitted rebuild delta.
    return {key: path.read_bytes() for key, path in store.paths.items() if path.exists()}

def raw_snapshot(store):
    return {key: store.read_jsonl(key) for key in ("signals", "events", "decisions", "candidates")}


def semantics(plan):
    # Cache I/O/progress counters describe different execution paths, not
    # admission identity. Compare every domain/admission field unchanged.
    metrics = {"progress", "bytes_consumed", "records_consumed", "query_rows",
               "materialized_rows", "sqlite_vm_steps"}
    return {key: value for key, value in plan.items() if key not in metrics}

@pytest.mark.parametrize("payload", ["claim", "error", "both", "malformed_cache"])
def test_public_raw_injection_cannot_suppress_distinct_material(tmp_path, payload):
    store = store_at(tmp_path)
    first = ingest(store, signal())
    row = signal("forged-wrapper", "r999", "different-item")
    row["artifact_meta"]["source_id"] = "different-source"
    if payload in {"claim", "both"}:
        row["_admission_claim"] = admission._claim_for_signal(store.instance, signal())[0]
    if payload in {"error", "both"}:
        row["_admission_error"] = "malformed_research_revision"
    if payload == "malformed_cache":
        row.update(_admission_claim={"item_key": []}, _admission_error=123)
    expected = admission._claim_for_signal(store.instance, {k:v for k,v in row.items() if not k.startswith("_admission")})
    assert admission._claim_for_signal(store.instance, row) == expected
    before = canonical(store)
    result = ingest(store, row)
    assert result["duplicate"] is False and result["candidate_id"] != first["candidate_id"]
    assert canonical(store) != before
    assert store.prepare_admission_index().complete
    indexed, error = admission.binding_for_candidate(store, result["candidate_id"])
    assert error is None and indexed["item_key"] == expected[0]["item_key"]

@pytest.mark.parametrize("producer", ["malformed", "conflicting", "missing_scope", "unknown", "different_source"])
def test_raw_event_and_snapshot_identity_ignore_reserved_fields(tmp_path, producer):
    store = store_at(tmp_path)
    row = signal()
    if producer == "malformed":
        row["source_revision"] = None
    elif producer == "conflicting":
        row["artifact_meta"]["revision_id"] = "r2"
    elif producer == "missing_scope":
        row.pop("sensor")
    elif producer == "unknown":
        row.pop("artifact_meta")
        row.pop("source_revision")
    else:
        row["artifact_meta"]["source_id"] = "other-source"
    expected = admission._claim_for_signal(store.instance, row)
    row.update(_admission_claim=admission._claim_for_signal(store.instance, signal())[0], _admission_error=None)
    assert admission._claim_for_signal(store.instance, row) == expected
    event = {"id": "event", "source_signal_ids": [row["id"]]}
    identity = admission.event_source_item_identity(store, event, signals=[row])
    assert identity == (None if expected[0] is None else (expected[0]["item_key"], expected[0]["revision_key"]))
    candidate = {"id": "candidate", "event_ids": ["event"], "status": "candidate"}
    snap = {"signals":[row], "events":[event], "candidates":[candidate], "decisions":[]}
    binding, error = admission._base_binding_from_snapshot(store, candidate, snap)
    assert error == expected[1]
    if expected[0]:
        assert binding["item_key"] == expected[0]["item_key"]
    elif not error:
        assert binding["identity_mode"] == "legacy"
    for stream, rows in snap.items():
        for retained in rows:
            store.append_jsonl(stream, retained)
    assert store.prepare_admission_index().complete
    indexed, indexed_error = admission.binding_for_candidate(store, candidate["id"])
    if error:
        assert indexed is None
        assert admission.build_admission_plan(store)["suppressed_counts"][error] == 1
    else:
        assert indexed_error is None and indexed == binding

@pytest.mark.parametrize("variant", ["r1", "missing", "malformed", "conflicting", "alias", "sha256", "matching_all"])
@pytest.mark.parametrize("old_generation", [0, 3, 4])
def test_public_base_schema3_cache_rebuild_matches_canonical(tmp_path, monkeypatch, variant, old_generation):
    store = store_at(tmp_path)
    created = ingest(store, signal())
    rows = store.read_jsonl("signals")
    row = rows[0]
    if variant == "missing":
        row.pop("source_revision")
    elif variant == "malformed":
        row["source_revision"] = None
    elif variant == "conflicting":
        row["artifact_meta"]["revision_id"] = "r2"
    elif variant == "alias":
        row.pop("source_revision")
        row["artifact_meta"]["revision_id"] = "r1"
    elif variant == "sha256":
        row.pop("source_revision")
        row["artifact_meta"]["sha256"] = "r1"
    elif variant == "matching_all":
        row["artifact_meta"].update(revision_id="r1", sha256="r1")

    store.rewrite_jsonl("signals", rows)
    base = public_base()
    with monkeypatch.context() as patch:
        patch.setattr(admission, "_claim_for_signal", base._claim_for_signal)
        patch.setattr(admission, "_base_binding_from_snapshot", base._base_binding_from_snapshot)
        patch.setattr(admission_index, "SCHEMA_VERSION", 3)
        assert store.prepare_admission_index().complete
    with sqlite3.connect(AdmissionIndex(store).path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        conn.execute(f"PRAGMA user_version={old_generation}")
    before = canonical(store)
    assert not store.read_admission_binding(created["candidate_id"]).complete
    assert store.prepare_admission_index().complete
    with sqlite3.connect(AdmissionIndex(store).path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == admission_index.SCHEMA_VERSION > 3
    snap = raw_snapshot(store)
    expected, error = admission._base_binding_from_snapshot(store, snap["candidates"][0], snap)
    warm = admission.build_admission_plan(store)
    if error:
        assert warm["selection"] is None
        assert warm["suppressed_counts"].get(error) == 1
    else:
        assert warm["selection"] == expected
    AdmissionIndex(store).path.unlink()
    cold = admission.build_admission_plan(store)
    assert semantics(cold) == semantics(warm)
    assert store.prepare_admission_index().complete
    assert semantics(admission.build_admission_plan(store)) == semantics(warm)
    assert canonical(store) == before

@pytest.mark.parametrize("revision", ["r1", None])
@pytest.mark.parametrize("settle", ["drop", "save"])
def test_settled_base_unversioned_stays_settled_but_r2_is_fresh(tmp_path, monkeypatch, revision, settle):
    store = store_at(tmp_path)
    created = ingest(store, signal(revision=revision))
    base = public_base()
    with monkeypatch.context() as patch:
        patch.setattr(admission, "_claim_for_signal", base._claim_for_signal)
        patch.setattr(admission, "_base_binding_from_snapshot", base._base_binding_from_snapshot)
        patch.setattr(admission_index, "SCHEMA_VERSION", 3)
        assert store.prepare_admission_index().complete
        old, error = admission.binding_for_candidate(store, created["candidate_id"])
        assert error is None
        result = run_subconscious_advisory(store, advisory_output={
            "action": settle.upper(), "rationale":"Synthetic settled source", "event_ids":[],
            "candidate_ids":[created["candidate_id"]]}, admission_binding=old,
            enabled=True, dry_run=False)
        assert result["action"] == settle
        assert admission.build_admission_plan(store)["selection"] is None
    before = canonical(store)
    assert store.prepare_admission_index().complete
    if revision is None:
        assert admission.build_admission_plan(store)["selection"] is None
    else:
        # Schema-3 recorded an unversioned key, not historical r1 proof.
        assert admission.build_admission_plan(store)["selection"]["revision_key"] != old["revision_key"]
    assert canonical(store) == before
    AdmissionIndex(store).path.unlink()
    if revision is None:
        assert admission.build_admission_plan(store)["selection"] is None
    else:
        assert admission.build_admission_plan(store)["selection"]["revision_key"] != old["revision_key"]
    assert ingest(store, signal("same-revision", revision))["duplicate"]
    assert canonical(store) == before
    updated = ingest(store, signal("fresh-r2", "r2"))
    assert updated["coalesced"] and updated["candidate_id"] == created["candidate_id"]
    assert store.prepare_admission_index().complete
    selected = admission.build_admission_plan(store)["selection"]
    assert selected and selected["admission_key"] != old["admission_key"]
    assert selected["revision_key"] != old["revision_key"]

@pytest.mark.parametrize("family", ["research", "frontier", "memory", "transition"])
def test_legitimate_projected_cacheless_and_sql_reads(tmp_path, family):
    store = store_at(tmp_path)
    row = signal()
    if family == "frontier":
        row["artifact_meta"] = {"entry_id": "synthetic-entry", "sha256": "r1"}
    elif family == "memory":
        row.pop("artifact_meta")
        row.update(sensor="sensorium.memory_reflection", source="memory",
                   memory_provenance={"provider":"synthetic", "bank_id":"fixture", "item_ids":["a"]})
    elif family == "transition":
        row.pop("artifact_meta")
        row.update(kind="synthetic_pressure", metric_family="fixture", source_revision="1")
    created = ingest(store, row)
    snap = raw_snapshot(store)
    expected, error = admission._base_binding_from_snapshot(store, snap["candidates"][0], snap)
    assert error is None
    projected = store.read_admission_snapshot()
    assert projected.complete
    assert "artifact_meta" not in projected.snapshot["signals"][0]
    got, error = admission._base_binding_from_snapshot(store, projected.snapshot["candidates"][0], projected.snapshot)
    assert got == expected and error is None
    # JSON roundtrips erase structural trust; serialized reserved keys cannot
    # confer projected-read authority on a raw producer or event join.
    serialized = json.loads(json.dumps(projected.snapshot))
    raw, error = admission._base_binding_from_snapshot(store, serialized["candidates"][0], serialized)
    assert error is None and raw["identity_mode"] == "legacy"
    assert admission.event_source_item_identity(store, projected.snapshot["events"][0],
        signals=serialized["signals"]) is None
    full_expected, error = admission._binding_from_snapshot(store, snap["candidates"][0], snap)
    assert error is None
    full_projected, error = admission._binding_from_snapshot(
        store, projected.snapshot["candidates"][0], projected.snapshot)
    assert error is None and full_projected == full_expected
    assert store.prepare_admission_index().complete
    assert admission.binding_for_candidate(store, created["candidate_id"]) == (full_expected, None)


def test_settled_successor_r1_injected_old_claim_does_not_hide_fresh_r2(tmp_path):
    store = store_at(tmp_path)
    created = ingest(store, signal())
    old, error = admission.binding_for_candidate(store, created["candidate_id"])
    assert error is None
    result = run_subconscious_advisory(store, advisory_output={
        "action":"DROP", "rationale":"Synthetic settled r1", "event_ids":[],
        "candidate_ids":[created["candidate_id"]]}, admission_binding=old,
        enabled=True, dry_run=False)
    assert result["action"] == "drop"
    assert admission.build_admission_plan(store)["selection"] is None
    row = signal("fresh-r2", "r2")
    row["_admission_claim"] = admission._claim_for_signal(store.instance, signal())[0]
    updated = ingest(store, row)
    assert not updated["duplicate"] and updated["coalesced"]
    assert updated["candidate_id"] == created["candidate_id"]
    assert store.prepare_admission_index().complete
    new = admission.build_admission_plan(store)["selection"]
    assert new and new["admission_key"] != old["admission_key"]
    assert new["revision_key"] != old["revision_key"]


@pytest.mark.parametrize("payload", ["error", "foreign_claim"])
def test_matching_raw_revision_replays_despite_reserved_payload(tmp_path, monkeypatch, payload):
    from agent_sensorium import tools
    store = store_at(tmp_path)
    original = ingest(store, signal())
    row = signal("replay-wrapper")
    if payload == "error":
        row["_admission_error"] = "fake-cached-error"
    else:
        row["_admission_claim"] = admission._claim_for_signal(store.instance, signal(item="foreign"))[0]
    def forbidden(*args, **kwargs):
        raise AssertionError("Replay must not decay or observe")
    monkeypatch.setattr(tools, "_apply_candidate_decay", forbidden)
    monkeypatch.setattr(tools, "observe_after_success", forbidden)
    before = canonical(store)
    replay = ingest(store, row)
    assert replay["duplicate"] and replay["candidate_id"] == original["candidate_id"]
    assert canonical(store) == before
