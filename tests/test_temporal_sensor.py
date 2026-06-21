"""Tests for deterministic temporal sensors over compact prior state."""

import json

import pytest

from agent_sensorium.inner_life import TemporalPredicateKind
from agent_sensorium.runner import list_sensors, run_sensor
from agent_sensorium.store import SensoriumStore
from agent_sensorium.temporal_sensor import (
    TemporalSensorConfigError,
    evaluate_temporal_block,
    load_temporal_block_config,
)


@pytest.fixture
def store(tmp_path):
    return SensoriumStore(instance="test", state_dir=str(tmp_path / "sensorium"))


def _append_signal(
    store,
    *,
    ts,
    kind="gateway_error",
    sensor="fixture",
    strength=0.7,
    key="gateway",
    source="machine",
    actor="operator",
    row_id=None,
):
    store.append_jsonl(
        "signals",
        {
            "id": row_id or f"sig_{ts.replace(':', '').replace('-', '')}",
            "ts": ts,
            "sensor": sensor,
            "source": source,
            "actor": actor,
            "kind": kind,
            "summary": "compact fixture row",
            "strength_hint": strength,
            "correlation_keys": [key],
            "sensitivity": "private",
            "allowed_surfaces": ["local"],
        },
    )


def _rate_block(**overrides):
    block = {
        "type": "temporal_sensor",
        "enabled": True,
        "predicate": {
            "kind": "rate_window",
            "state": "signals",
            "match": {"kind": "gateway_error", "correlation_key": "gateway"},
            "window_seconds": 1800,
            "threshold_count": 3,
        },
        "max_lookback_seconds": 3600,
        "cooldown_seconds": 300,
        "defaults": {"sensitivity": "private", "allowed_surfaces": ["local"]},
    }
    block.update(overrides)
    return block


def test_temporal_predicate_enum_is_closed():
    assert {kind.value for kind in TemporalPredicateKind} == {
        "rate_window",
        "recurrence",
        "slope",
        "ratio",
        "gap",
        "consecutive_state",
    }


def test_unknown_temporal_predicate_fails_closed():
    with pytest.raises(TemporalSensorConfigError, match="unknown temporal predicate"):
        load_temporal_block_config("trend", {"type": "temporal_sensor", "predicate": {"kind": "llm_reflect"}})


def test_rate_window_emits_deterministic_compact_trend_signal(store):
    for minute in ("00", "10", "20"):
        _append_signal(store, ts=f"2026-06-20T12:{minute}:00Z")

    first = evaluate_temporal_block(
        "repeat_gateway_errors",
        _rate_block(),
        store=store,
        now="2026-06-20T12:30:00Z",
    )
    second = evaluate_temporal_block(
        "repeat_gateway_errors",
        _rate_block(),
        store=store,
        now="2026-06-20T12:30:00Z",
    )

    assert first == second
    assert first["emitted"] is True
    signal = first["signal"]
    assert signal == second["signal"]
    assert signal["sensor"] == "sensorium.temporal.repeat_gateway_errors"
    assert signal["source"] == "machine"
    assert signal["kind"] == "temporal_trend"
    assert signal["temporal"]["predicate"] == "rate_window"
    assert signal["temporal"]["count"] == 3
    assert signal["temporal"]["window_seconds"] == 1800
    assert "compact fixture row" not in json.dumps(signal)


def test_max_lookback_bounds_candidate_rows(store):
    _append_signal(store, ts="2026-06-20T11:00:00Z")
    _append_signal(store, ts="2026-06-20T12:20:00Z")
    _append_signal(store, ts="2026-06-20T12:25:00Z")
    _append_signal(store, ts="2026-06-20T12:29:00Z")

    result = evaluate_temporal_block(
        "repeat_gateway_errors",
        _rate_block(max_lookback_seconds=600),
        store=store,
        now="2026-06-20T12:30:00Z",
    )

    assert result["sampled"] is True
    assert result["window"]["candidate_rows"] == 2
    assert result["emitted"] is False
    assert result["reason"] == "predicate_not_met"


def test_cooldown_suppresses_repeat_emit_and_records_run_state(store):
    for minute in ("00", "10", "20"):
        _append_signal(store, ts=f"2026-06-20T12:{minute}:00Z")

    step, err = run_sensor(
        "repeat_gateway_errors",
        store=store,
        config={},
        dry_run=False,
        kw={"instance": "test", "state_dir": str(store.root)},
        instance="test",
        now="2026-06-20T12:30:00Z",
        script_blocks={"repeat_gateway_errors": _rate_block()},
    )
    assert err is None
    assert step["emitted"] is True

    step, err = run_sensor(
        "repeat_gateway_errors",
        store=store,
        config={},
        dry_run=False,
        kw={"instance": "test", "state_dir": str(store.root)},
        instance="test",
        now="2026-06-20T12:31:00Z",
        script_blocks={"repeat_gateway_errors": _rate_block()},
    )
    assert err is None
    assert step["emitted"] is False
    assert step["reason"] == "cooldown_active"
    assert len(store.read_jsonl("signals")) == 4  # three fixture signals + first trend only
    state = store.read_block_run_state("repeat_gateway_errors")
    assert state["status"] == "ok"
    assert state["emitted"] is False
    assert state["cooldown_active"] is True


def test_runner_lists_and_runs_enabled_temporal_blocks(store):
    store.write_sensor_registry({"version": 2, "blocks": {"repeat_gateway_errors": _rate_block()}})
    listing = list_sensors(store)
    assert listing["temporal"] == [{"name": "repeat_gateway_errors", "enabled": True, "predicate": "rate_window"}]


def test_recurrence_slope_ratio_gap_and_consecutive_predicates_are_deterministic(store):
    rows = [
        ("2026-06-20T12:00:00Z", "gateway_error", 0.2, "gateway"),
        ("2026-06-20T12:10:00Z", "gateway_error", 0.4, "gateway"),
        ("2026-06-20T12:20:00Z", "gateway_error", 0.8, "gateway"),
        ("2026-06-20T12:25:00Z", "other", 0.1, "other"),
    ]
    for ts, kind, strength, key in rows:
        _append_signal(store, ts=ts, kind=kind, strength=strength, key=key)

    blocks = {
        "recurrence": {"predicate": {"kind": "recurrence", "state": "signals", "group_by": "kind", "window_seconds": 3600, "threshold_count": 3}},
        "slope": {"predicate": {"kind": "slope", "state": "signals", "match": {"kind": "gateway_error"}, "field": "strength_hint", "window_seconds": 3600, "min_points": 3, "threshold_slope": 0.0001}},
        "ratio": {"predicate": {"kind": "ratio", "state": "signals", "window_seconds": 3600, "numerator_match": {"kind": "gateway_error"}, "denominator_match": {}, "threshold_ratio": 0.7}},
        "gap": {"predicate": {"kind": "gap", "state": "signals", "match": {"kind": "heartbeat"}, "gap_seconds": 1200}},
        "consecutive": {"predicate": {"kind": "consecutive_state", "state": "signals", "match": {"kind": "gateway_error"}, "field": "kind", "equals": "gateway_error", "count": 3}},
    }
    for name, block in blocks.items():
        payload = {"type": "temporal_sensor", "enabled": True, "max_lookback_seconds": 3600, **block}
        first = evaluate_temporal_block(name, payload, store=store, now="2026-06-20T12:30:00Z")
        second = evaluate_temporal_block(name, payload, store=store, now="2026-06-20T12:30:00Z")
        assert first == second
        assert first["emitted"] is True, name
        assert first["signal"]["temporal"]["predicate"] == payload["predicate"]["kind"]


_SECRET = "sk-tem-leak-3456"


def _append_secret_signal(store, *, ts, key="gateway"):
    store.append_jsonl(
        "signals",
        {
            "id": f"sig_{ts.replace(':', '').replace('-', '')}",
            "ts": ts,
            "sensor": "fixture",
            "source": "machine",
            "kind": "gateway_error",
            "summary": "compact fixture row",
            "strength_hint": 0.7,
            "correlation_keys": [key],
            "sensitivity": "private",
            "allowed_surfaces": ["local"],
            "raw_transcript": _SECRET,
        },
    )


def test_recurrence_group_by_rejects_non_allowlisted_field(store):
    for minute in ("00", "10", "20"):
        _append_secret_signal(store, ts=f"2026-06-20T12:{minute}:00Z")

    block = {
        "type": "temporal_sensor",
        "enabled": True,
        "max_lookback_seconds": 3600,
        "predicate": {
            "kind": "recurrence",
            "state": "signals",
            "group_by": "raw_transcript",
            "window_seconds": 3600,
            "threshold_count": 3,
        },
    }
    with pytest.raises(TemporalSensorConfigError, match="group_by"):
        load_temporal_block_config("leak", block)

    with pytest.raises(TemporalSensorConfigError, match="group_by"):
        run_sensor(
            "leak",
            store=store,
            config={},
            dry_run=False,
            kw={"instance": "test", "state_dir": str(store.root)},
            instance="test",
            now="2026-06-20T12:30:00Z",
            script_blocks={"leak": block},
        )


def test_recurrence_and_slope_and_consecutive_field_reject_non_allowlisted_keys(store):
    for minute in ("00", "10", "20"):
        _append_secret_signal(store, ts=f"2026-06-20T12:{minute}:00Z")

    for field_key in ("slope", "consecutive_state"):
        if field_key == "slope":
            block = {
                "type": "temporal_sensor",
                "enabled": True,
                "max_lookback_seconds": 3600,
                "predicate": {
                    "kind": "slope",
                    "state": "signals",
                    "field": "raw_transcript",
                    "window_seconds": 3600,
                    "min_points": 1,
                },
            }
        else:
            block = {
                "type": "temporal_sensor",
                "enabled": True,
                "max_lookback_seconds": 3600,
                "predicate": {
                    "kind": "consecutive_state",
                    "state": "signals",
                    "field": "raw_transcript",
                    "equals": _SECRET,
                    "count": 1,
                },
            }
        with pytest.raises(TemporalSensorConfigError, match="field"):
            load_temporal_block_config("leak", block)


def test_match_predicate_rejects_non_allowlisted_keys(store):
    for minute in ("00", "10", "20"):
        _append_secret_signal(store, ts=f"2026-06-20T12:{minute}:00Z")

    block = {
        "type": "temporal_sensor",
        "enabled": True,
        "max_lookback_seconds": 3600,
        "predicate": {
            "kind": "rate_window",
            "state": "signals",
            "match": {"raw_transcript": _SECRET},
            "window_seconds": 3600,
            "threshold_count": 1,
        },
    }
    with pytest.raises(TemporalSensorConfigError, match="match"):
        load_temporal_block_config("leak", block)


def test_recurrence_with_allowlisted_group_by_never_echoes_secret_scalars(store):
    for minute in ("00", "10", "20"):
        _append_secret_signal(store, ts=f"2026-06-20T12:{minute}:00Z")

    block = {
        "type": "temporal_sensor",
        "enabled": True,
        "max_lookback_seconds": 3600,
        "predicate": {
            "kind": "recurrence",
            "state": "signals",
            "group_by": "kind",
            "window_seconds": 3600,
            "threshold_count": 3,
        },
    }
    result = evaluate_temporal_block("leak", block, store=store, now="2026-06-20T12:30:00Z")
    assert result["emitted"] is True
    assert _SECRET not in json.dumps(result)


_SECRET_SENSOR = "sk-ing2AAAAAAAAAAAAAAAAAAAA3456"
_SECRET_KIND = "sk-kinBBBBBBBBBBBBBBBBBBBB3456"


def test_recurrence_group_by_sensor_never_echoes_secret_shaped_sensor_value(store):
    """R4 repro: secret-shaped `sensor` values must not leak via group_value/source_refs."""
    for minute in ("00", "10", "20"):
        _append_signal(store, ts=f"2026-06-20T12:{minute}:00Z", sensor=_SECRET_SENSOR)

    block = {
        "type": "temporal_sensor",
        "enabled": True,
        "max_lookback_seconds": 3600,
        "predicate": {
            "kind": "recurrence",
            "state": "signals",
            "group_by": "sensor",
            "window_seconds": 3600,
            "threshold_count": 3,
        },
    }
    result = evaluate_temporal_block("leak_sensor", block, store=store, now="2026-06-20T12:30:00Z")
    assert result["emitted"] is True
    dumped = json.dumps(result)
    assert _SECRET_SENSOR not in dumped
    assert result["detail"]["group_value"] != _SECRET_SENSOR
    assert result["signal"]["temporal"]["group_value"] != _SECRET_SENSOR
    for ref in result["signal"]["temporal"]["source_refs"]:
        assert _SECRET_SENSOR not in ref["id"]
        assert _SECRET_SENSOR not in ref["kind"]
        assert _SECRET_SENSOR not in ref["fingerprint"]


def test_recurrence_group_by_kind_never_echoes_secret_shaped_kind_value(store):
    """R4 repro: secret-shaped `kind` values must not leak via group_value/source_refs."""
    for minute in ("00", "10", "20"):
        _append_signal(store, ts=f"2026-06-20T12:{minute}:00Z", kind=_SECRET_KIND)

    block = {
        "type": "temporal_sensor",
        "enabled": True,
        "max_lookback_seconds": 3600,
        "predicate": {
            "kind": "recurrence",
            "state": "signals",
            "group_by": "kind",
            "window_seconds": 3600,
            "threshold_count": 3,
        },
    }
    result = evaluate_temporal_block("leak_kind", block, store=store, now="2026-06-20T12:30:00Z")
    assert result["emitted"] is True
    dumped = json.dumps(result)
    assert _SECRET_KIND not in dumped
    assert result["detail"]["group_value"] != _SECRET_KIND
    assert result["signal"]["temporal"]["group_value"] != _SECRET_KIND
    for ref in result["signal"]["temporal"]["source_refs"]:
        assert _SECRET_KIND not in ref["id"]
        assert _SECRET_KIND not in ref["kind"]
        assert _SECRET_KIND not in ref["fingerprint"]


def test_rate_window_source_refs_never_echo_secret_shaped_kind_value(store):
    """Non-recurrence predicates also surface matched rows via source_refs; the
    classification value used to match them must not be echoed back raw either."""
    for minute in ("00", "10", "20"):
        _append_signal(store, ts=f"2026-06-20T12:{minute}:00Z", kind=_SECRET_KIND)

    block = _rate_block(predicate={
        "kind": "rate_window",
        "state": "signals",
        "match": {"kind": _SECRET_KIND},
        "window_seconds": 1800,
        "threshold_count": 3,
    })
    result = evaluate_temporal_block("leak_refs", block, store=store, now="2026-06-20T12:30:00Z")
    assert result["emitted"] is True
    dumped = json.dumps(result)
    assert _SECRET_KIND not in dumped
    for ref in result["signal"]["temporal"]["source_refs"]:
        assert _SECRET_KIND not in ref["kind"]


_SECRET_SOURCE = "sk-src-r4AAAAAAAAAAAAAAAAAAAA3456"
_SECRET_ACTOR = "sk-act-r4BBBBBBBBBBBBBBBBBBBB3456"
_SECRET_ID = "sk-id-r4CCCCCCCCCCCCCCCCCCCC3456"
_SECRET_EQUALS = "sk-eqr4DDDDDDDDDDDDDDDDDDDD3456"


def test_recurrence_group_by_source_never_echoes_out_of_vocab_secret(store):
    """R4 run-466 repro: schemas.validate_signal() never validates `source` against
    VALID_SOURCES, so ingest accepts a secret-shaped source. group_by=source must not
    echo it raw just because the key name `source` is allowlisted for selection."""
    for minute in ("00", "10", "20"):
        _append_signal(store, ts=f"2026-06-20T12:{minute}:00Z", source=_SECRET_SOURCE)

    block = {
        "type": "temporal_sensor",
        "enabled": True,
        "max_lookback_seconds": 3600,
        "predicate": {
            "kind": "recurrence",
            "state": "signals",
            "group_by": "source",
            "window_seconds": 3600,
            "threshold_count": 3,
        },
    }
    result = evaluate_temporal_block("leak_source", block, store=store, now="2026-06-20T12:30:00Z")
    assert result["emitted"] is True
    dumped = json.dumps(result)
    assert _SECRET_SOURCE not in dumped
    assert result["detail"]["group_value"] != _SECRET_SOURCE
    assert result["signal"]["temporal"]["group_value"] != _SECRET_SOURCE


def test_recurrence_group_by_actor_never_echoes_out_of_vocab_secret(store):
    """R4 run-466 repro: `actor` is likewise unvalidated at ingest against
    VALID_ACTORS, so group_by=actor must not echo a secret-shaped actor value."""
    for minute in ("00", "10", "20"):
        _append_signal(store, ts=f"2026-06-20T12:{minute}:00Z", actor=_SECRET_ACTOR)

    block = {
        "type": "temporal_sensor",
        "enabled": True,
        "max_lookback_seconds": 3600,
        "predicate": {
            "kind": "recurrence",
            "state": "signals",
            "group_by": "actor",
            "window_seconds": 3600,
            "threshold_count": 3,
        },
    }
    result = evaluate_temporal_block("leak_actor", block, store=store, now="2026-06-20T12:30:00Z")
    assert result["emitted"] is True
    dumped = json.dumps(result)
    assert _SECRET_ACTOR not in dumped
    assert result["detail"]["group_value"] != _SECRET_ACTOR
    assert result["signal"]["temporal"]["group_value"] != _SECRET_ACTOR


def test_source_refs_never_echo_secret_shaped_row_id(store):
    """R4 run-466 repro: normal ingest preserves caller-supplied `id`, so a
    secret-shaped id must not be echoed raw through signal.temporal.source_refs[].id."""
    for minute in ("00", "10", "20"):
        _append_signal(
            store,
            ts=f"2026-06-20T12:{minute}:00Z",
            row_id=f"{_SECRET_ID}-{minute}",
        )

    block = _rate_block()
    result = evaluate_temporal_block("leak_id", block, store=store, now="2026-06-20T12:30:00Z")
    assert result["emitted"] is True
    dumped = json.dumps(result)
    assert _SECRET_ID not in dumped
    for ref in result["signal"]["temporal"]["source_refs"]:
        assert _SECRET_ID not in ref["id"]


def test_consecutive_state_equals_never_echoes_secret_for_non_closed_field(store):
    """R4 run-466 repro: consecutive_state emitted raw `cfg.equals` via
    detail.equals/signal.temporal.equals; for non-closed free-text fields such as
    `kind`, a secret-shaped equality value must not leak."""
    for minute in ("00", "10", "20"):
        _append_signal(store, ts=f"2026-06-20T12:{minute}:00Z", kind=_SECRET_EQUALS)

    block = {
        "type": "temporal_sensor",
        "enabled": True,
        "max_lookback_seconds": 3600,
        "predicate": {
            "kind": "consecutive_state",
            "state": "signals",
            "match": {},
            "field": "kind",
            "equals": _SECRET_EQUALS,
            "count": 3,
        },
    }
    result = evaluate_temporal_block("leak_equals", block, store=store, now="2026-06-20T12:30:00Z")
    assert result["emitted"] is True
    dumped = json.dumps(result)
    assert _SECRET_EQUALS not in dumped
    assert result["detail"]["equals"] != _SECRET_EQUALS
    assert result["signal"]["temporal"]["equals"] != _SECRET_EQUALS
