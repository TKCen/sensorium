"""Synthetic execution overlays must only narrow retained disclosure authority."""

import copy
import json

import pytest

from agent_sensorium.dispatcher import dispatch_once
from agent_sensorium.store import SensoriumStore
from agent_sensorium.tools import handle_sensorium_dispatch_once, handle_sensorium_thread_open


def _seed(tmp_path):
    store = SensoriumStore(instance="synthetic", state_dir=str(tmp_path / "instance"))
    store.ensure_dirs()
    candidate = {
        "id": "candidate-synthetic", "status": "candidate", "pressure": 0.9,
        "kind": "design_decision", "summary": "Synthetic retained source",
        "sensitivity": "private", "allowed_surfaces": ["local"],
    }
    store.append_jsonl("candidates", candidate)
    return store, candidate


def _dispatch(store, overlay, entry):
    if entry == "direct":
        return dispatch_once(store, dry_run=False, config=overlay)
    return json.loads(handle_sensorium_dispatch_once(
        instance=store.instance, state_dir=str(store.root), dry_run=False,
        config=overlay))["data"]


def _open(store, result):
    return json.loads(handle_sensorium_thread_open(
        thread_id=result.get("thread_id", "missing"), instance=store.instance,
        state_dir=str(store.root)))


@pytest.mark.parametrize("entry", ["direct", "handler"])
@pytest.mark.parametrize("retained", [
    {"allowed_surfaces": []},
    {"allowed_surfaces": ["discord"]},
    {"max_sensitivity": "local_only"},
    {"allowed_surfaces": None},
    {"max_sensitivity": "invalid"},
    "{broken",
    "[]",
])
def test_root_override_cannot_create_unopenable_thread(tmp_path, entry, retained):
    store, _ = _seed(tmp_path)
    path = store.root / "instance.config.json"
    path.write_text(retained if isinstance(retained, str) else json.dumps(retained))
    config_before = path.read_bytes()
    candidate_before = store.paths["candidates"].read_bytes()
    overlay = {"legacy_thread_dispatch_enabled": True, "allowed_surfaces": ["local"],
               "max_sensitivity": "private", "_visibility_invalid": False}
    overlay_before = copy.deepcopy(overlay)
    result = _dispatch(store, overlay, entry)
    actual = {
        "action": result["action"], "opened": _open(store, result)["success"],
        "threads": len(store.read_jsonl("threads")),
        "decisions": len(store.read_jsonl("decisions")),
        "remaining": store.read_state()["budgets"]["dispatch"]["remaining"],
    }
    assert actual == {"action": "no_candidate", "opened": False, "threads": 0,
                      "decisions": 0, "remaining": 10}
    assert store.paths["candidates"].read_bytes() == candidate_before
    assert path.read_bytes() == config_before
    assert overlay == overlay_before


@pytest.mark.parametrize("entry", ["direct", "handler"])
@pytest.mark.parametrize("restriction", [
    {"allowed_surfaces": []}, {"allowed_surfaces": ["discord"]},
    {"max_sensitivity": "local_only"},
    {"allowed_surfaces": None}, {"allowed_surfaces": "local"},
    {"allowed_surfaces": ["local", ""]}, {"allowed_surfaces": [1]},
    {"max_sensitivity": None}, {"max_sensitivity": "invalid"},
    {"_visibility_invalid": True},
])
def test_present_overlay_restrictions_never_add_authority(tmp_path, entry, restriction):
    store, _ = _seed(tmp_path)
    path = store.root / "instance.config.json"
    path.write_text(json.dumps({"allowed_surfaces": ["local", "discord"],
                               "max_sensitivity": "private"}))
    before = (path.read_bytes(), store.paths["candidates"].read_bytes())
    result = _dispatch(store, {"legacy_thread_dispatch_enabled": True, **restriction}, entry)
    assert result["action"] == "no_candidate"
    assert not _open(store, result)["success"]
    assert store.read_jsonl("threads") == []
    assert store.read_jsonl("decisions") == []
    assert store.read_state()["budgets"]["dispatch"]["remaining"] == 10
    assert (path.read_bytes(), store.paths["candidates"].read_bytes()) == before


@pytest.mark.parametrize("entry", ["direct", "handler"])
@pytest.mark.parametrize("retained", [None, {}, {"allowed_surfaces": ["local", "discord"],
                                               "max_sensitivity": "public_safe"}])
@pytest.mark.parametrize("restriction", [{}, {"allowed_surfaces": ["local"]},
                                         {"max_sensitivity": "private"}])
def test_complete_source_and_lower_pressure_remain_openable(tmp_path, entry, retained, restriction):
    store, candidate = _seed(tmp_path)
    path = store.root / "instance.config.json"
    if retained is not None:
        path.write_text(json.dumps(retained))
    config_before = path.read_bytes() if path.exists() else None
    store.append_jsonl("candidates", {**candidate, "id": "candidate-hidden",
                                      "pressure": 0.99, "allowed_surfaces": []})
    before = store.paths["candidates"].read_bytes()
    result = _dispatch(store, {"legacy_thread_dispatch_enabled": True, **restriction}, entry)
    assert result["action"] == "promoted"
    assert result["candidate_id"] == candidate["id"]
    assert _open(store, result)["success"]
    assert len(store.read_jsonl("threads")) == 1
    assert len(store.read_jsonl("decisions")) == 1
    assert store.read_state()["budgets"]["dispatch"]["remaining"] == 9
    assert store.paths["candidates"].read_bytes() == before
    assert (path.read_bytes() if path.exists() else None) == config_before
    thread = store.read_jsonl("threads")[0]
    assert thread["origin_candidate_id"] == candidate["id"]
    store.rewrite_jsonl("candidates", [{**candidate, "status": "archived"}])
    assert _open(store, result)["success"]
    store.rewrite_jsonl("candidates", [{k: v for k, v in candidate.items() if k != "sensitivity"}])
    assert not _open(store, result)["success"]
    assert store.read_jsonl("threads")[0] == thread


@pytest.mark.parametrize("control,action,remaining", [
    ({}, "legacy_dispatch_disabled", 10),
    ({"legacy_thread_dispatch_enabled": True, "thresholds": {"dispatch_pressure": 0.95}},
     "no_candidate", 10),
    ({"legacy_thread_dispatch_enabled": True, "budgets": {"dispatch": {"capacity": 0}}},
     "budget_exhausted", 0),
])
def test_ordinary_execution_controls_remain_effective(tmp_path, control, action, remaining):
    store, _ = _seed(tmp_path)
    result = _dispatch(store, control, "handler")
    assert result["action"] == action
    assert store.read_jsonl("threads") == []
    assert store.read_jsonl("decisions") == []
    assert store.read_state()["budgets"]["dispatch"]["remaining"] == remaining


def test_prospective_thread_obeys_same_narrowing_as_candidate(tmp_path):
    store, candidate = _seed(tmp_path)
    candidate["sensitivity"] = "local_only"
    store.rewrite_jsonl("candidates", [candidate])
    result = _dispatch(store, {
        "legacy_thread_dispatch_enabled": True, "max_sensitivity": "local_only",
        "operational_pointer": {"enabled": True, "sensitivity": "private"},
    }, "handler")
    assert result["action"] == "no_candidate"
    assert store.read_jsonl("threads") == []
    assert store.read_jsonl("decisions") == []
    assert store.read_state()["budgets"]["dispatch"]["remaining"] == 10
