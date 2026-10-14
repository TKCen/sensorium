"""Focused compatibility controls for closed visibility metadata."""
import pytest

from agent_sensorium import tools
from agent_sensorium.config import _validate_config, visible_on_surface
from agent_sensorium.gate import promote_signal_to_event
from agent_sensorium.schemas import decode_visibility, normalize_signal, validate_ingress_visibility
from tests.test_canonical_ingress_replay import call, forbid_effects
from tests.test_core_material_replay import ingest, signal, snapshot, store_at


@pytest.mark.parametrize("bad", ["secret-invalid-value", None, {}, []])
def test_invalid_sensitivity_category_without_echo(tmp_path, monkeypatch, bad):
    store = store_at(tmp_path)
    row = {**signal(), "sensitivity": bad}
    before = snapshot(store)
    forbid_effects(store, monkeypatch)
    with pytest.raises(ValueError, match="^Invalid sensitivity$"):
        validate_ingress_visibility(row)
    for event in (False, True):
        incoming = promote_signal_to_event(signal()) if event else signal()
        incoming["sensitivity"] = bad
        result = call(store, incoming, event=event)
        assert not result["success"] and result["error"] == "Invalid sensitivity"
    assert snapshot(store) == before


@pytest.mark.parametrize("bad", ["local", {}, None, ["local", 7], [["local"]], [""]])
def test_malformed_surfaces_reject_before_effects(tmp_path, monkeypatch, bad):
    store = store_at(tmp_path)
    before = snapshot(store)
    forbid_effects(store, monkeypatch)
    row = {**signal(), "allowed_surfaces": bad}
    event = {**promote_signal_to_event(signal()), "allowed_surfaces": bad}
    assert not call(store, row)["success"]
    assert not call(store, event, event=True)["success"]
    with pytest.raises(ValueError, match="invalid_visibility_metadata"):
        tools._append_event_and_create_or_update_candidate(store, event=event, config={})
    assert snapshot(store) == before


def test_empty_surfaces_ingress_and_read_inverse(tmp_path):
    store = store_at(tmp_path)
    ingest(store, {**signal(), "allowed_surfaces": []})
    for stream in ("signals", "events", "candidates"):
        row = store.read_jsonl(stream)[0]
        assert row["allowed_surfaces"] == []
        assert not visible_on_surface(row, "local", _validate_config({}))
    event = {**promote_signal_to_event(signal()), "id": "empty-event",
             "source_signal_ids": [], "allowed_surfaces": []}
    assert call(store, event, event=True)["success"]
    internal = {**event, "id": "empty-internal", "summary": "Internal fictional event"}
    tools._append_event_and_create_or_update_candidate(store, event=internal, config={})
    assert all(row["allowed_surfaces"] == [] for row in store.read_jsonl("events"))
    assert all(row["allowed_surfaces"] == [] for row in store.read_jsonl("candidates"))


def test_empty_replay_narrows_without_observation(tmp_path, monkeypatch):
    store = store_at(tmp_path)
    row = signal()
    ingest(store, row)
    def forbidden(*args, **kwargs):
        raise AssertionError("Replay must not observe or decay")
    monkeypatch.setattr(tools, "observe_after_success", forbidden)
    monkeypatch.setattr(tools, "_apply_candidate_decay", forbidden)
    assert call(store, {**row, "allowed_surfaces": []})["data"]["duplicate"]
    for stream in ("signals", "events", "candidates"):
        assert store.read_jsonl(stream)[0]["allowed_surfaces"] == []
    frozen = snapshot(store)
    assert call(store, row)["data"]["duplicate"]
    assert snapshot(store) == frozen


def test_missing_ingress_defaults_are_not_retained_authority():
    row = {key: value for key, value in signal().items()
           if key not in {"sensitivity", "allowed_surfaces"}}
    normalized = normalize_signal(row)
    assert normalized["sensitivity"] == "private"
    assert normalized["allowed_surfaces"] == ["local"]
    assert decode_visibility(row) is None
    assert not visible_on_surface(row, "local", _validate_config({}))
    assert not visible_on_surface(normalized, "local", {})


def test_literal_custom_policy_and_empty_policy():
    row = {**signal(), "allowed_surfaces": [" custom "]}
    policy = _validate_config({"allowed_surfaces": [" custom "]})
    assert policy["allowed_surfaces"] == [" custom "]
    assert visible_on_surface(row, " custom ", policy)
    assert not visible_on_surface(row, "custom", policy)
    empty = _validate_config({"allowed_surfaces": []})
    assert empty["allowed_surfaces"] == []
    assert not visible_on_surface(signal(), "local", empty)
    malformed = _validate_config({"allowed_surfaces": "local"})
    assert malformed["_visibility_invalid"] is True
    assert not visible_on_surface(signal(), "local", malformed)


@pytest.mark.parametrize("consumer", ["status", "candidate_open", "thread_open"])
def test_duplicate_subject_withholds_full_serialized_prose(tmp_path, monkeypatch, consumer):
    import json
    from agent_sensorium import config
    from agent_sensorium.dispatcher import candidate_to_thread
    store = store_at(tmp_path)
    marker = "Fictional ambiguous visibility marker"
    candidate = {"id": "candidate-duplicate", "status": "candidate", "summary": marker,
                 "pressure": 0.9, "sensitivity": "private", "allowed_surfaces": ["local"]}
    thread = candidate_to_thread(candidate)
    stream, row = ("threads", thread) if consumer == "thread_open" else ("candidates", candidate)
    store.append_jsonl("candidates", candidate)
    store.append_jsonl("threads", thread)
    store.append_jsonl(stream, dict(row))
    monkeypatch.setattr(config, "load_instance_config", lambda **kw: (_validate_config({}), {}))
    before = {str(p.relative_to(store.root)): p.read_bytes()
              for p in store.root.rglob("*") if p.is_file()}
    kwargs = {"instance": store.instance, "state_dir": str(store.root), "surface": "local"}
    if consumer == "status":
        result = tools.handle_sensorium_status(reference_id=row["id"], **kwargs)
    elif consumer == "candidate_open":
        result = tools.handle_sensorium_candidate_open(candidate_id=row["id"], **kwargs)
    else:
        result = tools.handle_sensorium_thread_open(thread_id=row["id"], **kwargs)
    assert marker not in json.dumps(json.loads(result))
    assert {str(p.relative_to(store.root)): p.read_bytes()
            for p in store.root.rglob("*") if p.is_file()} == before


def _consumer_rows(marker, *, standalone=False):
    from tests.test_conscious_aperture import _candidate
    from agent_sensorium.dispatcher import candidate_to_thread
    candidate = _candidate("candidate-visible")
    candidate.update(summary=marker)
    if standalone:
        candidate["kind"] = "creative_pull"
    else:
        candidate["source_candidate_ids"] = ["consumer-source"]
    candidate["conscious_task"].update(title=marker, why=marker)
    thread = candidate_to_thread(candidate)
    thread["id"] = "thread-visible"
    return candidate, thread


def _retain_consumer_source(store, candidate):
    """Retain a separate inert synthetic canonical owner for derived fixtures."""
    if candidate.get("kind") == "subconscious_advisory":
        store.append_jsonl("candidates", {
            "id": "consumer-source", "kind": "design_decision", "status": "archived",
            "pressure": 0, "summary": "Synthetic canonical consumer source",
            "sensitivity": candidate.get("sensitivity", "private"),
            "allowed_surfaces": candidate.get("allowed_surfaces", ["local"]),
        })


def _consumer_snapshot(store):
    return {str(p.relative_to(store.root)): p.read_bytes()
            for p in store.root.rglob("*") if p.is_file()}


def _consumer_outputs(store, surface="local"):
    import json
    from agent_sensorium.attention import build_attention_inbox
    from agent_sensorium.conscious_aperture import open_conscious_aperture
    from agent_sensorium.pointers import select_attention_pointer, _saved_residue_candidates
    from agent_sensorium.volunteer_cards import build_volunteer_cards
    from agent_sensorium.config import load_instance_config
    policy, _ = load_instance_config(state_dir=str(store.root))
    kw = {"instance": store.instance, "state_dir": str(store.root), "surface": surface}
    outputs = {
        "status": json.loads(tools.handle_sensorium_status(**kw)),
        "exact_candidate": json.loads(tools.handle_sensorium_status(reference_id="candidate-visible", **kw)),
        "exact_thread": json.loads(tools.handle_sensorium_status(reference_id="thread-visible", **kw)),
        "candidate_open": json.loads(tools.handle_sensorium_candidate_open(candidate_id="candidate-visible", **kw)),
        "candidate_latest": json.loads(tools.handle_sensorium_candidate_open(candidate_id="latest", **kw)),
        "thread_open": json.loads(tools.handle_sensorium_thread_open(thread_id="thread-visible", **kw)),
        "thread_latest": json.loads(tools.handle_sensorium_thread_open(**kw)),
        "attention": build_attention_inbox(store, surface=surface, include_ongoing_threads=True),
        "cards": build_volunteer_cards(store, surface=surface, instance_config=policy),
        "pointer": select_attention_pointer(store, surface=surface, instance_config=policy),
        "saved": _saved_residue_candidates(store, surface, policy),
    }
    # None is the private internal lease-selection default, not a disclosure
    # request. Explicit surface tests exercise the read-only preview seam.
    if surface is not None:
        outputs["aperture"] = open_conscious_aperture(
            store, surface=surface, instance_config=policy, dry_run=True,
            now="2026-06-07T12:00:00Z")
    return outputs


def _prepare_consumers(store, monkeypatch, policy):
    from agent_sensorium import config, admission, pointers, conscious_aperture
    monkeypatch.setattr(config, "load_instance_config", lambda **kw: (policy, {}))
    # Modules with direct imports need the same synthetic operator policy.
    for module in (pointers, conscious_aperture):
        monkeypatch.setattr(module, "load_instance_config", lambda **kw: (policy, {}))
    import agent_sensorium.attention as attention
    import agent_sensorium.volunteer_cards as cards
    for module in (attention, cards):
        monkeypatch.setattr(module, "load_instance_config", lambda **kw: (policy, {}))
    with store.candidate_transaction():
        pass
    def forbidden(*args, **kwargs):
        raise AssertionError("Disclosure must not prepare admission, decay, write or observe")
    for name in ("ensure_admission_index_ready",):
        if hasattr(admission, name):
            monkeypatch.setattr(admission, name, forbidden)
    forbid_effects(store, monkeypatch)
    monkeypatch.setattr(pointers, "observe_after_success", forbidden)
    monkeypatch.setattr(conscious_aperture, "observe_after_success", forbidden)
    monkeypatch.setattr(type(store), "prepare_admission_index", forbidden)


_RETAINED_NEGATIVES = [
    ("sensitivity", None), ("sensitivity", {}), ("sensitivity", []),
    ("sensitivity", "unknown"), ("sensitivity", False), ("sensitivity", "missing"),
    ("allowed_surfaces", None), ("allowed_surfaces", {}), ("allowed_surfaces", "local"),
    ("allowed_surfaces", ["local", 1]), ("allowed_surfaces", [["local"]]),
    ("allowed_surfaces", [""]), ("allowed_surfaces", []), ("allowed_surfaces", "missing"),
    ("id", None), ("id", {}), ("id", []), ("id", ""), ("id", "missing"),
]


@pytest.mark.parametrize("stream", ["candidates", "threads"])
@pytest.mark.parametrize("field,value", _RETAINED_NEGATIVES)
def test_all_read_consumers_retained_shape_denial(tmp_path, monkeypatch, stream, field, value):
    import json
    store = store_at(tmp_path)
    marker = "Fictional retained boundary marker"
    candidate, thread = _consumer_rows(marker if stream == "candidates" else "Visible parent")
    if stream == "threads":
        thread["conscious_task"].update(title=marker, why=marker)
        thread["continuity_summary"] = [marker]
    target = candidate if stream == "candidates" else thread
    if value == "missing":
        target.pop(field)
    else:
        target[field] = value
    _retain_consumer_source(store, candidate)
    store.append_jsonl("candidates", candidate)
    store.append_jsonl("threads", thread)
    _prepare_consumers(store, monkeypatch, _validate_config({}))
    before = _consumer_snapshot(store)
    for name, output in _consumer_outputs(store).items():
        assert marker not in json.dumps(output), name
    assert _consumer_snapshot(store) == before


@pytest.mark.parametrize("stream", ["candidates", "threads"])
@pytest.mark.parametrize("terminal", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("saved", [False, True])
def test_full_stream_duplicate_permutations_all_consumers(tmp_path, monkeypatch, stream, terminal, reverse, saved):
    import copy
    import json
    store = store_at(tmp_path)
    marker = "Fictional full stream ambiguity marker"
    candidate, thread = _consumer_rows(marker if stream == "candidates" else "Visible parent")
    if stream == "threads":
        thread["conscious_task"].update(title=marker, why=marker)
        thread["continuity_summary"] = [marker]
    if saved:
        candidate.update(status="archived", kanban_settlement={"decision": "SAVE", "intake_task_id": "intake-fiction"})
    target = candidate if stream == "candidates" else thread
    duplicate = copy.deepcopy(target)
    if terminal:
        duplicate["status"] = "archived"
    rows = [target, duplicate]
    if reverse:
        rows.reverse()
    _retain_consumer_source(store, candidate)
    for name, row in [("candidates", candidate), ("threads", thread)]:
        for retained in rows if name == stream else [row]:
            store.append_jsonl(name, retained)
    store.append_jsonl("decisions", {"type": "thread.updated", "thread_id": thread["id"], "reason": marker})
    store.write_state({"last_dispatch_result": {"candidate_id": candidate["id"] if stream == "candidates" else None,
                                               "thread_id": thread["id"], "reason": marker}})
    _prepare_consumers(store, monkeypatch, _validate_config({}))
    before = _consumer_snapshot(store)
    for name, output in _consumer_outputs(store).items():
        assert marker not in json.dumps(output), name
    assert _consumer_snapshot(store) == before


@pytest.mark.parametrize("field,value", [
    ("max_sensitivity", None), ("max_sensitivity", {}), ("max_sensitivity", []),
    ("max_sensitivity", "unknown"), ("max_sensitivity", "missing"),
    ("allowed_surfaces", None), ("allowed_surfaces", {}), ("allowed_surfaces", "local"),
    ("allowed_surfaces", ["local", False]), ("allowed_surfaces", []), ("allowed_surfaces", "missing"),
])
def test_policy_shape_denies_all_consumers(tmp_path, monkeypatch, field, value):
    import json
    store = store_at(tmp_path)
    marker = "Fictional malformed policy marker"
    candidate, thread = _consumer_rows(marker)
    _retain_consumer_source(store, candidate)
    store.append_jsonl("candidates", candidate)
    store.append_jsonl("threads", thread)
    policy = _validate_config({})
    if value == "missing":
        policy.pop(field)
    else:
        policy[field] = value
    _prepare_consumers(store, monkeypatch, policy)
    before = _consumer_snapshot(store)
    for name, output in _consumer_outputs(store).items():
        assert marker not in json.dumps(output), name
    assert _consumer_snapshot(store) == before


@pytest.mark.parametrize("surface", [None, False, 0, [], {}, ["local"]])
def test_invalid_falsy_request_never_becomes_local(tmp_path, monkeypatch, surface):
    import json
    store = store_at(tmp_path)
    marker = "Fictional invalid request marker"
    candidate, thread = _consumer_rows(marker)
    _retain_consumer_source(store, candidate)
    store.append_jsonl("candidates", candidate)
    store.append_jsonl("threads", thread)
    _prepare_consumers(store, monkeypatch, _validate_config({}))
    before = _consumer_snapshot(store)
    for name, output in _consumer_outputs(store, surface).items():
        assert marker not in json.dumps(output), name
    assert _consumer_snapshot(store) == before


@pytest.mark.parametrize("link", ["candidate_id", "origin_candidate_id", "thread_id", "source_candidate_ids",
                                  "admission_binding", "nested_binding", "source_refs", "metadata"])
@pytest.mark.parametrize("state", ["hidden", "missing", "ambiguous", "historical"])
def test_nested_current_and_historical_links_all_consumers(tmp_path, monkeypatch, link, state):
    import json
    store = store_at(tmp_path)
    marker = "Fictional canonical linked marker"
    candidate, thread = _consumer_rows(marker)
    source = {"id": "source-subject", "status": "archived", "summary": "Source metadata",
              "sensitivity": "private", "allowed_surfaces": [] if state == "hidden" else ["local"]}
    if link == "thread_id":
        source.update(conscious_task={"title": "Source metadata"})
    stream = "threads" if link == "thread_id" else "candidates"
    if state != "missing":
        store.append_jsonl(stream, source)
    if state == "ambiguous":
        store.append_jsonl(stream, dict(source))
    identifier = source["id"]
    if link == "source_candidate_ids":
        declared = {link: [identifier]}
    elif link in {"admission_binding", "nested_binding"}:
        declared = {"admission_binding": {"source_candidate_id": identifier}}
        if link == "nested_binding":
            declared = {"advisory_meta": declared}
    elif link in {"source_refs", "metadata"}:
        declared = {link: {"candidate_id": identifier}}
    else:
        declared = {link: identifier}
    candidate.update(declared)
    _retain_consumer_source(store, candidate)
    store.append_jsonl("candidates", candidate)
    store.append_jsonl("threads", thread)
    receipt = {"type": "subconscious.advisory", "output_action": "SAVE", "reason": marker, **declared}
    store.append_jsonl("decisions", receipt)
    store.write_state({"last_dispatch_result": receipt})
    _prepare_consumers(store, monkeypatch, _validate_config({}))
    before = _consumer_snapshot(store)
    outputs = _consumer_outputs(store)
    for name, output in outputs.items():
        if state == "historical" and name not in {"saved"}:
            assert marker in json.dumps(output), name
        else:
            assert marker not in json.dumps(output), name
    assert _consumer_snapshot(store) == before


@pytest.mark.parametrize("sensitivity", ["local_only", "private", "public_safe"])
@pytest.mark.parametrize("maximum", ["local_only", "private", "public_safe"])
@pytest.mark.parametrize("surface,configured", [("local", True), ("custom-room", True), ("custom-room", False)])
def test_closed_rank_and_custom_positive_inverses(tmp_path, monkeypatch, sensitivity, maximum, surface, configured):
    import json
    from agent_sensorium.schemas import SENSITIVITY_RANK
    store = store_at(tmp_path)
    marker = "Fictional closed rank marker"
    candidate, thread = _consumer_rows(marker)
    for row in (candidate, thread):
        row.update(sensitivity=sensitivity, allowed_surfaces=[surface])
    _retain_consumer_source(store, candidate)
    store.append_jsonl("candidates", candidate)
    store.append_jsonl("threads", thread)
    policy = {"allowed_surfaces": [surface] if configured else ["local"], "max_sensitivity": maximum}
    _prepare_consumers(store, monkeypatch, policy)
    before = _consumer_snapshot(store)
    visible = configured and SENSITIVITY_RANK[sensitivity] <= SENSITIVITY_RANK[maximum]
    for name, output in _consumer_outputs(store, surface).items():
        assert (marker in json.dumps(output)) == (visible and name != "saved"), name
    assert _consumer_snapshot(store) == before


def test_real_default_and_custom_ingress_with_read_inverse(tmp_path, monkeypatch):
    from agent_sensorium import config
    import json
    store = store_at(tmp_path)
    default = signal()
    default.pop("sensitivity")
    default.pop("allowed_surfaces")
    assert call(store, default)["success"]
    custom = {**signal("custom-signal", item="custom"), "allowed_surfaces": ["custom-room"]}
    result = call(store, custom)
    assert result["success"]
    monkeypatch.setattr(config, "load_instance_config", lambda **kw: (_validate_config({"allowed_surfaces": ["custom-room"]}), {}))
    opened = json.loads(tools.handle_sensorium_candidate_open(candidate_id=result["data"]["candidate_id"],
                        surface="custom-room", instance=store.instance, state_dir=str(store.root)))
    assert opened["success"]


def test_cycles_own_identity_and_exact_missing_do_not_fallback(tmp_path, monkeypatch):
    import json
    from agent_sensorium.config import DisclosureVisibility
    store = store_at(tmp_path)
    candidate, thread = _consumer_rows("Fictional own identity marker", standalone=True)
    candidate["candidate_id"] = candidate["id"]
    policy = _validate_config({})
    assert DisclosureVisibility([candidate], [thread], "local", policy).subject("candidate", candidate["id"])
    cyclic = {**candidate, "source_candidate_ids": [candidate["id"]]}
    assert not DisclosureVisibility([cyclic], [thread], "local", policy).subject("candidate", candidate["id"])
    store.append_jsonl("candidates", candidate)
    store.append_jsonl("threads", thread)
    _prepare_consumers(store, monkeypatch, policy)
    before = _consumer_snapshot(store)
    result = json.loads(tools.handle_sensorium_candidate_open(candidate_id="missing", instance=store.instance,
                        state_dir=str(store.root)))
    assert not result["success"] and result["data"] is None
    assert _consumer_snapshot(store) == before


@pytest.mark.parametrize("state", ["valid_saved", "self_cycle", "pair_cycle", "bad_nested_binding"])
def test_saved_positive_and_actual_cycle_consumers(tmp_path, monkeypatch, state):
    import json
    store = store_at(tmp_path)
    marker = "Fictional saved cycle boundary marker"
    candidate, thread = _consumer_rows(marker, standalone=state != "valid_saved")
    candidate.update(status="archived", kanban_settlement={"decision": "SAVE", "intake_task_id": "intake-fiction"})
    if state == "self_cycle":
        candidate["source_candidate_ids"] = [candidate["id"]]
    elif state == "pair_cycle":
        candidate["source_candidate_ids"] = ["cycle-peer"]
        peer = {**candidate, "id": "cycle-peer", "source_candidate_ids": [candidate["id"]]}
        store.append_jsonl("candidates", peer)
    elif state == "bad_nested_binding":
        candidate["advisory_meta"] = {"admission_binding": {"source_candidate_id": []}}
    _retain_consumer_source(store, candidate)
    store.append_jsonl("candidates", candidate)
    store.append_jsonl("threads", thread)
    _prepare_consumers(store, monkeypatch, _validate_config({}))
    before = _consumer_snapshot(store)
    outputs = _consumer_outputs(store)
    for name, output in outputs.items():
        if state == "valid_saved" and name not in {"aperture"}:
            assert marker in json.dumps(output), name
        else:
            assert marker not in json.dumps(output), name
    assert _consumer_snapshot(store) == before
