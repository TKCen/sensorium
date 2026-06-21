"""Tests for the GET-only flow-DAG topology/config projection (sera-ck9.1)."""

import asyncio
import importlib.util
import json
from pathlib import Path


def _load_dashboard(monkeypatch, state_dir: Path):
    monkeypatch.setenv("SENSORIUM_STATE_DIR", str(state_dir))
    path = Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("sensorium_dashboard_topology_test", path)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_registry(root: Path, blocks: dict, edges: list):
    registry_dir = root / "sensors"
    registry_dir.mkdir(parents=True, exist_ok=True)
    (registry_dir / "registry.json").write_text(json.dumps({"version": 1, "blocks": blocks}))
    (registry_dir / "edges.json").write_text(json.dumps({"edges": edges}))


def test_topology_route_is_get_only(tmp_path, monkeypatch):
    mod = _load_dashboard(monkeypatch, tmp_path / "sensorium" / "demo")

    routes = {route.path: sorted(route.methods) for route in mod.router.routes if hasattr(route, "methods")}
    assert routes["/topology"] == ["GET"]


def test_topology_exposes_configured_nodes_with_zero_runtime_history(tmp_path, monkeypatch):
    root = tmp_path / "sensorium" / "demo"
    _write_registry(
        root,
        blocks={
            "runtime_heartbeat": {"type": "sensor", "status": "active", "enabled": True, "name": "Runtime Heartbeat"},
            "dispatcher": {"type": "processor", "status": "active", "enabled": True},
            "conscious_aperture": {"type": "gate", "status": "degraded", "enabled": True},
        },
        edges=[{"from": "runtime_heartbeat", "to": "dispatcher", "kind": "configured_flow"}],
    )

    mod = _load_dashboard(monkeypatch, root)
    data = asyncio.run(mod.topology(instance="demo"))

    assert data["ok"] is True
    assert data["privacy"] == "compact_only"
    assert data["generated_at"]
    assert data["as_of"]
    assert data["config_version"].startswith("sha256:")

    nodes_by_id = {node["id"]: node for node in data["nodes"]}
    assert "sensor:runtime_heartbeat" in nodes_by_id
    assert "processor:dispatcher" in nodes_by_id
    assert "gate:conscious_aperture" in nodes_by_id
    assert nodes_by_id["sensor:runtime_heartbeat"]["configured_status"] == "active"
    assert nodes_by_id["gate:conscious_aperture"]["configured_status"] == "degraded"
    assert nodes_by_id["sensor:runtime_heartbeat"]["enabled"] is True

    edge = data["edges"][0]
    assert edge["from"] == "sensor:runtime_heartbeat"
    assert edge["to"] == "processor:dispatcher"
    assert edge["kind"] == "configured_flow"

    assert data["meta"]["node_count"] == 3
    assert data["meta"]["edge_count"] == 1
    assert data["meta"]["privacy"] == "compact_only"


def test_topology_config_version_changes_with_fixture_content(tmp_path, monkeypatch):
    root = tmp_path / "sensorium" / "demo"
    _write_registry(root, blocks={"runtime_heartbeat": {"type": "sensor", "status": "active"}}, edges=[])
    mod = _load_dashboard(monkeypatch, root)
    before = asyncio.run(mod.topology(instance="demo"))["config_version"]

    _write_registry(
        root,
        blocks={"runtime_heartbeat": {"type": "sensor", "status": "active"}, "machine_body_pressure": {"type": "sensor", "status": "active"}},
        edges=[],
    )
    after = asyncio.run(mod.topology(instance="demo"))["config_version"]

    assert before != after


def test_topology_node_and_edge_kinds_and_statuses_are_closed_vocabulary(tmp_path, monkeypatch):
    root = tmp_path / "sensorium" / "demo"
    _write_registry(
        root,
        blocks={
            "weird_block": {"type": "totally_made_up_kind", "status": "totally_made_up_status"},
            "no_type_block": {},
        },
        edges=[{"from": "weird_block", "to": "no_type_block", "kind": "totally_made_up_edge_kind"}],
    )

    mod = _load_dashboard(monkeypatch, root)
    data = asyncio.run(mod.topology(instance="demo"))

    nodes_by_id = {node["id"]: node for node in data["nodes"]}
    weird = nodes_by_id["unknown:weird_block"]
    assert weird["kind"] == "unknown"
    assert weird["configured_status"] == "unknown"
    no_type = nodes_by_id["sensor:no_type_block"]
    assert no_type["kind"] == "sensor"

    edge = data["edges"][0]
    assert edge["kind"] == "configured_flow"


def test_topology_edge_status_is_closed_vocabulary_not_raw_atom(tmp_path, monkeypatch):
    """A safe-atom-shaped edge status must still map to the closed vocabulary, not leak verbatim.

    Regression: WEIRD_STATUS_SHOULD_NOT_LEAK passes the generic safe-atom regex
    (alnum/underscore), so a naive _safe_surface_atom("topology_edge_status", ...)
    call let it through unchanged even though it's not in the closed
    active|disabled|degraded|unknown vocabulary.
    """
    root = tmp_path / "sensorium" / "demo"
    sentinel = "WEIRD_STATUS_SHOULD_NOT_LEAK"
    _write_registry(
        root,
        blocks={"a": {"type": "sensor"}, "b": {"type": "processor"}},
        edges=[
            {"from": "a", "to": "b", "kind": "configured_flow", "status": sentinel},
            {"from": "b", "to": "a", "kind": "configured_flow", "status": "degraded"},
            {"from": "a", "to": "a", "kind": "configured_flow"},
        ],
    )

    mod = _load_dashboard(monkeypatch, root)
    data = asyncio.run(mod.topology(instance="demo"))
    payload = json.dumps(data, sort_keys=True)

    assert sentinel not in payload
    statuses = [edge["status"] for edge in data["edges"]]
    assert statuses[0] == "unknown"
    assert statuses[1] == "degraded"
    assert statuses[2] is None


def test_topology_does_not_leak_hostile_marker_verbatim(tmp_path, monkeypatch):
    root = tmp_path / "sensorium" / "demo"
    sentinel = "sk-t9-topology...4242"
    _write_registry(
        root,
        blocks={sentinel: {"type": sentinel, "status": sentinel, "name": sentinel}},
        edges=[{"from": sentinel, "to": sentinel, "kind": sentinel, "status": sentinel}],
    )

    mod = _load_dashboard(monkeypatch, root)
    data = asyncio.run(mod.topology(instance="demo"))
    payload = json.dumps(data, sort_keys=True)

    assert sentinel not in payload
    assert "topology_node#" in payload
