"""Recoverable bounded Conscious attention ownership.

The aperture leases individual internal ``conscious_task`` candidates to a
bounded consumer. Lease expiry releases execution ownership only: it never
settles, archives, or otherwise invents a conscious decision. Unresolved items
remain re-presentable with exact source binding.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta

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
MAX_AUTHORITY_TOKEN_BYTES = 160
VALID_SETTLEMENT_DECISIONS = {"REVIEWED", "HELD", "SETTLED", "PREPARED_EXTERNAL_WORK"}
SETTLEMENT_STATUS = {
    "REVIEWED": "reviewed",
    "HELD": "held",
    "SETTLED": "reviewed",
    "PREPARED_EXTERNAL_WORK": "prepared_external_work",
}

RECOVERY_LANE = "recovery"
FRESH_LANE = "fresh"
_AUTHORITY_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*\Z")


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


def valid_authority_token(value: object) -> bool:
    """Return whether an exact ownership token is safe to expose unchanged."""
    return (
        isinstance(value, str)
        and bool(value)
        and len(value.encode("utf-8")) <= MAX_AUTHORITY_TOKEN_BYTES
        and _AUTHORITY_TOKEN_RE.fullmatch(value) is not None
    )


def requires_exact_settlement(candidate: object) -> bool:
    """Return whether only the current exact aperture owner may mutate a row."""
    return isinstance(candidate, dict) and candidate.get("status") == OPEN_STATUS


def _candidate_has_valid_authority(candidate: dict) -> bool:
    return valid_authority_token(candidate.get("id"))


def _source_binding(candidate: dict) -> dict:
    raw_task = candidate.get("conscious_task")
    task = raw_task if isinstance(raw_task, dict) else {}
    raw_advisory = candidate.get("advisory_meta")
    advisory = raw_advisory if isinstance(raw_advisory, dict) else {}
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
        if (
            candidate.get("kind") != CONSCIOUS_KIND
            or not isinstance(candidate.get("conscious_task"), dict)
            or candidate.get("advisory_meta") is not None
            and not isinstance(candidate.get("advisory_meta"), dict)
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
        _candidate_has_valid_authority(candidate)
        and candidate.get("status") == PENDING_STATUS
        and candidate.get("kind") == CONSCIOUS_KIND
        and isinstance(candidate.get("conscious_task"), dict)
        and (
            candidate.get("advisory_meta") is None
            or isinstance(candidate.get("advisory_meta"), dict)
        )
    )


def _is_due_held_checkpoint(candidate: dict, *, now: datetime) -> bool:
    checkpoint = candidate.get("held_return")
    if not (
        _candidate_has_valid_authority(candidate)
        and candidate.get("status") == "held"
        and candidate.get("kind") == CONSCIOUS_KIND
        and isinstance(candidate.get("conscious_task"), dict)
        and (
            candidate.get("advisory_meta") is None
            or isinstance(candidate.get("advisory_meta"), dict)
        )
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


def _recovery_sort_key(
    candidate: dict, *, stale_after_minutes: int
) -> tuple[datetime, str]:
    """Order recovery work by oldest executable checkpoint, then id."""
    if candidate.get("status") == "held":
        checkpoint = parse_utc_z_checkpoint(
            (candidate.get("held_return") or {}).get("not_before")
        )
        due_at = checkpoint[1] if checkpoint is not None else datetime.max.replace(tzinfo=UTC)
    else:
        due_at = _lease_expiry(candidate, stale_after_minutes=stale_after_minutes)
        if due_at is None:
            due_at = datetime.max.replace(tzinfo=UTC)
    return due_at, str(candidate.get("id") or "")


def _fresh_sort_key(candidate: dict) -> tuple[datetime, str]:
    """Order fresh work by creation time, then id, independent of input order."""
    created_at = _parse_iso(candidate.get("created_at"))
    if created_at is None:
        created_at = datetime.max.replace(tzinfo=UTC)
    return created_at, str(candidate.get("id") or "")


def _last_fairness_lane(decisions: list[dict]) -> str | None:
    for receipt in reversed(decisions):
        if receipt.get("type") != "conscious.aperture.opened":
            continue
        lane = receipt.get("fairness_last_served_lane")
        if lane in {RECOVERY_LANE, FRESH_LANE}:
            return str(lane)
    return None


def _select_fair_claims(
    recovery: list[dict],
    fresh: list[dict],
    *,
    limit: int,
    last_lane: str | None,
    stale_after_minutes: int,
) -> tuple[list[dict], list[str]]:
    """Alternate persisted service between recovery and fresh claim lanes."""
    recovery = sorted(
        recovery,
        key=lambda candidate: _recovery_sort_key(
            candidate, stale_after_minutes=stale_after_minutes
        ),
    )
    fresh = sorted(fresh, key=_fresh_sort_key)
    selected: list[dict] = []
    lanes: list[str] = []
    next_lane = FRESH_LANE if last_lane == RECOVERY_LANE else RECOVERY_LANE
    while len(selected) < limit and (recovery or fresh):
        if recovery and fresh:
            lane = next_lane
        elif recovery:
            lane = RECOVERY_LANE
        else:
            lane = FRESH_LANE
        queue = recovery if lane == RECOVERY_LANE else fresh
        selected.append(queue.pop(0))
        lanes.append(lane)
        next_lane = FRESH_LANE if lane == RECOVERY_LANE else RECOVERY_LANE
    return selected, lanes


def _aperture_item(candidate: dict) -> dict:
    task = candidate.get("conscious_task") or {}
    ownership = candidate.get("conscious_aperture") or {}
    return {
        "candidate_id": candidate.get("id"),
        "aperture_id": ownership.get("id", ""),
        "consumer_id": ownership.get("consumer_id", ""),
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
    owner = str(consumer_id or "conscious-session").strip()
    if not valid_authority_token(owner):
        return {"success": False, "error": "invalid_consumer_id"}

    with store.candidate_transaction():
        candidates = store.read_jsonl("candidates")
        canonical_ids = _canonical_source_ids(candidates)
        active = [
            candidate
            for candidate in candidates
            if _candidate_has_valid_authority(candidate)
            and candidate.get("status") == OPEN_STATUS
            and isinstance(candidate.get("conscious_task"), dict)
            and (
                candidate.get("advisory_meta") is None
                or isinstance(candidate.get("advisory_meta"), dict)
            )
            and not _is_stale_active(
                candidate, now=now_dt, stale_after_minutes=stale_after_minutes
            )
        ]
        stale_active = [
            candidate
            for candidate in candidates
            if _candidate_has_valid_authority(candidate)
            and candidate.get("status") == OPEN_STATUS
            and isinstance(candidate.get("conscious_task"), dict)
            and (
                candidate.get("advisory_meta") is None
                or isinstance(candidate.get("advisory_meta"), dict)
            )
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

        recovery_eligible = []
        fresh_eligible = []
        for candidate in candidates:
            recovery_candidate = (
                _is_due_held_checkpoint(candidate, now=now_dt) or candidate in stale_active
            )
            fresh_candidate = _is_pending_conscious_task(candidate)
            if not recovery_candidate and not fresh_candidate:
                continue
            candidate_id = str(candidate.get("id") or "")
            if canonical_ids.get(_logical_source_key(candidate)) != candidate_id:
                continue
            if not _visible(candidate, surface=surface, instance_config=instance_config):
                continue
            if recovery_candidate:
                recovery_eligible.append(candidate)
            elif fresh_candidate:
                fresh_eligible.append(candidate)
        eligible = recovery_eligible + fresh_eligible
        selected, service_lanes = _select_fair_claims(
            recovery_eligible,
            fresh_eligible,
            limit=min(remaining_size, available_capacity),
            last_lane=_last_fairness_lane(store.read_jsonl("decisions")),
            stale_after_minutes=stale_after_minutes,
        )

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
            "fairness_service_lanes": service_lanes,
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
                "fairness_service_lanes": service_lanes,
                "fairness_last_served_lane": service_lanes[-1] if service_lanes else None,
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
    allow_legacy_ownerless: bool = False,
) -> dict | None:
    current = candidate.get("conscious_aperture") or {}
    if candidate.get("status") != OPEN_STATUS:
        return {
            "success": False,
            "error": "candidate_not_in_conscious_aperture",
            "candidate_id": candidate.get("id"),
            "status": candidate.get("status"),
        }
    expected_aperture = str(current.get("id") or "").strip()
    expected_consumer = str(current.get("consumer_id") or "").strip()
    legacy_ownerless = (
        not expected_aperture
        and not expected_consumer
        and current.get("generation") in (None, "", 0)
    )
    if legacy_ownerless and not allow_legacy_ownerless:
        return {
            "success": False,
            "error": "legacy_ownerless_lease_requires_explicit_admin_path",
            "candidate_id": candidate.get("id"),
        }
    if not legacy_ownerless and not str(aperture_id or "").strip():
        return {
            "success": False,
            "error": "aperture_id_required",
            "candidate_id": candidate.get("id"),
        }
    if not legacy_ownerless and not str(consumer_id or "").strip():
        return {
            "success": False,
            "error": "consumer_id_required",
            "candidate_id": candidate.get("id"),
        }
    if not legacy_ownerless and (not expected_aperture or not expected_consumer):
        return {
            "success": False,
            "error": "current_lease_ownership_incomplete",
            "candidate_id": candidate.get("id"),
        }
    if not legacy_ownerless and expected_aperture != str(aperture_id):
        return {
            "success": False,
            "error": "aperture_id_mismatch",
            "candidate_id": candidate.get("id"),
            "expected_aperture_id": expected_aperture,
            "aperture_id": aperture_id,
        }
    if not legacy_ownerless and str(consumer_id) != expected_consumer:
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


def record_conscious_aperture_presentation_attempt(
    store: SensoriumStore,
    *,
    aperture: list[dict],
    consumer_id: str,
    turn_id: str,
    surface: str,
    now: str | None = None,
) -> dict:
    """Atomically validate a whole packet and record only a presentation attempt."""
    now_iso = now or utc_now_iso()
    now_dt = _parse_iso(now_iso) or datetime.now(UTC)
    owner = str(consumer_id or "").strip()
    if not owner:
        return {"success": False, "error": "consumer_id_required"}
    if not isinstance(aperture, list) or not aperture:
        return {"success": False, "error": "aperture_packet_required"}
    with store.candidate_transaction():
        candidates = store.read_jsonl("candidates")
        validated_items: list[dict] = []
        seen_ids: set[str] = set()
        for item_index, item in enumerate(aperture):
            candidate_id = str((item or {}).get("candidate_id") or "").strip()
            aperture_id = str((item or {}).get("aperture_id") or "").strip()
            if not candidate_id or candidate_id in seen_ids:
                return {
                    "success": False,
                    "error": "invalid_aperture_packet_item",
                    "item_index": item_index,
                }
            seen_ids.add(candidate_id)
            idx = _find_candidate_index(candidates, candidate_id)
            if idx is None:
                return {
                    "success": False,
                    "error": "candidate_not_found",
                    "candidate_id": candidate_id,
                    "item_index": item_index,
                }
            candidate = candidates[idx]
            error = _validate_current_ownership(
                candidate,
                aperture_id=aperture_id,
                consumer_id=owner,
                now_dt=now_dt,
            )
            if error:
                return {**error, "item_index": item_index}
            validated_items.append(
                {
                    "candidate_id": candidate_id,
                    "aperture_id": aperture_id,
                    "source_binding": _source_binding(candidate),
                }
            )
        decisions = store.read_jsonl("decisions")
        existing = next(
            (
                receipt
                for receipt in reversed(decisions)
                if receipt.get("type") == "conscious.aperture.presentation_attempted"
                and receipt.get("turn_id") == turn_id
                and receipt.get("consumer_id") == owner
                and receipt.get("items") == validated_items
            ),
            None,
        )
        if existing is not None:
            return {
                "success": True,
                "action": "presentation_already_attempted",
                "receipt": existing,
            }
        receipt = {
            "ts": now_iso,
            "type": "conscious.aperture.presentation_attempted",
            "candidate_ids": [item["candidate_id"] for item in validated_items],
            "aperture_ids": sorted({item["aperture_id"] for item in validated_items}),
            "consumer_id": owner,
            "turn_id": str(turn_id or ""),
            "surface": str(surface or "local"),
            "items": validated_items,
            "host_consumption_confirmed": False,
        }
        store.append_jsonl("decisions", receipt)
        return {
            "success": True,
            "action": "presentation_attempt_recorded",
            "receipt": receipt,
        }


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
    allow_legacy_ownerless: bool = False,
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
    normalized_external_work: dict | None = None
    if normalized_decision == "PREPARED_EXTERNAL_WORK":
        if not isinstance(external_work, dict):
            return {"success": False, "error": "external_work_spec_required"}
        title = str(external_work.get("title") or "").strip()
        summary = str(external_work.get("summary") or "").strip()
        worker_type = str(external_work.get("worker_type") or "").strip()
        profile = external_work.get("profile")
        target = external_work.get("target")
        if (
            not title
            or not summary
            or not worker_type
            or not isinstance(profile, dict)
            or not isinstance(target, dict)
        ):
            return {"success": False, "error": "invalid_external_work_spec"}
        normalized_external_work = {
            "title": truncate_text(title, 200),
            "summary": truncate_text(summary, 1200),
            "worker_type": truncate_text(worker_type, 80),
            "profile": dict(profile),
            "target": dict(target),
        }

    with store.candidate_transaction():
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
        actual_aperture_id = str(aperture_id or "").strip()
        supplied_consumer_id = str(consumer_id or "").strip()
        legacy_ownerless = (
            not str(current_aperture.get("id") or "").strip()
            and not str(current_aperture.get("consumer_id") or "").strip()
            and current_aperture.get("generation") in (None, "", 0)
        )
        if not (allow_legacy_ownerless and legacy_ownerless):
            if not actual_aperture_id:
                return {
                    "success": False,
                    "error": "aperture_id_required",
                    "candidate_id": candidate_id,
                }
            if not supplied_consumer_id:
                return {
                    "success": False,
                    "error": "consumer_id_required",
                    "candidate_id": candidate_id,
                }
            expected_aperture_id = str(current_aperture.get("id") or "").strip()
            expected_consumer_id = str(current_aperture.get("consumer_id") or "").strip()
            if actual_aperture_id != expected_aperture_id:
                return {
                    "success": False,
                    "error": "aperture_id_mismatch",
                    "candidate_id": candidate_id,
                    "expected_aperture_id": expected_aperture_id,
                    "aperture_id": actual_aperture_id,
                }
            if supplied_consumer_id != expected_consumer_id:
                return {
                    "success": False,
                    "error": "consumer_id_mismatch",
                    "candidate_id": candidate_id,
                    "expected_consumer_id": expected_consumer_id,
                }
        existing = _existing_settlement(
            store.read_jsonl("decisions"),
            candidate_id=candidate_id,
            aperture_id=actual_aperture_id,
            decision=normalized_decision,
        )
        if existing is not None:
            settled_consumer = str(existing.get("consumer_id") or "")
            if supplied_consumer_id != settled_consumer:
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
            consumer_id=supplied_consumer_id,
            now_dt=now_dt,
            allow_legacy_ownerless=allow_legacy_ownerless,
        )
        if ownership_error:
            return ownership_error

        receipt = {
            "ts": now_iso,
            "type": "conscious.aperture.settled",
            "candidate_id": candidate_id,
            "aperture_id": actual_aperture_id,
            "consumer_id": supplied_consumer_id,
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
        if normalized_external_work is not None:
            receipt["external_work"] = normalized_external_work
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
