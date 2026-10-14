"""Synthetic canonical foundation; no ingress or disclosure-policy specifications."""
import json
import sqlite3

import pytest

from agent_sensorium import admission
from agent_sensorium.admission_index import AdmissionIndex
from agent_sensorium.canonical_records import lookup_record, record_index
from tests.test_core_cache_injection import canonical, raw_snapshot
from tests.test_core_material_replay import signal, store_at


def material_rows(stream, identical=False):
    signals = [signal("s1", "r1"), signal("s2", "r2")]
    events = [{"id": "e1", "source_signal_ids": ["s1"]},
              {"id": "e2", "source_signal_ids": ["s2"]}]
    candidates = [{"id": "c", "status": "candidate", "kind": "creative_pull",
                   "event_ids": ["e1"]}]
    if stream == "signals":
        signals[1]["id"] = "s1"
        events = [events[0]]
    elif stream == "events":
        events[1]["id"] = "e1"
    else:
        candidates.append({**candidates[0], "event_ids": ["e2"]})
    rows = {"signals": signals, "events": events, "candidates": candidates, "decisions": []}
    if identical:
        rows[stream][1] = dict(rows[stream][0])
    return rows


def read_binding(store, mode):
    if mode == "raw":
        snap = raw_snapshot(store)
        candidate = lookup_record(record_index(snap["candidates"]), "c").row
        return ((None, "source_candidate_unavailable") if candidate is None
                else admission._binding_from_snapshot(store, candidate, snap))
    if mode == "cacheless":
        assert not AdmissionIndex(store).path.exists()
        assert store.read_admission_snapshot().complete
    return admission.binding_for_candidate(store, "c")


@pytest.mark.parametrize("stream", ["signals", "events", "candidates"])
@pytest.mark.parametrize("mode", ["raw", "cacheless", "incremental", "rewrite_generation"])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("identical", [False, True])
def test_canonical_duplicate_ids_cannot_select_material(tmp_path, stream, mode, reverse, identical):
    store = store_at(tmp_path)
    rows = material_rows(stream, identical)
    if reverse:
        rows[stream].reverse()
    duplicates = list(rows[stream])
    if mode == "incremental":
        rows[stream] = duplicates[:1]
    for name, values in rows.items():
        for row in values:
            store.append_jsonl(name, row)
    if mode not in {"raw", "cacheless"}:
        assert store.prepare_admission_index().complete
        if mode == "incremental":
            store.append_jsonl(stream, duplicates[1])
            assert admission.binding_for_candidate(store, "c")[0] is None
        else:
            store.rewrite_jsonl(stream, list(reversed(duplicates)))
            with sqlite3.connect(AdmissionIndex(store).path) as conn:
                conn.execute("PRAGMA user_version=5")
            assert admission.binding_for_candidate(store, "c")[0] is None
        assert store.prepare_admission_index().complete
    before = canonical(store)
    binding, error = read_binding(store, mode)
    assert canonical(store) == before
    assert binding is None or binding["identity_mode"] != "source"


@pytest.mark.parametrize("mode", ["raw", "cacheless", "incremental"])
def test_distinct_event_successive_revision_allowed_inverse(tmp_path, mode):
    store = store_at(tmp_path)
    for row in [signal("s1", "r1"), signal("s2", "r2")]:
        store.append_jsonl("signals", row)
    for row in [{"id": "e1", "source_signal_ids": ["s1"]},
                {"id": "e2", "source_signal_ids": ["s2"]}]:
        store.append_jsonl("events", row)
    store.append_jsonl("candidates", {"id": "c", "status": "candidate", "event_ids": ["e2", "e1"]})
    if mode == "incremental":
        assert store.prepare_admission_index().complete
    before = canonical(store)
    binding, error = read_binding(store, mode)
    assert error is None
    assert binding["revision_key"] == admission._claim_for_signal(
        store.instance, signal("s2", "r2"))[0]["revision_key"]
    assert canonical(store) == before


@pytest.mark.parametrize("stream", ["signals", "events", "candidates"])
@pytest.mark.parametrize("reverse", [False, True])
def test_late_third_duplicate_and_rewrite_uniqueness_recovery(tmp_path, stream, reverse):
    store = store_at(tmp_path)
    rows = material_rows(stream)
    if reverse:
        rows[stream].reverse()
    first, second = rows[stream]
    for name, values in rows.items():
        for row in (values[:1] if name == stream else values):
            store.append_jsonl(name, row)
    assert store.prepare_admission_index().complete
    admitted, error = admission.binding_for_candidate(store, "c")
    assert error is None and admitted["identity_mode"] == "source"
    for duplicate in [second, first, second]:
        store.append_jsonl(stream, duplicate)
        assert admission.binding_for_candidate(store, "c")[0] is None
        assert store.prepare_admission_index().complete
        before = canonical(store)
        current, _ = admission.binding_for_candidate(store, "c")
        assert current is None or current["identity_mode"] != "source"
        assert canonical(store) == before
    store.rewrite_jsonl(stream, [first])
    assert admission.binding_for_candidate(store, "c")[0] is None
    assert store.prepare_admission_index().complete
    assert admission.binding_for_candidate(store, "c") == (admitted, None)


@pytest.mark.parametrize("variant,kind", [("source", "source"), ("legacy", "legacy"),
    ("invalid", "malformed"), ("conflict", "malformed"), ("injected", "legacy"),
    ("memory", "source"), ("telemetry", "source")])
def test_typed_canonical_claim_classification(variant, kind):
    row = signal()
    if variant in {"legacy", "injected"}:
        row.pop("artifact_meta")
        row.pop("source_revision")
    if variant == "injected":
        row.update(_admission_claim=admission._claim_for_signal("core", signal())[0],
                   _admission_error="forged", _admission_ambiguous=True)
    if variant == "invalid":
        row["source_revision"] = None
    if variant == "conflict":
        row["artifact_meta"]["revision_id"] = "other"
    if variant in {"memory", "telemetry"}:
        row.pop("artifact_meta")
        if variant == "memory":
            row.update(sensor="sensorium.memory_reflection", source="memory",
                       memory_provenance={"provider": "fixture", "bank_id": "bank",
                                          "item_ids": ["b", "a"]})
        else:
            row.update(kind="fixture_pressure", metric_family="metric", source_revision="1")
    got = admission.classify_signal_claim("core", row)
    assert got.kind == kind
    assert admission.equivalent_source_claims(got, got) is (kind == "source")
    if variant == "telemetry":
        assert got.evidence_class == "telemetry"
    if variant == "memory":
        assert len(got.member_keys) == 2
        permuted = {**row, "memory_provenance": {**row["memory_provenance"], "item_ids": ["a", "b"]}}
        assert admission.classify_signal_claim("core", permuted) == got
        replaced = {**row, "memory_provenance": {**row["memory_provenance"], "item_ids": ["a", "c"]}}
        assert not admission.equivalent_source_claims(got, admission.classify_signal_claim("core", replaced))
    projected = {"signals": [row], "events": [], "candidates": [], "decisions": []}
    assert admission._snapshot_claim("core", row, json.loads(json.dumps(projected))) == admission._claim_for_signal("core", row)


def test_literal_producer_components_and_lookup_identifiers():
    row = signal(" wrapper ", " r1 ", " item ")
    row["artifact_meta"]["source_id"] = " feed "
    before = json.dumps(row, sort_keys=True)
    claim = admission.classify_signal_claim("core", row)
    assert claim.kind == "source"
    assert claim != admission.classify_signal_claim("core", signal("wrapper", "r1", "item"))
    assert json.dumps(row, sort_keys=True) == before
    index = record_index([{"id": " c "}, {"id": "c"}])
    assert lookup_record(index, " c ").row == {"id": " c "}
    assert lookup_record(index, "c").row == {"id": "c"}


@pytest.mark.parametrize("identifier", [None, [], {}, 3, True, ""])
def test_record_local_lookup_is_total_and_never_normalizes(identifier):
    index = record_index([{"id": identifier}, {"id": "3"}, {"id": "same"}, {"id": "same"}])
    assert lookup_record(index, identifier).state == "missing"
    assert lookup_record(index, "3").state == "unique"
    assert lookup_record(index, "same").state == "ambiguous"
    assert lookup_record(index, "missing").state == "missing"


def foundation_seed(tmp_path, source=True):
    store = store_at(tmp_path)
    row = signal("s", "r1")
    if not source:
        row.pop("artifact_meta")
        row.pop("source_revision")
    store.append_jsonl("signals", row)
    store.append_jsonl("events", {"id": "e", "source_signal_ids": ["s"]})
    store.append_jsonl("candidates", {
        "id": "c", "kind": "creative_pull", "status": "candidate", "event_ids": ["e"]
    })
    return store


def foundation_finish(store):
    for _ in range(20):
        result = store.prepare_admission_index()
        if result.complete:
            return result
    raise AssertionError(str(result))


@pytest.mark.parametrize("bound", [False, True])
def test_duplicate_advisory_removes_disposition(tmp_path, bound):
    store = foundation_seed(tmp_path, source=bound)
    binding, _ = admission.binding_for_candidate(store, "c")
    advisory = {"id": "adv", "kind": "subconscious_advisory", "status": "held",
                "source_candidate_ids": ["c"]}
    if bound:
        advisory["admission_binding"] = binding
    store.append_jsonl("candidates", advisory)
    foundation_finish(store)
    assert admission.build_admission_plan(store)["selection"] is None
    for _ in range(2):
        store.append_jsonl("candidates", dict(advisory))
        foundation_finish(store)
        before = canonical(store)
        raw = admission._prior_for_binding(store, binding, raw_snapshot(store))
        assert raw[1] is False
        assert admission.build_admission_plan(store)["selection"] is not None
        assert canonical(store) == before
    store.rewrite_jsonl("candidates", [store.read_jsonl("candidates")[0], advisory])
    foundation_finish(store)
    assert admission.build_admission_plan(store)["selection"] is None


@pytest.mark.parametrize("size", [20, 40])
def test_normal_cacheless_cost(tmp_path, monkeypatch, size):
    store = foundation_seed(tmp_path)
    row = store.read_jsonl("candidates")[0]
    store.rewrite_jsonl("candidates", [{**row, "id": f"c{i}"} for i in range(size)])
    real = admission.record_index
    counts = {"calls": 0, "rows": 0}

    def counted(rows, **kwargs):
        counts["calls"] += 1
        counts["rows"] += len(rows)
        return real(rows, **kwargs)

    monkeypatch.setattr(admission, "record_index", counted)
    before = canonical(store)
    plan = admission.build_admission_plan(store, candidate_limit=1)
    assert plan["eligible_count"] == size and canonical(store) == before
    assert 0 < counts["calls"] <= 3
    assert 0 < counts["rows"] <= size + 2


@pytest.mark.parametrize("field", ["source_signal_ids", "event_ids"])
@pytest.mark.parametrize("invalid", [3, {}, [], None])
def test_malformed_join_raw_cacheless_agree(tmp_path, field, invalid):
    store = foundation_seed(tmp_path)
    stream, identifier, valid = (("events", "e", "s") if field == "source_signal_ids"
                                  else ("candidates", "c", "e"))
    row = store.read_jsonl(stream)[0]
    store.rewrite_jsonl(stream, [{**row, "id": identifier, field: [valid, invalid]}])
    before = canonical(store)
    raw, error = admission._binding_from_snapshot(
        store, store.read_jsonl("candidates")[0], raw_snapshot(store))
    cached, cached_error = admission.binding_for_candidate(store, "c")
    assert error is cached_error is None
    assert raw["identity_mode"] == cached["identity_mode"] == "legacy"
    assert raw == cached
    foundation_finish(store)
    assert admission.binding_for_candidate(store, "c") == (raw, None)
    assert canonical(store) == before


@pytest.mark.parametrize("refs", [["c", 3], ["c", {}], ["c", "other"], "c"])
def test_malformed_legacy_refs_do_not_gain_exact_settlement(tmp_path, refs):
    store = foundation_seed(tmp_path, source=False)
    binding, _ = admission.binding_for_candidate(store, "c")
    store.append_jsonl("candidates", {"id": "adv", "kind": "subconscious_advisory",
                                     "status": "held", "source_candidate_ids": refs})
    assert not admission._prior_for_binding(store, binding, raw_snapshot(store))[1]
    assert admission.build_admission_plan(store)["selection"] == binding
    foundation_finish(store)
    assert admission.build_admission_plan(store)["selection"] == binding


@pytest.mark.parametrize("mode", ["cacheless", "indexed"])
@pytest.mark.parametrize("transition", ["rewrite", "distinct"])
def test_source_revision_settlement_and_terminal_closure(tmp_path, mode, transition):
    store = foundation_seed(tmp_path)
    old, _ = admission.binding_for_candidate(store, "c")
    assert old["identity_mode"] == "source"
    store.append_jsonl("candidates", {"id": "adv", "kind": "subconscious_advisory",
                                     "status": "held", "admission_binding": old})
    if mode == "indexed":
        foundation_finish(store)
    candidate_bytes = store.paths["candidates"].read_bytes()
    store.append_jsonl("signals", signal("s2", "r2"))
    if transition == "rewrite":
        store.rewrite_jsonl("events", [{"id": "e", "source_signal_ids": ["s2"]}])
        assert store.paths["candidates"].read_bytes() == candidate_bytes
    else:
        store.append_jsonl("events", {"id": "e2", "source_signal_ids": ["s2"]})
        rows = store.read_jsonl("candidates")
        rows[0]["event_ids"] = ["e", "e2"]
        store.rewrite_jsonl("candidates", rows)
    if mode == "indexed":
        foundation_finish(store)
    current = admission.build_admission_plan(store)["selection"]
    assert current["identity_mode"] == "source"
    assert current["item_key"] == old["item_key"]
    assert current["revision_key"] != old["revision_key"]
    for binding in (old, current):
        store.append_jsonl("decisions", {"id": "receipt", "dry_run": False,
            "action": "drop", "output_action": "DROP", "admission_binding": binding})
        if mode == "indexed":
            foundation_finish(store)
        plan = admission.build_admission_plan(store)
        assert plan["selection"] == (current if binding == old else None)
    store.rewrite_jsonl("decisions", [])
    rows = store.read_jsonl("candidates")
    rows[1]["status"] = "archived"
    store.rewrite_jsonl("candidates", rows)
    if mode == "indexed":
        foundation_finish(store)
    before = canonical(store)
    plan = admission.build_admission_plan(store)
    assert plan["selection"] is None
    assert plan["suppressed_counts"] == {"explicit_item_closure": 1}
    assert canonical(store) == before


@pytest.mark.parametrize("stream", ["decisions", "candidates"])
@pytest.mark.parametrize("field", ["member_keys", "selected_member_keys"])
@pytest.mark.parametrize("invalid", [3, {}])
def test_malformed_member_refs_supply_no_settlement(tmp_path, stream, field, invalid):
    store = foundation_seed(tmp_path)
    row = store.read_jsonl("signals")[0]
    row.pop("artifact_meta")
    row.pop("source_revision")
    row.update(sensor="sensorium.memory_reflection", source="memory",
               memory_provenance={"provider": "fixture", "bank_id": "bank", "item_ids": ["a"]})
    store.rewrite_jsonl("signals", [row])
    binding, error = admission.binding_for_candidate(store, "c")
    assert error is None and binding is not None
    malformed = {**binding, field: [*binding[field], invalid]}
    store.append_jsonl(stream, {"id": "adv", "kind": "subconscious_advisory", "status": "held",
        "dry_run": False, "action": "drop", "output_action": "DROP", "admission_binding": malformed})
    before = canonical(store)
    raw = admission._binding_from_snapshot(store, store.read_jsonl("candidates")[0], raw_snapshot(store))
    assert raw == (binding, None)
    assert admission.build_admission_plan(store)["selection"] == binding
    foundation_finish(store)
    assert admission.build_admission_plan(store)["selection"] == binding
    assert canonical(store) == before


@pytest.mark.parametrize("invalid", [3, {}])
def test_binding_shape_rejects_untyped_members_without_raising(tmp_path, invalid):
    store = foundation_seed(tmp_path)
    row = store.read_jsonl("signals")[0]
    row.pop("artifact_meta")
    row.pop("source_revision")
    row.update(sensor="sensorium.memory_reflection", source="memory",
               memory_provenance={"provider": "fixture", "bank_id": "bank", "item_ids": ["a"]})
    store.rewrite_jsonl("signals", [row])
    binding, error = admission.binding_for_candidate(store, "c")
    assert error is None and binding is not None
    assert admission._binding_shape({**binding, "selected_member_keys": [*binding["selected_member_keys"], invalid]}) is False


@pytest.mark.parametrize("invalid", [3, {}])
def test_indexed_context_is_total_for_nonstring_event_refs(tmp_path, invalid):
    store = foundation_seed(tmp_path)
    row = store.read_jsonl("candidates")[0]
    store.rewrite_jsonl("candidates", [{**row, "event_ids": ["e", invalid]}])
    foundation_finish(store)
    before = canonical(store)
    result = store.read_admission_context("c")
    assert result.complete and result.state == "ready"
    assert [event["id"] for event in result.value["events"]] == ["e"]
    assert canonical(store) == before


@pytest.mark.parametrize("stream", ["decisions", "candidates"])
@pytest.mark.parametrize("field", ["member_keys", "selected_member_keys"])
@pytest.mark.parametrize("invalid", [3, {}])
def test_ordinary_source_malformed_present_members_no_settlement(tmp_path, stream, field, invalid):
    store = foundation_seed(tmp_path)
    binding, error = admission.binding_for_candidate(store, "c")
    assert error is None and binding["identity_mode"] == "source"
    assert "member_keys" not in binding
    malformed = {**binding, field: [invalid]}
    store.append_jsonl(stream, {"id":"adv", "kind":"subconscious_advisory", "status":"held",
        "dry_run":False, "action":"drop", "output_action":"DROP", "admission_binding":malformed})
    before = canonical(store)
    raw = admission._prior_for_binding(store, binding, raw_snapshot(store))
    cacheless = admission.build_admission_plan(store)
    foundation_finish(store)
    indexed = admission.build_admission_plan(store)
    assert canonical(store) == before
    print({"stream":stream,"field":field,"invalid":invalid,"raw_decided":raw[1],
           "cacheless_selection":cacheless["selection"],"indexed_selection":indexed["selection"]})
    assert raw[1] is False
    assert cacheless["selection"] == indexed["selection"] == binding


@pytest.mark.parametrize("authority", ["receipt", "held", "nested_held", "terminal", "nested_terminal"])
@pytest.mark.parametrize("source", [False, True])
@pytest.mark.parametrize("historical", [False, True])
@pytest.mark.parametrize("field", [None, "member_keys", "selected_member_keys"])
def test_ordinary_prior_member_validation_and_absent_inverse(
    tmp_path, authority, source, historical, field,
):
    store = foundation_seed(tmp_path, source=source)
    binding, error = admission.binding_for_candidate(store, "c")
    assert error is None and "member_keys" not in binding
    prior = dict(binding)
    if historical:
        prior["policy_version"] = "source-admission-v1"
    terminal = "terminal" in authority
    if terminal:
        prior["admission_key"] = "old-admission"
        prior["revision_key"] = "old-revision"
    if field is not None:
        prior[field] = [3]
    row = {"id": "adv", "kind": "subconscious_advisory",
           "status": "archived" if terminal else "held", "source_candidate_ids": ["c"],
           "dry_run": False, "action": "drop", "output_action": "DROP"}
    if authority.startswith("nested_"):
        row["advisory_meta"] = {"admission_binding": prior}
    else:
        row["admission_binding"] = prior
    store.append_jsonl("decisions" if authority == "receipt" else "candidates", row)
    before = canonical(store)
    raw = admission._prior_for_binding(store, binding, raw_snapshot(store))
    cacheless = admission.build_admission_plan(store)
    foundation_finish(store)
    indexed = admission.build_admission_plan(store)
    assert canonical(store) == before
    valid = field is None
    assert raw[1] is valid
    assert bool(raw[0]) is valid
    expected_reason = ("explicit_item_closure" if terminal else "prior_disposition") if valid else None
    assert raw[2] == expected_reason
    assert cacheless["selection"] == indexed["selection"] == (None if valid else binding)


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("field", [None, "member_keys", "selected_member_keys"])
def test_legacy_exact_fallback_rejects_present_members_but_keeps_absent_inverse(
    tmp_path, nested, field,
):
    store = foundation_seed(tmp_path, source=False)
    binding, error = admission.binding_for_candidate(store, "c")
    assert error is None and binding["identity_mode"] == "legacy"
    # A genuine unbound advisory is the historical legacy-exact inverse.
    prior = {**binding, "item_key": "other-item", "admission_key": "other-admission"}
    if field is not None:
        prior[field] = [{}]
    row = {"id": "adv", "kind": "subconscious_advisory", "status": "held",
           "source_candidate_ids": ["c"],
           "source_candidate_fingerprint": binding["source_candidate_fingerprint"]}
    if field is not None:
        if nested:
            row["advisory_meta"] = {"admission_binding": prior}
        else:
            row["admission_binding"] = prior
    store.append_jsonl("candidates", row)
    before = canonical(store)
    raw = admission._prior_for_binding(store, binding, raw_snapshot(store))
    cacheless = admission.build_admission_plan(store)
    foundation_finish(store)
    indexed = admission.build_admission_plan(store)
    assert canonical(store) == before
    valid = field is None
    assert raw[1] is valid
    assert bool(raw[0]) is valid
    assert raw[2] == ("legacy_exact_source_disposition" if valid else None)
    assert cacheless["selection"] == indexed["selection"] == (None if valid else binding)


@pytest.mark.parametrize('field', ['member_keys','selected_member_keys'])
@pytest.mark.parametrize('source,nested,invalid', [(False,False,None),(False,True,3),(False,False,{}),(False,True,['a'*64,3]),(True,False,None),(True,True,3),(True,False,{}),(True,True,['a'*64,3])])
def test_present_member_shape_before_every_authority(tmp_path, field, source, nested, invalid):
    store=foundation_seed(tmp_path,source=source)
    binding,error=admission.binding_for_candidate(store,'c')
    assert error is None
    prior={**binding,field:invalid}
    row={'id':'adv','kind':'subconscious_advisory','status':'archived','source_candidate_ids':['c'],'source_candidate_fingerprint':binding['source_candidate_fingerprint']}
    if nested:
        row['advisory_meta']={'admission_binding':prior}
    else:
        row['admission_binding']=prior
    store.append_jsonl('candidates',row)
    before=canonical(store)
    raw=admission._prior_for_binding(store,binding,raw_snapshot(store))
    cacheless=admission.build_admission_plan(store)
    foundation_finish(store)
    indexed=admission.build_admission_plan(store)
    assert canonical(store)==before
    print(json.dumps({'probe':'shape','source':source,'nested':nested,'invalid':invalid,'field':field,'raw':raw,'cacheless':cacheless['selection'],'indexed':indexed['selection'],'binding':binding}),flush=True)
    assert raw[0]==[] and raw[1] is False and raw[2] is None
    assert cacheless['selection']==indexed['selection']==binding


@pytest.mark.parametrize("field", ["member_keys", "selected_member_keys"])
@pytest.mark.parametrize("value,valid", [
    (None, False), (3, False), ({"untrusted": "payload"}, False),
    ("untrusted payload", False), (True, False), (["a" * 64, 3], False),
    (["b" * 64, "a" * 64], False), (["a" * 64, "a" * 64], False),
    ([], True), (["a" * 64, "b" * 64], True),
])
def test_binding_projection_preserves_member_shape(field, value, valid):
    from agent_sensorium.admission_index import _binding_projection, _decision_projection

    raw = {"item_key": "item", field: value}
    before = json.dumps(raw, sort_keys=True)
    projected = _binding_projection(raw)
    assert isinstance(projected, dict) and field in projected
    assert projected[field] == (value if isinstance(value, list) else None)
    assert admission._member_reference_shape(projected) is valid
    assert _decision_projection({"admission_binding": raw})["admission_binding"] == projected
    assert json.dumps(raw, sort_keys=True) == before
    assert _binding_projection({"item_key": "item"}) == {"item_key": "item"}


@pytest.mark.parametrize("source", [False, True])
@pytest.mark.parametrize("case", ["receipt_bool", "advisory_string", "invalid_top", "absent_top"])
def test_projection_precedence_and_authority_parity(tmp_path, source, case):
    store = foundation_seed(tmp_path, source=source)
    binding, error = admission.binding_for_candidate(store, "c")
    assert error is None and binding is not None
    historical = {**binding, "policy_version": "source-admission-v1"}
    row = {"id": "adv", "kind": "subconscious_advisory", "status": "archived",
           "source_candidate_ids": ["c"], "dry_run": False, "action": "drop",
           "output_action": "DROP"}
    if case == "receipt_bool":
        row["admission_binding"] = {**binding, "selected_member_keys": True}
    elif case == "advisory_string":
        row["admission_binding"] = {**binding, "member_keys": "untrusted payload"}
    else:
        row["advisory_meta"] = {"admission_binding": historical}
        if case == "invalid_top":
            row["admission_binding"] = {**binding, "member_keys": {"untrusted": "payload"}}
    store.append_jsonl("decisions" if case == "receipt_bool" else "candidates", row)
    before = canonical(store)
    raw = admission._prior_for_binding(store, binding, raw_snapshot(store))
    cacheless = admission.build_admission_plan(store)
    foundation_finish(store)
    indexed = admission.build_admission_plan(store)
    valid = case == "absent_top"
    assert raw[1] is valid and bool(raw[0]) is valid
    assert raw[2] == ("prior_disposition" if valid else None)
    assert cacheless["selection"] == indexed["selection"] == (None if valid else binding)
    assert canonical(store) == before



def test_predecessor_generation_ready_reads_refuse_until_bounded_rebuild(tmp_path):
    from agent_sensorium.admission_index import SCHEMA_VERSION

    store = foundation_seed(tmp_path)
    binding, error = admission.binding_for_candidate(store, "c")
    assert error is None and binding is not None
    foundation_finish(store)
    index = AdmissionIndex(store)
    with sqlite3.connect(index.path) as conn:
        conn.execute("PRAGMA user_version=6")
    before = canonical(store)
    cache_before = index.path.read_bytes()
    for read in [lambda: index.read_plan(candidate_limit=50),
                 lambda: index.read_binding("c"), lambda: index.read_dispositions(binding),
                 lambda: index.read_context("c"), index.read_snapshot]:
        result = read()
        assert not result.complete and result.state == "rebuilding"
        assert result.reason == "admission_claim_generation_changed"
    assert index.path.read_bytes() == cache_before
    first = store.prepare_admission_index(scan_bytes=256)
    assert not first.complete and first.bytes_consumed <= 256
    foundation_finish(store)
    assert SCHEMA_VERSION == 7
    assert admission.build_admission_plan(store)["selection"] == binding
    assert canonical(store) == before
