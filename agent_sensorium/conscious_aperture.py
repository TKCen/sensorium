"""Bounded Conscious aperture over internal conscious_task candidates.

The aperture is a small deterministic state transition: it selects pending
Subconscious advisory candidates that carry ``conscious_task`` payloads and marks
at most one bounded set as currently open for Conscious review. It does not
prepare worker requests, dispatch agents, send messages, or perform the final
Conscious decision itself.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .schemas import new_id, truncate_text, utc_now_iso
from .store import SensoriumStore

OPEN_STATUS = "in_conscious_aperture"
PENDING_STATUS = "candidate"
CONSCIOUS_KIND = "subconscious_advisory"
DEFAULT_APERTURE_SIZE = 5
DEFAULT_STALE_AFTER_MINUTES = 180
VALID_SETTLEMENT_DECISIONS = {"REVIEWED", "HELD", "SETTLED", "PREPARED_EXTERNAL_WORK"}
SETTLEMENT_STATUS = {
    "REVIEWED": "reviewed",
    "HELD": "held",
    "SETTLED": "reviewed",
    "PREPARED_EXTERNAL_WORK": "prepared_external_work",
}


def _parse_iso(ts: str | None) -> datetime | None:
    if not isinstance(ts, str) or not ts.strip():
        return None
    value = ts.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _is_stale_active(candidate: dict, *, now: datetime, stale_after_minutes: int) -> bool:
    aperture = candidate.get("conscious_aperture") or {}
    opened = _parse_iso(aperture.get("opened_at") or candidate.get("updated_at"))
    if opened is None:
        return False
    age_minutes = (now - opened).total_seconds() / 60.0
    return age_minutes >= max(1, stale_after_minutes)


def _is_pending_conscious_task(candidate: dict) -> bool:
    return (
        candidate.get("status") == PENDING_STATUS
        and candidate.get("kind") == CONSCIOUS_KIND
        and isinstance(candidate.get("conscious_task"), dict)
    )


def _task_type_priority(candidate: dict) -> int:
    task = candidate.get("conscious_task") or {}
    request_type = str(task.get("request_type") or "").upper()
    # Lower number = earlier in the aperture.
    return {
        "UPDATE_MEMORY_OR_SKILL": 0,
        "SAVE": 1,
        "CREATE_FOLLOWUP": 2,
        "DELEGATE_WORK": 3,
        "PRIVATE_EXPRESSION": 4,
        "THINK": 5,
    }.get(request_type, 6)


def _candidate_sort_key(candidate: dict) -> tuple:
    # Prefer high pressure and action-oriented tasks, then older candidates.
    try:
        pressure = float(candidate.get("pressure") or 0.0)
    except (TypeError, ValueError):
        pressure = 0.0
    return (-pressure, _task_type_priority(candidate), str(candidate.get("created_at") or ""), str(candidate.get("id") or ""))


def _aperture_item(candidate: dict) -> dict:
    task = candidate.get("conscious_task") or {}
    return {
        "candidate_id": candidate.get("id"),
        "summary": truncate_text(candidate.get("summary", ""), 220),
        "pressure": candidate.get("pressure"),
        "created_at": candidate.get("created_at", ""),
        "event_ids": list(candidate.get("event_ids") or []),
        "source_candidate_ids": list(candidate.get("source_candidate_ids") or []),
        "correlation_keys": list(candidate.get("correlation_keys") or []),
        "sensitivity": candidate.get("sensitivity", "private"),
        "allowed_surfaces": list(candidate.get("allowed_surfaces") or ["local"]),
        "conscious_task": {
            "id": task.get("id", ""),
            "request_type": task.get("request_type", ""),
            "title": task.get("title", ""),
            "why": task.get("why", ""),
            "expected_decision": task.get("expected_decision", ""),
        },
        "advisory_meta": dict(candidate.get("advisory_meta") or {}),
    }


def open_conscious_aperture(
    store: SensoriumStore,
    *,
    aperture_size: int = DEFAULT_APERTURE_SIZE,
    max_active_sessions: int = 1,
    stale_after_minutes: int = DEFAULT_STALE_AFTER_MINUTES,
    dry_run: bool = True,
    now: str | None = None,
) -> dict:
    """Open one bounded Conscious aperture over pending internal tasks.

    Returns a compact packet for a Conscious session. With ``dry_run=False`` the
    selected candidates are marked ``in_conscious_aperture`` and a decision
    receipt is appended. No worker request is prepared or dispatched.
    """
    store.ensure_dirs()
    now_iso = now or utc_now_iso()
    now_dt = _parse_iso(now_iso) or datetime.now(timezone.utc)
    size = max(1, int(aperture_size or DEFAULT_APERTURE_SIZE))
    active_limit = max(1, int(max_active_sessions or 1))

    candidates = store.read_jsonl("candidates")
    active = [
        c for c in candidates
        if c.get("status") == OPEN_STATUS
        and isinstance(c.get("conscious_task"), dict)
        and not _is_stale_active(c, now=now_dt, stale_after_minutes=stale_after_minutes)
    ]
    stale_active = [
        c for c in candidates
        if c.get("status") == OPEN_STATUS
        and isinstance(c.get("conscious_task"), dict)
        and _is_stale_active(c, now=now_dt, stale_after_minutes=stale_after_minutes)
    ]

    # A stale open aperture is still open. Reconciliation can report it, but
    # only this aperture owner may settle/reclaim it; never open a replacement.
    # This guard intentionally precedes selection in dry-run and write modes.
    if stale_active:
        return {
            "success": False,
            "action": "stale_aperture_requires_settlement",
            "dry_run": dry_run,
            "active_count": len(active),
            "stale_active_candidate_ids": sorted(str(c.get("id") or "") for c in stale_active),
            "candidate_ids": [],
            "aperture": [],
        }

    if len(active) >= active_limit:
        return {
            "success": True,
            "action": "active_aperture_exists",
            "dry_run": dry_run,
            "active_count": len(active),
            "active_candidate_ids": [c.get("id") for c in active],
            "stale_active_candidate_ids": [c.get("id") for c in stale_active],
            "aperture": [_aperture_item(c) for c in sorted(active, key=_candidate_sort_key)[:size]],
        }

    pending = sorted([c for c in candidates if _is_pending_conscious_task(c)], key=_candidate_sort_key)
    selected = pending[:size]
    aperture_id = new_id("cap")
    packet = {
        "success": True,
        "action": "would_open_aperture" if dry_run else "opened_aperture",
        "dry_run": dry_run,
        "aperture_id": aperture_id,
        "opened_at": now_iso,
        "aperture_size": size,
        "selected_count": len(selected),
        "pending_count": len(pending),
        "active_count": len(active),
        "stale_active_candidate_ids": [c.get("id") for c in stale_active],
        "candidate_ids": [c.get("id") for c in selected],
        "aperture": [_aperture_item(c) for c in selected],
        "instructions": {
            "settle_each_item": "Record a conscious.aperture.settled receipt for each item after Conscious decides.",
            "worker_requests": "Prepare worker_requests only for decisions that require external/durable execution.",
        },
    }
    if dry_run or not selected:
        return packet

    selected_ids = set(packet["candidate_ids"])
    rewritten: list[dict] = []
    for candidate in candidates:
        if candidate.get("id") in selected_ids:
            updated = dict(candidate)
            updated["status"] = OPEN_STATUS
            updated["updated_at"] = now_iso
            updated["conscious_aperture"] = {
                "id": aperture_id,
                "opened_at": now_iso,
                "state": "open",
            }
            rewritten.append(updated)
        else:
            rewritten.append(candidate)
    store.rewrite_jsonl("candidates", rewritten)
    store.append_jsonl("decisions", {
        "ts": now_iso,
        "type": "conscious.aperture.opened",
        "aperture_id": aperture_id,
        "candidate_ids": packet["candidate_ids"],
        "selected_count": len(selected),
        "pending_count": len(pending),
        "max_active_sessions": active_limit,
        "aperture_size": size,
    })
    return packet


def _find_candidate_index(candidates: list[dict], candidate_id: str) -> int | None:
    for idx, candidate in enumerate(candidates):
        if candidate.get("id") == candidate_id:
            return idx
    return None


def _existing_settlement(decisions: list[dict], *, candidate_id: str, aperture_id: str, decision: str) -> dict | None:
    for receipt in reversed(decisions):
        if receipt.get("type") != "conscious.aperture.settled":
            continue
        if receipt.get("candidate_id") != candidate_id:
            continue
        if aperture_id and receipt.get("aperture_id") != aperture_id:
            continue
        if receipt.get("decision") == decision:
            return receipt
    return None


def settle_conscious_aperture_item(
    store: SensoriumStore,
    *,
    candidate_id: str,
    decision: str,
    reason: str,
    aperture_id: str | None = None,
    external_work: dict | None = None,
    dry_run: bool = True,
    now: str | None = None,
) -> dict:
    """Settle one candidate currently opened in the Conscious aperture.

    Settlement is a state/receipt transition only. ``external_work`` is recorded
    as a prepared specification for later routing; this function does not append
    to ``worker_requests`` and never dispatches.
    """
    store.ensure_dirs()
    candidate_id = str(candidate_id or "").strip()
    normalized_decision = str(decision or "").strip().upper()
    if not candidate_id:
        return {"success": False, "error": "candidate_id_required"}
    if normalized_decision not in VALID_SETTLEMENT_DECISIONS:
        return {
            "success": False,
            "error": "invalid_decision",
            "valid_decisions": sorted(VALID_SETTLEMENT_DECISIONS),
        }
    if not str(reason or "").strip():
        return {"success": False, "error": "reason_required"}

    candidates = store.read_jsonl("candidates")
    idx = _find_candidate_index(candidates, candidate_id)
    if idx is None:
        return {"success": False, "error": "candidate_not_found", "candidate_id": candidate_id}
    candidate = candidates[idx]
    current_aperture = candidate.get("conscious_aperture") or {}
    actual_aperture_id = str(aperture_id or current_aperture.get("id") or "").strip()

    existing = _existing_settlement(
        store.read_jsonl("decisions"),
        candidate_id=candidate_id,
        aperture_id=actual_aperture_id,
        decision=normalized_decision,
    )
    if existing is not None:
        return {
            "success": True,
            "action": "already_settled",
            "dry_run": dry_run,
            "candidate_id": candidate_id,
            "aperture_id": actual_aperture_id,
            "receipt": existing,
        }

    if candidate.get("status") != OPEN_STATUS:
        return {
            "success": False,
            "error": "candidate_not_in_conscious_aperture",
            "candidate_id": candidate_id,
            "status": candidate.get("status"),
        }
    if aperture_id and current_aperture.get("id") != aperture_id:
        return {
            "success": False,
            "error": "aperture_id_mismatch",
            "candidate_id": candidate_id,
            "expected_aperture_id": current_aperture.get("id"),
            "aperture_id": aperture_id,
        }

    now_iso = now or utc_now_iso()
    receipt = {
        "ts": now_iso,
        "type": "conscious.aperture.settled",
        "candidate_id": candidate_id,
        "aperture_id": actual_aperture_id,
        "decision": normalized_decision,
        "new_status": SETTLEMENT_STATUS[normalized_decision],
        "reason": truncate_text(reason, 500),
        "conscious_task_id": (candidate.get("conscious_task") or {}).get("id", ""),
        "request_type": (candidate.get("conscious_task") or {}).get("request_type", ""),
    }
    if external_work:
        receipt["external_work"] = {
            "title": truncate_text(external_work.get("title", ""), 200),
            "summary": truncate_text(external_work.get("summary", ""), 1200),
            "worker_type": truncate_text(external_work.get("worker_type", "kanban_task"), 80),
            "profile": dict(external_work.get("profile") or {}),
            "target": dict(external_work.get("target") or {}),
        }

    if dry_run:
        return {
            "success": True,
            "action": "would_settle_aperture_item",
            "dry_run": True,
            "candidate_id": candidate_id,
            "aperture_id": actual_aperture_id,
            "receipt_preview": receipt,
        }

    updated = dict(candidate)
    updated["status"] = SETTLEMENT_STATUS[normalized_decision]
    updated["updated_at"] = now_iso
    updated_aperture = dict(current_aperture)
    updated_aperture.update({
        "state": "settled",
        "settled_at": now_iso,
        "decision": normalized_decision,
        "reason": truncate_text(reason, 240),
    })
    updated["conscious_aperture"] = updated_aperture
    updated.setdefault("conscious_settlements", []).append(receipt)
    candidates[idx] = updated
    store.rewrite_jsonl("candidates", candidates)
    store.append_jsonl("decisions", receipt)
    return {
        "success": True,
        "action": "settled_aperture_item",
        "dry_run": False,
        "candidate_id": candidate_id,
        "aperture_id": actual_aperture_id,
        "new_status": updated["status"],
        "receipt": receipt,
    }
