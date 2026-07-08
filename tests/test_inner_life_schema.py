"""Schema tests for the inner-life graph registry contracts."""

import pytest

from agent_sensorium.inner_life import (
    BlockKind,
    EdgeKind,
    InnerLifeValidationError,
    load_block_registry_v2,
    load_inner_life_config,
    signal_inbox_lineage_violations,
    validate_edge_registry,
    validate_policy,
)
from agent_sensorium.sensors import load_sensor_registry
from agent_sensorium.store import SensoriumStore


def test_block_and_edge_kind_enums_are_closed():
    assert {kind.value for kind in BlockKind} == {
        "sensor",
        "temporal_sensor",
        "memory_reflector",
        "normalizer",
        "aggregator",
        "dampener",
        "blocker",
        "gate",
        "router",
        "consciousness_review",
        "receipt_writer",
        "tendency_proposer",
        "dashboard_projection",
    }
    assert {kind.value for kind in EdgeKind} == {
        "emits",
        "promotes_to",
        "aggregates_into",
        "amplifies",
        "suppresses",
        "blocks",
        "routes_to",
        "opens_review",
        "settles",
        "feeds_back",
        "proposes_delta",
        "projects_to_dashboard",
    }


def test_registry_v1_loads_as_v2_sensor_blocks():
    loaded = load_block_registry_v2(
        {
            "gateway_pressure": {
                "status": "active",
                "defaults": {"sensitivity": "private", "allowed_surfaces": ["local"]},
            },
            "old_paused_sensor": {"status": "paused"},
        }
    )

    assert loaded["version"] == 2
    assert loaded["blocks"]["gateway_pressure"] == {
        "id": "gateway_pressure",
        "type": "sensor",
        "enabled": True,
        "status": "active",
        "defaults": {"sensitivity": "private", "allowed_surfaces": ["local"]},
    }
    assert loaded["blocks"]["old_paused_sensor"]["enabled"] is False


def test_unknown_block_kind_fails_closed():
    with pytest.raises(InnerLifeValidationError, match="unknown block kind"):
        load_block_registry_v2({"version": 2, "blocks": {"x": {"type": "operator_mood"}}})


def test_unknown_edge_kind_fails_closed():
    with pytest.raises(InnerLifeValidationError, match="unknown edge kind"):
        validate_edge_registry(
            {
                "version": 1,
                "edges": [{"id": "e1", "from": "a", "to": "b", "kind": "teleports"}],
            },
            known_blocks={"a", "b"},
        )


def test_feeds_back_edges_require_explicit_loop_policy():
    with pytest.raises(InnerLifeValidationError, match="feeds_back edge requires loop_policy"):
        validate_edge_registry(
            {
                "version": 1,
                "edges": [{"id": "fb", "from": "receipt", "to": "sensor", "kind": "feeds_back"}],
            },
            known_blocks={"receipt", "sensor"},
        )

    loaded = validate_edge_registry(
        {
            "version": 1,
            "edges": [
                {
                    "id": "fb",
                    "from": "receipt",
                    "to": "sensor",
                    "kind": "feeds_back",
                    "policy": {"loop_policy": {"kind": "cooldown", "cooldown_seconds": 300}},
                }
            ],
        },
        known_blocks={"receipt", "sensor"},
    )

    assert loaded["edges"][0]["policy"]["loop_policy"]["cooldown_seconds"] == 300


def test_edge_registry_rejects_unknown_endpoints_when_known_blocks_provided():
    with pytest.raises(InnerLifeValidationError, match="references unknown block"):
        validate_edge_registry(
            {
                "version": 1,
                "edges": [{"id": "e1", "from": "a", "to": "ghost", "kind": "routes_to"}],
            },
            known_blocks={"a", "b"},
        )

    with pytest.raises(InnerLifeValidationError, match="references unknown block"):
        validate_edge_registry(
            {
                "version": 1,
                "edges": [{"id": "e1", "from": "ghost", "to": "b", "kind": "routes_to"}],
            },
            known_blocks={"a", "b"},
        )

    loaded = validate_edge_registry(
        {
            "version": 1,
            "edges": [{"id": "e1", "from": "a", "to": "b", "kind": "routes_to"}],
        },
        known_blocks={"a", "b"},
    )
    assert loaded["edges"][0]["to"] == "b"


def test_explicit_cycle_configs_require_loop_policy():
    with pytest.raises(InnerLifeValidationError, match="cycle config requires loop_policy"):
        validate_edge_registry(
            {
                "version": 1,
                "edges": [
                    {
                        "id": "cycle_hint",
                        "from": "a",
                        "to": "b",
                        "kind": "routes_to",
                        "creates_cycle": True,
                    }
                ],
            },
            known_blocks={"a", "b"},
        )


def test_enabled_cycles_require_an_explicit_loop_policy_on_the_cycle():
    cyclic = {
        "version": 1,
        "edges": [
            {"id": "ab", "from": "a", "to": "b", "kind": "routes_to"},
            {"id": "ba", "from": "b", "to": "a", "kind": "suppresses"},
        ],
    }
    with pytest.raises(InnerLifeValidationError, match="cycle requires loop_policy"):
        validate_edge_registry(cyclic, known_blocks={"a", "b"})

    cyclic["edges"][1]["policy"] = {
        "loop_policy": {"kind": "max_passes", "max_passes": 1}
    }
    loaded = validate_edge_registry(cyclic, known_blocks={"a", "b"})
    assert loaded["edges"][1]["policy"]["loop_policy"]["max_passes"] == 1


def test_loop_policy_must_declare_a_real_bound():
    with pytest.raises(InnerLifeValidationError, match="loop_policy must declare at least one bound"):
        validate_edge_registry(
            {
                "version": 1,
                "edges": [
                    {
                        "id": "fb",
                        "from": "a",
                        "to": "b",
                        "kind": "feeds_back",
                        "policy": {"loop_policy": {"kind": "cooldown"}},
                    }
                ],
            },
            known_blocks={"a", "b"},
        )


def _loop_policy_edge(loop_policy):
    return {
        "version": 1,
        "edges": [
            {
                "id": "fb",
                "from": "a",
                "to": "b",
                "kind": "feeds_back",
                "policy": {"loop_policy": loop_policy},
            }
        ],
    }


@pytest.mark.parametrize(
    "loop_policy",
    [
        {"kind": "cooldown", "max_passes": True},
        {"kind": "idempotency", "idempotency_key": True},
        {"kind": "idempotency", "idempotency_key": 123},
        {"kind": "policy_gate", "policy_gate": True},
        {"kind": "dampener", "dampener": True},
        {"kind": "dampener", "dampener_id": True},
        {"kind": "policy_gate", "policy_gate_id": 5},
        {"kind": "cooldown", "cooldown_seconds": True},
    ],
)
def test_loop_policy_rejects_non_real_bound_values(loop_policy):
    with pytest.raises(InnerLifeValidationError):
        validate_edge_registry(
            _loop_policy_edge(loop_policy),
            known_blocks={"a", "b"},
        )


def test_loop_policy_accepts_real_bound_values():
    loaded = validate_edge_registry(
        _loop_policy_edge({"kind": "idempotency", "idempotency_key": "settle-once"}),
        known_blocks={"a", "b"},
    )
    assert loaded["edges"][0]["policy"]["loop_policy"]["idempotency_key"] == "settle-once"

    loaded = validate_edge_registry(
        _loop_policy_edge({"kind": "max_passes", "max_passes": 1}),
        known_blocks={"a", "b"},
    )
    assert loaded["edges"][0]["policy"]["loop_policy"]["max_passes"] == 1

    loaded = validate_edge_registry(
        _loop_policy_edge({"kind": "receipt_required", "receipt_required": True}),
        known_blocks={"a", "b"},
    )
    assert loaded["edges"][0]["policy"]["loop_policy"]["receipt_required"] is True


@pytest.mark.parametrize(
    "payload",
    [
        {"version": 1, "node_kinds": ["operator_mood"]},
        {"version": 1, "edge_kinds": ["teleports"]},
        {"version": 1, "caps": {"node_kinds": ["operator_mood"]}},
        {"version": 1, "caps": {"max_graph_nodes": 100, "kinds": ["teleports"]}},
        {"version": 1, "groups": {"sidecar": {"block_kinds": ["operator_mood"]}}},
        {"version": 1, "disabled_groups": ["experimental"], "extra": {"nested": {"edge_kinds": ["x"]}}},
        {"version": 1, "extra": [{"kinds": ["operator_mood"]}]},
    ],
)
def test_policy_validation_rejects_nested_kind_injection(payload):
    with pytest.raises(InnerLifeValidationError, match="must not define node or edge kinds"):
        validate_policy(payload)


def test_policy_validation_rejects_deployment_defined_kinds():
    with pytest.raises(InnerLifeValidationError, match="must not define node or edge kinds"):
        validate_policy({"version": 1, "node_kinds": ["operator_mood"]})
    with pytest.raises(InnerLifeValidationError, match="must not define node or edge kinds"):
        validate_policy({"version": 1, "edge_kinds": ["teleports"]})

    loaded = validate_policy(
        {
            "version": 1,
            "caps": {"max_graph_nodes": 100, "max_loop_passes": 3},
            "disabled_groups": ["experimental"],
        }
    )
    assert loaded["caps"]["max_graph_nodes"] == 100


def test_store_sidecars_load_with_validation(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "sensorium"))
    store.write_sensor_registry(
        {
            "version": 2,
            "blocks": {
                "sensor_a": {"type": "sensor", "enabled": True},
                "signal_inbox": {"type": "aggregator", "enabled": True},
            },
        }
    )
    store.write_sensor_edges(
        {
            "version": 1,
            "edges": [
                {
                    "id": "edge_a",
                    "from": "sensor_a",
                    "to": "signal_inbox",
                    "kind": "emits",
                }
            ],
        }
    )
    store.write_sensor_policy({"version": 1, "caps": {"max_graph_nodes": 10}})

    loaded = load_inner_life_config(store)
    assert loaded["registry"]["blocks"]["sensor_a"]["type"] == "sensor"
    assert loaded["edges"]["edges"][0]["kind"] == "emits"
    assert loaded["policy"]["caps"]["max_graph_nodes"] == 10


def test_enabled_emitting_sensors_require_signal_inbox_lineage():
    registry = load_block_registry_v2(
        {
            "version": 2,
            "blocks": {
                "provider_budget": {"type": "sensor", "enabled": True},
                "signal_inbox": {"type": "aggregator", "enabled": True},
                "non_emitting_probe": {"type": "sensor", "enabled": True, "emits_signals": False},
                "paused_sensor": {"type": "sensor", "enabled": False},
            },
        }
    )
    edges = validate_edge_registry(
        {
            "version": 1,
            "edges": [
                {
                    "id": "non_emitting_probe_internal_edge",
                    "from": "non_emitting_probe",
                    "to": "provider_budget",
                    "kind": "routes_to",
                    "enabled": True,
                }
            ],
        },
        known_blocks=set(registry["blocks"]),
    )

    violations = signal_inbox_lineage_violations(registry, edges)

    assert violations == [
        {
            "sensor": "provider_budget",
            "reason": "missing_signal_inbox_lineage",
            "required_target": "signal_inbox",
        }
    ]


def test_enabled_emitting_sensors_accept_indirect_signal_inbox_lineage():
    registry = load_block_registry_v2(
        {
            "version": 2,
            "blocks": {
                "provider_budget": {"type": "sensor", "enabled": True},
                "budget_normalizer": {"type": "normalizer", "enabled": True},
                "signal_inbox": {"type": "aggregator", "enabled": True},
            },
        }
    )
    edges = validate_edge_registry(
        {
            "version": 1,
            "edges": [
                {"id": "budget_to_normalizer", "from": "provider_budget", "to": "budget_normalizer", "kind": "emits"},
                {"id": "normalizer_to_inbox", "from": "budget_normalizer", "to": "signal_inbox", "kind": "aggregates_into"},
            ],
        },
        known_blocks=set(registry["blocks"]),
    )

    assert signal_inbox_lineage_violations(registry, edges) == []


def test_runtime_sensor_registry_can_read_v2_sensor_blocks(tmp_path):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "sensorium"))
    store.write_sensor_registry(
        {
            "version": 2,
            "blocks": {
                "gateway_pressure": {
                    "type": "sensor",
                    "enabled": True,
                    "defaults": {"strength_hint": 0.8},
                },
                "noise_dampener": {"type": "dampener", "enabled": True},
                "paused_sensor": {"type": "sensor", "enabled": False},
            },
        }
    )

    loaded = load_sensor_registry(store)

    assert loaded["gateway_pressure"]["status"] == "active"
    assert loaded["gateway_pressure"]["defaults"]["strength_hint"] == 0.8
    assert loaded["paused_sensor"]["status"] == "paused"
    assert "noise_dampener" not in loaded
