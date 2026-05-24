"""Tool handlers for Agent Sensorium — callable without live Hermes runtime."""

import json

from .dispatcher import dispatch_once as _dispatch_once
from .gate import (
    DEFAULT_CONFIG,
    event_to_candidate,
    promote_signal_to_event,
    should_promote_signal,
)
from .pointers import select_attention_pointer
from .schemas import normalize_signal, truncate_text, utc_now_iso, validate_event, validate_signal
from .store import SensoriumStore

ARCHIVED_STATUSES = {"archived", "closed"}

VALID_CANDIDATE_ACTIONS = {"suppress", "hold", "cancel", "mark_reviewed"}
VALID_THREAD_ACTIONS = {"close", "hold", "resume", "archive", "pin", "unpin", "mark_reviewed"}
_VISIBLE_STATUSES = {"dormant", "held"}
_ALLOWED_THREAD_TRANSITIONS: dict[str, set[str]] = {
    "dormant": {"close", "hold", "archive", "mark_reviewed", "pin", "unpin"},
    "held": {"close", "resume", "archive", "mark_reviewed", "pin", "unpin"},
}


def _ok(instance: str, data: dict) -> str:
    return json.dumps({"success": True, "instance": instance, "data": data, "error": None})


def _err(instance: str, error: str) -> str:
    return json.dumps({"success": False, "instance": instance, "data": None, "error": error})


def handle_sensorium_status(
    *, instance: str = "default", state_dir: str | None = None
) -> str:
    store = SensoriumStore(instance=instance, state_dir=state_dir)
    store.ensure_dirs()

    signals = store.read_jsonl("signals")
    events = store.read_jsonl("events")
    candidates = store.read_jsonl("candidates")
    threads = store.read_jsonl("threads")

    active_candidates = [c for c in candidates if c.get("status") == "candidate"]
    active_candidates.sort(key=lambda c: c.get("pressure", 0), reverse=True)

    visible_threads = [t for t in threads if t.get("status") in ("dormant", "held")]
    visible_threads.sort(key=lambda t: t.get("created_at", ""), reverse=True)

    data = {
        "instance": instance,
        "state_dir": str(store.root),
        "counts": {
            "signals": len(signals),
            "events": len(events),
            "candidates": len(candidates),
            "active_candidates": len(active_candidates),
            "threads": len(threads),
            "dormant_threads": len([t for t in threads if t.get("status") == "dormant"]),
            "held_threads": len([t for t in threads if t.get("status") == "held"]),
            "closed_threads": len([t for t in threads if t.get("status") == "closed"]),
            "archived_threads": len([t for t in threads if t.get("status") == "archived"]),
            "archived_candidates": len([c for c in candidates if c.get("status") == "archived"]),
        },
        "top_candidates": [
            {
                "id": c["id"],
                "kind": c.get("kind"),
                "pressure": c.get("pressure"),
                "summary": truncate_text(c.get("summary", ""), 120),
            }
            for c in active_candidates[:5]
        ],
        "top_threads": [
            {
                "id": t["id"],
                "status": t.get("status"),
                "title": truncate_text(t.get("conscious_task", {}).get("title", ""), 120),
                "origin_candidate_id": t.get("origin_candidate_id"),
                "created_at": t.get("created_at"),
            }
            for t in visible_threads[:5]
        ],
        "ts": utc_now_iso(),
    }

    decisions = store.read_jsonl("decisions")
    if decisions:
        latest = decisions[-1]
        data["latest_decision"] = {
            "ts": latest.get("ts"),
            "type": latest.get("type"),
            "thread_id": latest.get("thread_id"),
            "candidate_id": latest.get("candidate_id"),
            "action": latest.get("action"),
            "reason": truncate_text(latest.get("reason", ""), 80),
        }

    return _ok(instance, data)


def handle_sensorium_ingest_signal(
    *,
    signal: dict,
    instance: str = "default",
    state_dir: str | None = None,
    config: dict | None = None,
) -> str:
    try:
        validate_signal(signal)
    except ValueError as e:
        return _err(instance, str(e))

    store = SensoriumStore(instance=instance, state_dir=state_dir)
    store.ensure_dirs()

    normalized = normalize_signal(signal)
    store.append_jsonl("signals", normalized)

    promoted, reason = should_promote_signal(normalized, config)
    result: dict = {
        "signal_id": normalized["id"],
        "promoted": promoted,
        "reason": reason,
    }

    if promoted:
        event = promote_signal_to_event(normalized, config)
        store.append_jsonl("events", event)
        result["event_id"] = event["id"]

        candidate = event_to_candidate(event, config)
        store.append_jsonl("candidates", candidate)
        result["candidate_id"] = candidate["id"]

    return _ok(instance, result)


def handle_sensorium_ingest_event(
    *,
    event: dict,
    instance: str = "default",
    state_dir: str | None = None,
    config: dict | None = None,
) -> str:
    """Ingest an already-promoted trusted event and create a candidate."""
    try:
        validate_event(event)
    except ValueError as e:
        return _err(instance, str(e))

    store = SensoriumStore(instance=instance, state_dir=state_dir)
    store.ensure_dirs()

    incoming = dict(event)
    incoming.setdefault("source_signal_ids", [])
    incoming.setdefault("signal_count", len(incoming.get("source_signal_ids") or []))
    incoming.setdefault("strength", 0.5)
    incoming.setdefault("correlation_keys", [])
    incoming.setdefault("sensitivity", "private")
    incoming.setdefault("allowed_surfaces", ["local"])
    incoming.setdefault("expires_at", "")

    try:
        validate_event(incoming)
    except ValueError as e:
        return _err(instance, str(e))

    store.append_jsonl("events", incoming)
    candidate = event_to_candidate(incoming, config)
    store.append_jsonl("candidates", candidate)

    return _ok(instance, {
        "event_id": incoming["id"],
        "candidate_id": candidate["id"],
    })


def handle_sensorium_dispatch_once(
    *,
    instance: str = "default",
    state_dir: str | None = None,
    dry_run: bool = True,
    config: dict | None = None,
) -> str:
    store = SensoriumStore(instance=instance, state_dir=state_dir)
    store.ensure_dirs()
    result = _dispatch_once(store, dry_run=dry_run, config=config)
    return _ok(instance, result)


def handle_sensorium_candidate_update(
    *,
    candidate_id: str,
    action: str,
    reason: str = "",
    instance: str = "default",
    state_dir: str | None = None,
) -> str:
    if action not in VALID_CANDIDATE_ACTIONS:
        return _err(instance, f"Invalid action '{action}'. Must be one of: {sorted(VALID_CANDIDATE_ACTIONS)}")

    store = SensoriumStore(instance=instance, state_dir=state_dir)
    store.ensure_dirs()

    candidates = store.read_jsonl("candidates")
    target = None
    for c in candidates:
        if c.get("id") == candidate_id:
            target = c
            break

    if target is None:
        return _err(instance, f"Candidate '{candidate_id}' not found.")

    old_status = target.get("status", "candidate")
    if action == "suppress":
        new_status = "suppressed"
    elif action == "hold":
        new_status = "held"
    elif action == "cancel":
        new_status = "cancelled"
    elif action == "mark_reviewed":
        new_status = "reviewed"
    else:
        new_status = old_status

    target["status"] = new_status
    target["updated_at"] = utc_now_iso()

    receipt = {
        "ts": utc_now_iso(),
        "type": "candidate.updated",
        "candidate_id": candidate_id,
        "action": action,
        "old_status": old_status,
        "new_status": new_status,
        "reason": reason,
    }
    store.append_jsonl("decisions", receipt)

    _rewrite_jsonl(store, "candidates", candidates)

    return _ok(instance, {
        "candidate_id": candidate_id,
        "action": action,
        "old_status": old_status,
        "new_status": new_status,
        "receipt": receipt,
    })


def _find_thread(threads: list[dict], thread_id: str | None = None) -> dict | None:
    visible = [t for t in threads if t.get("status") in ("dormant", "held")]
    visible.sort(key=lambda t: t.get("created_at", ""), reverse=True)
    if not thread_id or thread_id == "latest":
        return visible[0] if visible else None
    for t in threads:
        if t.get("id") == thread_id:
            return t
    return None


def _thread_allowed_on_surface(thread: dict, surface: str) -> bool:
    allowed = set(thread.get("allowed_surfaces") or [])
    return bool(surface and surface in allowed)


def _compact_thread_capsule(thread: dict) -> dict:
    task = thread.get("conscious_task", {})
    return {
        "thread_id": thread.get("id"),
        "status": thread.get("status"),
        "title": task.get("title", ""),
        "conscious_task": {
            "id": task.get("id"),
            "request_type": task.get("request_type"),
            "why": task.get("why"),
            "expected_decision": task.get("expected_decision"),
        },
        "origin_candidate_id": thread.get("origin_candidate_id"),
        "continuity_summary": thread.get("continuity_summary", []),
        "open_questions": thread.get("open_questions", []),
        "next_prompt_to_operator": thread.get("next_prompt_to_operator", ""),
        "summary_dirty": bool(thread.get("summary_dirty")),
        "sensitivity": thread.get("sensitivity", "private"),
        "allowed_surfaces": thread.get("allowed_surfaces", []),
        "created_at": thread.get("created_at"),
        "updated_at": thread.get("updated_at"),
        "expires_at": thread.get("expires_at"),
    }


def handle_sensorium_thread_open(
    *,
    thread_id: str = "latest",
    surface: str = "local",
    instance: str = "default",
    state_dir: str | None = None,
) -> str:
    store = SensoriumStore(instance=instance, state_dir=state_dir)
    store.ensure_dirs()
    threads = store.read_jsonl("threads")
    target = _find_thread(threads, thread_id)
    if target is None:
        return _err(instance, f"Thread '{thread_id or 'latest'}' not found.")
    if target.get("status") not in _VISIBLE_STATUSES:
        return _err(instance, f"Thread '{target.get('id')}' is {target.get('status')} and cannot be opened.")
    if not _thread_allowed_on_surface(target, surface):
        return _err(instance, f"Thread '{target.get('id')}' is not allowed on surface '{surface}'.")
    return _ok(instance, _compact_thread_capsule(target))


def handle_sensorium_thread_update(
    *,
    thread_id: str,
    action: str,
    reason: str = "",
    instance: str = "default",
    state_dir: str | None = None,
) -> str:
    if action not in VALID_THREAD_ACTIONS:
        return _err(instance, f"Invalid action '{action}'. Must be one of: {sorted(VALID_THREAD_ACTIONS)}")

    store = SensoriumStore(instance=instance, state_dir=state_dir)
    store.ensure_dirs()
    threads = store.read_jsonl("threads")
    target = _find_thread(threads, thread_id)
    if target is None:
        return _err(instance, f"Thread '{thread_id}' not found.")

    old_status = target.get("status", "dormant")
    old_pinned = bool(target.get("pinned"))

    allowed = _ALLOWED_THREAD_TRANSITIONS.get(old_status)
    if allowed is None:
        return _err(instance, f"Thread '{target.get('id')}' is {old_status} and cannot be updated.")
    if action not in allowed:
        return _err(instance, f"Action '{action}' is not allowed on a {old_status} thread.")
    if action == "pin" and old_pinned:
        return _err(instance, f"Thread '{target.get('id')}' is already pinned.")
    if action == "unpin" and not old_pinned:
        return _err(instance, f"Thread '{target.get('id')}' is not pinned.")

    now = utc_now_iso()

    if action == "close":
        target["status"] = "closed"
    elif action == "hold":
        target["status"] = "held"
    elif action == "resume":
        target["status"] = "dormant"
    elif action == "archive":
        target["status"] = "archived"
    elif action == "mark_reviewed":
        target["status"] = "closed"
    elif action == "pin":
        target["pinned"] = True
    elif action == "unpin":
        target["pinned"] = False

    target["updated_at"] = now
    if target.get("status") in {"closed", "archived"}:
        _mark_origin_candidate_reviewed(
            store,
            origin_candidate_id=target.get("origin_candidate_id"),
            thread_id=target.get("id"),
            now=now,
            reason=reason,
        )
    receipt = {
        "ts": now,
        "type": "thread.updated",
        "thread_id": target.get("id"),
        "action": action,
        "old_status": old_status,
        "new_status": target.get("status", old_status),
        "old_pinned": old_pinned,
        "new_pinned": bool(target.get("pinned")),
        "reason": reason,
    }
    target.setdefault("decision_log", []).append(receipt)
    store.append_jsonl("decisions", receipt)
    _rewrite_jsonl(store, "threads", threads)
    return _ok(instance, receipt)


def _mark_origin_candidate_reviewed(
    store: SensoriumStore,
    *,
    origin_candidate_id: str | None,
    thread_id: str | None,
    now: str,
    reason: str,
) -> dict | None:
    """Mark the originating candidate reviewed when its conscious thread terminates."""
    if not origin_candidate_id:
        return None

    candidates = store.read_jsonl("candidates")
    target = None
    for candidate in candidates:
        if candidate.get("id") == origin_candidate_id:
            target = candidate
            break
    if target is None:
        return None

    old_status = target.get("status", "candidate")
    if old_status != "candidate":
        return None

    target["status"] = "reviewed"
    target["updated_at"] = now
    receipt = {
        "ts": now,
        "type": "candidate.updated",
        "candidate_id": origin_candidate_id,
        "thread_id": thread_id,
        "action": "mark_reviewed",
        "old_status": old_status,
        "new_status": "reviewed",
        "reason": reason or "origin thread closed",
    }
    store.append_jsonl("decisions", receipt)
    _rewrite_jsonl(store, "candidates", candidates)
    return receipt


def handle_sensorium_attention_pointer(
    *,
    instance: str = "default",
    state_dir: str | None = None,
    surface: str = "local",
    config: dict | None = None,
) -> str:
    store = SensoriumStore(instance=instance, state_dir=state_dir)
    store.ensure_dirs()
    return _ok(instance, select_attention_pointer(store, surface=surface, config=config))


def handle_sensorium_compact(
    *, instance: str = "default", state_dir: str | None = None
) -> str:
    store = SensoriumStore(instance=instance, state_dir=state_dir)
    store.ensure_dirs()

    now = utc_now_iso()
    candidates = store.read_jsonl("candidates")
    threads = store.read_jsonl("threads")

    archived_candidates: list[str] = []
    archived_threads: list[str] = []
    receipts: list[dict] = []

    for c in candidates:
        status = c.get("status", "candidate")
        if status in ARCHIVED_STATUSES:
            continue
        expires = c.get("expires_at", "")
        is_expired = bool(expires) and expires <= now
        is_terminal = status in ("suppressed", "cancelled")
        if is_expired or is_terminal:
            receipt = {
                "ts": now,
                "type": "compact.candidate_archived",
                "candidate_id": c["id"],
                "reason": "expired" if is_expired else f"terminal_status:{status}",
                "previous_status": status,
            }
            c["status"] = "archived"
            c["updated_at"] = now
            archived_candidates.append(c["id"])
            receipts.append(receipt)
            store.append_jsonl("decisions", receipt)

    for t in threads:
        status = t.get("status", "dormant")
        if status == "archived":
            continue
        if t.get("pinned"):
            continue
        expires = t.get("expires_at", "")
        if expires and expires <= now:
            receipt = {
                "ts": now,
                "type": "compact.thread_archived",
                "thread_id": t["id"],
                "reason": "expired",
                "previous_status": status,
            }
            t["status"] = "archived"
            t["updated_at"] = now
            archived_threads.append(t["id"])
            receipts.append(receipt)
            store.append_jsonl("decisions", receipt)

    if archived_candidates:
        _rewrite_jsonl(store, "candidates", candidates)
    if archived_threads:
        _rewrite_jsonl(store, "threads", threads)

    return _ok(instance, {
        "archived_candidates": archived_candidates,
        "archived_threads": archived_threads,
        "receipts_written": len(receipts),
    })


def _rewrite_jsonl(store: SensoriumStore, name: str, items: list[dict]) -> None:
    path = store._resolve(name)
    with open(path, "w") as f:
        for item in items:
            f.write(json.dumps(item, separators=(",", ":")) + "\n")
