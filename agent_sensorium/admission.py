"""Source-bound admission for the bounded Subconscious advisory lane.

Identity is derived from retained candidate -> event -> Signal provenance.  The
hashes in this module are opaque equality keys; they are not content-integrity
or truth claims.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .gate import candidate_fingerprint

POLICY_VERSION = "source-admission-v2"
MAX_COMPONENT_CHARS = 512
_TERMINAL_STATUSES = {"suppressed", "cancelled", "archived", "prepared_external_work"}


def _opaque(domain: str, values: list[str]) -> str:
    payload = json.dumps([domain, *values], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _component(value: Any) -> tuple[str | None, bool]:
    if not isinstance(value, str) or not value or len(value) > MAX_COMPONENT_CHARS:
        return None, False
    return value, True


def source_candidate_fingerprint(candidate: dict) -> str:
    """Return the inherited exact representation binding."""
    payload = {
        "candidate_fingerprint": candidate_fingerprint(candidate),
        "event_ids": sorted(str(value) for value in candidate.get("event_ids") or []),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _claim_for_signal(instance: str, signal: dict) -> tuple[dict | None, str | None]:
    sensor, sensor_ok = _component(signal.get("sensor"))
    source, source_ok = _component(signal.get("source"))
    if not sensor_ok or not source_ok:
        return None, "malformed_producer_scope"
    assert sensor is not None and source is not None

    if "memory_provenance" in signal:
        provenance = signal.get("memory_provenance")
        if sensor != "sensorium.memory_reflection" or source != "memory":
            return None, "malformed_memory_identity"
        if not isinstance(provenance, dict) or set(provenance) != {"provider", "bank_id", "item_ids"}:
            return None, "malformed_memory_identity"
        provider, provider_ok = _component(provenance.get("provider"))
        bank_id, bank_ok = _component(provenance.get("bank_id"))
        raw_item_ids = provenance.get("item_ids")
        if not provider_ok or not bank_ok or not isinstance(raw_item_ids, list) or not raw_item_ids:
            return None, "malformed_memory_identity"
        item_ids: list[str] = []
        for raw_item_id in raw_item_ids:
            item_id, item_ok = _component(raw_item_id)
            if not item_ok or item_id is None or item_id != item_id.strip():
                return None, "malformed_memory_identity"
            item_ids.append(item_id)
        if len(item_ids) != len(set(item_ids)):
            return None, "malformed_memory_identity"
        assert provider is not None and bank_id is not None
        item_values = [instance, sensor, source, "native_memory", provider, bank_id, *sorted(item_ids)]
        member_keys = sorted({
            _opaque(
                "memory-member",
                [instance, sensor, source, "native_memory", provider, bank_id, item_id],
            )
            for item_id in item_ids
        })
        return {
            "item_key": _opaque("item", item_values),
            "revision_key": _opaque("revision", [*item_values, "unversioned"]),
            "member_keys": member_keys,
            "identity_mode": "source",
            # Stable memory identity establishes equality, never independence.
            "evidence_class": "internal_interpretation",
        }, None

    meta = signal.get("artifact_meta")
    if meta is not None and not isinstance(meta, dict):
        return None, "malformed_artifact_meta"
    meta = meta if isinstance(meta, dict) else {}

    if "entry_id" in meta or "sha256" in meta:
        entry_id, entry_ok = _component(meta.get("entry_id"))
        revision, revision_ok = _component(meta.get("sha256"))
        if not entry_ok or not revision_ok:
            return None, "malformed_frontier_identity"
        assert entry_id is not None and revision is not None
        item_values = [instance, sensor, source, "frontier", entry_id]
        return {
            "item_key": _opaque("item", item_values),
            "revision_key": _opaque("revision", [*item_values, revision]),
            "identity_mode": "source",
            "evidence_class": "source_owned",
        }, None

    if "source_id" in meta or "item_id" in meta:
        source_id, source_id_ok = _component(meta.get("source_id"))
        item_id, item_id_ok = _component(meta.get("item_id"))
        if not source_id_ok or not item_id_ok:
            return None, "malformed_research_identity"
        assert source_id is not None and item_id is not None
        item_values = [instance, sensor, source, "research_feed", source_id, item_id]
        return {
            "item_key": _opaque("item", item_values),
            "revision_key": _opaque("revision", [*item_values, "unversioned"]),
            "identity_mode": "source",
            "evidence_class": "source_owned",
        }, None

    transition_fields = any(
        key in signal for key in ("metric_family", "transition_sequence", "source_revision")
    ) and str(signal.get("kind") or "").endswith("_pressure")
    if transition_fields:
        # Pre-P2 transition signals had no producer revision and remain an
        # uncertain legacy first consideration. A present bad claim is invalid.
        if "source_revision" not in signal:
            return None, None
        kind, kind_ok = _component(signal.get("kind"))
        metric, metric_ok = _component(signal.get("metric_family"))
        revision, revision_ok = _component(signal.get("source_revision"))
        if not kind_ok or not metric_ok:
            return None, "malformed_transition_identity"
        if not revision_ok or revision is None or not revision.isdecimal():
            return None, "malformed_transition_revision"
        assert kind is not None and metric is not None
        item_values = [instance, sensor, source, "transition", kind, metric]
        return {
            "item_key": _opaque("item", item_values),
            "revision_key": _opaque("revision", [*item_values, revision]),
            "identity_mode": "source",
            "evidence_class": "telemetry",
        }, None

    return None, None


def _snapshot(store) -> dict[str, list[dict]]:
    return {
        "signals": store.read_jsonl("signals"),
        "events": store.read_jsonl("events"),
        "candidates": store.read_jsonl("candidates"),
        "decisions": store.read_jsonl("decisions"),
    }


def _base_binding_from_snapshot(store, candidate: dict, snap: dict[str, list[dict]]) -> tuple[dict | None, str | None]:
    candidate_id, candidate_ok = _component(candidate.get("id"))
    if not candidate_ok:
        return None, "malformed_candidate_id"
    assert candidate_id is not None
    event_index = {row.get("id"): row for row in snap["events"] if isinstance(row, dict)}
    signal_index = {row.get("id"): row for row in snap["signals"] if isinstance(row, dict)}
    joined_signal_ids: set[str] = set()
    dangling = False
    for event_id in candidate.get("event_ids") or []:
        event = event_index.get(event_id)
        if not isinstance(event, dict):
            dangling = True
            continue
        for signal_id in event.get("source_signal_ids") or []:
            signal = signal_index.get(signal_id)
            if not isinstance(signal, dict):
                dangling = True
                continue
            joined_signal_ids.add(signal_id)

    # Candidate/Event references are membership only. Select and validate joined
    # signals in the canonical retained receipt order supplied by read_jsonl.
    joined = [
        signal for signal in snap["signals"]
        if isinstance(signal, dict) and signal.get("id") in joined_signal_ids
    ]
    claims: list[dict] = []
    for signal in joined:
        parsed_claim, error = _claim_for_signal(store.instance, signal)
        if error:
            return None, error
        if parsed_claim:
            claims.append(parsed_claim)

    claim_identities = {
        (claim["item_key"], tuple(claim.get("member_keys") or []))
        for claim in claims
    }
    claim: dict = {}
    if claims and not dangling and len(claim_identities) == 1:
        claim = claims[-1]
        identity_mode = claim["identity_mode"]
        evidence_class = claim["evidence_class"]
        item_key = claim["item_key"]
        revision_key = claim["revision_key"]
    else:
        # Missing/dangling joins and families without a stable producer key keep
        # one uncertain, candidate-scoped first-consideration slot.
        identity_mode = "legacy"
        kind = str(candidate.get("kind") or "")
        evidence_class = "internal_interpretation" if (
            "reflection" in kind or any("reflection" in str(signal.get("sensor") or "") for signal in joined)
        ) else "unknown"
        item_key = _opaque("legacy-item", [store.instance, candidate_id])
        revision_key = _opaque("legacy-revision", [item_key, "unversioned"])
        if dangling or claims:
            evidence_class = "unknown"

    representation = source_candidate_fingerprint(candidate)
    binding = {
        "schema_version": 1,
        "policy_version": POLICY_VERSION,
        "source_candidate_id": candidate_id,
        "source_candidate_fingerprint": representation,
        "item_key": item_key,
        "revision_key": revision_key,
        "identity_mode": identity_mode,
        "evidence_class": evidence_class,
    }
    member_keys = claim.get("member_keys") if identity_mode == "source" else None
    if member_keys:
        binding["envelope_key"] = item_key
        binding["member_keys"] = list(member_keys)
    else:
        binding["admission_key"] = _opaque("admission", [item_key, revision_key])
    return binding, None


def _binding_from_snapshot(store, candidate: dict, snap: dict[str, list[dict]]) -> tuple[dict | None, str | None]:
    binding, error = _base_binding_from_snapshot(store, candidate, snap)
    if error or binding is None or "member_keys" not in binding:
        return binding, error
    _, disposed, _ = _memory_prior_rows(store, binding, snap)
    selected = sorted(set(binding["member_keys"]) - disposed)
    binding["selected_member_keys"] = selected
    binding["admission_key"] = _opaque("memory-admission", selected)
    return binding, None

def binding_for_candidate(store, candidate_id: str) -> tuple[dict | None, str | None]:
    snap = _snapshot(store)
    candidate = next((row for row in snap["candidates"] if row.get("id") == candidate_id), None)
    if not isinstance(candidate, dict) or candidate.get("status", "candidate") != "candidate":
        return None, "source_candidate_unavailable"
    return _binding_from_snapshot(store, candidate, snap)


def _binding_shape(binding: Any) -> bool:
    if not isinstance(binding, dict):
        return False
    if binding.get("schema_version") != 1 or binding.get("policy_version") != POLICY_VERSION:
        return False
    for key in (
        "source_candidate_id", "source_candidate_fingerprint", "item_key",
        "revision_key", "admission_key", "identity_mode", "evidence_class",
    ):
        if not isinstance(binding.get(key), str) or not binding[key]:
            return False
    if binding.get("identity_mode") not in {"source", "legacy"} or binding.get("evidence_class") not in {
        "source_owned", "internal_interpretation", "telemetry", "unknown",
    }:
        return False
    memory_fields = {"envelope_key", "member_keys", "selected_member_keys"}
    if memory_fields.intersection(binding):
        if not memory_fields.issubset(binding):
            return False
        members = binding.get("member_keys")
        selected = binding.get("selected_member_keys")
        if (
            not isinstance(members, list) or not members
            or not isinstance(selected, list)
            or members != sorted(set(members))
            or selected != sorted(set(selected))
            or not set(selected).issubset(members)
            or any(not isinstance(key, str) or len(key) != 64 for key in [*members, *selected])
            or binding.get("envelope_key") != binding.get("item_key")
            or binding.get("identity_mode") != "source"
            or binding.get("evidence_class") != "internal_interpretation"
            or binding.get("admission_key") != _opaque("memory-admission", selected)
        ):
            return False
    return True

def validate_admission_binding(store, binding: Any) -> tuple[bool, str]:
    if not _binding_shape(binding):
        return False, "invalid_admission_binding"
    current, error = binding_for_candidate(store, binding["source_candidate_id"])
    if error or current is None:
        return False, error or "source_candidate_unavailable"
    if current != binding:
        return False, "stale_admission_binding"
    return True, "valid"


def _decision_projection(row: dict) -> dict:
    return {
        "ts": row.get("ts"),
        "type": row.get("type"),
        "dry_run": row.get("dry_run"),
        "action": row.get("action"),
        "output_action": row.get("output_action"),
        "candidate_id": row.get("candidate_id"),
        "reason_code": row.get("reason_code"),
        "admission_key": (row.get("admission_binding") or {}).get("admission_key"),
    }


def _is_successful_applying_disposition(row: dict) -> bool:
    """Return true only for a semantic choice that reached its applying owner."""
    if row.get("dry_run") is not False:
        return False
    action = row.get("action")
    output_action = row.get("output_action")
    return (
        (output_action == "DROP" and action == "drop")
        or (output_action == "SAVE" and action == "save")
        or (
            output_action == "CREATE_CONSCIOUS_TASK"
            and action in {"created_conscious_task_candidate", "updated_conscious_task_candidate"}
        )
    )


def _memory_members_for_prior(
    store, prior: dict, current: dict, snap: dict[str, list[dict]],
) -> tuple[set[str], str | None]:
    """Attribute persisted memory rows without rewriting historical bytes."""
    if prior.get("policy_version") == POLICY_VERSION:
        selected = prior.get("selected_member_keys")
        if isinstance(selected, list) and selected == sorted(set(selected)):
            return {key for key in selected if isinstance(key, str)}, None
        return set(), "malformed_v2_member_binding"
    if prior.get("policy_version") != "source-admission-v1":
        return set(), None
    source_candidate_id = prior.get("source_candidate_id")
    source_candidate = next(
        (row for row in snap["candidates"] if row.get("id") == source_candidate_id), None,
    )
    if isinstance(source_candidate, dict):
        reconstructed, error = _base_binding_from_snapshot(store, source_candidate, snap)
        if not error and reconstructed and reconstructed.get("member_keys"):
            return set(reconstructed["member_keys"]), None
    if prior.get("item_key") == current.get("envelope_key"):
        return set(current["member_keys"]), "legacy_exact_envelope_fallback"
    return set(), "legacy_lineage_unavailable"


def _memory_prior_rows(
    store, binding: dict, snap: dict[str, list[dict]],
) -> tuple[list[dict], set[str], int]:
    current_members = set(binding["member_keys"])
    relevant: list[dict] = []
    disposed: set[str] = set()
    gaps = 0
    for row in snap["decisions"]:
        prior = row.get("admission_binding")
        if not isinstance(prior, dict):
            continue
        attributed, limitation = _memory_members_for_prior(store, prior, binding, snap)
        overlap = sorted(current_members & attributed)
        if limitation == "legacy_lineage_unavailable":
            gaps += 1
        if not overlap:
            continue
        projection = _decision_projection(row)
        projection["overlap_member_keys"] = overlap
        if limitation:
            projection["attribution_limitation"] = limitation
        relevant.append(projection)
        if _is_successful_applying_disposition(row):
            disposed.update(overlap)
    for row in snap["candidates"]:
        if row.get("kind") != "subconscious_advisory":
            continue
        prior = row.get("admission_binding") or (row.get("advisory_meta") or {}).get("admission_binding")
        if not isinstance(prior, dict):
            continue
        attributed, limitation = _memory_members_for_prior(store, prior, binding, snap)
        overlap = sorted(current_members & attributed)
        if limitation == "legacy_lineage_unavailable":
            gaps += 1
        if not overlap:
            continue
        projection = {
            "ts": row.get("updated_at") or row.get("created_at"),
            "type": "subconscious.advisory.candidate",
            "action": (row.get("advisory_meta") or {}).get("action"),
            "candidate_id": row.get("id"),
            "status": row.get("status"),
            "admission_key": prior.get("admission_key"),
            "overlap_member_keys": overlap,
        }
        if limitation:
            projection["attribution_limitation"] = limitation
        relevant.append(projection)
        # A canonical advisory is the applying representation. Its selected
        # members remain represented through HELD and explicit terminal closure.
        disposed.update(overlap)
    return relevant[-20:], disposed, gaps


def _prior_for_binding(store, binding: dict, snap: dict[str, list[dict]]) -> tuple[list[dict], bool, str | None]:
    if "member_keys" in binding:
        relevant, disposed, _ = _memory_prior_rows(store, binding, snap)
        selected = set(binding.get("selected_member_keys") or [])
        decided = not selected or selected.issubset(disposed)
        return relevant, decided, "prior_member_disposition" if decided else None
    relevant: list[dict] = []
    decided = False
    reason = None
    for row in snap["decisions"]:
        prior = row.get("admission_binding")
        if not isinstance(prior, dict) or prior.get("item_key") != binding["item_key"]:
            continue
        relevant.append(_decision_projection(row))
        if (
            prior.get("admission_key") == binding["admission_key"]
            and _is_successful_applying_disposition(row)
        ):
            decided, reason = True, "prior_disposition"
    for row in snap["candidates"]:
        if row.get("kind") != "subconscious_advisory":
            continue
        prior = row.get("admission_binding") or (row.get("advisory_meta") or {}).get("admission_binding")
        if isinstance(prior, dict) and prior.get("item_key") == binding["item_key"]:
            relevant.append({
                "ts": row.get("updated_at") or row.get("created_at"),
                "type": "subconscious.advisory.candidate",
                "action": (row.get("advisory_meta") or {}).get("action"),
                "candidate_id": row.get("id"),
                "status": row.get("status"),
                "admission_key": prior.get("admission_key"),
            })
            if prior.get("admission_key") == binding["admission_key"]:
                decided, reason = True, "prior_disposition"
            elif row.get("status") in _TERMINAL_STATUSES:
                decided, reason = True, "explicit_item_closure"
        elif binding["identity_mode"] == "legacy" and list(row.get("source_candidate_ids") or []) == [binding["source_candidate_id"]]:
            old_fp = str(row.get("source_candidate_fingerprint") or "")
            if not old_fp or old_fp == binding["source_candidate_fingerprint"]:
                relevant.append({
                    "ts": row.get("updated_at") or row.get("created_at"),
                    "type": "legacy.subconscious.advisory.candidate",
                    "candidate_id": row.get("id"),
                    "status": row.get("status"),
                })
                decided, reason = True, "legacy_exact_source_disposition"
    return relevant[-20:], decided, reason


def prior_dispositions_for_binding(store, binding: dict) -> tuple[list[dict], bool, str | None]:
    """Read complete equality disposition state for one semantic binding."""
    return _prior_for_binding(store, binding, _snapshot(store))


def _priority(candidate: dict) -> tuple[float, str, str]:
    try:
        pressure = float(candidate.get("pressure") or 0.0)
    except (TypeError, ValueError):
        pressure = 0.0
    return (-pressure, str(candidate.get("created_at") or ""), str(candidate.get("id") or ""))


def _has_native_memory_lineage(candidate: dict, snap: dict[str, list[dict]]) -> bool:
    """Identify the automatic producer without changing legacy binding shape."""
    event_ids = set(candidate.get("event_ids") or [])
    signal_ids = {
        signal_id
        for event in snap["events"]
        if isinstance(event, dict) and event.get("id") in event_ids
        for signal_id in (event.get("source_signal_ids") or [])
    }
    return any(
        isinstance(signal, dict)
        and signal.get("id") in signal_ids
        and signal.get("sensor") == "sensorium.memory_reflection"
        and signal.get("source") == "memory"
        for signal in snap["signals"]
    )


def build_admission_plan(store, *, candidate_limit: int = 50) -> dict:
    """Select at most one source after complete source-bound disposition lookup."""
    del candidate_limit  # Selection is bounded to one only after complete filtering.
    snap = _snapshot(store)
    eligible: list[tuple[dict, dict, list[dict]]] = []
    suppressed: dict[str, int] = {}
    for candidate in snap["candidates"]:
        if candidate.get("status", "candidate") != "candidate":
            continue
        kind = str(candidate.get("kind") or "")
        if kind in {"subconscious_advisory", "body_pressure", "network_pressure", "process_pressure", "hindsight_pressure", "kanban_pressure"}:
            continue
        binding, error = _binding_from_snapshot(store, candidate, snap)
        if error or binding is None:
            key = error or "invalid_source_claim"
            suppressed[key] = suppressed.get(key, 0) + 1
            continue
        if binding.get("identity_mode") != "source" and _has_native_memory_lineage(candidate, snap):
            suppressed["unsupported_memory_identity"] = (
                suppressed.get("unsupported_memory_identity", 0) + 1
            )
            continue
        prior, decided, reason = _prior_for_binding(store, binding, snap)
        if decided:
            key = reason or "prior_disposition"
            suppressed[key] = suppressed.get(key, 0) + 1
            continue
        eligible.append((candidate, binding, prior))
    eligible.sort(key=lambda item: _priority(item[0]))
    selected = eligible[0] if eligible else None
    return {
        "policy_version": POLICY_VERSION,
        "selection": selected[1] if selected else None,
        "eligible_count": len(eligible),
        "suppressed_counts": dict(sorted(suppressed.items())),
        "source_decisions": selected[2] if selected else [],
        "attribution_gap_count": (
            _memory_prior_rows(store, selected[1], snap)[2]
            if selected and "member_keys" in selected[1] else 0
        ),
    }


def context_for_binding(store, binding: dict, *, source_decisions: list[dict] | None = None) -> dict:
    """Render exactly the preselected source; never select independently."""
    valid, reason = validate_admission_binding(store, binding)
    if not valid:
        raise ValueError(reason)
    snap = _snapshot(store)
    candidate = next(row for row in snap["candidates"] if row.get("id") == binding["source_candidate_id"])
    wanted_events = set(candidate.get("event_ids") or [])
    events = [row for row in snap["events"] if row.get("id") in wanted_events]
    if source_decisions is None:
        source_decisions, _, _ = _prior_for_binding(store, binding, snap)
    return {
        "policy_version": POLICY_VERSION,
        "selection": dict(binding),
        "candidate": dict(candidate),
        "events": events,
        "source_decisions": list(source_decisions),
    }


def event_source_item_key(store, event: dict, *, events: list[dict] | None = None, signals: list[dict] | None = None) -> str | None:
    """Return a stable item key for ingest coalescing safeguards when available."""
    del events
    signal_index = {row.get("id"): row for row in (signals if signals is not None else store.read_jsonl("signals"))}
    claims = []
    for signal_id in event.get("source_signal_ids") or []:
        signal = signal_index.get(signal_id)
        if not isinstance(signal, dict):
            continue
        claim, error = _claim_for_signal(store.instance, signal)
        if error:
            return None
        if claim:
            claims.append(claim)
    keys = {claim["item_key"] for claim in claims}
    return next(iter(keys)) if len(keys) == 1 else None
