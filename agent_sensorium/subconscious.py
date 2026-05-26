"""Subconscious advisory layer.

Phase 8 keeps this lane bounded: it builds compact context from promoted
Events/Candidates/Decisions, validates advisory-shaped output, and can write an
internal conscious-task candidate. It does not send messages, create external
work, open platform threads, or call a model unless a future caller explicitly
adds that capability.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .gate import candidate_fingerprint
from .schemas import (
    intersect_allowed_surfaces,
    merge_sensitivity,
    new_id,
    truncate_text,
    utc_now_iso,
)
from .store import SensoriumStore

VALID_ADVISORY_ACTIONS = {"DROP", "SAVE", "CREATE_CONSCIOUS_TASK"}
VALID_REQUEST_TYPES = {
    "THINK",
    "SAVE",
    "UPDATE_MEMORY_OR_SKILL",
    "CREATE_FOLLOWUP",
}

DEFAULT_ADVISORY_CONFIG: dict[str, Any] = {
    "event_limit": 8,
    "candidate_limit": 5,
    "decision_limit": 5,
    "summary_chars": 180,
    "default_pressure": 0.66,
}


def _merged_config(config: dict | None = None) -> dict:
    cfg = deepcopy(DEFAULT_ADVISORY_CONFIG)
    if config:
        cfg.update(config)
    return cfg


def _clamp_pressure(value: Any, default: float = 0.66) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        num = default
    return round(max(0.0, min(1.0, num)), 3)


def _compact_event(event: dict, *, summary_chars: int) -> dict:
    return {
        "id": event.get("id"),
        "ts": event.get("ts"),
        "kind": event.get("kind"),
        "summary": truncate_text(event.get("summary", ""), summary_chars),
        "strength": event.get("strength"),
        "correlation_keys": list(event.get("correlation_keys") or [])[:8],
        "sensitivity": event.get("sensitivity", "private"),
        "allowed_surfaces": event.get("allowed_surfaces") or ["local"],
    }


def _compact_candidate(candidate: dict, *, summary_chars: int) -> dict:
    return {
        "id": candidate.get("id"),
        "status": candidate.get("status"),
        "kind": candidate.get("kind"),
        "pressure": candidate.get("pressure"),
        "summary": truncate_text(candidate.get("summary", ""), summary_chars),
        "event_ids": list(candidate.get("event_ids") or [])[:8],
        "correlation_keys": list(candidate.get("correlation_keys") or [])[:8],
        "sensitivity": candidate.get("sensitivity", "private"),
        "allowed_surfaces": candidate.get("allowed_surfaces") or ["local"],
    }


def _compact_decision(decision: dict, *, summary_chars: int) -> dict:
    return {
        "ts": decision.get("ts"),
        "type": decision.get("type"),
        "candidate_id": decision.get("candidate_id"),
        "thread_id": decision.get("thread_id"),
        "action": decision.get("action"),
        "reason": truncate_text(decision.get("reason", ""), summary_chars),
    }


def build_advisory_context(store: SensoriumStore, config: dict | None = None) -> dict:
    """Build bounded advisory context without raw signals/transcripts/files."""
    cfg = _merged_config(config)
    store.ensure_dirs()

    events = store.read_jsonl("events")
    candidates = [c for c in store.read_jsonl("candidates") if c.get("status", "candidate") == "candidate"]
    candidates.sort(key=lambda c: c.get("pressure", 0), reverse=True)
    decisions = store.read_jsonl("decisions")
    from .probe_audit import audit_store, probe_inventory

    inventory = probe_inventory()
    audit = audit_store(state_dir=str(store.root), instance=store.instance)

    return {
        "schema_version": 1,
        "instance": store.instance,
        "built_at": utc_now_iso(),
        "config_summary": {
            "event_limit": cfg["event_limit"],
            "candidate_limit": cfg["candidate_limit"],
            "decision_limit": cfg["decision_limit"],
            "allowed_actions": sorted(VALID_ADVISORY_ACTIONS),
            "model_lane_default": "disabled",
            "side_effect_boundary": "internal_candidates_only",
        },
        "top_candidates": [
            _compact_candidate(c, summary_chars=int(cfg["summary_chars"]))
            for c in candidates[: int(cfg["candidate_limit"])]
        ],
        "recent_events": [
            _compact_event(e, summary_chars=int(cfg["summary_chars"]))
            for e in events[-int(cfg["event_limit"]):]
        ],
        "recent_decisions": [
            _compact_decision(d, summary_chars=int(cfg["summary_chars"]))
            for d in decisions[-int(cfg["decision_limit"]):]
        ],
        "probe_audit_summary": {
            "wired_live_probes": inventory.get("wired_live_probes", []),
            "blind_spots": audit.get("blind_spots", []),
            "counts": audit.get("counts", {}),
            "promotion_yield": audit.get("promotion_yield", {}),
        },
    }


def validate_advisory_output(output: dict) -> dict:
    if not isinstance(output, dict):
        raise ValueError("Advisory output must be an object")
    action = output.get("action")
    if action not in VALID_ADVISORY_ACTIONS:
        raise ValueError(f"Invalid advisory action: {action}. Must be one of {sorted(VALID_ADVISORY_ACTIONS)}")
    rationale = output.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        raise ValueError("Advisory output rationale must be a non-empty string")

    normalized = dict(output)
    normalized.setdefault("event_ids", [])
    normalized.setdefault("candidate_ids", [])
    if not isinstance(normalized["event_ids"], list):
        raise ValueError("Advisory output event_ids must be a list")
    if not isinstance(normalized["candidate_ids"], list):
        raise ValueError("Advisory output candidate_ids must be a list")

    if action == "CREATE_CONSCIOUS_TASK":
        task = normalized.get("conscious_task")
        if not isinstance(task, dict):
            raise ValueError("CREATE_CONSCIOUS_TASK conscious_task missing")
        required = {"request_type", "title", "why", "expected_decision"}
        missing = required - task.keys()
        if missing:
            raise ValueError(f"CREATE_CONSCIOUS_TASK conscious_task missing fields: {sorted(missing)}")
        if task.get("request_type") not in VALID_REQUEST_TYPES:
            raise ValueError(f"Invalid conscious_task request_type: {task.get('request_type')}")
        for field in required:
            value = task.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"conscious_task {field} must be a non-empty string")

    return normalized


def _refs_for_ids(items: list[dict], ids: list[str]) -> list[dict]:
    wanted = set(ids)
    return [item for item in items if item.get("id") in wanted]


def _candidate_from_advisory(store: SensoriumStore, output: dict, config: dict | None = None) -> dict:
    cfg = _merged_config(config)
    events = store.read_jsonl("events")
    candidates = store.read_jsonl("candidates")
    event_refs = _refs_for_ids(events, output.get("event_ids") or [])
    candidate_refs = _refs_for_ids(candidates, output.get("candidate_ids") or [])

    sensitivities = [r.get("sensitivity", "private") for r in [*event_refs, *candidate_refs]]
    surface_sets = [r.get("allowed_surfaces") or ["local"] for r in [*event_refs, *candidate_refs]]
    task = output["conscious_task"]
    now = utc_now_iso()
    summary = truncate_text(task.get("title", "Subconscious advisory"), int(cfg["summary_chars"]))
    pressure = _clamp_pressure(output.get("pressure"), default=float(cfg["default_pressure"]))
    keys = sorted({
        "subconscious-advisory",
        *(key for ref in [*event_refs, *candidate_refs] for key in (ref.get("correlation_keys") or [])),
    })

    candidate = {
        "id": new_id("cand"),
        "status": "candidate",
        "kind": "subconscious_advisory",
        "pressure": pressure,
        "novelty": 0.5,
        "repetition": 0.0,
        "identity_relevance": 0.5,
        "relationship_relevance": 0.0,
        "actionability": 0.6,
        "time_sensitivity": 0.0,
        "summary": summary,
        "event_ids": list(output.get("event_ids") or []),
        "source_candidate_ids": list(output.get("candidate_ids") or []),
        "correlation_keys": keys,
        "sensitivity": merge_sensitivity(sensitivities) if sensitivities else "private",
        "allowed_surfaces": intersect_allowed_surfaces(surface_sets) if surface_sets else ["local"],
        "created_at": now,
        "updated_at": now,
        "expires_at": "",
        "conscious_task": {
            "id": new_id("ctask"),
            "request_type": task["request_type"],
            "title": truncate_text(task["title"], 120),
            "why": truncate_text(task["why"], 240),
            "expected_decision": truncate_text(task["expected_decision"], 240),
        },
        "advisory_meta": {
            "rationale": truncate_text(output.get("rationale", ""), 300),
            "action": output.get("action"),
        },
    }
    candidate["fingerprint"] = candidate_fingerprint(candidate)
    return candidate


def _find_existing_candidate(candidates: list[dict], candidate: dict) -> dict | None:
    fp = candidate.get("fingerprint") or candidate_fingerprint(candidate)
    for existing in candidates:
        existing_fp = existing.get("fingerprint") or candidate_fingerprint(existing)
        if existing_fp == fp:
            return existing
    return None


def _write_advisory_receipt(
    store: SensoriumStore,
    *,
    result: dict,
    output: dict | None,
    dry_run: bool,
    context: dict,
    record_receipt: bool,
) -> None:
    if not record_receipt:
        return
    receipt = {
        "ts": utc_now_iso(),
        "type": "subconscious.advisory",
        "dry_run": dry_run,
        "action": result.get("action"),
        "output_action": (output or {}).get("action"),
        "candidate_id": result.get("candidate_id"),
        "reason": truncate_text(result.get("reason", ""), 160),
        "context_counts": {
            "recent_events": len(context.get("recent_events", [])),
            "top_candidates": len(context.get("top_candidates", [])),
            "recent_decisions": len(context.get("recent_decisions", [])),
        },
    }
    store.append_jsonl("decisions", receipt)


def run_subconscious_advisory(
    store: SensoriumStore,
    *,
    advisory_output: dict | None = None,
    enabled: bool = False,
    dry_run: bool = True,
    config: dict | None = None,
    record_receipt: bool = True,
) -> dict:
    """Run one bounded advisory pass.

    The model lane is intentionally disabled by default. Until a caller provides
    a validated advisory output, enabled runs return `model_output_required`.
    """
    store.ensure_dirs()
    context = build_advisory_context(store, config=config)

    if not enabled:
        result = {
            "action": "disabled",
            "dry_run": dry_run,
            "reason": "subconscious advisory model lane is disabled by default",
            "context": context,
        }
        _write_advisory_receipt(store, result=result, output=advisory_output, dry_run=dry_run, context=context, record_receipt=record_receipt)
        return result

    if advisory_output is None:
        result = {
            "action": "model_output_required",
            "dry_run": dry_run,
            "reason": "no advisory_output supplied; live model calls are not implemented in the plugin core",
            "context": context,
        }
        _write_advisory_receipt(store, result=result, output=None, dry_run=dry_run, context=context, record_receipt=record_receipt)
        return result

    output = validate_advisory_output(advisory_output)

    if output["action"] in {"DROP", "SAVE"}:
        result = {
            "action": output["action"].lower(),
            "dry_run": dry_run,
            "reason": output["rationale"],
            "context": context,
        }
        _write_advisory_receipt(store, result=result, output=output, dry_run=dry_run, context=context, record_receipt=record_receipt)
        return result

    candidate = _candidate_from_advisory(store, output, config=config)
    if dry_run:
        result = {
            "action": "would_create_conscious_task",
            "dry_run": True,
            "candidate_preview": candidate,
            "reason": output["rationale"],
            "context": context,
        }
        _write_advisory_receipt(store, result=result, output=output, dry_run=True, context=context, record_receipt=record_receipt)
        return result

    candidates = store.read_jsonl("candidates")
    existing = _find_existing_candidate(candidates, candidate)
    if existing:
        result = {
            "action": "already_exists",
            "dry_run": False,
            "candidate_id": existing.get("id"),
            "reason": "duplicate subconscious advisory candidate",
            "context": context,
        }
        _write_advisory_receipt(store, result=result, output=output, dry_run=False, context=context, record_receipt=record_receipt)
        return result

    store.append_jsonl("candidates", candidate)
    result = {
        "action": "created_conscious_task_candidate",
        "dry_run": False,
        "candidate_id": candidate["id"],
        "reason": output["rationale"],
        "context": context,
    }
    _write_advisory_receipt(store, result=result, output=output, dry_run=False, context=context, record_receipt=record_receipt)
    return result
