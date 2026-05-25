"""Tool handlers for Agent Sensorium — callable without live Hermes runtime."""

import json
from datetime import datetime, timezone, timedelta

from .dispatcher import dispatch_once as _dispatch_once
from .gate import (
    DEFAULT_CONFIG,
    candidate_fingerprint,
    event_fingerprint,
    event_to_candidate,
    promote_signal_to_event,
    should_promote_signal,
    signal_fingerprint,
)
from .pointers import select_attention_pointer
from .schemas import (
    intersect_allowed_surfaces,
    merge_sensitivity,
    normalize_signal,
    truncate_text,
    utc_now_iso,
    validate_event,
    validate_signal,
)
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
    state = store.read_state()

    active_candidates = [c for c in candidates if c.get("status") == "candidate"]
    active_candidates.sort(key=lambda c: c.get("pressure", 0), reverse=True)

    visible_threads = [t for t in threads if t.get("status") in ("dormant", "held")]
    visible_threads.sort(key=lambda t: t.get("created_at", ""), reverse=True)

    active_threads = [t for t in threads if t.get("status") in ("dormant", "held")]
    now_ts = utc_now_iso()
    dirty_count = sum(1 for t in active_threads if t.get("dirty_since"))
    expiring_count = 0
    starved_count = 0
    for t in active_threads:
        expires = t.get("expires_at", "")
        if expires:
            try:
                exp_dt = datetime.strptime(expires, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                now_dt = datetime.strptime(now_ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                if timedelta(0) < (exp_dt - now_dt) <= timedelta(hours=24):
                    expiring_count += 1
            except (ValueError, TypeError):
                pass
        last_interaction = t.get("last_interaction_at") or t.get("created_at", "")
        if last_interaction:
            try:
                last_dt = datetime.strptime(last_interaction, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                now_dt = datetime.strptime(now_ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                if (now_dt - last_dt) > timedelta(hours=72):
                    starved_count += 1
            except (ValueError, TypeError):
                pass

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
            "dirty_threads": dirty_count,
            "starved_threads": starved_count,
            "expiring_threads": expiring_count,
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

    if state:
        if "state_version" in state:
            data["state_version"] = state.get("state_version")
        if "last_dispatch_result" in state:
            data["last_dispatch_result"] = state.get("last_dispatch_result")
        if "budgets" in state:
            data["budgets"] = state.get("budgets")
        if "locks" in state:
            data["locks"] = state.get("locks")

    return _ok(instance, data)


def _stored_signal_fingerprint(signal: dict) -> str:
    return signal.get("fingerprint") or signal_fingerprint(signal)


def _stored_event_fingerprint(event: dict) -> str:
    return event.get("fingerprint") or event_fingerprint(event)


def _stored_candidate_fingerprint(candidate: dict) -> str:
    return candidate.get("fingerprint") or candidate_fingerprint(candidate)


def _candidate_for_event_id(candidates: list[dict], event_id: str) -> dict | None:
    for candidate in candidates:
        if event_id in (candidate.get("event_ids") or []):
            return candidate
    return None


def _candidate_for_event_fingerprint(candidates: list[dict], event: dict) -> dict | None:
    event_candidate = event_to_candidate(event)
    event_candidate_fp = event_candidate.get("fingerprint") or candidate_fingerprint(event_candidate)
    for candidate in candidates:
        if _stored_candidate_fingerprint(candidate) == event_candidate_fp:
            return candidate
    return None


def _find_existing_signal(signals: list[dict], normalized: dict) -> dict | None:
    incoming_fp = normalized.get("fingerprint") or signal_fingerprint(normalized)
    incoming_id = normalized.get("id")
    for existing in signals:
        if incoming_id and existing.get("id") == incoming_id:
            return existing
        if _stored_signal_fingerprint(existing) == incoming_fp:
            return existing
    return None


def _find_existing_event(events: list[dict], incoming: dict) -> dict | None:
    incoming_fp = incoming.get("fingerprint") or event_fingerprint(incoming)
    incoming_id = incoming.get("id")
    for existing in events:
        if incoming_id and existing.get("id") == incoming_id:
            return existing
        if _stored_event_fingerprint(existing) == incoming_fp:
            return existing
    return None


def _find_event_for_signal(events: list[dict], signal_id: str) -> dict | None:
    for event in events:
        if signal_id in (event.get("source_signal_ids") or []):
            return event
    return None


def _find_related_candidate(candidates: list[dict], event: dict) -> dict | None:
    incoming_keys = set(event.get("correlation_keys") or [])
    if not incoming_keys:
        return None
    for candidate in candidates:
        if candidate.get("status", "candidate") != "candidate":
            continue
        if candidate.get("kind") != event.get("kind"):
            continue
        if incoming_keys & set(candidate.get("correlation_keys") or []):
            return candidate
    return None


def _event_to_existing_result(event: dict, candidates: list[dict]) -> dict:
    candidate = (
        _candidate_for_event_id(candidates, event.get("id", ""))
        or _candidate_for_event_fingerprint(candidates, event)
    )
    result = {
        "event_id": event.get("id"),
        "duplicate": True,
    }
    if candidate:
        result["candidate_id"] = candidate.get("id")
    return result


def _promoted_signal_existing_result(signal: dict, events: list[dict], candidates: list[dict]) -> dict:
    event = _find_event_for_signal(events, signal.get("id", ""))
    result: dict = {
        "signal_id": signal.get("id"),
        "promoted": bool(event),
        "duplicate": True,
        "reason": "duplicate_signal",
    }
    if event:
        result["event_id"] = event.get("id")
        candidate = _candidate_for_event_id(candidates, event.get("id", ""))
        if candidate:
            result["candidate_id"] = candidate.get("id")
    return result


def _coalesce_candidate_with_event(
    store: SensoriumStore,
    *,
    candidate: dict,
    candidates: list[dict],
    event: dict,
    incoming_candidate: dict,
) -> dict:
    now = utc_now_iso()
    event_ids = list(candidate.get("event_ids") or [])
    if event.get("id") not in event_ids:
        event_ids.append(event.get("id"))
    candidate["event_ids"] = event_ids

    keys = sorted(set(candidate.get("correlation_keys") or []) | set(event.get("correlation_keys") or []))
    candidate["correlation_keys"] = keys
    candidate["repetition"] = round(min(1.0, max(candidate.get("repetition", 0.0), (len(event_ids) - 1) * 0.25)), 3)
    candidate["pressure"] = round(
        min(1.0, max(candidate.get("pressure", 0.0), incoming_candidate.get("pressure", 0.0)) + 0.05 * (len(event_ids) - 1)),
        3,
    )
    candidate["sensitivity"] = merge_sensitivity([candidate.get("sensitivity", "private"), event.get("sensitivity", "private")])
    candidate["allowed_surfaces"] = intersect_allowed_surfaces([
        candidate.get("allowed_surfaces") or [],
        event.get("allowed_surfaces") or [],
    ])
    candidate["updated_at"] = now
    if not candidate.get("feedback_meta") and incoming_candidate.get("feedback_meta"):
        candidate["feedback_meta"] = incoming_candidate["feedback_meta"]
    candidate.setdefault("related_event_fingerprints", [])
    event_fp = event.get("fingerprint") or event_fingerprint(event)
    if event_fp not in candidate["related_event_fingerprints"]:
        candidate["related_event_fingerprints"].append(event_fp)

    receipt = {
        "ts": now,
        "type": "candidate.coalesced",
        "candidate_id": candidate.get("id"),
        "event_id": event.get("id"),
        "event_count": len(event_ids),
        "pressure": candidate.get("pressure"),
        "repetition": candidate.get("repetition"),
    }
    store.append_jsonl("decisions", receipt)
    _rewrite_jsonl(store, "candidates", candidates)
    return receipt


def _append_event_and_create_or_update_candidate(
    store: SensoriumStore,
    *,
    event: dict,
    config: dict | None = None,
) -> dict:
    events = store.read_jsonl("events")
    candidates = store.read_jsonl("candidates")

    existing_event = _find_existing_event(events, event)
    if existing_event:
        return _event_to_existing_result(existing_event, candidates)

    store.append_jsonl("events", event)
    candidate = event_to_candidate(event, config)
    related = _find_related_candidate(candidates, event)
    if related:
        receipt = _coalesce_candidate_with_event(
            store,
            candidate=related,
            candidates=candidates,
            event=event,
            incoming_candidate=candidate,
        )
        return {
            "event_id": event["id"],
            "candidate_id": related["id"],
            "duplicate": False,
            "coalesced": True,
            "receipt": receipt,
        }

    store.append_jsonl("candidates", candidate)
    return {
        "event_id": event["id"],
        "candidate_id": candidate["id"],
        "duplicate": False,
        "coalesced": False,
    }


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
    normalized["fingerprint"] = signal_fingerprint(normalized)

    signals = store.read_jsonl("signals")
    existing_signal = _find_existing_signal(signals, normalized)
    if existing_signal:
        events = store.read_jsonl("events")
        candidates = store.read_jsonl("candidates")
        return _ok(instance, _promoted_signal_existing_result(existing_signal, events, candidates))

    store.append_jsonl("signals", normalized)

    promoted, reason = should_promote_signal(normalized, config)
    result: dict = {
        "signal_id": normalized["id"],
        "promoted": promoted,
        "duplicate": False,
        "reason": reason,
    }

    if promoted:
        event = promote_signal_to_event(normalized, config)
        event_result = _append_event_and_create_or_update_candidate(store, event=event, config=config)
        result["event_id"] = event_result["event_id"]
        if "candidate_id" in event_result:
            result["candidate_id"] = event_result["candidate_id"]
        result["coalesced"] = event_result.get("coalesced", False)

    return _ok(instance, result)


def handle_sensorium_ingest_event(
    *,
    event: dict,
    instance: str = "default",
    state_dir: str | None = None,
    config: dict | None = None,
) -> str:
    """Ingest an already-promoted trusted event and create or update a candidate."""
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
    incoming["fingerprint"] = event_fingerprint(incoming)

    try:
        validate_event(incoming)
    except ValueError as e:
        return _err(instance, str(e))

    result = _append_event_and_create_or_update_candidate(store, event=incoming, config=config)
    return _ok(instance, result)


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
        "dirty_since": thread.get("dirty_since"),
        "source_refs": thread.get("source_refs", []),
        "sensitivity": thread.get("sensitivity", "private"),
        "allowed_surfaces": thread.get("allowed_surfaces", []),
        "hold_reason": thread.get("hold_reason", ""),
        "resume_trigger": thread.get("resume_trigger", ""),
        "last_interaction_at": thread.get("last_interaction_at"),
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


def _thread_feedback_outcome(action: str) -> str:
    if action == "archive":
        return "operator_rejected"
    return "completed"


def _build_thread_feedback_signal(store: SensoriumStore, *, thread: dict, action: str, reason: str) -> dict:
    origin_candidate_id = thread.get("origin_candidate_id") or ""
    origin_candidate = None
    if origin_candidate_id:
        for candidate in store.read_jsonl("candidates"):
            if candidate.get("id") == origin_candidate_id:
                origin_candidate = candidate
                break

    caused_by = {
        "thread_id": thread.get("id", ""),
        "action": action,
    }
    if origin_candidate_id:
        caused_by["origin_candidate_id"] = origin_candidate_id

    summary_bits = [f"Thread {thread.get('id', '')} {action}"]
    if reason:
        summary_bits.append(reason)

    return {
        "sensor": "sensorium.thread_update",
        "source": "feedback",
        "kind": "task_result",
        "summary": ": ".join(summary_bits),
        "actor": "operator",
        "strength_hint": 0.5,
        "caused_by": caused_by,
        "outcome": _thread_feedback_outcome(action),
        "feedback_scope": "operator_evaluation",
        "correlation_keys": (origin_candidate.get("correlation_keys") or []) if origin_candidate else [],
        "sensitivity": thread.get("sensitivity", "private"),
        "allowed_surfaces": thread.get("allowed_surfaces") or ["local"],
        "source_ref": thread.get("id", ""),
    }


def handle_sensorium_thread_update(
    *,
    thread_id: str,
    action: str,
    reason: str = "",
    resume_trigger: str = "",
    emit_feedback: bool = False,
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
        target["hold_reason"] = reason
        if resume_trigger:
            target["resume_trigger"] = resume_trigger
    elif action == "resume":
        target["status"] = "dormant"
        target["hold_reason"] = ""
        target["resume_trigger"] = ""
    elif action == "archive":
        target["status"] = "archived"
    elif action == "mark_reviewed":
        target["status"] = "closed"
    elif action == "pin":
        target["pinned"] = True
    elif action == "unpin":
        target["pinned"] = False

    target["last_interaction_at"] = now
    target["updated_at"] = now
    if target.get("status") in {"closed", "archived"}:
        _mark_origin_candidate_reviewed(
            store,
            origin_candidate_id=target.get("origin_candidate_id"),
            thread_id=target.get("id"),
            now=now,
            reason=reason,
        )
        _write_settlement_hint(store, thread=target, now=now)
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

    if emit_feedback and target.get("status") in {"closed", "archived"}:
        feedback_signal_raw = handle_sensorium_ingest_signal(
            signal=_build_thread_feedback_signal(store, thread=target, action=action, reason=reason),
            instance=instance,
            state_dir=state_dir,
        )
        feedback_signal_result = json.loads(feedback_signal_raw)
        if not feedback_signal_result.get("success"):
            return _err(instance, feedback_signal_result.get("error") or "Failed to emit feedback signal.")
        feedback_signal_id = (feedback_signal_result.get("data") or {}).get("signal_id", "")
        feedback_receipt = {
            "ts": now,
            "type": "thread.feedback_emitted",
            "thread_id": target.get("id"),
            "origin_candidate_id": target.get("origin_candidate_id"),
            "feedback_signal_id": feedback_signal_id,
            "outcome": _thread_feedback_outcome(action),
            "feedback_scope": "operator_evaluation",
            "action": action,
            "reason": reason,
        }
        store.append_jsonl("decisions", feedback_receipt)
        receipt["feedback_emitted"] = True
        receipt["feedback_signal_id"] = feedback_signal_id

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


def _write_settlement_hint(store: SensoriumStore, *, thread: dict, now: str) -> None:
    origin_id = thread.get("origin_candidate_id")
    if not origin_id:
        return
    candidates = store.read_jsonl("candidates")
    origin = None
    for c in candidates:
        if c.get("id") == origin_id:
            origin = c
            break
    receipt = {
        "ts": now,
        "type": "thread.settlement",
        "thread_id": thread.get("id"),
        "origin_candidate_id": origin_id,
        "correlation_keys": (origin.get("correlation_keys") or []) if origin else [],
        "fingerprint": (origin.get("fingerprint") or "") if origin else "",
        "settlement_type": thread.get("status", "closed"),
    }
    store.append_jsonl("decisions", receipt)


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


def handle_sensorium_service_threads(
    *,
    instance: str = "default",
    state_dir: str | None = None,
    config: dict | None = None,
    now: str | None = None,
) -> str:
    """Deterministic thread service pass: TTL archival, starvation/dirty/expiring reports."""
    store = SensoriumStore(instance=instance, state_dir=state_dir)
    store.ensure_dirs()

    now_ts = now or utc_now_iso()
    threads = store.read_jsonl("threads")

    cfg = config or {}
    starvation_hours = cfg.get("starvation_hours", 72)
    expiring_window_hours = cfg.get("expiring_window_hours", 24)

    active_statuses = {"dormant", "held"}
    archived_ids: list[str] = []
    starved_ids: list[str] = []
    dirty_ids: list[str] = []
    expiring_ids: list[str] = []
    receipts: list[dict] = []
    changed = False

    for t in threads:
        status = t.get("status", "dormant")
        if status not in active_statuses:
            continue

        expires = t.get("expires_at", "")
        if expires and expires <= now_ts and not t.get("pinned"):
            receipt = {
                "ts": now_ts,
                "type": "service.thread_archived",
                "thread_id": t["id"],
                "reason": "ttl_expired",
                "previous_status": status,
            }
            t["status"] = "archived"
            t["updated_at"] = now_ts
            archived_ids.append(t["id"])
            receipts.append(receipt)
            store.append_jsonl("decisions", receipt)
            changed = True
            continue

        last_interaction = t.get("last_interaction_at") or t.get("created_at", "")
        if last_interaction:
            try:
                last_dt = datetime.strptime(last_interaction, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                now_dt = datetime.strptime(now_ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                if (now_dt - last_dt) > timedelta(hours=starvation_hours):
                    starved_ids.append(t["id"])
            except (ValueError, TypeError):
                pass

        if t.get("dirty_since"):
            dirty_ids.append(t["id"])

        if expires:
            try:
                exp_dt = datetime.strptime(expires, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                now_dt = datetime.strptime(now_ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                remaining = exp_dt - now_dt
                if timedelta(0) < remaining <= timedelta(hours=expiring_window_hours):
                    expiring_ids.append(t["id"])
            except (ValueError, TypeError):
                pass

    if changed:
        _rewrite_jsonl(store, "threads", threads)

    data = {
        "serviced": len(archived_ids) + len(starved_ids) + len(dirty_ids) + len(expiring_ids),
        "archived": archived_ids,
        "starved": starved_ids,
        "dirty": dirty_ids,
        "expiring": expiring_ids,
        "receipts_written": len(receipts),
    }
    return _ok(instance, data)


def _rewrite_jsonl(store: SensoriumStore, name: str, items: list[dict]) -> None:
    path = store._resolve(name)
    with open(path, "w") as f:
        for item in items:
            f.write(json.dumps(item, separators=(",", ":")) + "\n")
