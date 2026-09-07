"""Recoverable bounded Conscious attention ownership.

The aperture leases individual internal ``conscious_task`` candidates to a
bounded consumer. Lease expiry releases execution ownership only: it never
settles, archives, or otherwise invents a conscious decision. Unresolved items
remain re-presentable with exact source binding.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

try:  # pragma: no cover - native Linux is the supported runtime.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

from .config import visible_on_surface
from .schemas import new_id, parse_utc_z_checkpoint, truncate_text, utc_now_iso
from .store import SensoriumStore

OPEN_STATUS = "in_conscious_aperture"
PENDING_STATUS = "candidate"
CONSCIOUS_KIND = "subconscious_advisory"
DEFAULT_APERTURE_SIZE = 3
DEFAULT_STALE_AFTER_MINUTES = 180
DEFAULT_LEASE_MINUTES = 15
DEFAULT_MAX_ACTIVE_ITEMS = 3
VALID_SETTLEMENT_DECISIONS = {"REVIEWED", "HELD", "SETTLED", "PREPARED_EXTERNAL_WORK"}
SETTLEMENT_STATUS = {
    "REVIEWED": "reviewed",
    "HELD": "held",
    "SETTLED": "reviewed",
    "PREPARED_EXTERNAL_WORK": "prepared_external_work",
}

_FALLBACK_LOCK = threading.RLock()


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
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _format_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@contextlib.contextmanager
def _aperture_lock(store: SensoriumStore) -> Iterator[None]:
    """Serialize candidate claim/settlement across native Linux consumers."""
    store.ensure_dirs()
    path = store.root / "locks" / "conscious-aperture.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with _FALLBACK_LOCK, open(path, "a+", encoding="utf-8") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _source_binding(candidate: dict) -> dict:
    task = candidate.get("conscious_task") or {}
    advisory = candidate.get("advisory_meta") or {}
    source = {
        "candidate_id": str(candidate.get("id") or ""),
        "conscious_task_id": str(task.get("id") or ""),
        "candidate_fingerprint": str(candidate.get("fingerprint") or ""),
        "source_fingerprint": str(advisory.get("source_fingerprint") or ""),
        "source_revision": str(
            candidate.get("source_revision") or advisory.get("source_revision") or ""
        ),
        "event_ids": sorted(str(value) for value in (candidate.get("event_ids") or [])),
        "source_candidate_ids": sorted(
            str(value) for value in (candidate.get("source_candidate_ids") or [])
        ),
    }
    exact_material = {
        **source,
        "kind": candidate.get("kind"),
        "summary": candidate.get("summary"),
        "conscious_task": task,
        "advisory_meta": advisory,
    }
    source["source_digest"] = hashlib.sha256(
        json.dumps(exact_material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return source


def _logical_source_key(candidate: dict) -> str:
    binding = _source_binding(candidate)
    if binding["candidate_fingerprint"]:
        return "fingerprint:" + binding["candidate_fingerprint"]
    material = {
        "conscious_task_id": binding["conscious_task_id"],
        "source_fingerprint": binding["source_fingerprint"],
        "event_ids": binding["event_ids"],
        "source_candidate_ids": binding["source_candidate_ids"],
    }
    if any(material.values()):
        return "source:" + json.dumps(material, sort_keys=True, separators=(",", ":"))
    return "candidate:" + binding["candidate_id"]


def _canonical_source_ids(candidates: list[dict]) -> dict[str, str]:
    canonical: dict[str, tuple[tuple[str, str], str]] = {}
    for candidate in candidates:
        if candidate.get("kind") != CONSCIOUS_KIND or not isinstance(
            candidate.get("conscious_task"), dict
        ):
            continue
        key = _logical_source_key(candidate)
        candidate_id = str(candidate.get("id") or "")
        rank = (str(candidate.get("created_at") or ""), candidate_id)
        if key not in canonical or rank < canonical[key][0]:
            canonical[key] = (rank, candidate_id)
    return {key: value[1] for key, value in canonical.items()}


def _lease_expiry(candidate: dict, *, stale_after_minutes: int) -> datetime | None:
    aperture = candidate.get("conscious_aperture") or {}
    explicit = _parse_iso(aperture.get("lease_expires_at"))
    if explicit is not None:
        return explicit
    opened = _parse_iso(aperture.get("opened_at") or candidate.get("updated_at"))
    if opened is None:
        return None
    return opened + timedelta(minutes=max(1, int(stale_after_minutes or 1)))


def _is_stale_active(candidate: dict, *, now: datetime, stale_after_minutes: int) -> bool:
    expiry = _lease_expiry(candidate, stale_after_minutes=stale_after_minutes)
    return expiry is not None and now >= expiry


def _is_pending_conscious_task(candidate: dict) -> bool:
    return (
        candidate.get("status") == PENDING_STATUS
        and candidate.get("kind") == CONSCIOUS_KIND
        and isinstance(candidate.get("conscious_task"), dict)
    )


def _is_due_held_checkpoint(candidate: dict, *, now: datetime) -> bool:
    checkpoint = candidate.get("held_return")
    if not (
        candidate.get("status") == "held"
        and candidate.get("kind") == CONSCIOUS_KIND
        and isinstance(candidate.get("conscious_task"), dict)
        and isinstance(checkpoint, dict)
        and checkpoint.get("reason_code") == "time_checkpoint"
    ):
        return False
    parsed = parse_utc_z_checkpoint(checkpoint.get("not_before"))
    return parsed is not None and parsed[1] <= now


def _task_type_priority(candidate: dict) -> int:
    task = candidate.get("conscious_task") or {}
    request_type = str(task.get("request_type") or "").upper()
    return {
        "UPDATE_MEMORY_OR_SKILL": 0,
        "SAVE": 1,
        "CREATE_FOLLOWUP": 2,
        "DELEGATE_WORK": 3,
        "PRIVATE_EXPRESSION": 4,
        "THINK": 5,
    }.get(request_type, 6)


def _candidate_sort_key(candidate: dict) -> tuple:
    try:
        pressure = float(candidate.get("pressure") or 0.0)
    except (TypeError, ValueError):
        pressure = 0.0
    return (
        -pressure,
        _task_type_priority(candidate),
        str(candidate.get("created_at") or ""),
        str(candidate.get("id") or ""),
    )


def _aperture_item(candidate: dict) -> dict:
    task = candidate.get("conscious_task") or {}
    ownership = candidate.get("conscious_aperture") or {}
    return {
        "candidate_id": candidate.get("id"),
        "aperture_id": ownership.get("id", ""),
        "lease_expires_at": ownership.get("lease_expires_at", ""),
        "summary": truncate_text(candidate.get("summary", ""), 220),
        "pressure": candidate.get("pressure"),
        "created_at": candidate.get("created_at", ""),
        "event_ids": list(candidate.get("event_ids") or []),
        "source_candidate_ids": list(candidate.get("source_candidate_ids") or []),
        "correlation_keys": list(candidate.get("correlation_keys") or []),
        "sensitivity": candidate.get("sensitivity", "private"),
        "allowed_surfaces": list(candidate.get("allowed_surfaces") or ["local"]),
        "source_binding": _source_binding(candidate),
        "conscious_task": {
            "id": task.get("id", ""),
            "request_type": task.get("request_type", ""),
            "title": task.get("title", ""),
            "why": task.get("why", ""),
            "expected_decision": task.get("expected_decision", ""),
        },
        "advisory_meta": dict(candidate.get("advisory_meta") or {}),
    }


def _visible(candidate: dict, *, surface: str | None, instance_config: dict | None) -> bool:
    if surface is None:
        return True
    if not isinstance(instance_config, dict):
        return False
    return visible_on_surface(candidate, surface, instance_config)


def open_conscious_aperture(
    store: SensoriumStore,
    *,
    aperture_size: int = DEFAULT_APERTURE_SIZE,
    max_active_sessions: int = 1,
    max_active_items: int | None = None,
    stale_after_minutes: int = DEFAULT_STALE_AFTER_MINUTES,
    lease_minutes: int = DEFAULT_LEASE_MINUTES,
    consumer_id: str | None = None,
    surface: str | None = None,
    instance_config: dict | None = None,
    dry_run: bool = True,
    now: str | None = None,
) -> dict:
    """Claim a bounded packet without globally blocking on stale ownership.

    ``max_active_sessions`` remains as a compatibility alias. New callers should
    use ``max_active_items`` because the limit is global per-item ownership, not
    a session count.
    """
    store.ensure_dirs()
    now_iso = now or utc_now_iso()
    now_dt = _parse_iso(now_iso) or datetime.now(UTC)
    size = max(1, min(20, int(aperture_size or DEFAULT_APERTURE_SIZE)))
    legacy_limit_mode = max_active_items is None
    legacy_active_limit = max(size, int(max_active_sessions or 1))
    active_limit = max(
        1,
        min(
            100,
            int(max_active_items if max_active_items is not None else legacy_active_limit),
        ),
    )
    lease_duration = max(1, min(1440, int(lease_minutes or DEFAULT_LEASE_MINUTES)))
    explicit_consumer = bool(str(consumer_id or "").strip())
    owner = truncate_text(str(consumer_id or "conscious-session").strip(), 160)

    with _aperture_lock(store):
        candidates = store.read_jsonl("candidates")
        canonical_ids = _canonical_source_ids(candidates)
        active = [
            candidate
            for candidate in candidates
            if candidate.get("status") == OPEN_STATUS
            and isinstance(candidate.get("conscious_task"), dict)
            and not _is_stale_active(
                candidate, now=now_dt, stale_after_minutes=stale_after_minutes
            )
        ]
        stale_active = [
            candidate
            for candidate in candidates
            if candidate.get("status") == OPEN_STATUS
            and isinstance(candidate.get("conscious_task"), dict)
            and _is_stale_active(
                candidate, now=now_dt, stale_after_minutes=stale_after_minutes
            )
        ]
        resumable = (
            [
                candidate
                for candidate in active
                if str((candidate.get("conscious_aperture") or {}).get("consumer_id") or "")
                == owner
                and _visible(
                    candidate, surface=surface, instance_config=instance_config
                )
            ]
            if explicit_consumer
            else []
        )
        resumable = sorted(resumable, key=_candidate_sort_key)[:size]
        if legacy_limit_mode and active and not resumable:
            return {
                "success": True,
                "action": "active_aperture_exists",
                "dry_run": dry_run,
                "active_count": len(active),
                "active_candidate_ids": [candidate.get("id") for candidate in active],
                "stale_active_candidate_ids": [
                    candidate.get("id") for candidate in stale_active
                ],
                "candidate_ids": [],
                "aperture": [],
            }
        remaining_size = max(0, size - len(resumable))
        available_capacity = max(0, active_limit - len(active))

        eligible = []
        for candidate in candidates:
            candidate_id = str(candidate.get("id") or "")
            if canonical_ids.get(_logical_source_key(candidate)) != candidate_id:
                continue
            if not _visible(candidate, surface=surface, instance_config=instance_config):
                continue
            if (
                _is_pending_conscious_task(candidate)
                or _is_due_held_checkpoint(candidate, now=now_dt)
                or candidate in stale_active
            ):
                eligible.append(candidate)
        eligible.sort(key=_candidate_sort_key)
        selected = eligible[: min(remaining_size, available_capacity)]

        if not selected:
            if resumable:
                return {
                    "success": True,
                    "action": "resumed_aperture",
                    "dry_run": dry_run,
                    "active_count": len(active),
                    "selected_count": len(resumable),
                    "pending_count": len(eligible),
                    "candidate_ids": [candidate.get("id") for candidate in resumable],
                    "reclaimed_candidate_ids": [],
                    "returned_candidate_ids": [],
                    "aperture": [_aperture_item(candidate) for candidate in resumable],
                }
            if len(active) >= active_limit:
                return {
                    "success": True,
                    "action": "active_aperture_exists",
                    "dry_run": dry_run,
                    "active_count": len(active),
                    "active_candidate_ids": [candidate.get("id") for candidate in active],
                    "stale_active_candidate_ids": [
                        candidate.get("id") for candidate in stale_active
                    ],
                    "candidate_ids": [],
                    "aperture": [],
                }

        aperture_id = new_id("cap")
        lease_expires_at = _format_iso(now_dt + timedelta(minutes=lease_duration))
        reclaimed_ids = sorted(
            str(candidate.get("id") or "") for candidate in selected if candidate in stale_active
        )
        returned_ids = sorted(
            str(candidate.get("id") or "")
            for candidate in selected
            if candidate.get("status") == "held"
        )
        preview_rows: dict[str, dict] = {}
        for candidate in selected:
            updated = dict(candidate)
            previous = candidate.get("conscious_aperture") or {}
            try:
                generation = int(previous.get("generation") or 0) + 1
            except (TypeError, ValueError):
                generation = 1
            updated["status"] = OPEN_STATUS
            updated["updated_at"] = now_iso
            updated["conscious_aperture"] = {
                "id": aperture_id,
                "opened_at": now_iso,
                "state": "open",
                "consumer_id": owner,
                "lease_expires_at": lease_expires_at,
                "generation": generation,
                "source_binding": _source_binding(candidate),
            }
            if str(candidate.get("id") or "") in returned_ids:
                updated.pop("held_return", None)
            preview_rows[str(candidate.get("id") or "")] = updated

        packet_items = [_aperture_item(candidate) for candidate in resumable]
        packet_items.extend(
            _aperture_item(preview_rows[str(candidate.get("id") or "")])
            for candidate in selected
        )
        packet = {
            "success": True,
            "action": "would_open_aperture" if dry_run else "opened_aperture",
            "dry_run": dry_run,
            "aperture_id": aperture_id,
            "opened_at": now_iso,
            "lease_expires_at": lease_expires_at,
            "aperture_size": size,
            "max_active_items": active_limit,
            "selected_count": len(packet_items),
            "pending_count": len(eligible),
            "active_count": len(active),
            "stale_active_candidate_ids": sorted(
                str(candidate.get("id") or "") for candidate in stale_active
            ),
            "reclaimed_candidate_ids": reclaimed_ids,
            "returned_candidate_ids": returned_ids,
            "candidate_ids": [item["candidate_id"] for item in packet_items],
            "aperture": packet_items,
            "instructions": {
                "settle_each_item": (
                    "Settle each item explicitly; unresolved items remain re-presentable."
                ),
                "hold": "HELD requires a future UTC-Z return_at checkpoint.",
                "worker_requests": (
                    "External work remains a prepared specification until separately authorized."
                ),
            },
        }
        if dry_run or not selected:
            return packet

        rewritten = [
            preview_rows.get(str(candidate.get("id") or ""), candidate)
            for candidate in candidates
        ]
        store.rewrite_jsonl("candidates", rewritten)
        for candidate in selected:
            candidate_id = str(candidate.get("id") or "")
            if candidate_id in reclaimed_ids:
                previous = candidate.get("conscious_aperture") or {}
                store.append_jsonl(
                    "decisions",
                    {
                        "ts": now_iso,
                        "type": "conscious.aperture.reclaimed",
                        "candidate_id": candidate_id,
                        "previous_aperture_id": previous.get("id", ""),
                        "aperture_id": aperture_id,
                        "consumer_id": owner,
                        "reason_code": "execution_lease_expired",
                        "decision_preserved": True,
                    },
                )
        store.append_jsonl(
            "decisions",
            {
                "ts": now_iso,
                "type": "conscious.aperture.opened",
                "aperture_id": aperture_id,
                "candidate_ids": [
                    candidate.get("id") for candidate in selected
                ],
                "selected_count": len(selected),
                "pending_count": len(eligible),
                "max_active_items": active_limit,
                "aperture_size": size,
                "consumer_id": owner,
                "lease_expires_at": lease_expires_at,
            },
        )
        for candidate_id in returned_ids:
            store.append_jsonl(
                "decisions",
                {
                    "ts": now_iso,
                    "type": "conscious.aperture.returned",
                    "candidate_id": candidate_id,
                    "aperture_id": aperture_id,
                    "return_reason_code": "time_checkpoint",
                },
            )
        return packet


def _find_candidate_index(candidates: list[dict], candidate_id: str) -> int | None:
    for idx, candidate in enumerate(candidates):
        if candidate.get("id") == candidate_id:
            return idx
    return None


def _existing_settlement(
    decisions: list[dict], *, candidate_id: str, aperture_id: str, decision: str
) -> dict | None:
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


def _validate_current_ownership(
    candidate: dict,
    *,
    aperture_id: str,
    consumer_id: str | None,
    now_dt: datetime,
) -> dict | None:
    current = candidate.get("conscious_aperture") or {}
    if candidate.get("status") != OPEN_STATUS:
        return {
            "success": False,
            "error": "candidate_not_in_conscious_aperture",
            "candidate_id": candidate.get("id"),
            "status": candidate.get("status"),
        }
    if aperture_id and current.get("id") != aperture_id:
        return {
            "success": False,
            "error": "aperture_id_mismatch",
            "candidate_id": candidate.get("id"),
            "expected_aperture_id": current.get("id"),
            "aperture_id": aperture_id,
        }
    expected_consumer = str(current.get("consumer_id") or "")
    if consumer_id and expected_consumer and str(consumer_id) != expected_consumer:
        return {
            "success": False,
            "error": "consumer_id_mismatch",
            "candidate_id": candidate.get("id"),
            "expected_consumer_id": expected_consumer,
        }
    lease_expiry = _parse_iso(current.get("lease_expires_at"))
    if lease_expiry is not None and now_dt >= lease_expiry:
        return {
            "success": False,
            "error": "aperture_lease_expired",
            "candidate_id": candidate.get("id"),
            "aperture_id": current.get("id"),
            "lease_expires_at": current.get("lease_expires_at"),
        }
    binding = current.get("source_binding")
    if isinstance(binding, dict) and binding != _source_binding(candidate):
        return {
            "success": False,
            "error": "source_binding_mismatch",
            "candidate_id": candidate.get("id"),
            "aperture_id": current.get("id"),
        }
    return None


def mark_conscious_aperture_consumed(
    store: SensoriumStore,
    *,
    candidate_id: str,
    aperture_id: str,
    consumer_id: str,
    turn_id: str,
    surface: str,
    now: str | None = None,
) -> dict:
    """Record exact foreground presentation without settling the item."""
    now_iso = now or utc_now_iso()
    now_dt = _parse_iso(now_iso) or datetime.now(UTC)
    with _aperture_lock(store):
        candidates = store.read_jsonl("candidates")
        idx = _find_candidate_index(candidates, candidate_id)
        if idx is None:
            return {"success": False, "error": "candidate_not_found"}
        candidate = candidates[idx]
        error = _validate_current_ownership(
            candidate,
            aperture_id=aperture_id,
            consumer_id=consumer_id,
            now_dt=now_dt,
        )
        if error:
            return error
        decisions = store.read_jsonl("decisions")
        existing = next(
            (
                receipt
                for receipt in reversed(decisions)
                if receipt.get("type") == "conscious.aperture.consumed"
                and receipt.get("candidate_id") == candidate_id
                and receipt.get("aperture_id") == aperture_id
                and receipt.get("turn_id") == turn_id
            ),
            None,
        )
        if existing is not None:
            return {"success": True, "action": "already_consumed", "receipt": existing}
        receipt = {
            "ts": now_iso,
            "type": "conscious.aperture.consumed",
            "candidate_id": candidate_id,
            "aperture_id": aperture_id,
            "consumer_id": consumer_id,
            "turn_id": str(turn_id or ""),
            "surface": str(surface or "local"),
            "source_binding": _source_binding(candidate),
        }
        store.append_jsonl("decisions", receipt)
        return {"success": True, "action": "consumed_aperture_item", "receipt": receipt}


def settle_conscious_aperture_item(
    store: SensoriumStore,
    *,
    candidate_id: str,
    decision: str,
    reason: str,
    aperture_id: str | None = None,
    consumer_id: str | None = None,
    return_at: str | None = None,
    external_work: dict | None = None,
    dry_run: bool = True,
    now: str | None = None,
) -> dict:
    """Settle one exact owned item; never dispatch external work."""
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
    if normalized_decision == "HELD" and return_at is None:
        return {"success": False, "error": "return_at_required_for_hold"}
    now_iso = now or utc_now_iso()
    now_dt = _parse_iso(now_iso) or datetime.now(UTC)
    checkpoint = parse_utc_z_checkpoint(return_at) if return_at is not None else None
    if return_at is not None and (
        normalized_decision != "HELD" or checkpoint is None or checkpoint[1] <= now_dt
    ):
        return {"success": False, "error": "invalid_return_at"}

    with _aperture_lock(store):
        candidates = store.read_jsonl("candidates")
        idx = _find_candidate_index(candidates, candidate_id)
        if idx is None:
            return {
                "success": False,
                "error": "candidate_not_found",
                "candidate_id": candidate_id,
            }
        candidate = candidates[idx]
        current_aperture = candidate.get("conscious_aperture") or {}
        actual_aperture_id = str(
            aperture_id or current_aperture.get("id") or ""
        ).strip()
        existing = _existing_settlement(
            store.read_jsonl("decisions"),
            candidate_id=candidate_id,
            aperture_id=actual_aperture_id,
            decision=normalized_decision,
        )
        if existing is not None:
            settled_consumer = str(existing.get("consumer_id") or "")
            if consumer_id and settled_consumer and str(consumer_id) != settled_consumer:
                return {
                    "success": False,
                    "error": "consumer_id_mismatch",
                    "candidate_id": candidate_id,
                    "expected_consumer_id": settled_consumer,
                }
            return {
                "success": True,
                "action": "already_settled",
                "dry_run": dry_run,
                "candidate_id": candidate_id,
                "aperture_id": actual_aperture_id,
                "receipt": existing,
            }
        ownership_error = _validate_current_ownership(
            candidate,
            aperture_id=actual_aperture_id,
            consumer_id=consumer_id,
            now_dt=now_dt,
        )
        if ownership_error:
            return ownership_error

        receipt = {
            "ts": now_iso,
            "type": "conscious.aperture.settled",
            "candidate_id": candidate_id,
            "aperture_id": actual_aperture_id,
            "consumer_id": str(current_aperture.get("consumer_id") or consumer_id or ""),
            "decision": normalized_decision,
            "new_status": SETTLEMENT_STATUS[normalized_decision],
            "reason": truncate_text(reason, 500),
            "conscious_task_id": (candidate.get("conscious_task") or {}).get("id", ""),
            "request_type": (candidate.get("conscious_task") or {}).get("request_type", ""),
            "source_binding": _source_binding(candidate),
        }
        if checkpoint is not None:
            receipt.update(
                {
                    "return_state": "held_checkpoint_set",
                    "return_reason_code": "time_checkpoint",
                }
            )
        if external_work:
            receipt["external_work"] = {
                "title": truncate_text(external_work.get("title", ""), 200),
                "summary": truncate_text(external_work.get("summary", ""), 1200),
                "worker_type": truncate_text(
                    external_work.get("worker_type", "kanban_task"), 80
                ),
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
        updated_aperture.update(
            {
                "state": "settled",
                "settled_at": now_iso,
                "decision": normalized_decision,
                "reason": truncate_text(reason, 240),
            }
        )
        updated["conscious_aperture"] = updated_aperture
        if checkpoint is not None:
            updated["held_return"] = {
                "not_before": checkpoint[0],
                "reason_code": "time_checkpoint",
            }
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
