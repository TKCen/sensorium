"""Tests for dampener/blocker block semantics and edge precedence enforcement."""

from __future__ import annotations

import json

import pytest

from agent_sensorium.dampener_blocker import (
    DampenerBlockerConfigError,
    _run_state_label,
    _safe_scalar_label,
    evaluate_blocker_block,
    load_blocker_block_config,
    load_dampener_block_config,
    run_blocker_block,
    run_dampener_block,
)
from agent_sensorium.inner_life import (
    InnerLifeValidationError,
    resolve_precedence,
    validate_edge_registry,
)
from agent_sensorium.store import SensoriumStore


@pytest.fixture
def store(tmp_path):
    return SensoriumStore(state_dir=str(tmp_path / "instance"))


def _spine_snapshot(store: SensoriumStore) -> dict:
    return {
        name: store.read_jsonl(name)
        for name in ("signals", "events", "candidates", "threads", "decisions", "outbox", "artifacts")
    }


# --- dampener config validation -------------------------------------------------


def test_dampener_config_requires_known_mode(store):
    with pytest.raises(DampenerBlockerConfigError):
        load_dampener_block_config("d1", {"type": "dampener", "mode": "not_a_mode"})


def test_dampener_config_requires_mode_field():
    with pytest.raises(DampenerBlockerConfigError):
        load_dampener_block_config("d1", {"type": "dampener"})


def test_dampener_config_rejects_non_dampener_block():
    with pytest.raises(DampenerBlockerConfigError):
        load_dampener_block_config("d1", {"type": "blocker", "mode": "cooldown"})


def test_dampener_config_rejects_non_positive_bound():
    with pytest.raises(DampenerBlockerConfigError):
        load_dampener_block_config("d1", {"type": "dampener", "mode": "cooldown", "cooldown_seconds": 0})


def test_dampener_config_rejects_bool_bound():
    with pytest.raises(DampenerBlockerConfigError):
        load_dampener_block_config("d1", {"type": "dampener", "mode": "cooldown", "cooldown_seconds": True})


@pytest.mark.parametrize(
    "mode",
    ["cooldown", "rate_limit", "decay", "suppress_duplicate", "sample"],
)
def test_dampener_config_requires_explicit_mode_bound(mode):
    with pytest.raises(DampenerBlockerConfigError):
        load_dampener_block_config("d1", {"type": "dampener", "mode": mode})


@pytest.mark.parametrize(
    "block",
    [
        {"type": "dampener", "mode": "rate_limit", "rate_limit_max": 2},
        {"type": "dampener", "mode": "rate_limit", "rate_limit_window_seconds": 60},
    ],
)
def test_dampener_rate_limit_requires_explicit_cap_and_window(block):
    with pytest.raises(DampenerBlockerConfigError):
        load_dampener_block_config("d_rate", block)


# --- cooldown mode ---------------------------------------------------------------


def test_dampener_cooldown_suppresses_then_passes_after_window(store):
    block = {"type": "dampener", "mode": "cooldown", "cooldown_seconds": 60}
    first = run_dampener_block("d_cooldown", block, store=store, now="2026-06-20T00:00:00Z", pressure=0.8)
    assert first["after_pressure"] == 0.8
    assert first["reason"] == "cooldown_elapsed"

    second = run_dampener_block("d_cooldown", block, store=store, now="2026-06-20T00:00:30Z", pressure=0.8)
    assert second["after_pressure"] == 0.0
    assert second["reason"] == "cooldown_active"
    assert second["expires_at"] is not None

    third = run_dampener_block("d_cooldown", block, store=store, now="2026-06-20T00:01:01Z", pressure=0.8)
    assert third["after_pressure"] == 0.8
    assert third["reason"] == "cooldown_elapsed"


# --- rate_limit mode --------------------------------------------------------------


def test_dampener_rate_limit_caps_passes_per_window(store):
    block = {
        "type": "dampener",
        "mode": "rate_limit",
        "rate_limit_max": 2,
        "rate_limit_window_seconds": 300,
    }
    now = "2026-06-20T00:00:00Z"
    first = run_dampener_block("d_rate", block, store=store, now=now, pressure=1.0)
    second = run_dampener_block("d_rate", block, store=store, now=now, pressure=1.0)
    third = run_dampener_block("d_rate", block, store=store, now=now, pressure=1.0)
    assert [first["after_pressure"], second["after_pressure"]] == [1.0, 1.0]
    assert third["after_pressure"] == 0.0
    assert third["reason"] == "rate_limit_exceeded"

    later = run_dampener_block("d_rate", block, store=store, now="2026-06-20T00:10:00Z", pressure=1.0)
    assert later["after_pressure"] == 1.0
    assert later["reason"] == "rate_limit_ok"


# --- decay mode -------------------------------------------------------------------


def test_dampener_decay_reduces_pressure_monotonically(store):
    block = {"type": "dampener", "mode": "decay", "decay_half_life_seconds": 100}
    first = run_dampener_block("d_decay", block, store=store, now="2026-06-20T00:00:00Z", pressure=1.0)
    assert first["after_pressure"] == 1.0  # no elapsed time yet

    half_life_later = run_dampener_block("d_decay", block, store=store, now="2026-06-20T00:01:40Z", pressure=1.0)
    assert half_life_later["after_pressure"] == pytest.approx(0.5, abs=1e-3)

    two_half_lives = run_dampener_block("d_decay", block, store=store, now="2026-06-20T00:03:20Z", pressure=1.0)
    assert two_half_lives["after_pressure"] == pytest.approx(0.25, abs=1e-3)
    assert two_half_lives["after_pressure"] < half_life_later["after_pressure"]


# --- suppress_duplicate mode ------------------------------------------------------


def test_dampener_suppress_duplicate_dedups_within_ttl(store):
    block = {"type": "dampener", "mode": "suppress_duplicate", "dedup_ttl_seconds": 60}
    first = run_dampener_block(
        "d_dedup", block, store=store, now="2026-06-20T00:00:00Z", pressure=0.9, key="candidate-1"
    )
    assert first["after_pressure"] == 0.9
    assert first["reason"] == "first_seen"

    duplicate = run_dampener_block(
        "d_dedup", block, store=store, now="2026-06-20T00:00:10Z", pressure=0.9, key="candidate-1"
    )
    assert duplicate["after_pressure"] == 0.0
    assert duplicate["reason"] == "duplicate_suppressed"

    different_key = run_dampener_block(
        "d_dedup", block, store=store, now="2026-06-20T00:00:10Z", pressure=0.9, key="candidate-2"
    )
    assert different_key["after_pressure"] == 0.9

    after_ttl = run_dampener_block(
        "d_dedup", block, store=store, now="2026-06-20T00:02:00Z", pressure=0.9, key="candidate-1"
    )
    assert after_ttl["after_pressure"] == 0.9
    assert after_ttl["reason"] == "first_seen"


def test_dampener_suppress_duplicate_hashes_persisted_caller_key(store):
    block = {"type": "dampener", "mode": "suppress_duplicate", "dedup_ttl_seconds": 60}
    secret_key = "sk-ded...3456"

    run_dampener_block("d_dedup_secret", block, store=store, now="2026-06-20T00:00:00Z", key=secret_key)
    duplicate = run_dampener_block(
        "d_dedup_secret", block, store=store, now="2026-06-20T00:00:10Z", key=secret_key
    )

    state = store.read_block_run_state(_run_state_label("d_dedup_secret"))
    serialized_state = json.dumps(state, sort_keys=True)
    persisted_keys = list(state["dampener_seen_keys"])
    assert duplicate["reason"] == "duplicate_suppressed"
    assert secret_key not in serialized_state
    assert len(persisted_keys) == 1
    assert persisted_keys[0].startswith("sha256:")


# --- sample mode -------------------------------------------------------------------


def test_dampener_sample_passes_every_nth_call(store):
    block = {"type": "dampener", "mode": "sample", "sample_every": 3}
    results = [
        run_dampener_block("d_sample", block, store=store, now="2026-06-20T00:00:00Z", pressure=1.0)
        for _ in range(6)
    ]
    passes = [r["after_pressure"] > 0 for r in results]
    assert passes == [False, False, True, False, False, True]


# --- reversibility / no durable mutation ------------------------------------------


def test_dampener_never_mutates_durable_spine(store):
    before = _spine_snapshot(store)
    block = {"type": "dampener", "mode": "cooldown", "cooldown_seconds": 30}
    run_dampener_block("d_spine", block, store=store, now="2026-06-20T00:00:00Z", pressure=1.0)
    run_dampener_block("d_spine", block, store=store, now="2026-06-20T00:00:05Z", pressure=1.0)
    after = _spine_snapshot(store)
    assert before == after == {key: [] for key in before}


def test_dampener_effect_record_shape_and_audit_trail(store):
    block = {"type": "dampener", "mode": "cooldown", "cooldown_seconds": 30}
    effect = run_dampener_block(
        "d_shape", block, store=store, now="2026-06-20T00:00:00Z", pressure=0.6, target="candidate_review"
    )
    assert effect["type"] == "dampener_effect"
    assert effect["source_node"] == _safe_scalar_label("node", "d_shape")
    assert effect["target_node"] == _safe_scalar_label("node", "candidate_review")
    assert effect["mode"] == "cooldown"
    assert effect["before_pressure"] == 0.6
    assert "after_pressure" in effect
    assert "reason" in effect
    assert "expires_at" in effect
    assert effect["precedence"] == "dampens"

    logged = store.read_dampener_effects()
    assert len(logged) == 1
    assert logged[0]["source_node"] == _safe_scalar_label("node", "d_shape")


def test_dampener_effect_never_echoes_secret_target(store):
    block = {"type": "dampener", "mode": "cooldown", "cooldown_seconds": 30}
    secret_target = "sk-target...3456"
    effect = run_dampener_block(
        "sk-block...3456", block, store=store, now="2026-06-20T00:00:00Z", target=secret_target
    )
    assert effect["source_node"] == _safe_scalar_label("node", "sk-block...3456")
    assert effect["target_node"] == _safe_scalar_label("node", secret_target)
    assert secret_target not in json.dumps(effect)
    assert "sk-block...3456" not in json.dumps(effect)

    logged = store.read_dampener_effects()
    assert secret_target not in json.dumps(logged)
    assert "sk-block...3456" not in json.dumps(logged)

    sidecar_text = (store.root / "inner_life" / "dampeners.jsonl").read_text()
    assert secret_target not in sidecar_text
    assert "sk-block...3456" not in sidecar_text


def test_dampener_run_state_filename_never_contains_secret_block_id(store):
    block = {"type": "dampener", "mode": "cooldown", "cooldown_seconds": 30}
    secret_block_id = "sk-target...3456"
    run_dampener_block(
        secret_block_id, block, store=store, now="2026-06-20T00:00:00Z", pressure=0.6, target="t1"
    )

    all_paths = [str(p) for p in store.root.rglob("*")]
    assert not any(secret_block_id in p for p in all_paths)

    expected_label = _run_state_label(secret_block_id)
    state = store.read_block_run_state(expected_label)
    assert state != {}


def test_dampener_run_state_label_is_deterministic_and_distinct(store):
    assert _run_state_label("block-a") == _run_state_label("block-a")
    assert _run_state_label("block-a") != _run_state_label("block-b")

    block = {"type": "dampener", "mode": "cooldown", "cooldown_seconds": 30}
    run_dampener_block("block-a", block, store=store, now="2026-06-20T00:00:00Z", pressure=0.6)
    run_dampener_block("block-b", block, store=store, now="2026-06-20T00:00:00Z", pressure=0.6)

    state_a = store.read_block_run_state(_run_state_label("block-a"))
    state_b = store.read_block_run_state(_run_state_label("block-b"))
    assert state_a != {}
    assert state_b != {}
    assert state_a.get("dampener_last_pass_at") == state_b.get("dampener_last_pass_at")


def test_dampener_dry_run_does_not_persist(store):
    block = {"type": "dampener", "mode": "cooldown", "cooldown_seconds": 30}
    run_dampener_block("d_dry", block, store=store, now="2026-06-20T00:00:00Z", pressure=1.0, dry_run=True)
    assert store.read_dampener_effects() == []
    assert store.read_block_run_state("d_dry") == {}


# --- blocker config validation -----------------------------------------------------


@pytest.mark.parametrize(
    "block",
    [
        {"type": "blocker", "reason_kind": "policy", "reason": "x"},
        {"type": "blocker", "reason_kind": "schema", "reason": "x"},
        {"type": "blocker", "reason_kind": "authority", "reason": "x"},
    ],
)
def test_blocker_requires_provenance_for_policy_schema_authority(block):
    with pytest.raises(DampenerBlockerConfigError):
        load_blocker_block_config("b1", block)


def test_blocker_requires_reason_kind():
    with pytest.raises(DampenerBlockerConfigError):
        load_blocker_block_config("b1", {"type": "blocker", "reason": "x"})


def test_blocker_requires_nonempty_reason():
    with pytest.raises(DampenerBlockerConfigError):
        load_blocker_block_config(
            "b1", {"type": "blocker", "reason_kind": "policy", "policy_id": "p1", "reason": "   "}
        )


def test_blocker_unknown_reason_kind_rejected():
    with pytest.raises(DampenerBlockerConfigError):
        load_blocker_block_config("b1", {"type": "blocker", "reason_kind": "vibes", "reason": "x"})


@pytest.mark.parametrize("reason_kind", ["privacy", "safety"])
def test_blocker_privacy_safety_only_require_reason(reason_kind):
    cfg = load_blocker_block_config(
        "b1", {"type": "blocker", "reason_kind": reason_kind, "reason": "raw content detected"}
    )
    assert cfg.reason_kind.value == reason_kind
    assert cfg.policy_id is None


# --- blocker evaluation ------------------------------------------------------------


def test_blocker_effect_traces_to_policy_reason():
    block = {
        "type": "blocker",
        "reason_kind": "policy",
        "policy_id": "surface_sensitivity_v1",
        "reason": "private item cannot route to public surface",
    }
    effect = evaluate_blocker_block("privacy_policy_blocker", block, target="outbox_router", now="2026-06-20T00:00:00Z")
    assert effect["type"] == "blocker_effect"
    assert effect["reason_kind"] == "policy"
    assert effect["policy_id"] == _safe_scalar_label("ref", "surface_sensitivity_v1")
    assert effect["schema_ref"] is None
    assert effect["authority_ref"] is None
    assert effect["target_node"] == _safe_scalar_label("node", "outbox_router")
    assert effect["precedence"] == "blocks"
    assert effect["blocked_at"] == "2026-06-20T00:00:00Z"
    assert "surface_sensitivity_v1" not in json.dumps(effect)
    assert "outbox_router" not in json.dumps(effect)


def test_blocker_effect_traces_to_schema_reason():
    block = {"type": "blocker", "reason_kind": "schema", "schema_ref": "signal.v1", "reason": "schema invalid"}
    effect = evaluate_blocker_block("schema_blocker", block)
    assert effect["reason_kind"] == "schema"
    assert effect["schema_ref"] == _safe_scalar_label("ref", "signal.v1")


def test_blocker_effect_traces_to_authority_reason():
    block = {
        "type": "blocker",
        "reason_kind": "authority",
        "authority_ref": "missing_receipt",
        "reason": "missing authority/receipt",
    }
    effect = evaluate_blocker_block("authority_blocker", block)
    assert effect["reason_kind"] == "authority"
    assert effect["authority_ref"] == _safe_scalar_label("ref", "missing_receipt")


def test_blocker_effect_never_echoes_raw_reason_text():
    block = {"type": "blocker", "reason_kind": "safety", "reason": "raw unsafe content excerpt"}
    effect = evaluate_blocker_block("safety_blocker", block)
    assert "reason" not in effect
    assert effect["reason_label"] == _safe_scalar_label("reason", "raw unsafe content excerpt")
    assert "raw unsafe content excerpt" not in json.dumps(effect)


def test_run_blocker_block_persists_audit_trail(store):
    block = {"type": "blocker", "reason_kind": "policy", "policy_id": "p1", "reason": "blocked"}
    run_blocker_block("b_audit", block, store=store, target="t1", now="2026-06-20T00:00:00Z")
    logged = store.read_blocker_effects()
    assert len(logged) == 1
    assert logged[0]["source_node"] == _safe_scalar_label("node", "b_audit")
    assert logged[0]["policy_id"] == _safe_scalar_label("ref", "p1")


def test_run_blocker_block_dry_run_does_not_persist(store):
    block = {"type": "blocker", "reason_kind": "safety", "reason": "unsafe"}
    run_blocker_block("b_dry", block, store=store, dry_run=True)
    assert store.read_blocker_effects() == []


def test_run_blocker_block_never_echoes_secret_scalars(store):
    secret = "sk-target...3456"
    block = {
        "type": "blocker",
        "reason_kind": "policy",
        "policy_id": secret,
        "schema_ref": secret,
        "authority_ref": secret,
        "reason": secret,
    }
    effect = run_blocker_block(secret, block, store=store, target=secret, now="2026-06-20T00:00:00Z")

    assert effect["source_node"] == _safe_scalar_label("node", secret)
    assert effect["target_node"] == _safe_scalar_label("node", secret)
    assert effect["policy_id"] == _safe_scalar_label("ref", secret)
    assert effect["schema_ref"] == _safe_scalar_label("ref", secret)
    assert effect["authority_ref"] == _safe_scalar_label("ref", secret)
    assert effect["reason_label"] == _safe_scalar_label("reason", secret)
    assert "reason" not in effect
    assert secret not in json.dumps(effect)

    logged = store.read_blocker_effects()
    assert secret not in json.dumps(logged)

    sidecar_text = (store.root / "inner_life" / "blockers.jsonl").read_text()
    assert secret not in sidecar_text


# --- precedence resolution ---------------------------------------------------------


def test_resolve_precedence_blocks_wins_over_everything():
    effects = [
        {"id": "amp", "precedence": "amplifies"},
        {"id": "block", "precedence": "blocks"},
        {"id": "route", "precedence": "routes"},
    ]
    assert resolve_precedence(effects)["id"] == "block"


def test_resolve_precedence_full_ladder_order():
    effects = [
        {"id": "route", "precedence": "routes"},
        {"id": "amp", "precedence": "amplifies"},
        {"id": "dampen", "precedence": "dampens"},
        {"id": "gate", "precedence": "gates"},
        {"id": "block", "precedence": "blocks"},
        {"id": "project", "precedence": "projects"},
    ]
    assert resolve_precedence(effects)["id"] == "block"
    assert resolve_precedence([e for e in effects if e["id"] != "block"])["id"] == "gate"
    assert resolve_precedence([e for e in effects if e["id"] not in {"block", "gate"}])["id"] == "dampen"


def test_resolve_precedence_rejects_unknown_value():
    with pytest.raises(InnerLifeValidationError):
        resolve_precedence([{"id": "x", "precedence": "mystery"}])


def test_resolve_precedence_rejects_empty_list():
    with pytest.raises(InnerLifeValidationError):
        resolve_precedence([])


def test_resolve_precedence_ties_broken_by_input_order():
    effects = [{"id": "first", "precedence": "blocks"}, {"id": "second", "precedence": "blocks"}]
    assert resolve_precedence(effects)["id"] == "first"


# --- edge policy precedence validation (schema-level) ------------------------------


def test_edge_precedence_must_match_blocks_kind():
    edges = {
        "version": 1,
        "edges": [
            {
                "id": "e1",
                "from": "a",
                "to": "b",
                "kind": "blocks",
                "policy": {"precedence": "blocks"},
            }
        ],
    }
    result = validate_edge_registry(edges, known_blocks={"a", "b"})
    assert result["edges"][0]["policy"]["precedence"] == "blocks"


def test_edge_precedence_mismatch_with_kind_rejected():
    edges = {
        "version": 1,
        "edges": [
            {
                "id": "e1",
                "from": "a",
                "to": "b",
                "kind": "blocks",
                "policy": {"precedence": "amplifies"},
            }
        ],
    }
    with pytest.raises(InnerLifeValidationError):
        validate_edge_registry(edges, known_blocks={"a", "b"})


def test_edge_precedence_unknown_value_rejected():
    edges = {
        "version": 1,
        "edges": [
            {
                "id": "e1",
                "from": "a",
                "to": "b",
                "kind": "suppresses",
                "policy": {"precedence": "definitely_not_real"},
            }
        ],
    }
    with pytest.raises(InnerLifeValidationError):
        validate_edge_registry(edges, known_blocks={"a", "b"})


def test_edge_precedence_gates_value_allowed_without_kind_match():
    # "gates" precedence has no dedicated edge kind in the closed taxonomy (gate is a
    # block kind, not an edge kind), so it is accepted on edge kinds that have no
    # fixed precedence mapping, such as opens_review.
    edges = {
        "version": 1,
        "edges": [
            {
                "id": "e1",
                "from": "a",
                "to": "b",
                "kind": "opens_review",
                "policy": {"precedence": "gates"},
            }
        ],
    }
    result = validate_edge_registry(edges, known_blocks={"a", "b"})
    assert result["edges"][0]["policy"]["precedence"] == "gates"
