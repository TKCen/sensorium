"""Deterministic temporal sensors over compact Sensorium JSONL state.

Temporal sensors evaluate closed, code-defined predicates over bounded windows of
existing compact Sensorium rows and emit compact trend signals. They do not read
raw transcripts/logs/files, call models, or expand the live tool surface.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .inner_life import BlockKind, TemporalPredicateKind
from .schemas import VALID_ACTORS, VALID_SENSITIVITIES, VALID_SOURCES, truncate_text, utc_now_iso

_ALLOWED_STATES = frozenset({"signals", "events", "candidates", "decisions"})
# Compact-safe classification fields: bounded, code-validated vocab (kind/type/sensor
# names, enum-like source/sensitivity/actor/status/outcome). Never includes freeform or
# raw content fields (summary, raw_transcript, caused_by, etc.) so that group_by/field
# predicates cannot be used to echo arbitrary row scalars (including secrets) back out
# through group_value or other detail keys.
_ALLOWED_MATCH_KEYS = frozenset(
    {"kind", "type", "sensor", "source", "sensitivity", "actor", "status", "outcome", "correlation_key"}
)
_ALLOWED_GROUP_BY_KEYS = frozenset(
    {"kind", "type", "sensor", "source", "sensitivity", "actor", "status", "outcome"}
)
_ALLOWED_FIELD_KEYS = _ALLOWED_GROUP_BY_KEYS | {"strength_hint"}
# NOTE: schemas.validate_signal() only enforces VALID_SENSITIVITIES at ingest; it does
# not validate source/actor against any closed vocabulary, so normal ingest happily
# accepts arbitrary (including secret-shaped) source/actor strings. We therefore cannot
# trust the *key name* alone to mean "safe to echo" -- we must also check the *value*
# against the fixed vocabulary below at the point of emission. kind/type/sensor/status/
# outcome have no closed vocabulary at all and their literal values must never be
# echoed back through group_value/equals/source_refs even though the key names
# themselves are allowlisted for selection (group_by/field choose which column to
# inspect, not its content).
_CLOSED_ENUM_VOCAB: dict[str, frozenset[str]] = {
    "source": frozenset(VALID_SOURCES),
    "sensitivity": frozenset(VALID_SENSITIVITIES),
    "actor": frozenset(VALID_ACTORS),
}
_CLOSED_ENUM_KEYS = frozenset(_CLOSED_ENUM_VOCAB)
_DEFAULT_MAX_LOOKBACK_SECONDS = 24 * 3600
_HARD_MAX_LOOKBACK_SECONDS = 7 * 24 * 3600
_DEFAULT_MAX_RECORDS = 500
_HARD_MAX_RECORDS = 2000
_DEFAULT_COOLDOWN_SECONDS = 0
_SUMMARY_MAX = 180
_REF_MAX = 160


class TemporalSensorConfigError(ValueError):
    """Raised when a temporal sensor block fails closed config validation."""


@dataclass(frozen=True)
class TemporalBlockConfig:
    block_id: str
    predicate: TemporalPredicateKind
    state: str
    match: dict[str, Any]
    window_seconds: int
    max_lookback_seconds: int
    max_records: int
    cooldown_seconds: int
    threshold_count: int
    group_by: str
    field: str
    equals: Any
    min_points: int
    threshold_slope: float
    numerator_match: dict[str, Any]
    denominator_match: dict[str, Any]
    threshold_ratio: float
    gap_seconds: int
    consecutive_count: int
    defaults: dict[str, Any]


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_dt(now: str | None) -> datetime:
    if now is None:
        parsed = _parse_iso(utc_now_iso())
    else:
        parsed = _parse_iso(now)
    if parsed is None:
        raise TemporalSensorConfigError("now must be an ISO timestamp")
    return parsed


def _positive_int(value: Any, *, label: str, default: int, minimum: int = 1, maximum: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise TemporalSensorConfigError(f"{label} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise TemporalSensorConfigError(f"{label} must be a positive integer") from exc
    if parsed < minimum:
        raise TemporalSensorConfigError(f"{label} must be >= {minimum}")
    return min(parsed, maximum)


def _nonnegative_int(value: Any, *, label: str, default: int, maximum: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise TemporalSensorConfigError(f"{label} must be a non-negative integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise TemporalSensorConfigError(f"{label} must be a non-negative integer") from exc
    if parsed < 0:
        raise TemporalSensorConfigError(f"{label} must be >= 0")
    return min(parsed, maximum)


def _number(value: Any, *, label: str, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool):
        raise TemporalSensorConfigError(f"{label} must be numeric")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise TemporalSensorConfigError(f"{label} must be numeric") from exc


def _mapping(value: Any, *, label: str, default: dict | None = None) -> dict:
    if value is None:
        return dict(default or {})
    if not isinstance(value, dict):
        raise TemporalSensorConfigError(f"{label} must be an object")
    return dict(value)


def _match_mapping(value: Any, *, label: str, default: dict | None = None) -> dict:
    mapping = _mapping(value, label=label, default=default)
    disallowed = sorted(set(mapping) - _ALLOWED_MATCH_KEYS)
    if disallowed:
        raise TemporalSensorConfigError(
            f"{label} may only reference compact-safe keys {sorted(_ALLOWED_MATCH_KEYS)}; got {disallowed}"
        )
    return mapping


def _scalar_key(value: Any, *, label: str, default: str, allowed: frozenset[str]) -> str:
    key = str(value) if value is not None else default
    if key not in allowed:
        raise TemporalSensorConfigError(f"{label} must be one of {sorted(allowed)}; got {key!r}")
    return key


def _state_name(value: Any) -> str:
    state = value if isinstance(value, str) else "signals"
    if state not in _ALLOWED_STATES:
        raise TemporalSensorConfigError(f"temporal state must be one of {sorted(_ALLOWED_STATES)}")
    return state


def _predicate_kind(value: Any) -> TemporalPredicateKind:
    if not isinstance(value, str):
        raise TemporalSensorConfigError("temporal predicate kind required")
    try:
        return TemporalPredicateKind(value)
    except ValueError as exc:
        raise TemporalSensorConfigError(f"unknown temporal predicate: {value}") from exc


def load_temporal_block_config(
    block_id: str,
    block: dict,
    *,
    policy: dict | None = None,
) -> TemporalBlockConfig:
    """Validate a temporal_sensor block and return normalized bounded config."""

    if not isinstance(block, dict) or block.get("type") != BlockKind.TEMPORAL_SENSOR.value:
        raise TemporalSensorConfigError(f"block {block_id} is not a temporal_sensor")
    predicate_cfg = _mapping(block.get("predicate"), label=f"{block_id}.predicate")
    predicate = _predicate_kind(predicate_cfg.get("kind"))

    caps = _mapping((policy or {}).get("caps"), label="policy.caps", default={}) if isinstance(policy, dict) else {}
    max_lookback_cap = _positive_int(
        caps.get("max_temporal_lookback_seconds"),
        label="policy.caps.max_temporal_lookback_seconds",
        default=_HARD_MAX_LOOKBACK_SECONDS,
        maximum=_HARD_MAX_LOOKBACK_SECONDS,
    )
    max_records_cap = _positive_int(
        caps.get("max_temporal_records"),
        label="policy.caps.max_temporal_records",
        default=_HARD_MAX_RECORDS,
        maximum=_HARD_MAX_RECORDS,
    )

    window_seconds = _positive_int(
        predicate_cfg.get("window_seconds") or block.get("window_seconds"),
        label=f"{block_id}.window_seconds",
        default=3600,
        maximum=max_lookback_cap,
    )
    max_lookback_seconds = _positive_int(
        block.get("max_lookback_seconds") or predicate_cfg.get("max_lookback_seconds"),
        label=f"{block_id}.max_lookback_seconds",
        default=min(_DEFAULT_MAX_LOOKBACK_SECONDS, max_lookback_cap),
        maximum=max_lookback_cap,
    )
    max_records = _positive_int(
        block.get("max_records") or predicate_cfg.get("max_records"),
        label=f"{block_id}.max_records",
        default=min(_DEFAULT_MAX_RECORDS, max_records_cap),
        maximum=max_records_cap,
    )
    cooldown_seconds = _nonnegative_int(
        block.get("cooldown_seconds") or predicate_cfg.get("cooldown_seconds"),
        label=f"{block_id}.cooldown_seconds",
        default=_DEFAULT_COOLDOWN_SECONDS,
        maximum=max_lookback_cap,
    )

    return TemporalBlockConfig(
        block_id=block_id,
        predicate=predicate,
        state=_state_name(predicate_cfg.get("state") or block.get("state")),
        match=_match_mapping(predicate_cfg.get("match"), label=f"{block_id}.predicate.match"),
        window_seconds=window_seconds,
        max_lookback_seconds=max_lookback_seconds,
        max_records=max_records,
        cooldown_seconds=cooldown_seconds,
        threshold_count=_positive_int(
            predicate_cfg.get("threshold_count"), label=f"{block_id}.threshold_count", default=1, maximum=max_records
        ),
        group_by=_scalar_key(
            predicate_cfg.get("group_by"), label=f"{block_id}.group_by", default="kind", allowed=_ALLOWED_GROUP_BY_KEYS
        ),
        field=_scalar_key(
            predicate_cfg.get("field"), label=f"{block_id}.field", default="strength_hint", allowed=_ALLOWED_FIELD_KEYS
        ),
        equals=predicate_cfg.get("equals"),
        min_points=_positive_int(
            predicate_cfg.get("min_points"), label=f"{block_id}.min_points", default=2, maximum=max_records
        ),
        threshold_slope=_number(
            predicate_cfg.get("threshold_slope"), label=f"{block_id}.threshold_slope", default=0.0
        ),
        numerator_match=_match_mapping(predicate_cfg.get("numerator_match"), label=f"{block_id}.numerator_match"),
        denominator_match=_match_mapping(predicate_cfg.get("denominator_match"), label=f"{block_id}.denominator_match"),
        threshold_ratio=_number(
            predicate_cfg.get("threshold_ratio"), label=f"{block_id}.threshold_ratio", default=1.0
        ),
        gap_seconds=_positive_int(
            predicate_cfg.get("gap_seconds"), label=f"{block_id}.gap_seconds", default=window_seconds, maximum=max_lookback_cap
        ),
        consecutive_count=_positive_int(
            predicate_cfg.get("count"), label=f"{block_id}.count", default=2, maximum=max_records
        ),
        defaults=_mapping(block.get("defaults"), label=f"{block_id}.defaults"),
    )


def _row_ts(row: dict) -> datetime | None:
    for key in ("ts", "created_at", "updated_at", "decided_at", "last_run_at"):
        parsed = _parse_iso(row.get(key))
        if parsed is not None:
            return parsed
    return None


def _value_at(row: dict, key: str) -> Any:
    value: Any = row
    for part in key.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _matches(row: dict, criteria: dict[str, Any]) -> bool:
    for key, expected in sorted(criteria.items()):
        if key == "correlation_key":
            keys = row.get("correlation_keys")
            if not isinstance(keys, list) or expected not in keys:
                return False
            continue
        actual = _value_at(row, key)
        if isinstance(expected, list):
            if actual not in expected:
                return False
        elif actual != expected:
            return False
    return True


def _bounded_rows(store, cfg: TemporalBlockConfig, now_dt: datetime) -> tuple[list[dict], dict]:
    cutoff = now_dt - timedelta(seconds=cfg.max_lookback_seconds)
    rows = store.read_jsonl(cfg.state, limit=cfg.max_records)
    bounded: list[tuple[datetime, dict]] = []
    skipped_no_ts = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        ts = _row_ts(row)
        if ts is None:
            skipped_no_ts += 1
            continue
        if cutoff < ts <= now_dt:
            bounded.append((ts, row))
    bounded.sort(key=lambda item: (item[0], str(item[1].get("id", ""))))
    return [row for _, row in bounded], {
        "state": cfg.state,
        "max_records": cfg.max_records,
        "max_lookback_seconds": cfg.max_lookback_seconds,
        "candidate_rows": len(bounded),
        "skipped_no_ts": skipped_no_ts,
        "cutoff": _format_iso(cutoff),
        "until": _format_iso(now_dt),
    }


def _window_rows(rows: list[dict], now_dt: datetime, seconds: int) -> list[dict]:
    cutoff = now_dt - timedelta(seconds=seconds)
    return [row for row in rows if (ts := _row_ts(row)) is not None and cutoff <= ts <= now_dt]


def _safe_scalar_label(key: str, value: Any) -> str:
    """Bounded label for an emitted scalar that never echoes raw row/config content.

    A value is only echoed verbatim when its *key* has a fixed, code-defined
    vocabulary (sensitivity/source/actor) and its *value* is actually a member of
    that vocabulary -- ingest does not validate source/actor, so a secret-shaped
    value can land in those columns despite the key being "closed". Anything else
    (including a closed-enum key holding an out-of-vocabulary value) is replaced
    with a deterministic derived label instead of the raw value.
    """
    if value is None:
        return ""
    vocab = _CLOSED_ENUM_VOCAB.get(key)
    if vocab is not None and isinstance(value, str) and value in vocab:
        return truncate_text(value, _REF_MAX)
    digest = hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()
    return f"{key}#{digest[:12]}"


def _row_ref(row: dict) -> dict:
    kind_key = "kind" if row.get("kind") is not None else "type"
    return {
        "id": _safe_scalar_label("id", row.get("id", "")),
        "ts": truncate_text(str(row.get("ts") or row.get("created_at") or row.get("updated_at") or ""), _REF_MAX),
        "kind": _safe_scalar_label(kind_key, row.get(kind_key)),
        "fingerprint": _safe_scalar_label("fingerprint", row.get("fingerprint", "")),
    }


def _stable_hash(payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _compact_signal(cfg: TemporalBlockConfig, now: str, detail: dict) -> dict:
    refs = [_row_ref(row) for row in detail.pop("source_rows", [])[:10]]
    temporal = {
        "predicate": cfg.predicate.value,
        "state": cfg.state,
        "window_seconds": detail.get("window_seconds", cfg.window_seconds),
        "count": detail.get("count", 0),
        "max_lookback_seconds": cfg.max_lookback_seconds,
        "source_refs": refs,
        **detail,
    }
    fingerprint = _stable_hash({"block_id": cfg.block_id, "temporal": temporal})
    default_keys = cfg.defaults.get("correlation_keys")
    correlation_keys = default_keys if isinstance(default_keys, list) else []
    correlation_keys = [str(key)[:_REF_MAX] for key in correlation_keys if isinstance(key, str)]
    correlation_keys.append(f"temporal:{cfg.block_id}")
    sensitivity = cfg.defaults.get("sensitivity", "private")
    if sensitivity not in {"local_only", "private", "public_safe"}:
        sensitivity = "private"
    surfaces = cfg.defaults.get("allowed_surfaces")
    if not isinstance(surfaces, list) or not all(isinstance(item, str) for item in surfaces):
        surfaces = ["local"]
    return {
        "id": f"sig_temporal_{fingerprint[:12]}",
        "ts": now,
        "sensor": f"sensorium.temporal.{cfg.block_id}",
        "source": "machine",
        "kind": "temporal_trend",
        "summary": truncate_text(
            f"Temporal trend {cfg.predicate.value} matched for {cfg.block_id}", _SUMMARY_MAX
        ),
        "strength_hint": min(1.0, max(0.1, float(detail.get("strength_hint", 0.6)))),
        "sensitivity": sensitivity,
        "allowed_surfaces": surfaces,
        "correlation_keys": sorted(set(correlation_keys)),
        "source_ref": f"temporal:{cfg.block_id}:{fingerprint[:16]}",
        "temporal": temporal,
    }


def _evaluate_rate_window(cfg: TemporalBlockConfig, rows: list[dict], now_dt: datetime) -> dict:
    window = _window_rows(rows, now_dt, cfg.window_seconds)
    matches = [row for row in window if _matches(row, cfg.match)]
    return {
        "met": len(matches) >= cfg.threshold_count,
        "detail": {
            "window_seconds": cfg.window_seconds,
            "count": len(matches),
            "threshold_count": cfg.threshold_count,
            "source_rows": matches,
        },
    }


def _evaluate_recurrence(cfg: TemporalBlockConfig, rows: list[dict], now_dt: datetime) -> dict:
    window = _window_rows(rows, now_dt, cfg.window_seconds)
    counts: dict[str, list[dict]] = {}
    raw_values: dict[str, Any] = {}
    for row in window:
        if not _matches(row, cfg.match):
            continue
        value = _value_at(row, cfg.group_by)
        if value is None:
            continue
        key = str(value)[:_REF_MAX]
        counts.setdefault(key, []).append(row)
        raw_values.setdefault(key, value)
    winner, winner_rows = max(counts.items(), key=lambda item: (len(item[1]), item[0]), default=("", []))
    group_value = _safe_scalar_label(cfg.group_by, raw_values.get(winner)) if winner_rows else ""
    return {
        "met": len(winner_rows) >= cfg.threshold_count,
        "detail": {
            "window_seconds": cfg.window_seconds,
            "count": len(winner_rows),
            "threshold_count": cfg.threshold_count,
            "group_by": cfg.group_by,
            "group_value": group_value,
            "source_rows": winner_rows,
        },
    }


def _evaluate_slope(cfg: TemporalBlockConfig, rows: list[dict], now_dt: datetime) -> dict:
    points: list[tuple[float, float, dict]] = []
    for row in _window_rows(rows, now_dt, cfg.window_seconds):
        if not _matches(row, cfg.match):
            continue
        ts = _row_ts(row)
        value = _value_at(row, cfg.field)
        if ts is None or isinstance(value, bool):
            continue
        try:
            y = float(value)
        except (TypeError, ValueError):
            continue
        points.append((ts.timestamp(), y, row))
    if len(points) < cfg.min_points:
        slope = 0.0
    else:
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        x_mean = sum(xs) / len(xs)
        y_mean = sum(ys) / len(ys)
        denom = sum((x - x_mean) ** 2 for x in xs)
        slope = 0.0 if denom == 0 else sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / denom
    return {
        "met": len(points) >= cfg.min_points and slope >= cfg.threshold_slope,
        "detail": {
            "window_seconds": cfg.window_seconds,
            "count": len(points),
            "field": cfg.field,
            "slope": round(slope, 8),
            "threshold_slope": cfg.threshold_slope,
            "min_points": cfg.min_points,
            "source_rows": [point[2] for point in points],
        },
    }


def _evaluate_ratio(cfg: TemporalBlockConfig, rows: list[dict], now_dt: datetime) -> dict:
    window = _window_rows(rows, now_dt, cfg.window_seconds)
    denominator = [row for row in window if _matches(row, cfg.denominator_match)]
    numerator = [row for row in denominator if _matches(row, cfg.numerator_match)]
    ratio = 0.0 if not denominator else len(numerator) / len(denominator)
    return {
        "met": bool(denominator) and ratio >= cfg.threshold_ratio,
        "detail": {
            "window_seconds": cfg.window_seconds,
            "count": len(numerator),
            "denominator_count": len(denominator),
            "ratio": round(ratio, 6),
            "threshold_ratio": cfg.threshold_ratio,
            "source_rows": numerator,
        },
    }


def _evaluate_gap(cfg: TemporalBlockConfig, rows: list[dict], now_dt: datetime) -> dict:
    matches = [row for row in rows if _matches(row, cfg.match)]
    latest_ts = max((_row_ts(row) for row in matches), default=None)
    elapsed = None if latest_ts is None else int((now_dt - latest_ts).total_seconds())
    met = latest_ts is None or (elapsed is not None and elapsed >= cfg.gap_seconds)
    return {
        "met": met,
        "detail": {
            "window_seconds": cfg.gap_seconds,
            "count": 0 if latest_ts is None else 1,
            "gap_seconds": elapsed,
            "threshold_gap_seconds": cfg.gap_seconds,
            "source_rows": matches[-1:] if latest_ts is not None else [],
        },
    }


def _evaluate_consecutive_state(cfg: TemporalBlockConfig, rows: list[dict], now_dt: datetime) -> dict:
    window = _window_rows(rows, now_dt, cfg.window_seconds)
    filtered = [row for row in window if _matches(row, cfg.match)] if cfg.match else window
    recent = filtered[-cfg.consecutive_count :]
    met = len(recent) == cfg.consecutive_count and all(_value_at(row, cfg.field) == cfg.equals for row in recent)
    return {
        "met": met,
        "detail": {
            "window_seconds": cfg.window_seconds,
            "count": len(recent),
            "field": cfg.field,
            "equals": _safe_scalar_label(cfg.field, cfg.equals),
            "consecutive_count": cfg.consecutive_count,
            "source_rows": recent,
        },
    }


_EVALUATORS = {
    TemporalPredicateKind.RATE_WINDOW: _evaluate_rate_window,
    TemporalPredicateKind.RECURRENCE: _evaluate_recurrence,
    TemporalPredicateKind.SLOPE: _evaluate_slope,
    TemporalPredicateKind.RATIO: _evaluate_ratio,
    TemporalPredicateKind.GAP: _evaluate_gap,
    TemporalPredicateKind.CONSECUTIVE_STATE: _evaluate_consecutive_state,
}


def _cooldown_active(store, cfg: TemporalBlockConfig, now_dt: datetime) -> tuple[bool, str]:
    if cfg.cooldown_seconds <= 0:
        return False, ""
    state = store.read_block_run_state(cfg.block_id)
    last = _parse_iso(state.get("last_emitted_at"))
    if last is None:
        return False, ""
    next_allowed = last + timedelta(seconds=cfg.cooldown_seconds)
    if now_dt < next_allowed:
        return True, _format_iso(next_allowed)
    return False, ""


def evaluate_temporal_block(block_id: str, block: dict, *, store, now: str | None = None) -> dict:
    """Evaluate a temporal block against bounded compact state without ingesting."""

    now_dt = _now_dt(now)
    now_iso = _format_iso(now_dt)
    cfg = load_temporal_block_config(block_id, block, policy=store.read_sensor_policy())
    cooldown, next_allowed = _cooldown_active(store, cfg, now_dt)
    rows, window_info = _bounded_rows(store, cfg, now_dt)
    if cooldown:
        return {
            "sampled": True,
            "emitted": False,
            "reason": "cooldown_active",
            "cooldown_active": True,
            "next_allowed_at": next_allowed,
            "window": window_info,
        }
    evaluated = _EVALUATORS[cfg.predicate](cfg, rows, now_dt)
    detail = evaluated["detail"]
    result = {
        "sampled": True,
        "emitted": bool(evaluated["met"]),
        "reason": "predicate_met" if evaluated["met"] else "predicate_not_met",
        "predicate": cfg.predicate.value,
        "window": window_info,
        "detail": {key: value for key, value in detail.items() if key != "source_rows"},
    }
    if evaluated["met"]:
        result["signal"] = _compact_signal(cfg, now_iso, dict(detail))
    return result
