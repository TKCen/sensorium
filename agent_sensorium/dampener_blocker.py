"""Dampener and blocker block semantics over bounded run-state telemetry.

Dampeners are soft, reversible pressure controls (cooldown/rate_limit/decay/
suppress_duplicate/sample). They only ever read/write small per-block
run-state telemetry sidecars (`sensors/run_state/<id>.json`) plus an
append-only audit trace (`inner_life/dampeners.jsonl`) -- they never mutate
the durable signal/event/candidate/decision spine, and their effect on
pressure is always recomputable/finite (bounded by an explicit cooldown,
window, half-life, TTL, or sample stride).

Blockers are hard stops: they always terminate the path and must trace to a
policy/schema/authority/privacy/safety reason. Blocker evaluation has no
hidden state -- it fails closed if the required provenance for its
`reason_kind` is missing.

No model calls. No network. No subprocess. Deterministic given fixed input
config/state/`now`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .inner_life import BlockerReasonKind, BlockKind, DampenerModeKind
from .schemas import truncate_text, utc_now_iso

_HARD_MAX_SECONDS = 7 * 24 * 3600
_HARD_MAX_PASSES = 100000
_HARD_SAMPLE_EVERY = 10000
_REASON_MAX = 280
_REF_MAX = 160


class DampenerBlockerConfigError(ValueError):
    """Raised when a dampener or blocker block fails closed config validation."""


def _run_state_label(block_id: str) -> str:
    digest = hashlib.sha256((block_id or "").encode("utf-8")).hexdigest()
    return f"block_{digest}"


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
        raise DampenerBlockerConfigError("now must be an ISO timestamp")
    return parsed


def _positive_int(value: Any, *, label: str, default: int, maximum: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise DampenerBlockerConfigError(f"{label} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise DampenerBlockerConfigError(f"{label} must be a positive integer") from exc
    if parsed <= 0:
        raise DampenerBlockerConfigError(f"{label} must be a positive integer")
    return min(parsed, maximum)


def _require_positive_int(value: Any, *, label: str, maximum: int) -> int:
    """Like `_positive_int` but fails closed when the bound is not explicit."""
    if value is None:
        raise DampenerBlockerConfigError(f"{label} must be explicitly set to a positive integer")
    return _positive_int(value, label=label, default=0, maximum=maximum)


def _safe_key_label(key: str) -> str:
    """Derive a deterministic, non-reversible label for a caller-supplied dedupe key.

    Caller keys may carry sensitive content (e.g. secrets), so only the hash is
    ever persisted to run-state or emitted in effects -- never the raw key.
    """
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _safe_scalar_label(prefix: str, value: Any) -> str | None:
    """Derive a deterministic, non-reversible label for a caller/operator-controlled scalar.

    Block ids, targets, and policy/schema/authority references are free-form
    strings that may carry sensitive content (e.g. secrets embedded by a
    caller). They are never echoed verbatim into returned effects or audit
    sidecars -- only a stable hash label, so the same input always resolves
    to the same label without exposing the raw value.
    """
    if value is None:
        return None
    try:
        seed = json.dumps(value, default=str, sort_keys=True)
    except TypeError:
        seed = str(value)
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return f"{prefix}#{digest}"


@dataclass(frozen=True)
class DampenerBlockConfig:
    block_id: str
    mode: DampenerModeKind
    cooldown_seconds: int
    rate_limit_max: int
    rate_limit_window_seconds: int
    decay_half_life_seconds: int
    dedup_ttl_seconds: int
    sample_every: int


def load_dampener_block_config(block_id: str, block: dict) -> DampenerBlockConfig:
    """Validate a dampener block and return normalized, bounded config.

    Every mode carries at least one finite bound (cooldown/window/half-life/
    TTL/stride) so a dampener can never silently become an unbounded
    accumulator.
    """
    if not isinstance(block, dict) or block.get("type") != BlockKind.DAMPENER.value:
        raise DampenerBlockerConfigError(f"block {block_id} is not a dampener")
    mode_value = block.get("mode")
    if not isinstance(mode_value, str):
        raise DampenerBlockerConfigError(f"dampener {block_id} requires mode")
    try:
        mode = DampenerModeKind(mode_value)
    except ValueError as exc:
        raise DampenerBlockerConfigError(f"unknown dampener mode for {block_id}: {mode_value}") from exc

    if mode is DampenerModeKind.COOLDOWN:
        cooldown_seconds = _require_positive_int(
            block.get("cooldown_seconds"), label=f"{block_id}.cooldown_seconds", maximum=_HARD_MAX_SECONDS
        )
    else:
        cooldown_seconds = _positive_int(
            block.get("cooldown_seconds"), label=f"{block_id}.cooldown_seconds", default=60, maximum=_HARD_MAX_SECONDS
        )

    if mode is DampenerModeKind.RATE_LIMIT:
        rate_limit_window_seconds = _require_positive_int(
            block.get("rate_limit_window_seconds") or block.get("window_seconds"),
            label=f"{block_id}.rate_limit_window_seconds",
            maximum=_HARD_MAX_SECONDS,
        )
        rate_limit_max = _require_positive_int(
            block.get("rate_limit_max"), label=f"{block_id}.rate_limit_max", maximum=_HARD_MAX_PASSES
        )
    else:
        rate_limit_window_seconds = _positive_int(
            block.get("rate_limit_window_seconds") or block.get("window_seconds"),
            label=f"{block_id}.rate_limit_window_seconds",
            default=3600,
            maximum=_HARD_MAX_SECONDS,
        )
        rate_limit_max = _positive_int(
            block.get("rate_limit_max"), label=f"{block_id}.rate_limit_max", default=3, maximum=_HARD_MAX_PASSES
        )

    if mode is DampenerModeKind.DECAY:
        decay_half_life_seconds = _require_positive_int(
            block.get("decay_half_life_seconds"),
            label=f"{block_id}.decay_half_life_seconds",
            maximum=_HARD_MAX_SECONDS,
        )
    else:
        decay_half_life_seconds = _positive_int(
            block.get("decay_half_life_seconds"),
            label=f"{block_id}.decay_half_life_seconds",
            default=600,
            maximum=_HARD_MAX_SECONDS,
        )

    if mode is DampenerModeKind.SUPPRESS_DUPLICATE:
        dedup_ttl_seconds = _require_positive_int(
            block.get("dedup_ttl_seconds") or block.get("ttl_seconds"),
            label=f"{block_id}.dedup_ttl_seconds",
            maximum=_HARD_MAX_SECONDS,
        )
    else:
        dedup_ttl_seconds = _positive_int(
            block.get("dedup_ttl_seconds") or block.get("ttl_seconds"),
            label=f"{block_id}.dedup_ttl_seconds",
            default=1800,
            maximum=_HARD_MAX_SECONDS,
        )

    if mode is DampenerModeKind.SAMPLE:
        sample_every = _require_positive_int(
            block.get("sample_every"), label=f"{block_id}.sample_every", maximum=_HARD_SAMPLE_EVERY
        )
    else:
        sample_every = _positive_int(
            block.get("sample_every"), label=f"{block_id}.sample_every", default=2, maximum=_HARD_SAMPLE_EVERY
        )

    return DampenerBlockConfig(
        block_id=block_id,
        mode=mode,
        cooldown_seconds=cooldown_seconds,
        rate_limit_max=rate_limit_max,
        rate_limit_window_seconds=rate_limit_window_seconds,
        decay_half_life_seconds=decay_half_life_seconds,
        dedup_ttl_seconds=dedup_ttl_seconds,
        sample_every=sample_every,
    )


@dataclass(frozen=True)
class BlockerBlockConfig:
    block_id: str
    reason_kind: BlockerReasonKind
    reason: str
    policy_id: str | None
    schema_ref: str | None
    authority_ref: str | None


_REASON_PROVENANCE_FIELD = {
    BlockerReasonKind.POLICY: "policy_id",
    BlockerReasonKind.SCHEMA: "schema_ref",
    BlockerReasonKind.AUTHORITY: "authority_ref",
}


def load_blocker_block_config(block_id: str, block: dict) -> BlockerBlockConfig:
    """Validate a blocker block. Fails closed unless it traces to a concrete reason.

    `privacy` and `safety` reasons must still carry a non-empty `reason`
    string; `policy`/`schema`/`authority` additionally require their
    matching provenance reference (`policy_id`/`schema_ref`/`authority_ref`)
    so a blocker effect can never be "this is blocked" with no traceable
    cause.
    """
    if not isinstance(block, dict) or block.get("type") != BlockKind.BLOCKER.value:
        raise DampenerBlockerConfigError(f"block {block_id} is not a blocker")
    reason_value = block.get("reason_kind")
    if not isinstance(reason_value, str):
        raise DampenerBlockerConfigError(f"blocker {block_id} requires reason_kind")
    try:
        reason_kind = BlockerReasonKind(reason_value)
    except ValueError as exc:
        raise DampenerBlockerConfigError(f"unknown blocker reason_kind for {block_id}: {reason_value}") from exc

    reason = block.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise DampenerBlockerConfigError(f"blocker {block_id} requires a non-empty reason")

    policy_id = block.get("policy_id")
    schema_ref = block.get("schema_ref")
    authority_ref = block.get("authority_ref")

    required_field = _REASON_PROVENANCE_FIELD.get(reason_kind)
    if required_field is not None:
        value = block.get(required_field)
        if not isinstance(value, str) or not value.strip():
            raise DampenerBlockerConfigError(
                f"blocker {block_id} reason_kind {reason_kind.value!r} requires {required_field}"
            )

    return BlockerBlockConfig(
        block_id=block_id,
        reason_kind=reason_kind,
        reason=truncate_text(reason.strip(), _REASON_MAX),
        policy_id=truncate_text(policy_id, _REF_MAX) if isinstance(policy_id, str) else None,
        schema_ref=truncate_text(schema_ref, _REF_MAX) if isinstance(schema_ref, str) else None,
        authority_ref=truncate_text(authority_ref, _REF_MAX) if isinstance(authority_ref, str) else None,
    )


def _read_run_state(store, block_id: str) -> dict:
    return store.read_block_run_state(_run_state_label(block_id))


def _write_run_state(store, block_id: str, state: dict) -> None:
    store.write_block_run_state(_run_state_label(block_id), state)


def _evaluate_cooldown(cfg: DampenerBlockConfig, *, state: dict, now_dt: datetime, pressure: float) -> tuple[dict, dict]:
    last = _parse_iso(state.get("dampener_last_pass_at"))
    if last is not None and now_dt < last + timedelta(seconds=cfg.cooldown_seconds):
        expires_at = _format_iso(last + timedelta(seconds=cfg.cooldown_seconds))
        return (
            {"after_pressure": 0.0, "reason": "cooldown_active", "expires_at": expires_at},
            state,
        )
    next_state = dict(state)
    next_state["dampener_last_pass_at"] = _format_iso(now_dt)
    expires_at = _format_iso(now_dt + timedelta(seconds=cfg.cooldown_seconds))
    return (
        {"after_pressure": pressure, "reason": "cooldown_elapsed", "expires_at": expires_at},
        next_state,
    )


def _evaluate_rate_limit(cfg: DampenerBlockConfig, *, state: dict, now_dt: datetime, pressure: float) -> tuple[dict, dict]:
    window_start = _parse_iso(state.get("dampener_window_start"))
    count = state.get("dampener_window_count", 0)
    if not isinstance(count, int) or window_start is None or now_dt >= window_start + timedelta(
        seconds=cfg.rate_limit_window_seconds
    ):
        window_start = now_dt
        count = 0
    expires_at = _format_iso(window_start + timedelta(seconds=cfg.rate_limit_window_seconds))
    if count >= cfg.rate_limit_max:
        next_state = dict(state)
        next_state["dampener_window_start"] = _format_iso(window_start)
        next_state["dampener_window_count"] = count
        return (
            {"after_pressure": 0.0, "reason": "rate_limit_exceeded", "expires_at": expires_at},
            next_state,
        )
    next_state = dict(state)
    next_state["dampener_window_start"] = _format_iso(window_start)
    next_state["dampener_window_count"] = count + 1
    return (
        {"after_pressure": pressure, "reason": "rate_limit_ok", "expires_at": expires_at},
        next_state,
    )


def _evaluate_decay(cfg: DampenerBlockConfig, *, state: dict, now_dt: datetime, pressure: float) -> tuple[dict, dict]:
    reference = _parse_iso(state.get("dampener_reference_at"))
    if reference is None:
        reference = now_dt
    elapsed = max(0.0, (now_dt - reference).total_seconds())
    half_life = cfg.decay_half_life_seconds
    decayed = pressure * (0.5 ** (elapsed / half_life)) if half_life > 0 else pressure
    decayed = max(0.0, min(pressure, decayed))
    next_state = dict(state)
    next_state["dampener_reference_at"] = _format_iso(reference)
    expires_at = _format_iso(reference + timedelta(seconds=half_life * 10))
    return (
        {"after_pressure": round(decayed, 6), "reason": "decayed", "expires_at": expires_at},
        next_state,
    )


def _evaluate_suppress_duplicate(
    cfg: DampenerBlockConfig, *, state: dict, now_dt: datetime, pressure: float, key: str | None
) -> tuple[dict, dict]:
    dedup_key = _safe_key_label(key or "default")
    seen = state.get("dampener_seen_keys")
    seen = dict(seen) if isinstance(seen, dict) else {}
    pruned = {
        existing_key: expiry
        for existing_key, expiry in seen.items()
        if isinstance(expiry, str) and (_parse_iso(expiry) or now_dt) > now_dt
    }
    expiry_dt = now_dt + timedelta(seconds=cfg.dedup_ttl_seconds)
    existing_expiry = _parse_iso(pruned.get(dedup_key))
    if existing_expiry is not None and now_dt < existing_expiry:
        pruned[dedup_key] = _format_iso(existing_expiry)
        next_state = dict(state)
        next_state["dampener_seen_keys"] = pruned
        return (
            {"after_pressure": 0.0, "reason": "duplicate_suppressed", "expires_at": _format_iso(existing_expiry)},
            next_state,
        )
    pruned[dedup_key] = _format_iso(expiry_dt)
    next_state = dict(state)
    next_state["dampener_seen_keys"] = pruned
    return (
        {"after_pressure": pressure, "reason": "first_seen", "expires_at": _format_iso(expiry_dt)},
        next_state,
    )


def _evaluate_sample(cfg: DampenerBlockConfig, *, state: dict, now_dt: datetime, pressure: float) -> tuple[dict, dict]:
    counter = state.get("dampener_sample_counter", 0)
    counter = counter + 1 if isinstance(counter, int) else 1
    next_state = dict(state)
    next_state["dampener_sample_counter"] = counter % cfg.sample_every
    passes = counter % cfg.sample_every == 0
    return (
        {
            "after_pressure": pressure if passes else 0.0,
            "reason": "sampled_pass" if passes else "sampled_skip",
            "expires_at": None,
        },
        next_state,
    )


_MODE_EVALUATORS = {
    DampenerModeKind.COOLDOWN: lambda cfg, **kw: _evaluate_cooldown(cfg, **kw),
    DampenerModeKind.RATE_LIMIT: lambda cfg, **kw: _evaluate_rate_limit(cfg, **kw),
    DampenerModeKind.DECAY: lambda cfg, **kw: _evaluate_decay(cfg, **kw),
    DampenerModeKind.SUPPRESS_DUPLICATE: lambda cfg, **kw: _evaluate_suppress_duplicate(cfg, **kw),
    DampenerModeKind.SAMPLE: lambda cfg, **kw: _evaluate_sample(cfg, **kw),
}


def run_dampener_block(
    block_id: str,
    block: dict,
    *,
    store,
    now: str | None = None,
    pressure: float = 1.0,
    key: str | None = None,
    target: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Evaluate a dampener block against bounded run-state telemetry.

    Never touches the durable signal/event/candidate/decision spine. State
    mutation is confined to the small per-block run-state sidecar, and the
    decision is always recomputable from `now` plus that sidecar -- i.e. it
    is reversible (time/context can flip the outcome) and finite (every mode
    carries an explicit bound from `load_dampener_block_config`).
    """
    cfg = load_dampener_block_config(block_id, block)
    now_dt = _now_dt(now)
    state = _read_run_state(store, block_id)
    kwargs: dict[str, Any] = {"state": state, "now_dt": now_dt, "pressure": pressure}
    if cfg.mode is DampenerModeKind.SUPPRESS_DUPLICATE:
        kwargs["key"] = key
    outcome, next_state = _MODE_EVALUATORS[cfg.mode](cfg, **kwargs)

    effect = {
        "type": "dampener_effect",
        "source_node": _safe_scalar_label("node", block_id),
        "target_node": _safe_scalar_label("node", target),
        "mode": cfg.mode.value,
        "before_pressure": round(float(pressure), 6),
        "after_pressure": round(float(outcome["after_pressure"]), 6),
        "reason": outcome["reason"],
        "expires_at": outcome["expires_at"],
        "evaluated_at": _format_iso(now_dt),
        "precedence": "dampens",
    }

    if not dry_run:
        _write_run_state(store, block_id, next_state)
        store.append_dampener_effect(effect)
    return effect


def evaluate_blocker_block(
    block_id: str,
    block: dict,
    *,
    target: str | None = None,
    now: str | None = None,
) -> dict:
    """Evaluate a blocker block. A blocker always terminates the path.

    Returns a `blocker_effect` record that traces to the configured
    policy/schema/authority/privacy/safety reason. Raises
    `DampenerBlockerConfigError` (fail closed) if the block does not declare
    a concrete, traceable reason.
    """
    cfg = load_blocker_block_config(block_id, block)
    now_dt = _now_dt(now)
    return {
        "type": "blocker_effect",
        "source_node": _safe_scalar_label("node", block_id),
        "target_node": _safe_scalar_label("node", target),
        "policy_id": _safe_scalar_label("ref", cfg.policy_id),
        "schema_ref": _safe_scalar_label("ref", cfg.schema_ref),
        "authority_ref": _safe_scalar_label("ref", cfg.authority_ref),
        "reason_kind": cfg.reason_kind.value,
        "reason_label": _safe_scalar_label("reason", cfg.reason),
        "blocked_at": _format_iso(now_dt),
        "precedence": "blocks",
    }


def run_blocker_block(
    block_id: str,
    block: dict,
    *,
    store,
    target: str | None = None,
    now: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Evaluate a blocker block and append it to the append-only audit trace."""
    effect = evaluate_blocker_block(block_id, block, target=target, now=now)
    if not dry_run:
        store.append_blocker_effect(effect)
    return effect
