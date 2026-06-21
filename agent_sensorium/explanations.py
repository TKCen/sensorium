"""Deterministic pressure explanation builder over compact Sensorium state.

This is a pure, read-only substrate. Given a subject reference (a candidate /
review / candidate-like compact record id), the already-loaded compact store
rows, and optional dampener/blocker and temporal evidence, it assembles a
single explanation dict that answers "why did this candidate/review surface,
and what pushed back?" without leaking raw transcript/secret content.

Hard guarantees:
- No model calls, no network, no subprocess, no graph/vector DB, no new
  runtime storage. It only reads dicts the caller already loaded.
- Deterministic: same inputs + same config -> JSON-identical output every
  time. No wall-clock/random/uuid; any timestamp is taken from the input rows
  or an explicitly supplied `now`.
- Output-boundary safe: every free-form scalar that originates from a
  caller/config/row (ids, summaries, reasons, source refs, provenance,
  labels) is passed through a deterministic, non-reversible safe-label/hash
  transform before being placed in the explanation, never echoed verbatim.
  This reuses the same `_safe_scalar_label` pattern established in
  `temporal_sensor.py` / `dampener_blocker.py` (T4/T5 "safe labels").
- Fails soft: a missing referenced record / source ref / unknown field
  becomes a `warnings` entry, never an exception.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

from .inner_life import DampenerModeKind
from .schemas import truncate_text

# Bounds mirror the conventions already used by temporal_sensor / dampener_blocker.
_REF_MAX = 160
_LABEL_MAX = 64
_MAX_COMPONENTS = 64
_MAX_INHIBITORS = 64
_MAX_SOURCE_REFS = 24
_MAX_WARNINGS = 64

# Compact, code-defined vocab that is always safe to echo verbatim. These are
# closed enum-like classification values produced by Sensorium itself, never
# free-form caller content. Anything outside this set is hash-labeled.
_SAFE_KINDS = frozenset(
    {
        "subconscious_advisory",
        "temporal_trend",
        "gateway_error",
        "heartbeat",
        "candidate",
        "review",
        "consciousness_review",
    }
)
_SAFE_STATUSES = frozenset(
    {
        "candidate",
        "reviewed",
        "suppressed",
        "dropped",
        "settled",
        "dormant",
        "held",
        "closed",
        "archived",
        "active",
        "paused",
        "deprecated",
    }
)
# Component kinds are a closed, code-defined taxonomy of contribution sources.
_COMPONENT_KINDS = frozenset(
    {
        "base_pressure",
        "strength_hint",
        "temporal_trend",
        "correlation",
        "source_event",
    }
)
# Inhibitor kinds are a closed, code-defined taxonomy of suppression sources.
_INHIBITOR_KINDS = frozenset({"dampener", "blocker", "policy_gate"})


class ExplanationError(ValueError):
    """Reserved for misuse of the explanation API itself (not data gaps).

    Data gaps (missing records/refs/fields) are intentionally NOT raised; they
    are degraded into `warnings`. This type exists so a genuinely broken call
    (e.g. a non-mapping config) can still fail loudly if a caller wants it to,
    but the default builder never raises on missing data.
    """


def _safe_scalar_label(prefix: str, value: Any) -> str | None:
    """Deterministic, non-reversible label for a caller/row/config scalar.

    Ids, summaries, reasons, refs and provenance values are free-form strings
    that may carry sensitive content (e.g. embedded secrets). They are never
    echoed verbatim into the explanation -- only a stable hash label, so the
    same input always resolves to the same label without exposing the raw
    value. Mirrors `temporal_sensor._safe_scalar_label` /
    `dampener_blocker._safe_scalar_label`.
    """
    if value is None:
        return None
    try:
        seed = json.dumps(value, default=str, sort_keys=True)
    except TypeError:
        seed = str(value)
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return f"{prefix}#{digest[:12]}"


def _safe_classification(value: Any, *, allowed: frozenset[str], prefix: str) -> str | None:
    """Echo a value only if it is a member of a closed, code-defined vocab.

    Ingest does not validate kind/status against any closed vocabulary, so a
    secret-shaped value can land in those columns. We therefore check the
    *value* against the fixed vocab at emission time -- anything else is
    hash-labeled instead of echoed.
    """
    if value is None:
        return None
    if isinstance(value, str) and value in allowed:
        return value
    return _safe_scalar_label(prefix, value)


def _safe_number(value: Any) -> float | None:
    """Coerce a numeric scalar to a bounded float, or None if not numeric.

    Booleans are rejected (they are not pressures). Numbers are clamped to a
    finite, JSON-stable representation via round() so replay stays byte-stable.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result or result in (float("inf"), float("-inf")):  # NaN/inf guard
        return None
    return round(result, 6)


def _subject_ref(record: dict | None, subject_id: Any) -> dict:
    """Build the safe-labeled subject reference for the explanation header."""
    if isinstance(record, dict):
        raw_id = record.get("id", subject_id)
        kind = record.get("kind")
        status = record.get("status")
    else:
        raw_id = subject_id
        kind = None
        status = None
    return {
        "id": _safe_scalar_label("subject", raw_id) or _safe_scalar_label("subject", subject_id),
        "kind": _safe_classification(kind, allowed=_SAFE_KINDS, prefix="kind"),
        "status": _safe_classification(status, allowed=_SAFE_STATUSES, prefix="status"),
    }


def _safe_source_refs(values: Iterable[Any], *, prefix: str) -> list[str]:
    """Hash-label an iterable of free-form source references, bounded + sorted."""
    refs: list[str] = []
    for value in values:
        label = _safe_scalar_label(prefix, value)
        if label is not None:
            refs.append(label)
    # Deterministic order, de-duplicated, bounded.
    return sorted(set(refs))[:_MAX_SOURCE_REFS]


def _pressure_components(record: dict, *, temporal_evidence: list[dict]) -> tuple[list[dict], list[str]]:
    """Derive deterministic pressure-contribution components from a record.

    Each component is {kind, label, value (when safe/numeric), source_refs}.
    Free-form scalars are always safe-labeled. `warnings` collects any field
    that was referenced but missing/unusable.
    """
    components: list[dict] = []
    warnings: list[str] = []

    base = _safe_number(record.get("pressure"))
    if base is not None:
        components.append(
            {
                "kind": "base_pressure",
                "label": "recorded candidate pressure",
                "value": base,
                "source_refs": [_safe_scalar_label("subject", record.get("id"))] if record.get("id") else [],
            }
        )
    else:
        warnings.append("missing_or_invalid_pressure")

    strength = _safe_number(record.get("strength_hint"))
    if strength is not None:
        components.append(
            {
                "kind": "strength_hint",
                "label": "signal strength hint",
                "value": strength,
                "source_refs": [],
            }
        )

    event_ids = record.get("event_ids")
    if isinstance(event_ids, list) and event_ids:
        components.append(
            {
                "kind": "source_event",
                "label": "source events",
                "value": float(len(event_ids)),
                "source_refs": _safe_source_refs(event_ids, prefix="event"),
            }
        )
    elif event_ids is not None and not isinstance(event_ids, list):
        warnings.append("event_ids_not_a_list")

    correlation_keys = record.get("correlation_keys")
    if isinstance(correlation_keys, list) and correlation_keys:
        components.append(
            {
                "kind": "correlation",
                "label": "correlation keys",
                "value": float(len(correlation_keys)),
                "source_refs": _safe_source_refs(correlation_keys, prefix="corr"),
            }
        )
    elif correlation_keys is not None and not isinstance(correlation_keys, list):
        warnings.append("correlation_keys_not_a_list")

    for evidence in temporal_evidence:
        if not isinstance(evidence, dict):
            warnings.append("temporal_evidence_not_a_mapping")
            continue
        temporal = evidence.get("temporal") if isinstance(evidence.get("temporal"), dict) else evidence
        predicate = temporal.get("predicate")
        count = _safe_number(temporal.get("count"))
        refs_source: list[Any] = []
        for ref in temporal.get("source_refs") or []:
            if isinstance(ref, dict):
                refs_source.append(ref.get("id"))
            else:
                refs_source.append(ref)
        components.append(
            {
                "kind": "temporal_trend",
                "label": _safe_classification(predicate, allowed=_SAFE_KINDS, prefix="predicate")
                or _safe_scalar_label("predicate", predicate)
                or "temporal trend",
                "value": count,
                "source_refs": _safe_source_refs(
                    [r for r in refs_source if r is not None] or [evidence.get("id")],
                    prefix="temporal",
                ),
            }
        )

    components = [_normalize_component(c) for c in components][:_MAX_COMPONENTS]
    components.sort(key=_component_sort_key)
    return components, warnings


def _normalize_component(component: dict) -> dict:
    kind = component.get("kind")
    safe_kind = kind if kind in _COMPONENT_KINDS else (_safe_scalar_label("component", kind) or "unknown")
    label = component.get("label")
    safe_label = truncate_text(str(label), _LABEL_MAX) if label is not None else ""
    value = component.get("value")
    normalized = {
        "kind": safe_kind,
        "label": safe_label,
        "source_refs": list(component.get("source_refs") or []),
    }
    safe_value = _safe_number(value)
    if safe_value is not None:
        normalized["value"] = safe_value
    return normalized


def _component_sort_key(component: dict) -> tuple:
    return (str(component.get("kind")), str(component.get("label")), json.dumps(component.get("source_refs"), sort_keys=True))


def _inhibitor_from_dampener(effect: dict) -> dict:
    """Safe-labeled inhibitor view of a dampener_effect record."""
    before = _safe_number(effect.get("before_pressure"))
    after = _safe_number(effect.get("after_pressure"))
    mode = effect.get("mode")
    inhibitor = {
        "kind": "dampener",
        "label": _safe_classification(mode, allowed=_DAMPENER_MODES, prefix="mode") or "dampener",
        "reason": _safe_classification(effect.get("reason"), allowed=_DAMPENER_REASONS, prefix="reason"),
        # source_node/target_node in a dampener_effect are ALREADY safe hash
        # labels (node#...), but we defensively re-label so this stays safe even
        # if a caller hands in a raw, unlabeled effect dict.
        "source_refs": _safe_source_refs(
            [effect.get("source_node"), effect.get("target_node")],
            prefix="node",
        ),
    }
    if before is not None:
        inhibitor["before_pressure"] = before
    if after is not None:
        inhibitor["after_pressure"] = after
    return inhibitor


# Closed dampener mode vocab from inner_life.DampenerModeKind. A dampener
# sidecar is caller/sensor-owned data, not validated against this enum at
# ingest, so an unknown/secret-shaped `mode` must never be echoed verbatim.
_DAMPENER_MODES = frozenset({mode.value for mode in DampenerModeKind})

# Closed dampener reason vocab emitted by dampener_blocker._evaluate_*.
_DAMPENER_REASONS = frozenset(
    {
        "cooldown_active",
        "cooldown_elapsed",
        "rate_limit_exceeded",
        "rate_limit_ok",
        "decayed",
        "duplicate_suppressed",
        "first_seen",
        "sampled_pass",
        "sampled_skip",
    }
)
# Closed blocker reason-kind vocab from inner_life.BlockerReasonKind.
_BLOCKER_REASON_KINDS = frozenset({"policy", "schema", "authority", "privacy", "safety"})


def _inhibitor_from_blocker(effect: dict) -> dict:
    """Safe-labeled inhibitor view of a blocker_effect record."""
    reason_kind = effect.get("reason_kind")
    inhibitor = {
        "kind": "blocker",
        "label": reason_kind if reason_kind in _BLOCKER_REASON_KINDS else "blocker",
        "reason": reason_kind if reason_kind in _BLOCKER_REASON_KINDS else None,
        # policy_id/schema_ref/authority_ref/reason_label/source_node/target_node
        # are already safe hash labels in a blocker_effect; re-label defensively.
        "source_refs": _safe_source_refs(
            [
                effect.get("source_node"),
                effect.get("target_node"),
                effect.get("policy_id"),
                effect.get("schema_ref"),
                effect.get("authority_ref"),
                effect.get("reason_label"),
            ],
            prefix="node",
        ),
    }
    return inhibitor


def _build_inhibitors(
    dampener_effects: list[dict],
    blocker_effects: list[dict],
) -> tuple[list[dict], list[str], float | None]:
    """Assemble inhibitors and the worst-case effective pressure factor.

    Returns (inhibitors, warnings, min_after_pressure). The effective-pressure
    floor is taken from the lowest dampener after_pressure plus a hard zero if
    any blocker is present (blockers terminate the path entirely).
    """
    inhibitors: list[dict] = []
    warnings: list[str] = []
    after_pressures: list[float] = []
    blocked = False

    for effect in dampener_effects:
        if not isinstance(effect, dict):
            warnings.append("dampener_effect_not_a_mapping")
            continue
        inhibitor = _inhibitor_from_dampener(effect)
        if "after_pressure" in inhibitor:
            after_pressures.append(inhibitor["after_pressure"])
        inhibitors.append(inhibitor)

    for effect in blocker_effects:
        if not isinstance(effect, dict):
            warnings.append("blocker_effect_not_a_mapping")
            continue
        blocked = True
        inhibitors.append(_inhibitor_from_blocker(effect))

    inhibitors = inhibitors[:_MAX_INHIBITORS]
    inhibitors.sort(key=_inhibitor_sort_key)

    floor: float | None
    if blocked:
        floor = 0.0
    elif after_pressures:
        floor = min(after_pressures)
    else:
        floor = None
    return inhibitors, warnings, floor


def _inhibitor_sort_key(inhibitor: dict) -> tuple:
    return (
        str(inhibitor.get("kind")),
        str(inhibitor.get("label")),
        json.dumps(inhibitor.get("source_refs"), sort_keys=True),
    )


def _coerce_evidence_list(value: Any, *, warnings: list[str], label: str) -> list[dict]:
    """Normalize optional evidence (a dict or list of dicts) into a list."""
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return value
    warnings.append(f"{label}_not_a_mapping_or_list")
    return []


def build_explanation(
    subject_id: Any,
    *,
    records: dict | None = None,
    record: dict | None = None,
    dampener_evidence: Any = None,
    blocker_evidence: Any = None,
    temporal_evidence: Any = None,
    config: dict | None = None,
) -> dict:
    """Build a deterministic pressure-explanation dict for one subject.

    Args:
      subject_id: the candidate/review/candidate-like record id to explain. Any
        free-form scalar; it is safe-labeled, never echoed verbatim.
      records: optional mapping of {id: compact_record} (e.g. an
        `_index_by_id`-style index) to resolve the subject and referenced rows.
      record: optional already-resolved subject record. If both are given,
        `record` wins. If neither resolves the subject, a warning is added and
        a degraded explanation is still returned.
      dampener_evidence/blocker_evidence: optional dampener_effect /
        blocker_effect record(s) (a dict or list of dicts) from
        dampener_blocker.py.
      temporal_evidence: optional temporal trend evidence (a dict or list of
        dicts) from temporal_sensor.py -- either a full compact temporal signal
        or its `temporal` sub-block.
      config: optional config mapping. Only closed, code-defined keys are read;
        unknown keys are ignored (never echoed). Currently no config keys alter
        output, but the parameter is part of the replay fingerprint so callers
        can pin behavior.

    Returns a dict with keys: subject_id, subject, pressure,
    effective_pressure, components, inhibitors, warnings, replay_key.
    Never raises on missing data.
    """
    if config is not None and not isinstance(config, dict):
        raise ExplanationError("config must be a mapping when provided")
    safe_config = _safe_config(config or {})

    warnings: list[str] = []

    resolved = record if isinstance(record, dict) else None
    if resolved is None and isinstance(records, dict):
        candidate = records.get(subject_id)
        if isinstance(candidate, dict):
            resolved = candidate
    if resolved is None:
        warnings.append("subject_record_unavailable")

    subject = _subject_ref(resolved, subject_id)
    safe_subject_id = subject["id"]

    base_record = resolved if resolved is not None else {}
    pressure = _safe_number(base_record.get("pressure"))

    temporal_list = _coerce_evidence_list(temporal_evidence, warnings=warnings, label="temporal_evidence")
    components, component_warnings = _pressure_components(base_record, temporal_evidence=temporal_list)
    warnings.extend(component_warnings)

    dampener_list = _coerce_evidence_list(dampener_evidence, warnings=warnings, label="dampener_evidence")
    blocker_list = _coerce_evidence_list(blocker_evidence, warnings=warnings, label="blocker_evidence")
    inhibitors, inhibitor_warnings, floor = _build_inhibitors(dampener_list, blocker_list)
    warnings.extend(inhibitor_warnings)

    # effective_pressure: the base pressure after the strongest inhibitor floor.
    # Determined purely from inputs (no wall clock). If no inhibitor evidence,
    # effective == base. If blocked, the floor is 0.0.
    if pressure is None:
        effective_pressure = floor
    elif floor is None:
        effective_pressure = pressure
    else:
        effective_pressure = round(min(pressure, floor), 6)

    # Deduplicate + bound warnings deterministically.
    warnings = sorted(set(warnings))[:_MAX_WARNINGS]

    explanation = {
        "subject_id": safe_subject_id,
        "subject": subject,
        "pressure": pressure,
        "effective_pressure": effective_pressure,
        "components": components,
        "inhibitors": inhibitors,
        "warnings": warnings,
    }
    explanation["replay_key"] = _replay_key(explanation, safe_config)
    return explanation


def _safe_config(config: dict) -> dict:
    """Project config into a small, output-safe, sorted fingerprint input.

    Config can carry arbitrary caller content, so values are safe-labeled. We
    only keep the key NAMES (sorted) plus a hash of each value; raw config
    values never enter the explanation or the replay key in cleartext.
    """
    projected: dict[str, str | None] = {}
    for key in sorted(config):
        if not isinstance(key, str):
            continue
        projected[truncate_text(key, _LABEL_MAX)] = _safe_scalar_label("cfg", config[key])
    return projected


def _replay_key(explanation: dict, safe_config: dict) -> str:
    """Stable fingerprint over the safe compact explanation + config.

    Computed over a canonical JSON serialization (sorted keys) so the same
    safe inputs always produce the same fingerprint, and so it is itself safe
    to surface (it is derived only from already-safe-labeled material plus
    bounded numeric/enum values).
    """
    payload = {
        "subject": explanation["subject"],
        "subject_id": explanation["subject_id"],
        "pressure": explanation["pressure"],
        "effective_pressure": explanation["effective_pressure"],
        "components": explanation["components"],
        "inhibitors": explanation["inhibitors"],
        "warnings": explanation["warnings"],
        "config": safe_config,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"
