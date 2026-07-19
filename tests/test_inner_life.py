"""Comprehensive tests for the inner-life module validation and contracts."""

import pytest

from agent_sensorium.inner_life import (
    EdgeKind,
    InnerLifeValidationError,
    _ensure_mapping,
    _ensure_list,
    _ensure_nonempty_string,
    _normalize_enabled,
    _validate_block_kind,
    _validate_edge_kind,
    _validate_loop_policy,
    _validate_precedence,
    _edge_endpoint,
    resolve_precedence,
    validate_edge_registry,
    validate_policy,
    load_block_registry_v2,
    signal_inbox_lineage_violations,
    _validate_signal_inbox_lineage,
)


def test_ensure_mapping_raises_on_invalid():
    with pytest.raises(InnerLifeValidationError, match="test_label must be an object"):
        _ensure_mapping("not a dict", label="test_label")
    assert _ensure_mapping({"key": "val"}, label="test_label") == {"key": "val"}


def test_ensure_list_raises_on_invalid():
    with pytest.raises(InnerLifeValidationError, match="test_label must be a list"):
        _ensure_list("not a list", label="test_label")
    assert _ensure_list([1, 2, 3], label="test_label") == [1, 2, 3]


def test_ensure_nonempty_string_raises_on_invalid():
    with pytest.raises(InnerLifeValidationError, match="test_label must be a non-empty string"):
        _ensure_nonempty_string("", label="test_label")
    with pytest.raises(InnerLifeValidationError, match="test_label must be a non-empty string"):
        _ensure_nonempty_string("   ", label="test_label")
    with pytest.raises(InnerLifeValidationError, match="test_label must be a non-empty string"):
        _ensure_nonempty_string(123, label="test_label")
    assert _ensure_nonempty_string("  hello  ", label="test_label") == "hello"


def test_normalize_enabled():
    assert _normalize_enabled(None, default=True) is True
    assert _normalize_enabled(None, default=False) is False
    assert _normalize_enabled(True) is True
    assert _normalize_enabled(False) is False


def test_validate_block_kind_raises_on_unknown():
    with pytest.raises(InnerLifeValidationError, match="unknown block kind for b1"):
        _validate_block_kind("invalid_kind", block_id="b1")


def test_validate_edge_kind_raises_on_unknown():
    with pytest.raises(InnerLifeValidationError, match="unknown edge kind for e1"):
        _validate_edge_kind("invalid_kind", edge_id="e1")


def test_load_block_registry_v2_v2_version_error():
    with pytest.raises(InnerLifeValidationError, match="registry version must be 2"):
        load_block_registry_v2({"version": 3, "blocks": {}})


def test_validate_loop_policy_numeric_bounds_validation():
    # max_passes is a boolean (should raise error)
    with pytest.raises(InnerLifeValidationError, match="max_passes must be positive"):
        _validate_loop_policy({"max_passes": True}, edge_id="e1")

    # max_passes is invalid type
    with pytest.raises(InnerLifeValidationError, match="max_passes must be positive"):
        _validate_loop_policy({"max_passes": "not-an-int"}, edge_id="e1")

    # max_passes is non-positive
    with pytest.raises(InnerLifeValidationError, match="max_passes must be positive"):
        _validate_loop_policy({"max_passes": 0}, edge_id="e1")

    # cooldown_seconds is boolean
    with pytest.raises(InnerLifeValidationError, match="cooldown_seconds must be positive"):
        _validate_loop_policy({"cooldown_seconds": True}, edge_id="e1")

    # cooldown_seconds is non-positive
    with pytest.raises(InnerLifeValidationError, match="cooldown_seconds must be positive"):
        _validate_loop_policy({"cooldown_seconds": -5}, edge_id="e1")

    # cooldown_seconds is string but can't be float
    with pytest.raises(InnerLifeValidationError, match="cooldown_seconds must be positive"):
        _validate_loop_policy({"cooldown_seconds": "not-a-float"}, edge_id="e1")

    # receipt_required invalid type
    with pytest.raises(InnerLifeValidationError, match="receipt_required must be a boolean"):
        _validate_loop_policy({"receipt_required": "not-a-bool"}, edge_id="e1")

    # Float cooldown_seconds that represents integer vs float
    policy = _validate_loop_policy({"cooldown_seconds": 60.0}, edge_id="e1")
    assert policy["cooldown_seconds"] == 60
    assert isinstance(policy["cooldown_seconds"], int)

    policy = _validate_loop_policy({"cooldown_seconds": 60.5}, edge_id="e1")
    assert policy["cooldown_seconds"] == 60.5
    assert isinstance(policy["cooldown_seconds"], float)


def test_validate_precedence():
    # If precedence not in policy_map, returns map unchanged
    assert _validate_precedence({}, edge_id="e1", edge_kind="emits") == {}

    # Precedence not valid
    with pytest.raises(InnerLifeValidationError, match="policy.precedence must be one of"):
        _validate_precedence({"precedence": "invalid_prec"}, edge_id="e1", edge_kind="emits")

    # Contradicts edge kind
    with pytest.raises(InnerLifeValidationError, match="contradicts edge kind"):
        _validate_precedence({"precedence": "blocks"}, edge_id="e1", edge_kind=EdgeKind.SUPPRESSES.value)

    # Correct match
    policy_map = {"precedence": "blocks"}
    assert _validate_precedence(policy_map, edge_id="e1", edge_kind=EdgeKind.BLOCKS.value) == {"precedence": "blocks"}


def test_resolve_precedence():
    # No effects
    with pytest.raises(InnerLifeValidationError, match="resolve_precedence requires at least one effect"):
        resolve_precedence([])

    # Effect with unknown precedence
    with pytest.raises(InnerLifeValidationError, match="has unknown precedence"):
        resolve_precedence([{"precedence": "unknown"}])

    # Correct resolve order blocks > gates > dampens > amplifies > routes/projects
    effects = [
        {"precedence": "routes", "id": "e_routes"},
        {"precedence": "blocks", "id": "e_blocks"},
        {"precedence": "gates", "id": "e_gates"},
    ]
    assert resolve_precedence(effects)["id"] == "e_blocks"

    # Tie-breaking (first wins)
    effects = [
        {"precedence": "gates", "id": "e_gates1"},
        {"precedence": "gates", "id": "e_gates2"},
    ]
    assert resolve_precedence(effects)["id"] == "e_gates1"


def test_validate_signal_inbox_lineage_more_than_10_violations():
    # Construct a registry with 11 violations to trigger the (+x more) suffix
    blocks = {"version": 2, "blocks": {}}
    for i in range(12):
        blocks["blocks"][f"sensor_{i}"] = {"type": "sensor", "enabled": True}
    # No edges to signal_inbox
    edges = {"version": 1, "edges": []}

    with pytest.raises(InnerLifeValidationError, match=r"enabled sensor missing signal_inbox lineage: .* \(\+2 more\)"):
        _validate_signal_inbox_lineage(blocks, edges)


def test_validate_policy_caps_validation():
    # policy cap must be a positive integer
    with pytest.raises(InnerLifeValidationError, match="must be a positive integer"):
        validate_policy({"version": 1, "caps": {"max_graph_nodes": "not-an-int"}})

    with pytest.raises(InnerLifeValidationError, match="must be a positive integer"):
        validate_policy({"version": 1, "caps": {"max_graph_nodes": -1}})

    with pytest.raises(InnerLifeValidationError, match="must be a positive integer"):
        validate_policy({"version": 1, "caps": {"max_graph_nodes": False}})

    # policy disabled_groups must be lists of nonempty strings
    with pytest.raises(InnerLifeValidationError, match="must be a list"):
        validate_policy({"version": 1, "disabled_groups": "not-a-list"})

    with pytest.raises(InnerLifeValidationError, match="must be a non-empty string"):
        validate_policy({"version": 1, "disabled_groups": [""]})


def test_remaining_inner_life_edge_cases():
    # 1. 188: empty block registry payload load
    assert load_block_registry_v2({}) == {"version": 2, "blocks": {}}
    assert load_block_registry_v2(None) == {"version": 2, "blocks": {}}

    # 2. 197: v1 sensor loader with unknown/invalid status defaults to active
    v1_loaded = load_block_registry_v2({"gateway_pressure": {"status": "invalid_status"}})
    assert v1_loaded["blocks"]["gateway_pressure"]["status"] == "active"

    # 3. 234: unknown loop_policy kind in _validate_loop_policy
    with pytest.raises(InnerLifeValidationError, match="unknown loop_policy kind for e1"):
        _validate_loop_policy({"kind": "invalid_kind", "cooldown_seconds": 10}, edge_id="e1")

    # 4. 376: _edge_endpoint fallback and 378: _edge_endpoint return None
    edge_no_fallback = {"from": "a"}
    assert _edge_endpoint(edge_no_fallback, "to", "target") is None
    edge_with_fallback = {"target": "b"}
    assert _edge_endpoint(edge_with_fallback, "to", "target") == "b"

    # 5. 389: disabled edge handling in signal_inbox_lineage_violations
    registry = {
        "version": 2,
        "blocks": {
            "sensor_a": {"type": "sensor", "enabled": True},
            "signal_inbox": {"type": "aggregator", "enabled": True},
        },
    }
    # With edge disabled, lineage is broken -> violation
    disabled_edges = {
        "version": 1,
        "edges": [{"id": "e1", "from": "sensor_a", "to": "signal_inbox", "kind": "emits", "enabled": False}],
    }
    violations = signal_inbox_lineage_violations(registry, disabled_edges)
    assert len(violations) == 1

    # 6. 411: non-emitting block (e.g. BlockKind.DAMPENER) ignored in lineage validation
    dampener_registry = {
        "version": 2,
        "blocks": {
            "dampener_a": {"type": "dampener", "enabled": True},
        },
    }
    # No edges to signal_inbox, but since dampener is not an emitting block kind, no violations
    assert signal_inbox_lineage_violations(dampener_registry, {"version": 1, "edges": []}) == []

    # 7. 491: validate_edge_registry error for invalid version
    with pytest.raises(InnerLifeValidationError, match="edges registry version must be 1"):
        validate_edge_registry({"version": 2})

    # 8. 499: validate_edge_registry error for duplicate edge id
    duplicate_edges = {
        "version": 1,
        "edges": [
            {"id": "e1", "from": "a", "to": "b", "kind": "routes_to"},
            {"id": "e1", "from": "c", "to": "d", "kind": "routes_to"},
        ],
    }
    with pytest.raises(InnerLifeValidationError, match="duplicate edge id: e1"):
        validate_edge_registry(duplicate_edges, known_blocks={"a", "b", "c", "d"})

    # 9. 540: validate_policy error for invalid version
    with pytest.raises(InnerLifeValidationError, match="policy version must be 1"):
        validate_policy({"version": 2})


def test_has_enabled_path_hits_already_seen():
    # 389: continue in _has_enabled_path when a node is visited twice
    registry = {
        "version": 2,
        "blocks": {
            "sensor_a": {"type": "sensor", "enabled": True},
            "node_b": {"type": "normalizer", "enabled": True},
            "node_d": {"type": "normalizer", "enabled": True},
            "signal_inbox": {"type": "aggregator", "enabled": True},
        }
    }
    # We construct a graph where node_d is queued twice on the stack
    edges = {
        "version": 1,
        "edges": [
            {"id": "e1", "from": "sensor_a", "to": "node_d", "kind": "emits"},
            {"id": "e2", "from": "sensor_a", "to": "node_b", "kind": "emits"},
            {"id": "e3", "from": "node_b", "to": "node_d", "kind": "emits"},
        ]
    }
    # This checks lineage but there is no path to signal_inbox, so it returns False.
    # Because node_d is reached from both sensor_a and node_b, it will be processed and checked if node in seen.
    violations = signal_inbox_lineage_violations(registry, edges)
    assert len(violations) == 1


def test_edge_cycle_sets_back_edge_not_start():
    # 464: continue in _edge_cycle_sets when next_node is in seen_nodes but is not start
    # Graph: a -> b -> c -> b, where start = a.
    # The back-edge is c -> b (b is in seen_nodes but is not start, i.e., not a)
    cyclic_edges = {
        "version": 1,
        "edges": [
            {"id": "ab", "from": "a", "to": "b", "kind": "routes_to"},
            {"id": "bc", "from": "b", "to": "c", "kind": "routes_to"},
            {"id": "cb", "from": "c", "to": "b", "kind": "routes_to", "policy": {"loop_policy": {"kind": "max_passes", "max_passes": 1}}},
        ]
    }
    # Simply validating the edge registry with this configuration executes _edge_cycle_sets
    res = validate_edge_registry(cyclic_edges, known_blocks={"a", "b", "c"})
    assert len(res["edges"]) == 3
