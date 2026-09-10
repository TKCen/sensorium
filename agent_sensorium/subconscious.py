"""Subconscious advisory layer.

Phase 8 keeps this lane bounded: it builds compact context from promoted
Events/Candidates/Decisions, validates advisory-shaped output, and can write an
internal conscious-task candidate. It does not send messages, create external
work, or open platform threads. Model reasoning is cheap/OpenAI-compatible and
runs only when explicitly enabled.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.request
from copy import deepcopy
from pathlib import Path
from typing import Any

from .admission import (
    binding_for_candidate,
    build_admission_plan,
    context_for_binding,
    prior_dispositions_for_binding,
    validate_admission_binding,
)
from .gate import candidate_fingerprint
from .http_urls import validate_http_endpoint_url
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
    "PRIVATE_EXPRESSION",
    "SAVE",
    "UPDATE_MEMORY_OR_SKILL",
    "CREATE_FOLLOWUP",
    "DELEGATE_WORK",
}

DIRECT_CONSCIOUS_KINDS = {
    "body_pressure",
    "network_pressure",
    "process_pressure",
    "hindsight_pressure",
    "kanban_pressure",
}
ADVISORY_SOURCE_EXCLUDED_KINDS = DIRECT_CONSCIOUS_KINDS | {"subconscious_advisory"}


def is_direct_conscious_kind(kind: str | None) -> bool:
    return str(kind or "") in DIRECT_CONSCIOUS_KINDS


def is_advisory_source_kind(kind: str | None) -> bool:
    return str(kind or "") not in ADVISORY_SOURCE_EXCLUDED_KINDS


DEFAULT_ADVISORY_CONFIG: dict[str, Any] = {
    "event_limit": 8,
    "candidate_limit": 5,
    "decision_limit": 5,
    "summary_chars": 180,
    "default_pressure": 0.66,
    "model_enabled": False,
    "model_provider": "minimax",
    "model": "MiniMax-M3",
    "model_base_url": "https://api.minimax.io/v1",
    "model_api_key_env": "MINIMAX_API_KEY",
    "model_api_key": None,
    "model_timeout_seconds": 20,
    "model_max_tokens": 1200,
    "model_temperature": 0.0,
    "model_response_format": {"type": "json_object"},
}


def _merged_config(config: dict | None = None) -> dict:
    cfg = deepcopy(DEFAULT_ADVISORY_CONFIG)
    env_overrides = {
        "model_provider": os.getenv("SENSORIUM_SUBCONSCIOUS_PROVIDER"),
        "model": os.getenv("SENSORIUM_SUBCONSCIOUS_MODEL"),
        "model_base_url": os.getenv("SENSORIUM_SUBCONSCIOUS_BASE_URL"),
        "model_api_key_env": os.getenv("SENSORIUM_SUBCONSCIOUS_API_KEY_ENV"),
    }
    for key, value in env_overrides.items():
        if value:
            cfg[key] = value
    if os.getenv("SENSORIUM_SUBCONSCIOUS_MODEL_ENABLED") in {"1", "true", "yes", "on"}:
        cfg["model_enabled"] = True
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


def build_advisory_context(
    store: SensoriumStore,
    config: dict | None = None,
    *,
    admission_binding: dict | None = None,
    source_decisions: list[dict] | None = None,
) -> dict:
    """Build bounded advisory context without raw signals/transcripts/files."""
    cfg = _merged_config(config)
    store.ensure_dirs()

    bound = context_for_binding(
        store, admission_binding, source_decisions=source_decisions,
    ) if admission_binding is not None else None
    events = (
        list(bound["events"]) if bound is not None else
        [e for e in store.read_jsonl("events") if is_advisory_source_kind(e.get("kind"))]
    )
    candidates = (
        [bound["candidate"]] if bound is not None else [
            c for c in store.read_jsonl("candidates")
            if c.get("status", "candidate") == "candidate" and is_advisory_source_kind(c.get("kind"))
        ]
    )
    candidates.sort(key=lambda c: c.get("pressure", 0), reverse=True)
    decisions = list(source_decisions or []) if bound is not None else store.read_jsonl("decisions")
    direct_counts = {
        "events": sum(1 for e in store.read_jsonl("events") if is_direct_conscious_kind(e.get("kind"))),
        "candidates": sum(
            1 for c in store.read_jsonl("candidates")
            if c.get("status", "candidate") == "candidate" and is_direct_conscious_kind(c.get("kind"))
        ),
    }
    from .probe_audit import audit_store, probe_inventory

    inventory = probe_inventory()
    audit = audit_store(state_dir=str(store.root), instance=store.instance)

    return {
        "schema_version": 1,
        "admission": {
            "policy_version": (admission_binding or {}).get("policy_version"),
            "selection": admission_binding,
            "source_decisions": decisions if bound is not None else [],
        },
        "instance": store.instance,
        "built_at": utc_now_iso(),
        "config_summary": {
            "event_limit": cfg["event_limit"],
            "candidate_limit": cfg["candidate_limit"],
            "decision_limit": cfg["decision_limit"],
            "allowed_actions": sorted(VALID_ADVISORY_ACTIONS),
            "model_lane_default": "disabled",
            "side_effect_boundary": "internal_candidates_only",
            "direct_conscious_kinds_excluded": sorted(DIRECT_CONSCIOUS_KINDS),
            "advisory_source_kinds_excluded": sorted(ADVISORY_SOURCE_EXCLUDED_KINDS),
            "direct_conscious_material_omitted": direct_counts,
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


def _hermes_env_path() -> Path:
    try:
        from importlib import import_module

        constants = import_module("hermes_constants")
        return Path(constants.get_hermes_home()) / ".env"
    except Exception:
        return Path.home() / ".hermes" / ".env"


def _load_dotenv_value(key: str) -> str | None:
    env_path = _hermes_env_path()
    try:
        text = env_path.read_text(errors="ignore")
    except OSError:
        return None
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=\s*(.*)\s*$", re.MULTILINE)
    match = pattern.search(text)
    if not match:
        return None
    value = match.group(1).strip().strip('"').strip("'")
    return value or None


def _model_api_key(cfg: dict) -> str | None:
    explicit = cfg.get("model_api_key")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    env_name = str(cfg.get("model_api_key_env") or "").strip()
    if not env_name:
        return None
    return os.getenv(env_name) or _load_dotenv_value(env_name)


def _post_openai_chat_completion(url: str, payload: dict, headers: dict, timeout: int | float) -> dict:
    validate_http_endpoint_url(url)
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=float(timeout)) as response:
        return json.loads(response.read().decode("utf-8"))


def _extract_json_object(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.DOTALL | re.IGNORECASE).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            return json.loads(cleaned[start:end + 1])
        raise


def _model_prompt(context: dict) -> list[dict]:
    system = (
        "You are the cheap Subconscious advisory lane for Agent Sensorium. "
        "Read only the bounded JSON context. Do not ask questions. Do not send messages. "
        "Return exactly one JSON object with action DROP, SAVE, or CREATE_CONSCIOUS_TASK. "
        "CREATE_CONSCIOUS_TASK must name exactly one existing candidate_id from top_candidates; "
        "use SAVE instead when no single candidate owns the pressure. "
        "Use CREATE_CONSCIOUS_TASK when semantic/contextual Events/Candidates merit later conscious attention. "
        "Do not adjudicate directly quantified machine/queue/work-board pressure thresholds; those are routed "
        "through deterministic consciousness promotion before this Subconscious lane. Your job is semantic linking, "
        "cross-event correlation, memory/session pattern detection, and ambiguous actionability. "
        "Allowed conscious_task request_type values: THINK, SAVE, UPDATE_MEMORY_OR_SKILL, CREATE_FOLLOWUP. "
        "Never output REACH_OUT. Never invent raw transcript/file/task details."
    )
    user = {
        "context": context,
        "required_schema": {
            "action": "DROP | SAVE | CREATE_CONSCIOUS_TASK",
            "rationale": "short reason",
            "event_ids": ["evt_id"],
            "candidate_ids": ["cand_id"],
            "pressure": "optional 0..1",
            "conscious_task": {
                "request_type": "THINK | SAVE | UPDATE_MEMORY_OR_SKILL | CREATE_FOLLOWUP",
                "title": "required only for CREATE_CONSCIOUS_TASK",
                "why": "required only for CREATE_CONSCIOUS_TASK",
                "expected_decision": "required only for CREATE_CONSCIOUS_TASK",
            },
        },
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, separators=(",", ":"))},
    ]


def generate_advisory_output(
    context: dict,
    *,
    config: dict | None = None,
    transport=None,
) -> dict:
    """Generate advisory output with a cheap OpenAI-compatible model."""
    cfg = _merged_config(config)
    api_key = _model_api_key(cfg)
    if not api_key:
        raise RuntimeError(f"missing API key for {cfg.get('model_provider')} ({cfg.get('model_api_key_env')})")

    base_url = str(cfg["model_base_url"]).rstrip("/")
    url = f"{base_url}/chat/completions"
    payload = {
        "model": cfg["model"],
        "messages": _model_prompt(context),
        "temperature": float(cfg["model_temperature"]),
        "max_tokens": int(cfg["model_max_tokens"]),
    }
    response_format = cfg.get("model_response_format")
    if isinstance(response_format, dict) and response_format:
        payload["response_format"] = response_format
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-Title": "Agent Sensorium Subconscious",
    }
    call = transport or _post_openai_chat_completion
    response = call(url, payload, headers, int(cfg["model_timeout_seconds"]))
    try:
        content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("model response missing choices[0].message.content") from exc
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("model response content was empty")
    return validate_advisory_output(_extract_json_object(content))


def _refs_for_ids(items: list[dict], ids: list[str]) -> list[dict]:
    wanted = set(ids)
    return [item for item in items if item.get("id") in wanted]


def _source_candidate_fingerprint(candidate: dict) -> str:
    """Fingerprint source-owned meaning and evidence, never advisory prose."""
    payload = {
        "candidate_fingerprint": candidate_fingerprint(candidate),
        "event_ids": sorted(str(event_id) for event_id in candidate.get("event_ids") or []),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _candidate_from_advisory(
    store: SensoriumStore,
    output: dict,
    config: dict | None = None,
    *,
    admission_binding: dict | None = None,
) -> dict:
    cfg = _merged_config(config)
    events = store.read_jsonl("events")
    candidates = store.read_jsonl("candidates")
    event_refs = _refs_for_ids(events, output.get("event_ids") or [])
    candidate_refs = _refs_for_ids(candidates, output.get("candidate_ids") or [])

    sensitivities = [r.get("sensitivity", "private") for r in [*event_refs, *candidate_refs]]
    surface_sets = [r.get("allowed_surfaces") or ["local"] for r in [*event_refs, *candidate_refs]]
    task = output["conscious_task"]
    source_candidate = candidate_refs[0]
    source_candidate_fingerprint = _source_candidate_fingerprint(source_candidate)
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
        "source_candidate_fingerprint": source_candidate_fingerprint,
        "admission_binding": deepcopy(admission_binding),
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
            "admission_binding": deepcopy(admission_binding),
        },
    }
    candidate["fingerprint"] = candidate_fingerprint(candidate)
    return candidate


def _find_existing_candidate(candidates: list[dict], candidate: dict) -> dict | None:
    """Return the lifecycle-authoritative advisory for one semantic item."""
    incoming_binding = candidate.get("admission_binding")
    source_ids = list(candidate.get("source_candidate_ids") or [])
    matches = []
    for existing in candidates:
        if existing.get("kind") != "subconscious_advisory":
            continue
        existing_binding = existing.get("admission_binding") or (existing.get("advisory_meta") or {}).get("admission_binding")
        if isinstance(incoming_binding, dict) and isinstance(existing_binding, dict):
            if existing_binding.get("item_key") == incoming_binding.get("item_key"):
                matches.append(existing)
        elif len(source_ids) == 1 and list(existing.get("source_candidate_ids") or []) == source_ids:
            matches.append(existing)
    if not matches:
        return None

    status_rank = {
        "in_conscious_aperture": 0,
        "suppressed": 1,
        "cancelled": 1,
        "archived": 1,
        "prepared_external_work": 1,
        "held": 2,
        "reviewed": 3,
        "candidate": 4,
    }
    return min(
        matches,
        key=lambda row: (
            status_rank.get(str(row.get("status") or ""), 5),
            str(row.get("created_at") or ""),
            str(row.get("id") or ""),
        ),
    )


def _stable_source_candidate(store: SensoriumStore, output: dict) -> dict | None:
    source_ids = output.get("candidate_ids") or []
    if len(source_ids) != 1 or not isinstance(source_ids[0], str) or not source_ids[0].strip():
        return None
    source = next(
        (row for row in store.read_jsonl("candidates") if row.get("id") == source_ids[0]),
        None,
    )
    if not source or source.get("status", "candidate") != "candidate":
        return None
    if not is_advisory_source_kind(source.get("kind")):
        return None
    return source


def _refresh_existing_advisory(
    store: SensoriumStore,
    *,
    candidates: list[dict],
    existing: dict,
    incoming: dict,
) -> tuple[dict, bool, str]:
    """Refresh one advisory in place when its source materially changes."""
    old_source_fp = str(existing.get("source_candidate_fingerprint") or "")
    new_source_fp = str(incoming.get("source_candidate_fingerprint") or "")
    old_binding = existing.get("admission_binding") or (existing.get("advisory_meta") or {}).get("admission_binding")
    new_binding = incoming.get("admission_binding")
    if (
        isinstance(old_binding, dict)
        and isinstance(new_binding, dict)
        and old_binding.get("admission_key") == new_binding.get("admission_key")
    ):
        return existing, False, "unchanged_source_revision"

    # Historical advisories predate source fingerprints.  Backfill identity
    # without treating installation of the producer guard as a new revision.
    if not old_source_fp:
        updated = dict(existing)
        updated["source_candidate_fingerprint"] = new_source_fp
        for idx, row in enumerate(candidates):
            if row.get("id") == existing.get("id"):
                candidates[idx] = updated
                store.rewrite_jsonl("candidates", candidates)
                break
        return updated, False, "source_identity_backfilled"

    if old_source_fp == new_source_fp and not (
        isinstance(old_binding, dict)
        and isinstance(new_binding, dict)
        and old_binding.get("revision_key") != new_binding.get("revision_key")
    ):
        return existing, False, "unchanged_source"

    status = str(existing.get("status") or "")
    if status == "in_conscious_aperture":
        return existing, False, "source_changed_during_open_aperture"
    if status in {"suppressed", "cancelled", "archived", "prepared_external_work"}:
        return existing, False, "source_changed_but_explicitly_closed"
    if status not in {"candidate", "held", "reviewed"}:
        return existing, False, "source_changed_but_state_not_reopenable"

    updated = dict(existing)
    for field in (
        "pressure",
        "summary",
        "event_ids",
        "source_candidate_ids",
        "source_candidate_fingerprint",
        "admission_binding",
        "correlation_keys",
        "sensitivity",
        "allowed_surfaces",
        "conscious_task",
        "advisory_meta",
    ):
        updated[field] = deepcopy(incoming[field])
    updated["updated_at"] = utc_now_iso()
    if status == "reviewed":
        updated["status"] = "candidate"
        updated.pop("conscious_aperture", None)
    updated["fingerprint"] = candidate_fingerprint(updated)

    for idx, row in enumerate(candidates):
        if row.get("id") == existing.get("id"):
            candidates[idx] = updated
            store.rewrite_jsonl("candidates", candidates)
            break
    return updated, True, "source_material_changed"


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
        "admission_binding": deepcopy(result.get("admission_binding")),
        "reason": truncate_text(result.get("reason", ""), 160),
        "reason_code": result.get("reason_code"),
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
    model_generate=None,
    admission_binding: dict | None = None,
) -> dict:
    """Run one bounded advisory pass.

    The model lane is cheap/OpenAI-compatible and opt-in. If enabled with
    model_enabled=true and no advisory_output is supplied, it generates bounded
    advisory JSON from compact context only.
    """
    store.ensure_dirs()
    cfg = _merged_config(config)
    binding = deepcopy(admission_binding)
    source_decisions: list[dict] = []

    if binding is not None:
        valid, reason = validate_admission_binding(store, binding)
        if not valid:
            raise ValueError(reason)
        source_decisions, _, _ = prior_dispositions_for_binding(store, binding)
    elif enabled and advisory_output is None:
        plan = build_admission_plan(store, candidate_limit=int(cfg["candidate_limit"]))
        binding = deepcopy(plan["selection"])
        source_decisions = list(plan["source_decisions"])
        if binding is None:
            return {
                "action": "skipped_no_eligible_source",
                "dry_run": dry_run,
                "model_used": False,
                "reason": "source admission found no eligible unchanged item",
                "admission_plan": plan,
            }
    elif isinstance(advisory_output, dict):
        source_ids = advisory_output.get("candidate_ids") or []
        if len(source_ids) == 1 and isinstance(source_ids[0], str):
            binding, binding_error = binding_for_candidate(store, source_ids[0])
            if binding_error and binding_error != "source_candidate_unavailable":
                raise ValueError(binding_error)

    context = build_advisory_context(
        store,
        config=cfg,
        admission_binding=binding,
        source_decisions=source_decisions,
    )
    if not enabled:
        result = {
            "action": "disabled", "dry_run": dry_run, "model_used": False,
            "reason": "subconscious advisory model lane is disabled by default",
            "context": context, "admission_binding": binding,
        }
        _write_advisory_receipt(store, result=result, output=advisory_output, dry_run=dry_run, context=context, record_receipt=record_receipt)
        return result

    model_used = False
    if advisory_output is None:
        if not cfg.get("model_enabled"):
            result = {
                "action": "model_output_required", "dry_run": dry_run,
                "model_used": False,
                "reason": "no advisory_output supplied and cheap model lane is not enabled",
                "context": context, "admission_binding": binding,
            }
            _write_advisory_receipt(store, result=result, output=None, dry_run=dry_run, context=context, record_receipt=record_receipt)
            return result
        generator = model_generate or generate_advisory_output
        try:
            advisory_output = generator(context, config=cfg)
            model_used = True
        except Exception as exc:
            result = {
                "action": "model_unavailable", "dry_run": dry_run,
                "model_used": False, "model_provider": cfg.get("model_provider"),
                "model": cfg.get("model"), "reason": truncate_text(str(exc), 220),
                "context": context, "admission_binding": binding,
            }
            _write_advisory_receipt(store, result=result, output=None, dry_run=dry_run, context=context, record_receipt=record_receipt)
            return result

    output = validate_advisory_output(advisory_output)
    if binding is not None:
        source_id = binding["source_candidate_id"]
        candidate_ids = output.get("candidate_ids") or []
        if candidate_ids and candidate_ids != [source_id]:
            raise ValueError("advisory_output_references_other_source")
        if output["action"] == "CREATE_CONSCIOUS_TASK" and candidate_ids != [source_id]:
            raise ValueError("stable_source_candidate_required")
        source = next((row for row in store.read_jsonl("candidates") if row.get("id") == source_id), None)
        allowed_events = set((source or {}).get("event_ids") or [])
        if any(event_id not in allowed_events for event_id in output.get("event_ids") or []):
            raise ValueError("advisory_output_references_other_source")

    if output["action"] == "CREATE_CONSCIOUS_TASK" and _stable_source_candidate(store, output) is None:
        result = {
            "action": "save", "dry_run": dry_run, "model_used": model_used,
            "reason": "CREATE_CONSCIOUS_TASK requires exactly one live source candidate",
            "reason_code": "stable_source_candidate_required", "context": context,
            "admission_binding": binding, "output_action": output["action"],
        }
        _write_advisory_receipt(store, result=result, output=output, dry_run=dry_run, context=context, record_receipt=record_receipt)
        return result

    if dry_run:
        if output["action"] in {"DROP", "SAVE"}:
            result = {
                "action": output["action"].lower(), "dry_run": True,
                "model_used": model_used, "reason": output["rationale"],
                "context": context, "admission_binding": binding,
                "output_action": output["action"],
            }
        else:
            candidate = _candidate_from_advisory(
                store, output, config=cfg, admission_binding=binding,
            )
            result = {
                "action": "would_create_conscious_task", "dry_run": True,
                "model_used": model_used, "candidate_preview": candidate,
                "reason": output["rationale"], "context": context,
                "admission_binding": binding, "output_action": output["action"],
            }
        _write_advisory_receipt(store, result=result, output=output, dry_run=True, context=context, record_receipt=record_receipt)
        return result

    with store.candidate_transaction():
        if binding is not None:
            valid, reason = validate_admission_binding(store, binding)
            if not valid:
                raise ValueError(reason)
            _, decided, disposition_reason = prior_dispositions_for_binding(store, binding)
            if decided:
                existing = next((
                    row for row in store.read_jsonl("candidates")
                    if row.get("kind") == "subconscious_advisory"
                    and isinstance(row.get("admission_binding"), dict)
                    and row["admission_binding"].get("item_key") == binding.get("item_key")
                ), None)
                return {
                    "action": "already_exists", "dry_run": False,
                    "model_used": model_used,
                    "candidate_id": (existing or {}).get("id"),
                    "reason": "source revision already has a canonical disposition",
                    "reason_code": disposition_reason,
                    "context": context, "admission_binding": binding,
                    "output_action": output["action"],
                }

        if output["action"] in {"DROP", "SAVE"}:
            result = {
                "action": output["action"].lower(), "dry_run": False,
                "model_used": model_used,
                "model_provider": cfg.get("model_provider") if model_used else None,
                "model": cfg.get("model") if model_used else None,
                "reason": output["rationale"], "context": context,
                "admission_binding": binding, "output_action": output["action"],
            }
            _write_advisory_receipt(store, result=result, output=output, dry_run=False, context=context, record_receipt=record_receipt)
            return result

        if _stable_source_candidate(store, output) is None:
            raise ValueError("stable_source_candidate_required")
        candidate = _candidate_from_advisory(
            store, output, config=cfg, admission_binding=binding,
        )
        candidates = store.read_jsonl("candidates")
        existing = _find_existing_candidate(candidates, candidate)
        if existing:
            existing, changed, reason_code = _refresh_existing_advisory(
                store, candidates=candidates, existing=existing, incoming=candidate,
            )
            result = {
                "action": "updated_conscious_task_candidate" if changed else "already_exists",
                "dry_run": False, "model_used": model_used,
                "model_provider": cfg.get("model_provider") if model_used else None,
                "model": cfg.get("model") if model_used else None,
                "candidate_id": existing.get("id"),
                "reason": "source revision updated in canonical advisory" if changed else "source revision already represented",
                "reason_code": reason_code, "context": context,
                "admission_binding": binding, "output_action": output["action"],
            }
            _write_advisory_receipt(store, result=result, output=output, dry_run=False, context=context, record_receipt=record_receipt)
            return result

        store.append_jsonl("candidates", candidate)
        result = {
            "action": "created_conscious_task_candidate", "dry_run": False,
            "model_used": model_used,
            "model_provider": cfg.get("model_provider") if model_used else None,
            "model": cfg.get("model") if model_used else None,
            "candidate_id": candidate["id"], "reason": output["rationale"],
            "context": context, "admission_binding": binding,
            "output_action": output["action"],
        }
        _write_advisory_receipt(store, result=result, output=output, dry_run=False, context=context, record_receipt=record_receipt)
        return result
