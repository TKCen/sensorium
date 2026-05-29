"""Deprecated Sensorium-local thread action compatibility layer.

The generic motor-plan shape remains useful as compact refs/receipts, but live
action/work ticketing now belongs to Kanban. Prefer Kanban task comments,
child tasks, artifacts, and settlement refs over creating hidden Sensorium-only
action queues.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy

from .schemas import new_id, truncate_text, utc_now_iso
from .store import SensoriumStore

VALID_ACTION_STATUSES = {
    "proposed", "prepared", "offered", "acted",
    "closed", "expired", "cancelled", "rejected",
}
TERMINAL_STATUSES = {"acted", "closed", "expired", "cancelled", "rejected"}
VALID_OUTCOMES = {"completed", "failed", "cancelled", "expired", "rejected", "superseded"}
VALID_ATTACHMENT_KINDS = {"worker_request", "outbox_request", "artifact_ref", "external_ref"}
OPENABLE_THREAD_STATUSES = {"dormant", "held"}

ACTION_DEFAULTS: dict = {
    "enabled": True,
    "max_title_chars": 200,
    "max_intent_chars": 500,
    "max_summary_chars": 1500,
    "max_why_now_chars": 500,
    "max_refs": 10,
    "max_attachments": 20,
    "max_attachment_metadata_chars": 500,
    "allowed_attachment_kinds": [
        "worker_request", "outbox_request", "artifact_ref", "external_ref",
    ],
}


def _merged_action_config(config: dict | None = None) -> dict:
    cfg = deepcopy(ACTION_DEFAULTS)
    if not config:
        return cfg
    for key, value in config.items():
        cfg[key] = value
    return cfg


def _compute_idempotency_key(
    *, origin_thread_id: str, intent: str, title: str,
) -> str:
    parts = [origin_thread_id, intent, title]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:24]


def _find_thread_by_id(threads: list[dict], thread_id: str) -> dict | None:
    for t in threads:
        if t.get("id") == thread_id:
            return t
    return None


def _find_action_by_id(actions: list[dict], action_id: str) -> dict | None:
    for a in actions:
        if a.get("id") == action_id:
            return a
    return None


def _find_existing_action(
    actions: list[dict], idempotency_key: str,
) -> dict | None:
    for action in actions:
        if action.get("idempotency_key") == idempotency_key:
            return action
    return None


def _rewrite_jsonl(store: SensoriumStore, name: str, items: list[dict]) -> None:
    store.rewrite_jsonl(name, items)


def _denied(reason: str, detail: str, *, thread_id: str = "") -> dict:
    return {
        "success": False,
        "error": reason,
        "detail": detail,
        "thread_id": thread_id,
    }


def prepare_action(
    store: SensoriumStore,
    *,
    thread_id: str,
    intent: str,
    title: str,
    summary: str = "",
    why_now: str = "",
    refs: dict | None = None,
    resume_trigger: str = "",
    config: dict | None = None,
) -> dict:
    cfg = _merged_action_config(config)
    now = utc_now_iso()

    if not cfg.get("enabled"):
        return _denied(
            "actions_disabled", "Action system is disabled in config.",
            thread_id=thread_id,
        )

    if not intent or not intent.strip():
        return _denied(
            "missing_intent", "Intent must be a non-empty string.",
            thread_id=thread_id,
        )

    store.ensure_dirs()
    threads = store.read_jsonl("threads")
    thread = _find_thread_by_id(threads, thread_id)

    if thread is None:
        return _denied(
            "thread_not_found", f"Thread '{thread_id}' not found.",
            thread_id=thread_id,
        )

    thread_status = thread.get("status", "")
    if thread_status not in OPENABLE_THREAD_STATUSES:
        return _denied(
            "thread_not_openable",
            f"Thread '{thread_id}' is {thread_status}, not openable.",
            thread_id=thread_id,
        )

    max_title = int(cfg.get("max_title_chars", 200))
    max_intent = int(cfg.get("max_intent_chars", 500))
    max_summary = int(cfg.get("max_summary_chars", 1500))
    max_why_now = int(cfg.get("max_why_now_chars", 500))
    max_refs = int(cfg.get("max_refs", 10))

    truncated_title = truncate_text(title, max_title) if title else ""
    truncated_intent = truncate_text(intent.strip(), max_intent)
    truncated_summary = truncate_text(summary, max_summary) if summary else ""
    truncated_why_now = truncate_text(why_now, max_why_now) if why_now else ""

    bounded_refs: dict = {}
    if refs and isinstance(refs, dict):
        for k, v in list(refs.items())[:max_refs]:
            if isinstance(k, str) and isinstance(v, str):
                bounded_refs[k] = truncate_text(v, 200)

    idempotency_key = _compute_idempotency_key(
        origin_thread_id=thread_id,
        intent=truncated_intent,
        title=truncated_title,
    )

    existing_actions = store.read_jsonl("thread_actions")
    existing = _find_existing_action(existing_actions, idempotency_key)
    if existing is not None:
        return {
            "success": True,
            "data": existing,
            "idempotent_hit": True,
        }

    action_id = new_id("tact")
    action = {
        "id": action_id,
        "ts": now,
        "updated_at": now,
        "status": "proposed",
        "origin_thread_id": thread_id,
        "origin_candidate_id": thread.get("origin_candidate_id", ""),
        "title": truncated_title,
        "intent": truncated_intent,
        "summary": truncated_summary,
        "why_now": truncated_why_now,
        "refs": bounded_refs,
        "attachments": [],
        "sensitivity": thread.get("sensitivity", "private"),
        "allowed_surfaces": list(thread.get("allowed_surfaces") or []),
        "resume_trigger": resume_trigger or "",
        "idempotency_key": idempotency_key,
        "outcome": "",
        "result_summary": "",
        "feedback_signal_id": "",
        "closed_reason": "",
    }

    store.append_jsonl("thread_actions", action)

    receipt = {
        "ts": now,
        "type": "action.proposed",
        "thread_id": thread_id,
        "action_id": action_id,
        "intent": truncated_intent,
        "idempotency_key": idempotency_key,
    }
    store.append_jsonl("decisions", receipt)

    thread.setdefault("interaction_refs", []).append({
        "type": "action_proposed",
        "action_id": action_id,
        "ts": now,
    })
    thread.setdefault("decision_log", []).append({
        "ts": now,
        "type": "action.proposed",
        "action_id": action_id,
        "intent": truncated_intent,
    })
    thread["updated_at"] = now
    _rewrite_jsonl(store, "threads", threads)

    return {
        "success": True,
        "data": action,
        "receipt": receipt,
    }


def attach_action_ref(
    store: SensoriumStore,
    *,
    action_id: str,
    kind: str,
    ref_id: str,
    metadata: dict | None = None,
    config: dict | None = None,
) -> dict:
    cfg = _merged_action_config(config)
    now = utc_now_iso()

    if not cfg.get("enabled"):
        return _denied("actions_disabled", "Action system is disabled in config.")

    allowed_kinds = set(cfg.get("allowed_attachment_kinds") or [])
    if kind not in allowed_kinds:
        return _denied(
            "invalid_attachment_kind",
            f"Attachment kind '{kind}' not in allowed kinds {sorted(allowed_kinds)}",
        )

    if not ref_id or not ref_id.strip():
        return _denied("missing_ref_id", "Attachment ref_id must be non-empty.")

    store.ensure_dirs()
    actions = store.read_jsonl("thread_actions")
    action = _find_action_by_id(actions, action_id)

    if action is None:
        return _denied("action_not_found", f"Action '{action_id}' not found.")

    if action.get("status") in TERMINAL_STATUSES:
        return _denied(
            "action_terminal",
            f"Action '{action_id}' is {action.get('status')}, cannot attach.",
        )

    max_attachments = int(cfg.get("max_attachments", 20))
    current_attachments = action.get("attachments") or []
    if len(current_attachments) >= max_attachments:
        return _denied(
            "too_many_attachments",
            f"Action already has {len(current_attachments)} attachments "
            f"(max {max_attachments}).",
        )

    max_meta_chars = int(cfg.get("max_attachment_metadata_chars", 500))
    bounded_metadata: dict = {}
    if metadata and isinstance(metadata, dict):
        serialized = json.dumps(metadata, separators=(",", ":"))
        if len(serialized) > max_meta_chars:
            return _denied(
                "metadata_too_large",
                f"Attachment metadata is {len(serialized)} chars "
                f"(max {max_meta_chars}).",
            )
        bounded_metadata = metadata

    attachment = {
        "kind": kind,
        "ref_id": ref_id.strip(),
        "metadata": bounded_metadata,
        "attached_at": now,
    }
    current_attachments.append(attachment)
    action["attachments"] = current_attachments
    action["updated_at"] = now

    receipt = {
        "ts": now,
        "type": "action.attached",
        "action_id": action_id,
        "thread_id": action.get("origin_thread_id"),
        "kind": kind,
        "ref_id": ref_id.strip(),
    }
    store.append_jsonl("decisions", receipt)
    _rewrite_jsonl(store, "thread_actions", actions)

    return {
        "success": True,
        "data": action,
        "receipt": receipt,
    }


def _build_action_feedback_signal(action: dict, *, store: SensoriumStore) -> dict:
    origin_candidate_id = action.get("origin_candidate_id", "")
    correlation_keys: list[str] = []
    if origin_candidate_id:
        for c in store.read_jsonl("candidates"):
            if c.get("id") == origin_candidate_id:
                correlation_keys = list(c.get("correlation_keys") or [])
                break

    outcome = action.get("outcome", "failed")
    result_summary = action.get("result_summary", "")
    title = action.get("title", "")

    return {
        "sensor": "sensorium.action_result",
        "source": "feedback",
        "kind": "task_result",
        "summary": truncate_text(
            f"Action {action.get('id', '')} {outcome}: "
            f"{result_summary or title}",
            200,
        ),
        "actor": "tool",
        "strength_hint": 0.6,
        "caused_by": {
            "action_id": action.get("id", ""),
            "origin_thread_id": action.get("origin_thread_id", ""),
            "origin_candidate_id": origin_candidate_id,
            "intent": truncate_text(action.get("intent", ""), 120),
        },
        "outcome": outcome,
        "feedback_scope": "system_action",
        "correlation_keys": correlation_keys,
        "sensitivity": action.get("sensitivity", "private"),
        "allowed_surfaces": action.get("allowed_surfaces") or ["local"],
        "source_ref": action.get("id", ""),
    }


def _attach_action_result_to_thread(
    store: SensoriumStore,
    *,
    origin_thread_id: str,
    action_id: str,
    outcome: str,
    attachments: list[dict],
    now: str,
) -> None:
    """Attach compact action-result ref to origin thread, mark summary_dirty."""
    if not origin_thread_id:
        return
    threads = store.read_jsonl("threads")
    thread = _find_thread_by_id(threads, origin_thread_id)
    if thread is None:
        return
    output_refs: list[dict] = []
    for att in attachments or []:
        kind = att.get("kind", "")
        ref_id = att.get("ref_id", "")
        if not kind or not ref_id:
            continue
        output_refs.append({"type": kind, "ref_id": ref_id})
    thread.setdefault("interaction_refs", []).append({
        "type": "action_result",
        "action_id": action_id,
        "outcome": outcome,
        "output_refs": output_refs,
        "ts": now,
    })
    thread["summary_dirty"] = True
    if not thread.get("dirty_since"):
        thread["dirty_since"] = now
    thread["updated_at"] = now
    _rewrite_jsonl(store, "threads", threads)


def record_action_result(
    store: SensoriumStore,
    *,
    action_id: str,
    outcome: str,
    result_summary: str = "",
    closed_reason: str = "",
    config: dict | None = None,
) -> dict:
    cfg = _merged_action_config(config)
    now = utc_now_iso()

    store.ensure_dirs()
    actions = store.read_jsonl("thread_actions")
    action = _find_action_by_id(actions, action_id)

    if action is None:
        return _denied("action_not_found", f"Action '{action_id}' not found.")

    if action.get("status") in TERMINAL_STATUSES:
        return {
            "success": False,
            "error": "already_terminal",
            "detail": f"Action '{action_id}' is already "
            f"{action.get('status')}, cannot record result.",
        }

    if outcome not in VALID_OUTCOMES:
        return {
            "success": False,
            "error": "invalid_outcome",
            "detail": f"Invalid outcome: {outcome}. "
            f"Must be one of: {sorted(VALID_OUTCOMES)}",
        }

    max_summary = int(cfg.get("max_summary_chars", 1500))

    if outcome == "completed":
        new_status = "acted"
    elif outcome == "rejected":
        new_status = "rejected"
    elif outcome == "cancelled":
        new_status = "cancelled"
    elif outcome == "expired":
        new_status = "expired"
    else:
        new_status = "closed"

    action["status"] = new_status
    action["updated_at"] = now
    action["outcome"] = outcome
    action["result_summary"] = (
        truncate_text(result_summary, max_summary) if result_summary else ""
    )
    action["closed_reason"] = (
        truncate_text(closed_reason, 500) if closed_reason else ""
    )

    receipt = {
        "ts": now,
        "type": "action.result",
        "thread_id": action.get("origin_thread_id"),
        "action_id": action_id,
        "outcome": outcome,
        "new_status": new_status,
    }
    store.append_jsonl("decisions", receipt)
    _rewrite_jsonl(store, "thread_actions", actions)

    _attach_action_result_to_thread(
        store,
        origin_thread_id=action.get("origin_thread_id", ""),
        action_id=action_id,
        outcome=outcome,
        attachments=action.get("attachments") or [],
        now=now,
    )

    feedback_signal = _build_action_feedback_signal(action, store=store)

    return {
        "success": True,
        "data": action,
        "receipt": receipt,
        "feedback_signal": feedback_signal,
    }


def list_thread_actions(
    store: SensoriumStore,
    *,
    thread_id: str | None = None,
    status: str | None = None,
    limit: int = 20,
) -> list[dict]:
    store.ensure_dirs()
    actions = store.read_jsonl("thread_actions")
    if thread_id:
        actions = [a for a in actions if a.get("origin_thread_id") == thread_id]
    if status:
        actions = [a for a in actions if a.get("status") == status]
    compact = []
    for action in actions[-limit:]:
        compact.append({
            "id": action.get("id"),
            "status": action.get("status"),
            "intent": truncate_text(action.get("intent", ""), 120),
            "title": truncate_text(action.get("title", ""), 120),
            "origin_thread_id": action.get("origin_thread_id"),
            "outcome": action.get("outcome"),
            "attachment_count": len(action.get("attachments") or []),
            "ts": action.get("ts"),
            "updated_at": action.get("updated_at"),
        })
    return compact


def compact_actions_for_thread(
    store: SensoriumStore,
    thread_id: str,
    *,
    surface: str = "local",
    instance_config: dict | None = None,
) -> list[dict]:
    """Compact action summaries visible on surface, for thread_open capsule."""
    from .config import visible_on_surface

    actions = store.read_jsonl("thread_actions")
    thread_actions = [
        a for a in actions if a.get("origin_thread_id") == thread_id
    ]
    if not thread_actions:
        return []

    inst_cfg = instance_config or {}
    visible = []
    for action in thread_actions:
        if action.get("status") in TERMINAL_STATUSES:
            continue
        if not visible_on_surface(action, surface, inst_cfg):
            continue
        visible.append({
            "id": action.get("id"),
            "status": action.get("status"),
            "intent": truncate_text(action.get("intent", ""), 80),
            "title": truncate_text(action.get("title", ""), 80),
        })
    return visible


def count_active_actions_for_thread(
    store: SensoriumStore, thread_id: str,
) -> int:
    """Count non-terminal actions for a thread (for pointer hints)."""
    actions = store.read_jsonl("thread_actions")
    return sum(
        1 for a in actions
        if a.get("origin_thread_id") == thread_id
        and a.get("status") not in TERMINAL_STATUSES
    )
