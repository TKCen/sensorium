"""Deprecated background-conscious claim/complete compatibility helpers.

Kanban is now the only activation substrate. These functions remain for old
state migration and explicit tests, but claiming dormant Sensorium threads is
disabled by default; live conscious review should be represented as Kanban
`conscious:review:*` tasks instead.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone

from .schemas import new_id, utc_now_iso
from .store import SensoriumStore

SAFE_REQUEST_TYPES = {
    "THINK", "SAVE", "UPDATE_MEMORY_OR_SKILL", "DELEGATE_WORK",
}

CONSCIOUS_DEFAULTS: dict = {
    "enabled": False,
    "lease_seconds": 600,
    "allowed_request_types": sorted(SAFE_REQUEST_TYPES),
    "default_actor": "background_conscious",
    "default_mode": "background",
}


def _merged_conscious_config(config: dict | None = None) -> dict:
    cfg = deepcopy(CONSCIOUS_DEFAULTS)
    if not config:
        return cfg
    for key, value in config.items():
        cfg[key] = value
    return cfg


def _parse_utc(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _rewrite_jsonl(store: SensoriumStore, name: str, items: list[dict]) -> None:
    store.rewrite_jsonl(name, items)


def _find_thread_by_id(threads: list[dict], thread_id: str) -> dict | None:
    for t in threads:
        if t.get("id") == thread_id:
            return t
    return None


def _lease_active(thread: dict, now: datetime) -> bool:
    lease = thread.get("active_lease")
    if not isinstance(lease, dict):
        return False
    expires = _parse_utc(lease.get("expires_at"))
    if expires is None:
        return True
    return expires > now


def _request_type(thread: dict) -> str:
    task = thread.get("conscious_task") or {}
    return task.get("request_type", "")


def _is_eligible(thread: dict, allowed_types: set[str], now: datetime) -> bool:
    if thread.get("status") != "dormant":
        return False
    if _lease_active(thread, now):
        return False
    pickup = thread.get("pickup") or {}
    if not pickup.get("background"):
        return False
    req_type = _request_type(thread)
    if not req_type:
        return False
    if req_type not in allowed_types:
        return False
    return True


def _capsule(thread: dict) -> dict:
    task = thread.get("conscious_task") or {}
    return {
        "thread_id": thread.get("id"),
        "request_type": task.get("request_type"),
        "title": task.get("title", ""),
        "why": task.get("why", ""),
        "expected_decision": task.get("expected_decision", ""),
        "origin_candidate_id": thread.get("origin_candidate_id"),
        "continuity_summary": list(thread.get("continuity_summary") or []),
        "summary_dirty": bool(thread.get("summary_dirty")),
        "dirty_since": thread.get("dirty_since"),
        "sensitivity": thread.get("sensitivity", "private"),
        "allowed_surfaces": list(thread.get("allowed_surfaces") or []),
        "pickup": deepcopy(thread.get("pickup") or {}),
    }


def claim_dormant_thread(
    store: SensoriumStore,
    *,
    actor: str = "",
    mode: str = "",
    thread_id: str = "",
    config: dict | None = None,
) -> dict:
    """Claim one eligible dormant thread, attach a lease, return a compact capsule."""
    cfg = _merged_conscious_config(config)

    if not cfg.get("enabled"):
        return {
            "success": False,
            "error": "conscious_disabled",
            "detail": (
                "Background conscious claim is deprecated and disabled. "
                "Create/dispatch conscious:review Kanban tasks instead, or pass "
                "config.enabled=True only for legacy migration/tests."
            ),
        }

    allowed_types = set(cfg.get("allowed_request_types") or [])
    if not allowed_types:
        return {
            "success": False,
            "error": "no_allowed_request_types",
            "detail": "Config allowed_request_types is empty; nothing safe to claim.",
        }

    store.ensure_dirs()
    threads = store.read_jsonl("threads")
    now_dt = datetime.now(timezone.utc)
    now_iso = _iso(now_dt)

    target: dict | None = None
    if thread_id:
        target = _find_thread_by_id(threads, thread_id)
        if target is None:
            return {
                "success": False,
                "error": "thread_not_found",
                "detail": f"Thread '{thread_id}' not found.",
            }
        if not _is_eligible(target, allowed_types, now_dt):
            return {
                "success": False,
                "error": "thread_not_eligible",
                "detail": (
                    f"Thread '{thread_id}' is not eligible for background claim "
                    f"(status={target.get('status')}, request_type={_request_type(target)}, "
                    f"pickup={target.get('pickup')})."
                ),
            }
    else:
        eligible = [t for t in threads if _is_eligible(t, allowed_types, now_dt)]
        if not eligible:
            return {
                "success": False,
                "error": "no_eligible_thread",
                "detail": "No dormant threads eligible for background conscious claim.",
            }
        eligible.sort(key=lambda t: t.get("created_at", ""))
        target = eligible[0]

    lease_seconds = max(60, int(cfg.get("lease_seconds", 600)))
    lease_id = new_id("lease")
    actor_str = actor or cfg.get("default_actor", "background_conscious")
    mode_str = mode or cfg.get("default_mode", "background")
    expires_at = _iso(now_dt + timedelta(seconds=lease_seconds))

    target["active_lease"] = {
        "lease_id": lease_id,
        "actor": actor_str,
        "mode": mode_str,
        "claimed_at": now_iso,
        "expires_at": expires_at,
    }
    target["last_interaction_at"] = now_iso
    target["updated_at"] = now_iso

    receipt = {
        "ts": now_iso,
        "type": "conscious.claimed",
        "thread_id": target.get("id"),
        "lease_id": lease_id,
        "actor": actor_str,
        "mode": mode_str,
        "expires_at": expires_at,
        "request_type": _request_type(target),
    }
    target.setdefault("decision_log", []).append(receipt)
    store.append_jsonl("decisions", receipt)
    _rewrite_jsonl(store, "threads", threads)

    return {
        "success": True,
        "data": {
            "lease_id": lease_id,
            "actor": actor_str,
            "mode": mode_str,
            "claimed_at": now_iso,
            "expires_at": expires_at,
            "thread": _capsule(target),
        },
        "receipt": receipt,
    }


def complete_claim(
    store: SensoriumStore,
    *,
    thread_id: str,
    lease_id: str,
    outcome: str = "",
    notes: str = "",
) -> dict:
    """Verify lease, clear active_lease, and record a completion receipt."""
    if not thread_id:
        return {
            "success": False,
            "error": "missing_thread_id",
            "detail": "thread_id is required to complete a claim.",
        }
    if not lease_id:
        return {
            "success": False,
            "error": "missing_lease_id",
            "detail": "lease_id is required to complete a claim.",
        }

    store.ensure_dirs()
    threads = store.read_jsonl("threads")
    target = _find_thread_by_id(threads, thread_id)
    if target is None:
        return {
            "success": False,
            "error": "thread_not_found",
            "detail": f"Thread '{thread_id}' not found.",
        }

    lease = target.get("active_lease") or {}
    if not isinstance(lease, dict) or lease.get("lease_id") != lease_id:
        return {
            "success": False,
            "error": "lease_mismatch",
            "detail": (
                f"Lease '{lease_id}' does not match active lease on thread "
                f"'{thread_id}'."
            ),
        }

    now = utc_now_iso()
    actor = lease.get("actor", "")
    mode = lease.get("mode", "")
    target["active_lease"] = None
    target["last_interaction_at"] = now
    target["updated_at"] = now

    receipt = {
        "ts": now,
        "type": "conscious.claim_completed",
        "thread_id": thread_id,
        "lease_id": lease_id,
        "actor": actor,
        "mode": mode,
        "outcome": outcome or "completed",
        "notes": notes,
    }
    target.setdefault("decision_log", []).append(receipt)
    store.append_jsonl("decisions", receipt)
    _rewrite_jsonl(store, "threads", threads)

    return {
        "success": True,
        "data": {
            "thread_id": thread_id,
            "lease_id": lease_id,
            "outcome": outcome or "completed",
        },
        "receipt": receipt,
    }
