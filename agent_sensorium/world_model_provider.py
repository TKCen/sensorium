"""Generic, read-only adapter seam for versioned world-model providers.

This module deliberately stores no corpus and registers no runtime tool.  A
private installation may inject a local provider implementation through normal
instance configuration; Sensorium only validates the small versioned envelope.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

WORLD_MODEL_PROTOCOL_VERSION = "world-model-read-v1"
_ALLOWED_OPERATIONS = {"search", "page", "relations", "trace"}

DEFAULT_WORLD_MODEL_PROVIDER_CONFIG: dict[str, Any] = {
    "enabled": False,
    "protocol_version": WORLD_MODEL_PROTOCOL_VERSION,
    "bundle_root": None,
    "timeout_ms": 300,
    "max_search_results": 20,
}


class WorldModelProvider(Protocol):
    """Minimal injected provider contract; implementations are read-only."""

    def read(self, request: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True)
class WorldModelProviderConfig:
    enabled: bool
    protocol_version: str
    bundle_root: str | None
    timeout_ms: int
    max_search_results: int


def validate_world_model_provider_config(raw: object) -> WorldModelProviderConfig:
    """Fail closed on unknown fields and preserve no installation-specific default."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("world_model_provider_config_rejected")
    unknown = set(raw) - set(DEFAULT_WORLD_MODEL_PROVIDER_CONFIG)
    if unknown:
        raise ValueError("world_model_provider_unknown_field")
    enabled = raw.get("enabled", DEFAULT_WORLD_MODEL_PROVIDER_CONFIG["enabled"])
    version = raw.get("protocol_version", WORLD_MODEL_PROTOCOL_VERSION)
    bundle_root = raw.get("bundle_root")
    timeout_ms = raw.get("timeout_ms", DEFAULT_WORLD_MODEL_PROVIDER_CONFIG["timeout_ms"])
    max_results = raw.get("max_search_results", DEFAULT_WORLD_MODEL_PROVIDER_CONFIG["max_search_results"])
    if not isinstance(enabled, bool) or not isinstance(version, str) or version != WORLD_MODEL_PROTOCOL_VERSION:
        raise ValueError("world_model_provider_config_rejected")
    if bundle_root is not None and (not isinstance(bundle_root, str) or not bundle_root.strip()):
        raise ValueError("world_model_provider_config_rejected")
    if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) or not 1 <= timeout_ms <= 1000:
        raise ValueError("world_model_provider_config_rejected")
    if not isinstance(max_results, int) or isinstance(max_results, bool) or not 1 <= max_results <= 20:
        raise ValueError("world_model_provider_config_rejected")
    return WorldModelProviderConfig(enabled, version, bundle_root, timeout_ms, max_results)


def sanitized_world_model_provider_config(raw: object) -> dict[str, Any]:
    """Return bounded config suitable for runtime use; no provider is started."""
    config = validate_world_model_provider_config(raw)
    return {
        "enabled": config.enabled,
        "protocol_version": config.protocol_version,
        "bundle_root": config.bundle_root,
        "timeout_ms": config.timeout_ms,
        "max_search_results": config.max_search_results,
    }


class WorldModelProviderAdapter:
    """Validate requests and compact envelopes without retaining any corpus."""

    def __init__(self, provider: WorldModelProvider, config: WorldModelProviderConfig):
        self._provider = provider
        self._config = config

    def read(self, request: dict[str, Any]) -> dict[str, Any]:
        if not self._config.enabled:
            raise ValueError("world_model_provider_disabled")
        if not isinstance(request, dict) or set(request) - {"operation", "query", "limit", "object_id", "hops"}:
            raise ValueError("world_model_request_rejected")
        operation = request.get("operation")
        allowed_fields = {
            "search": {"operation", "query", "limit"},
            "page": {"operation", "object_id"},
            "relations": {"operation", "object_id", "hops"},
            "trace": {"operation", "object_id"},
        }
        if operation not in _ALLOWED_OPERATIONS or set(request) - allowed_fields[operation]:
            raise ValueError("world_model_request_rejected")
        if operation == "search":
            limit = request.get("limit", self._config.max_search_results)
            if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= self._config.max_search_results:
                raise ValueError("world_model_request_rejected")
        if operation == "relations":
            hops = request.get("hops", 1)
            if not isinstance(hops, int) or isinstance(hops, bool) or not 1 <= hops <= 2:
                raise ValueError("world_model_request_rejected")
        response = self._provider.read(dict(request))
        self._validate_envelope(response, operation)
        return response

    @staticmethod
    def _validate_envelope(response: object, operation: str) -> None:
        if not isinstance(response, dict):
            raise ValueError("world_model_response_rejected")
        required = {"contract_version", "accepted_knowledge", "corpus", "privacy_label", "degradation_state", "receipt", "data"}
        if set(response) != required or response.get("contract_version") != WORLD_MODEL_PROTOCOL_VERSION:
            raise ValueError("world_model_response_rejected")
        receipt, data = response.get("receipt"), response.get("data")
        if not isinstance(receipt, dict) or receipt.get("operation") != operation or not isinstance(data, dict):
            raise ValueError("world_model_response_rejected")


class FixtureWorldModelProvider:
    """Deterministic fixture provider for adapter tests; it does not persist data."""

    def __init__(self, responses: dict[str, dict[str, Any]]):
        self.responses = dict(responses)
        self.calls: list[dict[str, Any]] = []

    def read(self, request: dict[str, Any]) -> dict[str, Any]:
        operation = request.get("operation")
        self.calls.append(dict(request))
        if operation not in self.responses:
            raise ValueError("fixture_operation_missing")
        return dict(self.responses[operation])
