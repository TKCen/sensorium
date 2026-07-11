"""Bounded read-only World Model projection for the Sensorium dashboard.

Sensorium only brokers the versioned provider contract here. The configured
provider remains the owner of accepted content, ranking, provenance, and page
validation; this module applies dashboard-specific input, surface, and payload
bounds without persisting a corpus or registering a tool.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import re
from pathlib import Path
from typing import Any

from .world_model_provider import WorldModelProviderAdapter

MAX_WORLD_MODEL_QUERY_LENGTH = 256
MAX_WORLD_MODEL_RESULTS = 20
MAX_WORLD_MODEL_NODES = 25
MAX_WORLD_MODEL_EDGES = 50
MAX_WORLD_MODEL_RESPONSE_BYTES = 256 * 1024
_OBJECT_ID_RE = re.compile(r"kb:v1:canonical:canonical_page:[0-9a-f]{20}\Z")
_FORBIDDEN_PROJECTION_MARKERS = (
    "hindsight", "lcm", "session-search", "profile-memory", "raw transcript",
    "raw log", "skill body", "sensorium candidate", "sensorium thread",
    "sensorium artifact", "state_dir", "api_key", "password", "secret",
)


class WorldModelDashboardUnavailable(ValueError):
    """A compact non-diagnostic availability failure for dashboard callers."""


def _response_bytes(value: dict[str, Any]) -> int:
    return len(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def _safe_projection(value: Any) -> bool:
    """Reject operational/private corpus markers outside selected canonical Markdown."""
    if isinstance(value, dict):
        return all(_safe_projection(item) for item in value.values())
    if isinstance(value, list):
        return all(_safe_projection(item) for item in value)
    if isinstance(value, str):
        return not any(marker in value.lower() for marker in _FORBIDDEN_PROJECTION_MARKERS)
    return True


class WorldModelDashboardService:
    """Validate a read-only provider response before exposing it to the dashboard."""

    def __init__(self, adapter: WorldModelProviderAdapter, *, timeout_ms: int) -> None:
        self._adapter = adapter
        self._timeout_ms = timeout_ms

    def _validate_request(self, request: dict[str, Any]) -> None:
        operation = request.get("operation")
        if operation == "search":
            query, limit = request.get("query"), request.get("limit")
            if (
                not isinstance(query, str)
                or not query.strip()
                or len(query) > MAX_WORLD_MODEL_QUERY_LENGTH
                or not isinstance(limit, int)
                or isinstance(limit, bool)
                or not 1 <= limit <= MAX_WORLD_MODEL_RESULTS
            ):
                raise ValueError("world_model_request_rejected")
            return
        object_id = request.get("object_id")
        if not isinstance(object_id, str) or not _OBJECT_ID_RE.fullmatch(object_id):
            raise ValueError("world_model_request_rejected")
        if operation == "relations":
            hops = request.get("hops")
            if not isinstance(hops, int) or isinstance(hops, bool) or not 1 <= hops <= 2:
                raise ValueError("world_model_request_rejected")

    @staticmethod
    def _validate_envelope(response: dict[str, Any], operation: str) -> None:
        corpus = response.get("corpus")
        data = response.get("data")
        if (
            response.get("accepted_knowledge") != "accepted-slice1-only"
            or response.get("privacy_label") != "private"
            or response.get("degradation_state") not in {"available", "degraded"}
            or not isinstance(corpus, dict)
            or set(corpus) != {"source_build_name", "bundle_name", "source_current_digest"}
            or not isinstance(data, dict)
        ):
            raise ValueError("world_model_response_rejected")
        if operation == "search":
            results = data.get("results")
            if not isinstance(results, list) or len(results) > MAX_WORLD_MODEL_RESULTS or not _safe_projection(results):
                raise ValueError("world_model_response_rejected")
        elif operation == "relations":
            outgoing = data.get("outgoing")
            backlinks = data.get("backlinks")
            graph = data.get("graph")
            if not isinstance(outgoing, list) or not isinstance(backlinks, list) or not isinstance(graph, dict):
                raise ValueError("world_model_response_rejected")
            nodes, edges = graph.get("nodes"), graph.get("edges")
            if (
                not isinstance(nodes, list)
                or not isinstance(edges, list)
                or len(nodes) > MAX_WORLD_MODEL_NODES
                or len(outgoing) + len(backlinks) + len(edges) > MAX_WORLD_MODEL_EDGES
                or not _safe_projection({"outgoing": outgoing, "backlinks": backlinks, "graph": graph, "counts": data.get("counts")})
            ):
                raise ValueError("world_model_response_rejected")
        elif operation == "trace":
            provenance = data.get("provenance")
            if not isinstance(provenance, list) or len(provenance) > 20 or not _safe_projection(provenance):
                raise ValueError("world_model_response_rejected")
        elif operation == "page":
            # Canonical Markdown is intentionally available only after opaque ID
            # selection. Metadata remains compact and has the same safe surface.
            metadata = {key: value for key, value in data.items() if key != "markdown"}
            if not isinstance(data.get("markdown"), str) or not _safe_projection(metadata):
                raise ValueError("world_model_response_rejected")

    def read(self, request: dict[str, Any]) -> dict[str, Any]:
        self._validate_request(request)
        response = self._adapter.read(dict(request))
        self._validate_envelope(response, str(request["operation"]))
        if _response_bytes(response) > MAX_WORLD_MODEL_RESPONSE_BYTES:
            raise ValueError("world_model_response_too_large")
        return response

    async def read_bounded(self, request: dict[str, Any]) -> dict[str, Any]:
        try:
            return await asyncio.wait_for(asyncio.to_thread(self.read, request), timeout=self._timeout_ms / 1000)
        except asyncio.TimeoutError as exc:
            raise WorldModelDashboardUnavailable("world_model_unavailable") from exc


def configured_world_model_service(config: dict[str, Any]) -> WorldModelDashboardService:
    """Build the local private provider only from the validated configured seam.

    The bundle path is operator configuration, never a request parameter. The
    imported provider module is constrained to the matching wiki checkout root,
    and it independently verifies its manifest/checksum authority before read.
    """
    raw = config.get("world_model_provider") if isinstance(config, dict) else None
    if not isinstance(raw, dict) or not raw.get("enabled"):
        raise WorldModelDashboardUnavailable("world_model_unavailable")
    try:
        from .world_model_provider import validate_world_model_provider_config

        provider_config = validate_world_model_provider_config(raw)
        bundle_root = Path(provider_config.bundle_root or "").resolve()
        if bundle_root.name != "s2a-world-model" or not bundle_root.is_dir():
            raise WorldModelDashboardUnavailable("world_model_unavailable")
        repo_root = bundle_root.parent.parent.resolve()
        module_path = (repo_root / "scripts" / "research_second_brain" / "world_model_read.py").resolve()
        if repo_root not in module_path.parents or not module_path.is_file():
            raise WorldModelDashboardUnavailable("world_model_unavailable")
        spec = importlib.util.spec_from_file_location("_configured_world_model_provider", module_path)
        if spec is None or spec.loader is None:
            raise WorldModelDashboardUnavailable("world_model_unavailable")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        provider = module.WorldModelReadProvider(repo_root=repo_root, bundle_root=bundle_root)
        adapter = WorldModelProviderAdapter(provider, provider_config)
        return WorldModelDashboardService(adapter, timeout_ms=provider_config.timeout_ms)
    except WorldModelDashboardUnavailable:
        raise
    except Exception as exc:
        raise WorldModelDashboardUnavailable("world_model_unavailable") from exc
