"""Tests for the GET-only trace/provenance projection (sera-ck9.3)."""

import asyncio
import importlib.util
import json
from pathlib import Path


def _load_dashboard(monkeypatch, state_dir: Path):
    monkeypatch.setenv("SENSORIUM_STATE_DIR", str(state_dir))
    path = Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("sensorium_dashboard_trace_test", path)
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


def test_trace_route_is_get_only(tmp_path, monkeypatch):
    mod = _load_dashboard(monkeypatch, tmp_path / "sensorium" / "demo")

    routes = {route.path: sorted(route.methods) for route in mod.router.routes if hasattr(route, "methods")}
    assert routes["/trace"] == ["GET"]


def test_trace_topology_node_shows_configured_neighbors_and_overlay_status(tmp_path, monkeypatch):
    root = tmp_path / "sensorium" / "demo"
    _write_registry(
        root,
        blocks={
            "runtime_heartbeat": {"type": "sensor", "status": "active", "enabled": True},
            "dispatcher": {"type": "processor", "status": "active", "enabled": True},
        },
        edges=[{"from": "runtime_heartbeat", "to": "dispatcher", "kind": "configured_flow"}],
    )
    _append_jsonl(root / "signals" / "inbox.jsonl", {"id": "sig_1", "sensor": "runtime_heartbeat", "ts": "2026-06-21T10:00:00Z"})

    mod = _load_dashboard(monkeypatch, root)
    data = asyncio.run(mod.trace(node_id="sensor:runtime_heartbeat", instance="demo"))

    assert data["ok"] is True
    assert data["privacy"] == "compact_only"
    assert data["subject"]["id"] == "sensor:runtime_heartbeat"
    assert data["subject"]["type"] == "node"
    assert data["subject"]["origin"] == "topology"
    assert data["subject"]["status"] == "active"
    assert data["downstream"] == [{"id": "processor:dispatcher", "kind": "topology_node", "relation": "configured_flow"}]
    assert data["upstream"] == []
    assert data["config_refs"][0]["kind"] == "sensor_registry"
    assert data["config_refs"][0]["version"].startswith("sha256:")
    assert data["timestamps"] == {}
    assert data["meta"]["limitations"]


def test_trace_topology_edge_reports_endpoints_and_traversal_limitation(tmp_path, monkeypatch):
    root = tmp_path / "sensorium" / "demo"
    _write_registry(
        root,
        blocks={"a": {"type": "sensor"}, "b": {"type": "processor"}},
        edges=[{"from": "a", "to": "b", "kind": "configured_flow"}],
    )
    mod = _load_dashboard(monkeypatch, root)
    topo = asyncio.run(mod.topology(instance="demo"))
    edge_id = topo["edges"][0]["id"]

    data = asyncio.run(mod.trace(edge_id=edge_id, instance="demo"))

    assert data["subject"]["id"] == edge_id
    assert data["subject"]["type"] == "edge"
    assert data["subject"]["kind"] == "configured_flow"
    assert data["upstream"] == [{"id": "sensor:a", "kind": "topology_node", "relation": "configured_source"}]
    assert data["downstream"] == [{"id": "processor:b", "kind": "topology_node", "relation": "configured_target"}]
    assert any("traversal" in note for note in data["meta"]["limitations"])


def test_trace_candidate_node_shows_upstream_signals_and_downstream_action(tmp_path, monkeypatch):
    root = tmp_path / "sensorium" / "demo"
    _write_registry(root, blocks={})
    _append_jsonl(root / "signals" / "inbox.jsonl", {"id": "sig_1", "sensor": "s", "ts": "2026-06-21T09:00:00Z"})
    _append_jsonl(root / "events.jsonl", {"id": "evt_1", "kind": "spike", "source_signal_ids": ["sig_1"], "ts": "2026-06-21T09:01:00Z"})
    _append_jsonl(root / "candidates.jsonl", {
        "id": "cand_trace_1",
        "status": "candidate",
        "kind": "review",
        "event_ids": ["evt_1"],
        "created_at": "2026-06-21T09:02:00Z",
        "updated_at": "2026-06-21T09:02:00Z",
    })
    _append_jsonl(root / "thread_actions.jsonl", {
        "id": "action_trace_1", "status": "proposed", "origin_candidate_id": "cand_trace_1", "updated_at": "2026-06-21T09:03:00Z",
    })

    mod = _load_dashboard(monkeypatch, root)
    cand_node_id = "candidate:" + mod._safe_trace_candidate_id("cand_trace_1")
    data = asyncio.run(mod.trace(node_id=cand_node_id, instance="demo"))

    assert data["subject"]["kind"] == "candidate"
    assert data["subject"]["status"] == "candidate"
    upstream_kinds = {ref["kind"] for ref in data["upstream"]}
    assert upstream_kinds == {"signal", "event"}
    downstream_ids = {ref["id"] for ref in data["downstream"]}
    assert "action:action_trace_1" in downstream_ids
    assert data["timestamps"]["created_at"] == "2026-06-21T09:02:00Z"


def test_trace_edge_id_collision_safe_for_parallel_edges(tmp_path, monkeypatch):
    """Two edges with the same from/to but different kind must get distinct ids."""
    root = tmp_path / "sensorium" / "demo"
    _write_registry(
        root,
        blocks={"a": {"type": "sensor"}, "b": {"type": "processor"}},
        edges=[
            {"from": "a", "to": "b", "kind": "configured_flow"},
            {"from": "a", "to": "b", "kind": "fallback"},
            {"from": "a", "to": "b", "kind": "configured_flow"},
        ],
    )
    mod = _load_dashboard(monkeypatch, root)
    data = asyncio.run(mod.topology(instance="demo"))

    edge_ids = [edge["id"] for edge in data["edges"]]
    assert len(edge_ids) == len(set(edge_ids))
    assert any(eid.endswith("#fallback") for eid in edge_ids)
    assert any(eid.endswith("#configured_flow") for eid in edge_ids)
    assert any(eid.endswith("#configured_flow:1") for eid in edge_ids)


def test_trace_unsupported_subject_returns_empty_with_limitation(tmp_path, monkeypatch):
    root = tmp_path / "sensorium" / "demo"
    _write_registry(root, blocks={})
    mod = _load_dashboard(monkeypatch, root)

    data = asyncio.run(mod.trace(node_id="candidate:does_not_exist", instance="demo"))

    assert data["ok"] is True
    assert data["subject"] is None
    assert data["upstream"] == []
    assert data["downstream"] == []
    assert data["meta"]["limitations"]


def test_trace_missing_subject_args_returns_empty(tmp_path, monkeypatch):
    mod = _load_dashboard(monkeypatch, tmp_path / "sensorium" / "demo")

    data = asyncio.run(mod.trace(instance="demo"))

    assert data["ok"] is True
    assert data["subject"] is None
    assert data["meta"]["limitations"]


def test_trace_does_not_leak_hostile_marker(tmp_path, monkeypatch):
    root = tmp_path / "sensorium" / "demo"
    sentinel = "sk-t9-trace...4242"
    _write_registry(root, blocks={sentinel: {"type": sentinel, "label": sentinel}}, edges=[])
    _append_jsonl(root / "candidates.jsonl", {
        "id": sentinel, "status": sentinel, "summary": sentinel, "updated_at": "2026-06-21T10:00:00Z",
    })

    mod = _load_dashboard(monkeypatch, root)
    topo_data = asyncio.run(mod.topology(instance="demo"))
    topo_node_id = topo_data["nodes"][0]["id"]
    trace_data = asyncio.run(mod.trace(node_id=topo_node_id, instance="demo"))
    cand_node_id = "candidate:" + mod._safe_trace_candidate_id(sentinel)
    cand_trace_data = asyncio.run(mod.trace(node_id=cand_node_id, instance="demo"))

    payload = json.dumps(trace_data, sort_keys=True) + json.dumps(cand_trace_data, sort_keys=True)
    assert sentinel not in payload
