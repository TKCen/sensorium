"""Tests for the GET-only runtime status overlay contract (sera-ck9.2)."""

import asyncio
import importlib.util
import json
import os
import time
from pathlib import Path


def _load_dashboard(monkeypatch, state_dir: Path):
    monkeypatch.setenv("SENSORIUM_STATE_DIR", str(state_dir))
    path = Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("sensorium_dashboard_runtime_status_test", path)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_registry(root: Path, blocks: dict, edges: list | None = None):
    registry_dir = root / "sensors"
    registry_dir.mkdir(parents=True, exist_ok=True)
    (registry_dir / "registry.json").write_text(json.dumps({"version": 1, "blocks": blocks}))
    (registry_dir / "edges.json").write_text(json.dumps({"edges": edges or []}))


def _append_jsonl(path: Path, row: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def test_runtime_status_route_is_get_only(tmp_path, monkeypatch):
    mod = _load_dashboard(monkeypatch, tmp_path / "sensorium" / "demo")

    routes = {route.path: sorted(route.methods) for route in mod.router.routes if hasattr(route, "methods")}
    assert routes["/runtime-status"] == ["GET"]


def test_runtime_status_configured_nodes_default_quiet_with_no_runtime_rows(tmp_path, monkeypatch):
    root = tmp_path / "sensorium" / "demo"
    _write_registry(
        root,
        blocks={
            "runtime_heartbeat": {"type": "sensor", "status": "active", "enabled": True},
            "dispatcher": {"type": "processor", "status": "active", "enabled": True},
            "conscious_aperture": {"type": "gate", "status": "degraded", "enabled": True},
        },
    )

    mod = _load_dashboard(monkeypatch, root)
    data = asyncio.run(mod.runtime_status(instance="demo"))

    assert data["ok"] is True
    assert data["privacy"] == "compact_only"
    assert data["generated_at"]
    assert data["as_of"]
    assert data["topology_config_version"].startswith("sha256:")
    assert set(data["status_vocab"]) == {
        "active", "quiet", "degraded", "error", "processing", "waiting",
        "reviewing", "blocked", "held", "settled", "stale",
    }

    nodes_by_id = {node["id"]: node for node in data["nodes"]}
    assert nodes_by_id["sensor:runtime_heartbeat"]["status"] == "quiet"
    assert nodes_by_id["processor:dispatcher"]["status"] == "quiet"
    assert nodes_by_id["gate:conscious_aperture"]["status"] == "quiet"
    for node in data["nodes"]:
        assert node["status"] in data["status_vocab"]

    assert data["edges"] == []
    assert data["meta"]["topology_node_count"] == 3
    assert data["meta"]["instance_node_count"] == 0
    assert data["meta"]["is_stale"] is False


def test_runtime_status_sensor_node_active_via_recent_signal(tmp_path, monkeypatch):
    root = tmp_path / "sensorium" / "demo"
    _write_registry(root, blocks={"runtime_heartbeat": {"type": "sensor", "status": "active"}})
    _append_jsonl(root / "signals" / "inbox.jsonl", {
        "id": "sig_1",
        "sensor": "runtime_heartbeat",
        "kind": "heartbeat",
        "ts": "2026-06-21T10:00:00Z",
    })

    mod = _load_dashboard(monkeypatch, root)
    data = asyncio.run(mod.runtime_status(instance="demo"))

    nodes_by_id = {node["id"]: node for node in data["nodes"]}
    assert nodes_by_id["sensor:runtime_heartbeat"]["status"] == "active"
    assert nodes_by_id["sensor:runtime_heartbeat"]["source"] == "signal_match"


def test_runtime_status_maps_candidate_review_held_stale_cases(tmp_path, monkeypatch):
    root = tmp_path / "sensorium" / "demo"
    _write_registry(root, blocks={})
    _append_jsonl(root / "candidates.jsonl", {
        "id": "cand_active_1", "status": "candidate", "pressure": 0.9, "updated_at": "2026-06-21T10:00:00Z",
    })
    _append_jsonl(root / "candidates.jsonl", {
        "id": "cand_reviewed_1", "status": "reviewed", "updated_at": "2026-06-21T10:01:00Z",
    })
    _append_jsonl(root / "candidates.jsonl", {
        "id": "cand_suppressed_1", "status": "suppressed", "updated_at": "2026-06-21T10:02:00Z",
    })
    _append_jsonl(root / "threads.jsonl", {
        "id": "thread_held_1", "status": "held", "updated_at": "2026-06-21T10:03:00Z",
    })
    _append_jsonl(root / "threads.jsonl", {
        "id": "thread_closed_1", "status": "closed", "updated_at": "2026-06-21T10:04:00Z",
    })
    _append_jsonl(root / "thread_actions.jsonl", {
        "id": "action_1", "status": "prepared", "updated_at": "2026-06-21T10:05:00Z",
    })
    _append_jsonl(root / "outbox.jsonl", {
        "id": "outbox_1", "status": "failed", "updated_at": "2026-06-21T10:06:00Z",
    })

    mod = _load_dashboard(monkeypatch, root)
    data = asyncio.run(mod.runtime_status(instance="demo"))

    nodes_by_id = {node["id"]: node for node in data["nodes"] if node["origin"] == "instance"}
    assert any(n["status"] == "waiting" for n in nodes_by_id.values() if n["kind"] == "candidate")
    assert any(n["status"] == "reviewing" for n in nodes_by_id.values() if n["kind"] == "candidate")
    assert any(n["status"] == "stale" for n in nodes_by_id.values() if n["kind"] == "candidate")
    thread_nodes = [n for n in nodes_by_id.values() if n["kind"] == "thread"]
    # closed thread is not an "active" instance and must not be surfaced at all.
    assert len(thread_nodes) == 1
    assert thread_nodes[0]["status"] == "held"
    assert any(n["status"] == "processing" for n in nodes_by_id.values() if n["kind"] == "action")
    assert any(n["status"] == "error" for n in nodes_by_id.values() if n["kind"] == "outbox")

    for node in data["nodes"]:
        assert node["status"] in data["status_vocab"]


def test_runtime_status_unknown_and_hostile_status_does_not_leak(tmp_path, monkeypatch):
    root = tmp_path / "sensorium" / "demo"
    sentinel = "sk-runtime-status...hostile-4242"
    _write_registry(root, blocks={"a": {"type": "sensor", "status": sentinel}})
    _append_jsonl(root / "candidates.jsonl", {
        "id": "cand_weird_1", "status": "TOTALLY_MADE_UP_STATUS", "updated_at": "2026-06-21T10:00:00Z",
    })
    _append_jsonl(root / "candidates.jsonl", {
        "id": sentinel, "status": sentinel, "updated_at": "2026-06-21T10:01:00Z",
    })

    mod = _load_dashboard(monkeypatch, root)
    data = asyncio.run(mod.runtime_status(instance="demo"))
    payload = json.dumps(data, sort_keys=True)

    assert sentinel not in payload
    for node in data["nodes"]:
        assert node["status"] in data["status_vocab"]
    # Unrecognized candidate statuses are not a known "active" lifecycle state,
    # so they are conservatively excluded from the bounded instance overlay
    # entirely rather than surfaced with a guessed status.
    assert not any(n["kind"] == "candidate" for n in data["nodes"])
    sensor_node = next(n for n in data["nodes"] if n["kind"] == "sensor")
    assert sensor_node["status"] == "quiet"


def test_runtime_status_bounded_for_many_rows(tmp_path, monkeypatch):
    root = tmp_path / "sensorium" / "demo"
    _write_registry(root, blocks={})
    for i in range(40):
        _append_jsonl(root / "candidates.jsonl", {
            "id": f"cand_many_{i}", "status": "candidate", "updated_at": f"2026-06-21T10:{i:02d}:00Z",
        })

    mod = _load_dashboard(monkeypatch, root)
    data = asyncio.run(mod.runtime_status(instance="demo"))

    candidate_nodes = [n for n in data["nodes"] if n["kind"] == "candidate"]
    assert len(candidate_nodes) == data["meta"]["instance_node_limit"]
    assert data["meta"]["truncated_candidates"] is True


def test_runtime_status_marks_stale_when_canonical_state_is_old(tmp_path, monkeypatch):
    root = tmp_path / "sensorium" / "demo"
    _write_registry(root, blocks={"runtime_heartbeat": {"type": "sensor", "status": "active"}})
    mod = _load_dashboard(monkeypatch, root)
    monkeypatch.setattr(mod, "METRICS_DIR", tmp_path / "metrics_unused")
    signals_path = root / "signals" / "inbox.jsonl"
    _append_jsonl(signals_path, {"id": "sig_old", "sensor": "other_sensor", "ts": "2020-01-01T00:00:00Z"})
    old_ts = time.time() - (48 * 60 * 60)
    os.utime(signals_path, (old_ts, old_ts))

    data = asyncio.run(mod.runtime_status(instance="demo"))

    nodes_by_id = {node["id"]: node for node in data["nodes"]}
    assert nodes_by_id["sensor:runtime_heartbeat"]["status"] == "stale"
    assert nodes_by_id["sensor:runtime_heartbeat"]["source"] == "freshness_stale"
    assert data["meta"]["is_stale"] is True


def test_runtime_status_existing_topology_route_unaffected(tmp_path, monkeypatch):
    root = tmp_path / "sensorium" / "demo"
    _write_registry(root, blocks={"runtime_heartbeat": {"type": "sensor", "status": "active"}})

    mod = _load_dashboard(monkeypatch, root)
    topology_data = asyncio.run(mod.topology(instance="demo"))
    runtime_data = asyncio.run(mod.runtime_status(instance="demo"))

    assert topology_data["config_version"] == runtime_data["topology_config_version"]
