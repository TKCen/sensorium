"""Read-only World Model dashboard vertical contract tests."""

import asyncio
import importlib.util
import json
import os
from pathlib import Path

import pytest

from agent_sensorium.world_model_dashboard import (
    MAX_WORLD_MODEL_RESPONSE_BYTES,
    WorldModelDashboardService,
)
from agent_sensorium.world_model_provider import (
    FixtureWorldModelProvider,
    WORLD_MODEL_PROTOCOL_VERSION,
    WorldModelProviderAdapter,
    validate_world_model_provider_config,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
WIKI_ROOT = Path(os.environ.get("S2A_WIKI_ROOT", REPO_ROOT.parents[5] / "wiki"))


def _envelope(operation, data, *, privacy="private"):
    return {
        "contract_version": WORLD_MODEL_PROTOCOL_VERSION,
        "accepted_knowledge": "accepted-slice1-only",
        "corpus": {
            "source_build_name": "fixture",
            "bundle_name": "fixture",
            "source_current_digest": "a" * 64,
        },
        "privacy_label": privacy,
        "degradation_state": "available",
        "receipt": {"operation": operation, "elapsed_ms": 1, "budget_ms": 300},
        "data": data,
    }


def _service(responses):
    adapter = WorldModelProviderAdapter(
        FixtureWorldModelProvider(responses),
        validate_world_model_provider_config({"enabled": True, "timeout_ms": 50}),
    )
    return WorldModelDashboardService(adapter, timeout_ms=50)


def _load_dashboard(monkeypatch, state_dir: Path):
    monkeypatch.setenv("SENSORIUM_STATE_DIR", str(state_dir))
    path = REPO_ROOT / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("sensorium_world_model_dashboard_test", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_service_rejects_unbounded_queries_results_and_private_operational_projection():
    service = _service({"search": _envelope("search", {"results": []})})
    for request in (
        {"operation": "search", "query": "x" * 257, "limit": 1},
        {"operation": "search", "query": "known", "limit": 21},
        {"operation": "search", "query": "known", "limit": True},
    ):
        with pytest.raises(ValueError, match="world_model_request_rejected"):
            service.read(request)

    hostile = _service({"search": _envelope("search", {"results": [{"title": "raw transcript leak"}]})})
    with pytest.raises(ValueError, match="world_model_response_rejected"):
        hostile.read({"operation": "search", "query": "known", "limit": 1})


def test_service_enforces_contract_graph_and_response_caps():
    object_id = "kb:v1:canonical:canonical_page:0123456789abcdef0123"
    too_many = [{"source": object_id, "target": object_id, "relation": "links_to"}] * 51
    relations = _service({"relations": _envelope("relations", {
        "outgoing": too_many,
        "backlinks": [],
        "graph": {"nodes": [{"object_id": object_id}] * 26, "edges": [], "caps": {"nodes": 25, "edges": 50}},
        "counts": {},
    })})
    with pytest.raises(ValueError, match="world_model_response_rejected"):
        relations.read({"operation": "relations", "object_id": object_id, "hops": 1})

    oversized = _service({"page": _envelope("page", {"markdown": "x" * MAX_WORLD_MODEL_RESPONSE_BYTES})})
    with pytest.raises(ValueError, match="world_model_response_too_large"):
        oversized.read({"operation": "page", "object_id": object_id})


def test_dashboard_routes_are_read_only_and_fail_closed_without_enabled_provider(tmp_path, monkeypatch):
    mod = _load_dashboard(monkeypatch, tmp_path / "sensorium" / "demo")
    routes = {route.path: set(route.methods) for route in mod.router.routes if hasattr(route, "methods")}
    expected = {
        "/world-model/search",
        "/world-model/pages/{object_id}",
        "/world-model/pages/{object_id}/relations",
        "/world-model/pages/{object_id}/trace",
    }
    assert expected <= set(routes)
    assert all(methods <= {"GET", "HEAD"} for methods in routes.values())
    assert not any(method in {"POST", "PUT", "PATCH", "DELETE"} for methods in routes.values() for method in methods)

    before = sorted((path, path.stat().st_mtime_ns) for path in tmp_path.rglob("*"))
    result = asyncio.run(mod.world_model_search(query="known", instance="demo"))
    after = sorted((path, path.stat().st_mtime_ns) for path in tmp_path.rglob("*"))
    assert result == {"ok": False, "degradation_state": "unavailable", "error": "world_model_unavailable"}
    assert after == before


def test_dashboard_known_page_is_lazy_and_wiki_owned_without_store_mutation(tmp_path, monkeypatch):
    if not (WIKI_ROOT / ".git").exists():
        pytest.skip("requires the mission's fresh wiki clone via S2A_WIKI_ROOT")
    module_path = WIKI_ROOT / "scripts" / "research_second_brain" / "world_model_read.py"
    spec = importlib.util.spec_from_file_location("world_model_read_dashboard_test", module_path)
    assert spec and spec.loader
    wiki_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wiki_module)
    provider = wiki_module.WorldModelReadProvider(repo_root=WIKI_ROOT, bundle_root=WIKI_ROOT / "frozen" / "s2a-world-model")
    search = provider.search(query="canonical", limit=1)
    assert search["data"]["results"]
    object_id = search["data"]["results"][0]["object_id"]

    state_root = tmp_path / "sensorium" / "demo"
    state_root.mkdir(parents=True)
    (state_root / "instance.config.json").write_text(json.dumps({
        "world_model_provider": {
            "enabled": True,
            "bundle_root": str(WIKI_ROOT / "frozen" / "s2a-world-model"),
            "timeout_ms": 300,
        },
    }))
    wiki_mtimes = {path: path.stat().st_mtime_ns for path in WIKI_ROOT.rglob("*") if path.is_file()}
    state_mtimes = {path: path.stat().st_mtime_ns for path in state_root.rglob("*") if path.is_file()}
    mod = _load_dashboard(monkeypatch, state_root)

    found = asyncio.run(mod.world_model_search(query="canonical", limit=1, instance="demo"))
    assert found["ok"] is True
    assert found["accepted_knowledge"] == "accepted-slice1-only"
    assert "markdown" not in json.dumps(found["data"])
    selected = asyncio.run(mod.world_model_page(object_id=object_id, instance="demo"))
    assert selected["ok"] is True
    assert selected["data"]["object_id"] == object_id
    assert selected["data"]["markdown"]
    relations = asyncio.run(mod.world_model_relations(object_id=object_id, hops=2, instance="demo"))
    assert relations["ok"] is True
    assert len(relations["data"]["graph"]["nodes"]) <= 25
    assert sum(len(relations["data"].get(key, [])) for key in ("outgoing", "backlinks")) + len(relations["data"]["graph"]["edges"]) <= 50
    trace = asyncio.run(mod.world_model_trace(object_id=object_id, instance="demo"))
    assert trace["ok"] is True
    payload = json.dumps([found, selected, relations, trace], sort_keys=True)
    for forbidden in ("hindsight", "lcm", "session-search", "profile-memory", "raw transcript", "skill body", "sensorium candidates"):
        assert forbidden not in payload.lower()
    assert {path: path.stat().st_mtime_ns for path in WIKI_ROOT.rglob("*") if path.is_file()} == wiki_mtimes
    assert {path: path.stat().st_mtime_ns for path in state_root.rglob("*") if path.is_file()} == state_mtimes
