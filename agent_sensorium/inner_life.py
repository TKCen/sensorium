"""Validated inner-life graph registry contracts.

This module is intentionally read/validation-only. It defines the closed block
and edge kinds for the graph projection/config substrate without adding runtime
behavior, live tools, dashboard mutation routes, or persistence beyond the
existing JSON sidecars.
"""

from __future__ import annotations

from enum import Enum
from typing import Iterable


class InnerLifeValidationError(ValueError):
    """Raised when inner-life config fails closed validation."""


class BlockKind(str, Enum):
    SENSOR = "sensor"
    TEMPORAL_SENSOR = "temporal_sensor"
    MEMORY_REFLECTOR = "memory_reflector"
    NORMALIZER = "normalizer"
    AGGREGATOR = "aggregator"
    DAMPENER = "dampener"
    BLOCKER = "blocker"
    GATE = "gate"
    ROUTER = "router"
    CONSCIOUSNESS_REVIEW = "consciousness_review"
    RECEIPT_WRITER = "receipt_writer"
    TENDENCY_PROPOSER = "tendency_proposer"
    DASHBOARD_PROJECTION = "dashboard_projection"


class EdgeKind(str, Enum):
    EMITS = "emits"
    PROMOTES_TO = "promotes_to"
    AGGREGATES_INTO = "aggregates_into"
    AMPLIFIES = "amplifies"
    SUPPRESSES = "suppresses"
    BLOCKS = "blocks"
    ROUTES_TO = "routes_to"
    OPENS_REVIEW = "opens_review"
    SETTLES = "settles"
    FEEDS_BACK = "feeds_back"
    PROPOSES_DELTA = "proposes_delta"
    PROJECTS_TO_DASHBOARD = "projects_to_dashboard"


class TemporalPredicateKind(str, Enum):
    RATE_WINDOW = "rate_window"
    RECURRENCE = "recurrence"
    SLOPE = "slope"
    RATIO = "ratio"
    GAP = "gap"
    CONSECUTIVE_STATE = "consecutive_state"


class LoopPolicyKind(str, Enum):
    COOLDOWN = "cooldown"
    IDEMPOTENCY = "idempotency"
    TTL = "ttl"
    RECEIPT_REQUIRED = "receipt_required"
    DAMPENER = "dampener"
    POLICY_GATE = "policy_gate"
    MAX_PASSES = "max_passes"


class DampenerModeKind(str, Enum):
    COOLDOWN = "cooldown"
    RATE_LIMIT = "rate_limit"
    DECAY = "decay"
    SUPPRESS_DUPLICATE = "suppress_duplicate"
    SAMPLE = "sample"


class BlockerReasonKind(str, Enum):
    POLICY = "policy"
    SCHEMA = "schema"
    AUTHORITY = "authority"
    PRIVACY = "privacy"
    SAFETY = "safety"


_BLOCK_KIND_VALUES = {kind.value for kind in BlockKind}
_EDGE_KIND_VALUES = {kind.value for kind in EdgeKind}
_LOOP_POLICY_KIND_VALUES = {kind.value for kind in LoopPolicyKind}
_DAMPENER_MODE_VALUES = {mode.value for mode in DampenerModeKind}
_BLOCKER_REASON_VALUES = {reason.value for reason in BlockerReasonKind}

# Closed precedence ladder per architecture section 8: blocks > gates >
# dampens/suppresses > amplifies > routes/projects. Lower rank wins.
PRECEDENCE_RANK: dict[str, int] = {
    "blocks": 0,
    "gates": 1,
    "dampens": 2,
    "suppresses": 2,
    "amplifies": 3,
    "routes": 4,
    "projects": 4,
}
_PRECEDENCE_VALUES = frozenset(PRECEDENCE_RANK)
# Edge kinds whose precedence is closed/derivable from the kind itself. An
# edge declaring a `policy.precedence` that contradicts its own kind is a
# schema violation (the ladder must stay tied to the closed edge-kind enum,
# not become a second freeform vocabulary).
_EDGE_PRECEDENCE_BY_KIND: dict[str, str] = {
    EdgeKind.BLOCKS.value: "blocks",
    EdgeKind.SUPPRESSES.value: "suppresses",
    EdgeKind.AMPLIFIES.value: "amplifies",
    EdgeKind.ROUTES_TO.value: "routes",
    EdgeKind.PROJECTS_TO_DASHBOARD.value: "projects",
}
_V1_SENSOR_STATUSES = {"active", "paused", "deprecated"}
_POLICY_KIND_KEYS = frozenset({"node_kinds", "block_kinds", "edge_kinds", "kinds"})
_LOOP_STRING_BOUND_KEYS = frozenset(
    {
        "idempotency_key",
        "dampener",
        "dampener_id",
        "policy_gate",
        "policy_gate_id",
    }
)
_LOOP_NUMERIC_BOUND_KEYS = frozenset({"cooldown_seconds", "ttl_seconds", "ttl_hours", "max_passes"})
_LOOP_BOOL_BOUND_KEYS = frozenset({"receipt_required"})
_LOOP_BOUND_KEYS = _LOOP_STRING_BOUND_KEYS | _LOOP_NUMERIC_BOUND_KEYS | _LOOP_BOOL_BOUND_KEYS
_SIGNAL_INBOX_BLOCK_ID = "signal_inbox"
_SIGNAL_EMITTING_SENSOR_KINDS = frozenset({BlockKind.SENSOR.value, BlockKind.TEMPORAL_SENSOR.value})


def _ensure_mapping(value: object, *, label: str) -> dict:
    if not isinstance(value, dict):
        raise InnerLifeValidationError(f"{label} must be an object")
    return value


def _ensure_list(value: object, *, label: str) -> list:
    if not isinstance(value, list):
        raise InnerLifeValidationError(f"{label} must be a list")
    return value


def _ensure_nonempty_string(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InnerLifeValidationError(f"{label} must be a non-empty string")
    return value.strip()


def _normalize_enabled(value: object, *, default: bool = True) -> bool:
    if value is None:
        return default
    return bool(value)


def _validate_block_kind(value: object, *, block_id: str) -> str:
    kind = _ensure_nonempty_string(value, label=f"block {block_id} type")
    if kind not in _BLOCK_KIND_VALUES:
        raise InnerLifeValidationError(f"unknown block kind for {block_id}: {kind}")
    return kind


def _validate_edge_kind(value: object, *, edge_id: str) -> str:
    kind = _ensure_nonempty_string(value, label=f"edge {edge_id} kind")
    if kind not in _EDGE_KIND_VALUES:
        raise InnerLifeValidationError(f"unknown edge kind for {edge_id}: {kind}")
    return kind


def _is_v2_registry(raw: dict) -> bool:
    return raw.get("version") == 2 or "blocks" in raw


def _v1_sensor_enabled(status: str) -> bool:
    return status == "active"


def load_block_registry_v2(raw: dict | None) -> dict:
    """Validate and normalize a registry payload into v2 block form.

    Backward compatibility: legacy v1 `sensors/registry.json` files are plain
    `{sensor_name: entry}` mappings. They load as v2 `sensor` blocks while
    preserving the legacy status/defaults used by current runtime helpers.
    """

    payload = _ensure_mapping(raw or {}, label="registry")
    if not payload:
        return {"version": 2, "blocks": {}}

    if not _is_v2_registry(payload):
        blocks: dict[str, dict] = {}
        for block_id, entry in sorted(payload.items()):
            safe_id = _ensure_nonempty_string(block_id, label="sensor id")
            entry_map = _ensure_mapping(entry, label=f"sensor {safe_id}")
            status = entry_map.get("status", "active")
            if status not in _V1_SENSOR_STATUSES:
                status = "active"
            block = {
                "id": safe_id,
                "type": BlockKind.SENSOR.value,
                "enabled": _v1_sensor_enabled(status),
                "status": status,
                "defaults": dict(entry_map.get("defaults") or {}),
            }
            blocks[safe_id] = block
        return {"version": 2, "blocks": blocks}

    if payload.get("version", 2) != 2:
        raise InnerLifeValidationError("registry version must be 2")
    raw_blocks = _ensure_mapping(payload.get("blocks", {}), label="registry.blocks")
    blocks = {}
    for block_id, entry in sorted(raw_blocks.items()):
        safe_id = _ensure_nonempty_string(block_id, label="block id")
        entry_map = _ensure_mapping(entry, label=f"block {safe_id}")
        block = dict(entry_map)
        block["id"] = safe_id
        block["type"] = _validate_block_kind(block.get("type"), block_id=safe_id)
        block["enabled"] = _normalize_enabled(block.get("enabled"), default=True)
        blocks[safe_id] = block
    return {"version": 2, "blocks": blocks}


def _has_loop_policy(edge: dict) -> bool:
    policy = edge.get("policy")
    return isinstance(policy, dict) and isinstance(policy.get("loop_policy"), dict)


def _validate_loop_policy(loop_policy: dict, *, edge_id: str) -> dict:
    policy = dict(_ensure_mapping(loop_policy, label=f"edge {edge_id} loop_policy"))
    kind = policy.get("kind")
    if kind is not None:
        kind_value = _ensure_nonempty_string(kind, label=f"edge {edge_id} loop_policy.kind")
        if kind_value not in _LOOP_POLICY_KIND_VALUES:
            raise InnerLifeValidationError(f"unknown loop_policy kind for {edge_id}: {kind_value}")
        policy["kind"] = kind_value

    for string_key in _LOOP_STRING_BOUND_KEYS:
        if string_key in policy:
            policy[string_key] = _ensure_nonempty_string(
                policy[string_key], label=f"edge {edge_id} {string_key}"
            )

    if "max_passes" in policy:
        if isinstance(policy["max_passes"], bool):
            raise InnerLifeValidationError(f"edge {edge_id} max_passes must be positive")
        try:
            max_passes = int(policy["max_passes"])
        except (TypeError, ValueError) as exc:
            raise InnerLifeValidationError(f"edge {edge_id} max_passes must be positive") from exc
        if max_passes <= 0:
            raise InnerLifeValidationError(f"edge {edge_id} max_passes must be positive")
        policy["max_passes"] = max_passes

    for seconds_key in ("cooldown_seconds", "ttl_seconds", "ttl_hours"):
        if seconds_key in policy:
            if isinstance(policy[seconds_key], bool):
                raise InnerLifeValidationError(f"edge {edge_id} {seconds_key} must be positive")
            try:
                value = float(policy[seconds_key])
            except (TypeError, ValueError) as exc:
                raise InnerLifeValidationError(f"edge {edge_id} {seconds_key} must be positive") from exc
            if value <= 0:
                raise InnerLifeValidationError(f"edge {edge_id} {seconds_key} must be positive")
            policy[seconds_key] = int(value) if value.is_integer() else value

    if "receipt_required" in policy and not isinstance(policy["receipt_required"], bool):
        raise InnerLifeValidationError(f"edge {edge_id} receipt_required must be a boolean")

    has_bound = any(key in policy for key in _LOOP_BOUND_KEYS - _LOOP_BOOL_BOUND_KEYS) or (
        policy.get("receipt_required") is True
    )
    if not has_bound:
        raise InnerLifeValidationError(f"edge {edge_id} loop_policy must declare at least one bound")
    return policy


def _validate_precedence(policy_map: dict, *, edge_id: str, edge_kind: str) -> dict:
    if "precedence" not in policy_map:
        return policy_map
    precedence = _ensure_nonempty_string(policy_map["precedence"], label=f"edge {edge_id} policy.precedence")
    if precedence not in _PRECEDENCE_VALUES:
        raise InnerLifeValidationError(
            f"edge {edge_id} policy.precedence must be one of {sorted(_PRECEDENCE_VALUES)}: {precedence}"
        )
    expected = _EDGE_PRECEDENCE_BY_KIND.get(edge_kind)
    if expected is not None and precedence != expected:
        raise InnerLifeValidationError(
            f"edge {edge_id} policy.precedence {precedence!r} contradicts edge kind "
            f"{edge_kind!r} (expected {expected!r})"
        )
    policy_map["precedence"] = precedence
    return policy_map


def _validate_edge_policy(edge: dict) -> dict:
    edge_id = edge["id"]
    policy = edge.get("policy")
    if policy is None:
        return edge
    policy_map = dict(_ensure_mapping(policy, label=f"edge {edge_id} policy"))
    if "loop_policy" in policy_map:
        policy_map["loop_policy"] = _validate_loop_policy(policy_map["loop_policy"], edge_id=edge_id)
    policy_map = _validate_precedence(policy_map, edge_id=edge_id, edge_kind=edge.get("kind"))
    edge["policy"] = policy_map
    return edge


def resolve_precedence(effects: list[dict]) -> dict:
    """Resolve competing edge/block effects on the same target by closed precedence.

    Each effect must carry a `precedence` key whose value is one of the closed
    ladder values (`blocks > gates > dampens/suppresses > amplifies >
    routes/projects`). Returns the single winning effect (lowest rank). Ties
    are broken by input order (first-listed wins) so resolution stays
    deterministic and replayable.
    """
    if not effects:
        raise InnerLifeValidationError("resolve_precedence requires at least one effect")
    best: dict | None = None
    best_rank: int | None = None
    for index, effect in enumerate(effects):
        precedence = effect.get("precedence")
        if precedence not in PRECEDENCE_RANK:
            raise InnerLifeValidationError(f"effect[{index}] has unknown precedence: {precedence!r}")
        rank = PRECEDENCE_RANK[precedence]
        if best_rank is None or rank < best_rank:
            best = effect
            best_rank = rank
    return best


def _declares_cycle_config(edge: dict) -> bool:
    policy = edge.get("policy")
    return bool(
        edge.get("cycle")
        or edge.get("creates_cycle")
        or (isinstance(policy, dict) and (policy.get("cycle") or policy.get("creates_cycle")))
    )


def _enabled_edges(edges: Iterable[dict]) -> list[dict]:
    return [edge for edge in edges if edge.get("enabled", True)]


def _registry_blocks(registry: dict) -> dict:
    blocks = registry.get("blocks") if isinstance(registry, dict) else {}
    return blocks if isinstance(blocks, dict) else {}


def _edge_items(edge_registry: dict) -> list[dict]:
    edges = edge_registry.get("edges") if isinstance(edge_registry, dict) else []
    return [edge for edge in edges if isinstance(edge, dict)] if isinstance(edges, list) else []


def _block_is_enabled(block: dict) -> bool:
    if block.get("enabled") is False:
        return False
    status = str(block.get("status") or "").strip().lower()
    return status not in {"paused", "deprecated", "disabled"}


def _block_emits_signals(block: dict) -> bool:
    """Whether a sensor block should have signal_inbox lineage.

    Sensor blocks are emitting by default. Operators can explicitly declare a
    non-emitting sensor-like block with ``emits_signals: false``; that is the
    narrow allowlist for intentional topology exceptions.
    """

    return block.get("emits_signals") is not False


def _edge_endpoint(edge: dict, primary: str, fallback: str) -> str | None:
    value = edge.get(primary)
    if value is None:
        value = edge.get(fallback)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _has_enabled_path(start: str, target: str, adjacency: dict[str, list[str]]) -> bool:
    seen: set[str] = set()
    stack = [start]
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        for next_node in adjacency.get(node, []):
            if next_node == target:
                return True
            if next_node not in seen:
                stack.append(next_node)
    return False


def signal_inbox_lineage_violations(registry: dict, edge_registry: dict) -> list[dict[str, str]]:
    """Return enabled emitting sensor blocks without a path to ``signal_inbox``.

    This is a topology completeness guard, not a runtime traversal log. It only
    reads compact registry/edge sidecars and treats ``emits_signals: false`` as
    the explicit allowlist for intentionally non-emitting sensor blocks.
    """

    blocks = _registry_blocks(registry)
    adjacency: dict[str, list[str]] = {}
    for edge in _edge_items(edge_registry):
        if edge.get("enabled", True) is False:
            continue
        source = _edge_endpoint(edge, "from", "source")
        target = _edge_endpoint(edge, "to", "target")
        if source and target:
            adjacency.setdefault(source, []).append(target)

    violations: list[dict[str, str]] = []
    for block_id, block in sorted(blocks.items(), key=lambda item: str(item[0])):
        block_map = block if isinstance(block, dict) else {}
        kind = str(block_map.get("type") or block_map.get("kind") or BlockKind.SENSOR.value).strip().lower()
        sensor_id = str(block_id).strip()
        if not sensor_id or kind not in _SIGNAL_EMITTING_SENSOR_KINDS:
            continue
        if not _block_is_enabled(block_map) or not _block_emits_signals(block_map):
            continue
        if not _has_enabled_path(sensor_id, _SIGNAL_INBOX_BLOCK_ID, adjacency):
            violations.append(
                {
                    "sensor": sensor_id,
                    "reason": "missing_signal_inbox_lineage",
                    "required_target": _SIGNAL_INBOX_BLOCK_ID,
                }
            )
    return violations


def _validate_signal_inbox_lineage(registry: dict, edge_registry: dict) -> None:
    violations = signal_inbox_lineage_violations(registry, edge_registry)
    if violations:
        sensors = ", ".join(violation["sensor"] for violation in violations[:10])
        suffix = "" if len(violations) <= 10 else f" (+{len(violations) - 10} more)"
        raise InnerLifeValidationError(
            f"enabled sensor missing signal_inbox lineage: {sensors}{suffix}"
        )


def _edge_cycle_sets(edges: list[dict]) -> list[set[str]]:
    """Return edge-id sets that participate in simple directed cycles."""

    adjacency: dict[str, list[tuple[str, str]]] = {}
    for edge in _enabled_edges(edges):
        adjacency.setdefault(edge["from"], []).append((edge["to"], edge["id"]))

    cycles: list[set[str]] = []
    for start in sorted(adjacency):
        stack: list[tuple[str, list[str], set[str]]] = [(start, [], {start})]
        while stack:
            node, path_edge_ids, seen_nodes = stack.pop()
            for next_node, edge_id in adjacency.get(node, []):
                if next_node == start:
                    cycles.append(set(path_edge_ids + [edge_id]))
                    continue
                if next_node in seen_nodes:
                    continue
                stack.append((next_node, path_edge_ids + [edge_id], seen_nodes | {next_node}))
    unique: list[set[str]] = []
    seen_keys: set[tuple[str, ...]] = set()
    for cycle in cycles:
        key = tuple(sorted(cycle))
        if key not in seen_keys:
            seen_keys.add(key)
            unique.append(cycle)
    return unique


def _validate_cycles_have_loop_policy(edges: list[dict]) -> None:
    edges_by_id = {edge["id"]: edge for edge in edges}
    for cycle in _edge_cycle_sets(edges):
        if not any(_has_loop_policy(edges_by_id[edge_id]) for edge_id in cycle):
            raise InnerLifeValidationError(
                f"cycle requires loop_policy on at least one edge: {sorted(cycle)}"
            )


def validate_edge_registry(raw: dict | None, *, known_blocks: set[str] | None = None) -> dict:
    """Validate the directional edge registry and loop-safety invariants."""

    payload = _ensure_mapping(raw or {}, label="edges registry")
    version = payload.get("version", 1)
    if version != 1:
        raise InnerLifeValidationError("edges registry version must be 1")
    raw_edges = _ensure_list(payload.get("edges", []), label="edges")
    edges: list[dict] = []
    seen_ids: set[str] = set()
    for index, raw_edge in enumerate(raw_edges):
        entry = dict(_ensure_mapping(raw_edge, label=f"edge[{index}]"))
        edge_id = _ensure_nonempty_string(entry.get("id"), label=f"edge[{index}].id")
        if edge_id in seen_ids:
            raise InnerLifeValidationError(f"duplicate edge id: {edge_id}")
        seen_ids.add(edge_id)
        source = _ensure_nonempty_string(entry.get("from"), label=f"edge {edge_id} from")
        target = _ensure_nonempty_string(entry.get("to"), label=f"edge {edge_id} to")
        if known_blocks is not None:
            if source not in known_blocks:
                raise InnerLifeValidationError(f"edge {edge_id} references unknown block: {source}")
            if target not in known_blocks:
                raise InnerLifeValidationError(f"edge {edge_id} references unknown block: {target}")
        entry["id"] = edge_id
        entry["from"] = source
        entry["to"] = target
        entry["kind"] = _validate_edge_kind(entry.get("kind"), edge_id=edge_id)
        entry["enabled"] = _normalize_enabled(entry.get("enabled"), default=True)
        entry = _validate_edge_policy(entry)
        if entry["kind"] == EdgeKind.FEEDS_BACK.value:
            if not _has_loop_policy(entry):
                raise InnerLifeValidationError(f"feeds_back edge requires loop_policy: {edge_id}")
        if _declares_cycle_config(entry) and not _has_loop_policy(entry):
            raise InnerLifeValidationError(f"cycle config requires loop_policy: {edge_id}")
        edges.append(entry)
    _validate_cycles_have_loop_policy(edges)
    return {"version": 1, "edges": edges}


def _contains_kind_injection(value: object) -> bool:
    if isinstance(value, dict):
        if _POLICY_KIND_KEYS.intersection(value):
            return True
        return any(_contains_kind_injection(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_kind_injection(item) for item in value)
    return False


def validate_policy(raw: dict | None) -> dict:
    """Validate the sidecar policy file without permitting new core kinds."""

    payload = dict(_ensure_mapping(raw or {}, label="policy"))
    version = payload.get("version", 1)
    if version != 1:
        raise InnerLifeValidationError("policy version must be 1")
    if _contains_kind_injection(payload):
        raise InnerLifeValidationError("policy must not define node or edge kinds")
    caps = payload.get("caps", {})
    if caps is not None:
        caps_map = dict(_ensure_mapping(caps, label="policy.caps"))
        for key, value in list(caps_map.items()):
            if key.startswith("max_") or key.startswith("min_"):
                try:
                    numeric = int(value)
                except (TypeError, ValueError) as exc:
                    raise InnerLifeValidationError(f"policy cap {key} must be a positive integer") from exc
                if numeric <= 0:
                    raise InnerLifeValidationError(f"policy cap {key} must be a positive integer")
                caps_map[key] = numeric
        payload["caps"] = caps_map
    disabled_groups = payload.get("disabled_groups")
    if disabled_groups is not None:
        groups = _ensure_list(disabled_groups, label="policy.disabled_groups")
        for group in groups:
            _ensure_nonempty_string(group, label="policy.disabled_groups item")
    payload["version"] = 1
    return payload


def load_inner_life_config(store) -> dict:
    """Read and validate registry/edge/policy sidecars from a SensoriumStore."""

    registry = load_block_registry_v2(store.read_sensor_registry())
    edges = validate_edge_registry(
        store.read_sensor_edges(),
        known_blocks=set(registry["blocks"]),
    )
    _validate_signal_inbox_lineage(registry, edges)
    policy = validate_policy(store.read_sensor_policy())
    return {"registry": registry, "edges": edges, "policy": policy}
