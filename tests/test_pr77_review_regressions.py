"""Fictional counterexamples for boundary, settlement, replay and join owners."""
import json
import sqlite3

import pytest

from agent_sensorium import admission, tools
from agent_sensorium.admission_index import AdmissionIndex
from agent_sensorium.config import visible_on_surface
from agent_sensorium.dispatcher import candidate_to_thread
from agent_sensorium.subconscious import run_subconscious_advisory
from tests.test_core_cache_injection import canonical, public_base, raw_snapshot
from tests.test_core_material_replay import ingest, signal, store_at


@pytest.mark.parametrize("budget", [63, 64, 65, 128])
def test_unchanged_boundary_budget_preserves_dependent_state(tmp_path, budget):
    store = store_at(tmp_path)
    created = ingest(store, signal())
    assert store.prepare_admission_index().complete
    index = AdmissionIndex(store)
    with sqlite3.connect(index.path) as conn:
        before = conn.execute("SELECT * FROM source_cursor ORDER BY stream").fetchall()
        state = conn.execute("SELECT * FROM candidate_state").fetchall()
    for _ in range(3):
        result = store.prepare_admission_index(scan_bytes=budget)
        assert result.bytes_consumed <= budget
        with sqlite3.connect(index.path) as conn:
            assert conn.execute("SELECT * FROM source_cursor ORDER BY stream").fetchall() == before
            assert conn.execute("SELECT * FROM candidate_state").fetchall() == state
    assert admission.binding_for_candidate(store, created["candidate_id"])[0]
    rows = store.read_jsonl("signals")
    rows[0]["source_revision"] = "r2"
    store.rewrite_jsonl("signals", rows)
    assert store.prepare_admission_index().complete
    assert admission.binding_for_candidate(store, created["candidate_id"])[0]["revision_key"] == admission._claim_for_signal(store.instance, rows[0])[0]["revision_key"]


@pytest.mark.parametrize("indexed", [False, True])
@pytest.mark.parametrize("revision", [None, "r1", "r2"])
@pytest.mark.parametrize("settle", ["DROP", "SAVE"])
def test_retained_id_legacy_settlement_requires_revision_proof(tmp_path, monkeypatch, indexed, revision, settle):
    store = store_at(tmp_path)
    created = ingest(store, signal())
    base = public_base()
    snap = raw_snapshot(store)
    old, error = base._base_binding_from_snapshot(store, snap["candidates"][0], snap)
    assert error is None
    with monkeypatch.context() as patch:
        patch.setattr(admission, "_claim_for_signal", base._claim_for_signal)
        patch.setattr(admission, "_base_binding_from_snapshot", base._base_binding_from_snapshot)
        result = run_subconscious_advisory(store, advisory_output={
            "action": settle, "rationale": "Fictional legacy disposition", "event_ids": [],
            "candidate_ids": [created["candidate_id"]]}, admission_binding=old,
            enabled=True, dry_run=False)
        assert result["action"] == settle.lower()
    rows = store.read_jsonl("signals")
    if revision is None:
        rows[0].pop("source_revision")
    else:
        rows[0]["source_revision"] = revision
    store.rewrite_jsonl("signals", rows)
    before = canonical(store)
    if indexed:
        assert store.prepare_admission_index().complete
    selection = admission.build_admission_plan(store)["selection"]
    if revision is None:
        assert selection is None
    else:
        # A schema-3 unversioned key never proved which explicit revision settled.
        assert selection and selection["revision_key"] != old["revision_key"]
    assert canonical(store) == before


@pytest.mark.parametrize("indexed", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("shape", ["mixed", "same", "separate", "dangling", "unknown", "duplicate"])
def test_binding_validates_each_complete_event_revision_join(tmp_path, indexed, reverse, shape):
    store = store_at(tmp_path)
    rows = [signal("sig-a"), signal("sig-b", "r1" if shape == "same" else "r2")]
    refs = [row["id"] for row in rows]
    if shape == "dangling":
        refs.append("absent")
    if shape == "unknown":
        rows[1].pop("artifact_meta")
    if shape == "duplicate":
        rows[1]["id"] = rows[0]["id"]
        refs = [rows[0]["id"]]
    if reverse:
        rows.reverse()
        refs.reverse()
    events = [{"id": "evt-a", "source_signal_ids": refs}]
    if shape == "separate":
        events = [{"id": f"evt-{i}", "source_signal_ids": [row["id"]]} for i, row in enumerate(rows)]
    candidate = {"id": "cand-fiction", "status": "candidate", "kind": "creative_pull",
                 "event_ids": [row["id"] for row in events]}
    for stream, values in {"signals": rows, "events": events, "candidates": [candidate]}.items():
        for row in values:
            store.append_jsonl(stream, row)
    if indexed:
        assert store.prepare_admission_index().complete
    binding, error = admission.binding_for_candidate(store, candidate["id"])
    if shape in {"same", "separate"}:
        assert error is None and binding["identity_mode"] == "source"
        assert binding["revision_key"] == admission._claim_for_signal(store.instance, rows[-1])[0]["revision_key"]
    else:
        assert binding is None or binding["identity_mode"] != "source"


@pytest.mark.parametrize("path", ["signal_id", "signal_fingerprint", "signal_material", "event_id", "event_fingerprint", "event_material"])
@pytest.mark.parametrize("restriction", ["sensitivity", "surface"])
def test_replay_narrows_future_candidate_derived_and_thread_visibility(tmp_path, monkeypatch, path, restriction):
    store = store_at(tmp_path)
    first = signal()
    first.update(sensitivity="public_safe", allowed_surfaces=["local", "telegram"])
    created = ingest(store, first)
    candidate = store.read_jsonl("candidates")[0]
    assert candidate["id"] == created["candidate_id"]
    derived = {**candidate, "id": "cand-derived", "event_ids": [],
               "source_candidate_ids": [candidate["id"]], "kind": "subconscious_advisory"}
    store.append_jsonl("candidates", derived)
    for source in [candidate, derived]:
        store.append_jsonl("threads", candidate_to_thread(source))
    # Delivered history is not undone by a future-disclosure restriction.
    store.append_jsonl("outbox", {"id": "ob-history", "status": "delivered", "source_candidate_ids": [candidate["id"]]})
    store.append_jsonl("decisions", {"type": "synthetic.control", "candidate_id": candidate["id"]})
    policy = {"allowed_surfaces": ["local", "telegram"], "max_sensitivity": "public_safe"}
    assert all(visible_on_surface(row, "telegram", policy) for row in store.read_jsonl("candidates"))
    before = canonical(store)
    def forbidden(*args, **kwargs):
        raise AssertionError("Replay must not add pressure or observations")
    monkeypatch.setattr(tools, "_apply_candidate_decay", forbidden)
    monkeypatch.setattr(tools, "observe_after_success", forbidden)
    incoming = dict(first if path.startswith("signal") else store.read_jsonl("events")[0])
    if not path.endswith("_id"):
        incoming["id"] = "sig-wrapper" if path.startswith("signal") else "evt-wrapper"
    if path.endswith("_material"):
        incoming["summary"] = "Fictional wrapper variation"
    if restriction == "sensitivity":
        incoming["sensitivity"] = "local_only"
    else:
        incoming["allowed_surfaces"] = ["local"]
    handler = tools.handle_sensorium_ingest_signal if path.startswith("signal") else tools.handle_sensorium_ingest_event
    keyword = "signal" if path.startswith("signal") else "event"
    result = json.loads(handler(**{keyword: incoming}, instance=store.instance, state_dir=str(store.root), config={}))
    assert result["success"] and result["data"]["duplicate"]
    for name in ["candidates", "threads"]:
        assert all(not visible_on_surface(row, "telegram", policy) for row in store.read_jsonl(name))
    after = canonical(store)
    for name in ["decisions", "outbox"]:
        assert after[name] == before[name]
    for retained in store.read_jsonl("candidates"):
        assert retained["pressure"] == candidate["pressure"]
        assert retained["event_ids"] == (candidate["event_ids"] if retained["id"] == candidate["id"] else [])
    # Equal and wider metadata cannot restore any restriction or cause writes.
    for replay in [incoming, {**incoming, "sensitivity": "public_safe", "allowed_surfaces": ["telegram", "local"]}]:
        assert json.loads(handler(**{keyword: replay}, instance=store.instance, state_dir=str(store.root), config={}))["data"]["duplicate"]
        assert canonical(store) == after
