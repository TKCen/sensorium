"""Legacy dispatcher advisory for Agent Sensorium.

Kanban is now the only activation and ticketing substrate. This module still
selects eligible candidates and can build a compatibility thread preview, but
`dispatch_once` no longer creates dormant Sensorium threads unless callers pass
an explicit legacy opt-in (`legacy_thread_dispatch_enabled=True`). Normal use is
read-only advisory/status so a clean Kanban board cannot split from a hidden
Sensorium `would_promote` lane.
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .gate import is_feedback_self_loop
from .actuator_contracts import mediated_artifact_review_contract
from .schemas import new_id, truncate_text, utc_now_iso
from .store import SensoriumStore

STATE_VERSION = 1

BACKGROUND_SAFE_REQUEST_TYPES = {
    "THINK", "SAVE", "UPDATE_MEMORY_OR_SKILL", "DELEGATE_WORK",
}

DEFAULT_DISPATCH_CONFIG: dict = {
    "thresholds": {
        "dispatch_pressure": 0.5,
    },
    "thread_ttl_hours": 168,
    "lock": {
        "lease_seconds": 120,
    },
    "budgets": {
        "dispatch": {"capacity": 10, "window_seconds": 3600},
        "pointer": {"capacity": 12, "window_seconds": 3600},
        "conscious": {"capacity": 3, "window_seconds": 3600},
        "advisory": {"capacity": 0, "window_seconds": 3600},
    },
    "operational_pointer": {
        "enabled": False,
        "kinds": [],
        "surfaces": [],
        "sensitivity": "private",
    },
    # Compatibility only. New activation must flow through the Sensorium-on-Kanban
    # bridge, which creates sensor:intake -> subconscious:review ->
    # conscious:review Kanban tasks. Keep this false by default so no live tool or
    # cron can silently create internal conscious threads as a second scheduler.
    "legacy_thread_dispatch_enabled": False,
    "kanban_bridge": {
        "board": "sensorium",
        "intake_prefix": "sensor:intake:",
        "review_prefix": "subconscious:review:",
    },
}


def _merged_config(config: dict | None = None) -> dict:
    cfg = deepcopy(DEFAULT_DISPATCH_CONFIG)
    if not config:
        return cfg
    for key, value in config.items():
        if isinstance(value, dict) and isinstance(cfg.get(key), dict):
            for inner_key, inner_value in value.items():
                if isinstance(inner_value, dict) and isinstance(cfg[key].get(inner_key), dict):
                    cfg[key][inner_key].update(inner_value)
                else:
                    cfg[key][inner_key] = inner_value
        else:
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


def _lock_path(store: SensoriumStore) -> Path:
    return store.root / "locks" / "dispatcher.lock"


def _lock_payload(owner: str, lease_seconds: int) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "owner": owner,
        "acquired_at": _iso(now),
        "expires_at": _iso(now + timedelta(seconds=lease_seconds)),
    }


def _read_lock(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _recover_stale_lock(store: SensoriumStore, path: Path, payload: dict) -> None:
    receipt = {
        "ts": utc_now_iso(),
        "type": "dispatch.lock_recovered",
        "owner": payload.get("owner", ""),
        "expires_at": payload.get("expires_at", ""),
        "reason": "stale_dispatch_lock",
    }
    store.append_jsonl("decisions", receipt)
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _acquire_dispatch_lock(store: SensoriumStore, cfg: dict) -> tuple[bool, dict]:
    store.ensure_dirs()
    path = _lock_path(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)

    if path.exists():
        payload = _read_lock(path)
        expires_at = _parse_utc(payload.get("expires_at"))
        if expires_at and expires_at <= now:
            _recover_stale_lock(store, path, payload)
        else:
            return False, {
                "action": "lock_unavailable",
                "reason": "dispatcher lock is currently held",
                "lock": {
                    "owner": payload.get("owner", ""),
                    "expires_at": payload.get("expires_at", ""),
                },
            }

    owner = new_id("dispatch")
    payload = _lock_payload(owner, int(cfg.get("lock", {}).get("lease_seconds", 120)))
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        payload = _read_lock(path)
        return False, {
            "action": "lock_unavailable",
            "reason": "dispatcher lock is currently held",
            "lock": {
                "owner": payload.get("owner", ""),
                "expires_at": payload.get("expires_at", ""),
            },
        }
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f, separators=(",", ":"))
    return True, payload


def _release_dispatch_lock(store: SensoriumStore, owner: str) -> None:
    path = _lock_path(store)
    payload = _read_lock(path) if path.exists() else {}
    if payload.get("owner") == owner:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _fresh_bucket(spec: dict, now: datetime) -> dict:
    capacity = max(0, int(spec.get("capacity", 0)))
    window = max(1, int(spec.get("window_seconds", 3600)))
    return {
        "capacity": capacity,
        "remaining": capacity,
        "window_seconds": window,
        "reset_at": _iso(now + timedelta(seconds=window)),
    }


def _budget_state(store: SensoriumStore, cfg: dict) -> dict:
    now = datetime.now(timezone.utc)
    current = store.read_state().get("budgets") or {}
    budgets: dict = {}
    for name, spec in cfg.get("budgets", {}).items():
        existing = current.get(name) or {}
        reset_at = _parse_utc(existing.get("reset_at"))
        capacity = max(0, int(spec.get("capacity", 0)))
        window = max(1, int(spec.get("window_seconds", 3600)))
        if (
            reset_at is None
            or reset_at <= now
            or existing.get("capacity") != capacity
            or existing.get("window_seconds") != window
        ):
            budgets[name] = _fresh_bucket(spec, now)
        else:
            remaining = max(0, min(capacity, int(existing.get("remaining", capacity))))
            budgets[name] = {
                "capacity": capacity,
                "remaining": remaining,
                "window_seconds": window,
                "reset_at": existing.get("reset_at"),
            }
    return budgets


def current_budget_state(store: SensoriumStore, config: dict | None = None) -> dict:
    """Return budget windows as they would apply now, without writing state.latest.

    The compatibility dispatcher may be idle while JSONL/Kanban state remains
    fresh. Status surfaces should therefore compute budget reset windows from
    current config and legacy state instead of replaying stale state.latest
    values indefinitely.
    """
    budgets = _budget_state(store, _merged_config(config))
    return {
        name: {**bucket, "source": "current_config_window"}
        for name, bucket in budgets.items()
    }


def _consume_budget(budgets: dict, name: str) -> bool:
    bucket = budgets.get(name)
    if not bucket:
        return False
    if int(bucket.get("remaining", 0)) <= 0:
        return False
    bucket["remaining"] = int(bucket.get("remaining", 0)) - 1
    return True


def _write_latest(
    store: SensoriumStore,
    *,
    result: dict,
    budgets: dict,
    lock_status: dict | None = None,
) -> None:
    state = store.read_state()
    state.update({
        "state_version": STATE_VERSION,
        "updated_at": utc_now_iso(),
        "last_dispatch_result": result,
        "budgets": budgets,
        "locks": {
            "dispatcher": lock_status or {"status": "free"},
        },
    })
    store.write_state(state)


def select_candidate(candidates: list[dict], config: dict | None = None) -> dict | None:
    cfg = _merged_config(config)
    threshold = cfg.get("thresholds", {}).get("dispatch_pressure", 0.5)
    eligible = [
        c for c in candidates
        if c.get("status") == "candidate"
        and c.get("pressure", 0) >= threshold
        and not is_feedback_self_loop(c)
    ]
    if not eligible:
        return None
    eligible.sort(key=lambda c: c.get("pressure", 0), reverse=True)
    return eligible[0]


def candidate_to_thread(candidate: dict, config: dict | None = None) -> dict:
    cfg = _merged_config(config)
    ttl_hours = cfg.get("thread_ttl_hours", 168)
    now = utc_now_iso()
    now_dt = datetime.now(timezone.utc)
    expires = (now_dt + timedelta(hours=ttl_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")

    summary = candidate.get("summary", "")
    kind = candidate.get("kind", "")

    contract = mediated_artifact_review_contract(candidate)
    request_type = "PRIVATE_EXPRESSION" if contract else "THINK"
    expected_decision = (
        "Choose a mediated-presence outcome: prepare_thread_artifact, offer_choice, "
        "choose_silence, decline/block delivery, or HOLD with explicit no-artifact reason. "
        "Do not leave the artifact/action lane implicit."
        if contract else
        "Suppress, hold for later, save as workflow guidance, or create bounded follow-up."
    )
    conscious_task: dict = {
        "id": new_id("ctask"),
        "request_type": request_type,
        "title": f"Review {kind}: {truncate_text(summary, 80)}",
        "why": f"Candidate pressure {candidate.get('pressure', 0)} crossed dispatch threshold.",
        "expected_decision": expected_decision,
    }
    if contract:
        conscious_task["actuator_contract"] = contract

    thread = {
        "id": new_id("sth"),
        "status": "dormant",
        "origin": "candidate",
        "conscious_task": conscious_task,
        "origin_candidate_id": candidate.get("id", ""),
        "continuity_summary": _derive_continuity(candidate),
        "decision_log": [],
        "interaction_refs": [],
        "summary_dirty": False,
        "open_questions": [],
        "next_prompt_to_operator": (
            f"Take up this {kind} thread and make the required mediated-artifact choice?"
            if contract else
            f"Take up this {kind} thread, suppress it, or save as workflow guidance?"
        ),
        "sensitivity": candidate.get("sensitivity", "private"),
        "allowed_surfaces": candidate.get("allowed_surfaces", ["local"]),
        "dirty_since": None,
        "hold_reason": "",
        "resume_trigger": "",
        "last_interaction_at": now,
        "source_refs": [],
        "pickup": _default_pickup(
            request_type=request_type,
            allowed_surfaces=candidate.get("allowed_surfaces", ["local"]),
        ),
        "active_lease": None,
        "created_at": now,
        "updated_at": now,
        "expires_at": expires,
    }
    if contract:
        thread["actuator_contract"] = contract
    _apply_operational_pointer_policy(thread, candidate, cfg)
    return thread


def _default_pickup(*, request_type: str, allowed_surfaces: list[str]) -> dict:
    """Conservative pickup policy: background-eligible only for safe request types."""
    background = request_type in BACKGROUND_SAFE_REQUEST_TYPES
    return {
        "background": bool(background),
        "surfaces": list(allowed_surfaces or ["local"]),
        "requires_user_open": not background,
    }


def _apply_operational_pointer_policy(thread: dict, candidate: dict, cfg: dict) -> None:
    """Optionally make operational threads visible on configured pointer surfaces.

    This only changes the internal conscious-thread door handle created by the
    dispatcher. Raw signals/events/candidates remain local, and outbox/direct
    delivery is controlled separately by outbox policy.
    """
    policy = cfg.get("operational_pointer") or {}
    if not policy.get("enabled"):
        return
    kind = candidate.get("kind", "")
    kinds = set(policy.get("kinds") or [])
    if kinds and kind not in kinds:
        return
    surfaces = sorted(set(thread.get("allowed_surfaces") or []) | set(policy.get("surfaces") or []))
    if surfaces:
        thread["allowed_surfaces"] = surfaces
    sensitivity = policy.get("sensitivity")
    if isinstance(sensitivity, str) and sensitivity:
        thread["sensitivity"] = sensitivity
    thread["visibility_policy"] = {
        "mode": "operational_pointer",
        "source_kind": kind,
        "surfaces": list(policy.get("surfaces") or []),
    }


def _derive_continuity(candidate: dict) -> list[str]:
    bullets = []
    summary = candidate.get("summary", "")
    if summary:
        bullets.append(summary)
    keys = candidate.get("correlation_keys", [])
    if keys:
        bullets.append(f"Correlation keys: {', '.join(keys)}")
    kind = candidate.get("kind", "")
    if kind:
        bullets.append(f"Kind: {kind}")
    return bullets[:4]


def _dispatch_core(
    store: SensoriumStore,
    *,
    dry_run: bool,
    cfg: dict,
    budgets: dict,
) -> dict:
    candidates = store.read_jsonl("candidates")
    selected = select_candidate(candidates, cfg)

    if selected is None:
        return {"action": "no_candidate", "reason": "No eligible candidate above threshold."}

    existing_threads = store.read_jsonl("threads")
    for t in existing_threads:
        if t.get("origin_candidate_id") == selected["id"]:
            return {
                "action": "already_exists",
                "thread_id": t["id"],
                "candidate_id": selected["id"],
                "status": t.get("status", "dormant"),
            }

    thread = candidate_to_thread(selected, cfg)

    if dry_run:
        return {
            "action": "kanban_review_required",
            "dry_run": True,
            "candidate_id": selected["id"],
            "candidate_pressure": selected.get("pressure"),
            "candidate_kind": selected.get("kind", ""),
            "candidate_summary": truncate_text(selected.get("summary", ""), 200),
            "recommended_activation": "kanban_bridge",
            "kanban_bridge": cfg.get("kanban_bridge", {}),
            "legacy_thread_preview": thread,
            "deprecated_actions": ["would_promote", "promoted"],
        }

    if not cfg.get("legacy_thread_dispatch_enabled"):
        return {
            "action": "legacy_dispatch_disabled",
            "dry_run": False,
            "candidate_id": selected["id"],
            "candidate_pressure": selected.get("pressure"),
            "candidate_kind": selected.get("kind", ""),
            "recommended_activation": "kanban_bridge",
            "kanban_bridge": cfg.get("kanban_bridge", {}),
            "reason": (
                "Internal Sensorium thread dispatch is deprecated and disabled; "
                "mirror this candidate into Kanban intake/review instead."
            ),
        }

    if not _consume_budget(budgets, "dispatch"):
        return {
            "action": "budget_exhausted",
            "dry_run": False,
            "candidate_id": selected["id"],
            "budget": "dispatch",
            "reason": "dispatch budget exhausted for current window",
        }

    store.append_jsonl("threads", thread)

    receipt = {
        "ts": utc_now_iso(),
        "type": "dispatch.promoted_to_thread",
        "candidate_id": selected["id"],
        "thread_id": thread["id"],
        "dry_run": False,
    }
    store.append_jsonl("decisions", receipt)

    return {
        "action": "promoted",
        "dry_run": False,
        "candidate_id": selected["id"],
        "thread_id": thread["id"],
    }


def dispatch_once(
    store: SensoriumStore,
    *,
    dry_run: bool = True,
    config: dict | None = None,
) -> dict:
    cfg = _merged_config(config)
    store.ensure_dirs()
    budgets = _budget_state(store, cfg)

    if dry_run:
        result = _dispatch_core(store, dry_run=True, cfg=cfg, budgets=budgets)
        _write_latest(
            store,
            result=result,
            budgets=budgets,
            lock_status={"status": "not_acquired", "reason": "dry_run"},
        )
        return result

    if not cfg.get("legacy_thread_dispatch_enabled"):
        result = _dispatch_core(store, dry_run=False, cfg=cfg, budgets=budgets)
        _write_latest(
            store,
            result=result,
            budgets=budgets,
            lock_status={"status": "not_acquired", "reason": "legacy_dispatch_disabled"},
        )
        return result

    acquired, lock = _acquire_dispatch_lock(store, cfg)
    if not acquired:
        result = dict(lock)
        _write_latest(
            store,
            result=result,
            budgets=budgets,
            lock_status={"status": "busy", **lock.get("lock", {})},
        )
        return result

    try:
        result = _dispatch_core(store, dry_run=False, cfg=cfg, budgets=budgets)
    finally:
        _release_dispatch_lock(store, lock.get("owner", ""))

    _write_latest(store, result=result, budgets=budgets, lock_status={"status": "free"})
    return result
