"""Dispatcher: select candidate, create dormant thread capsule.

Phase 3 keeps dispatch as the single attention scheduler by adding:
- a local lock/lease for mutating dispatch;
- small token-bucket state for dispatcher lanes;
- state.latest updates for observability.
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .schemas import new_id, truncate_text, utc_now_iso
from .store import SensoriumStore

STATE_VERSION = 1

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
        if c.get("status") == "candidate" and c.get("pressure", 0) >= threshold
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

    return {
        "id": new_id("sth"),
        "status": "dormant",
        "origin": "candidate",
        "conscious_task": {
            "id": new_id("ctask"),
            "request_type": "THINK",
            "title": f"Review {kind}: {truncate_text(summary, 80)}",
            "why": f"Candidate pressure {candidate.get('pressure', 0)} crossed dispatch threshold.",
            "expected_decision": "Suppress, hold for later, save as workflow guidance, or create bounded follow-up.",
        },
        "origin_candidate_id": candidate.get("id", ""),
        "continuity_summary": _derive_continuity(candidate),
        "decision_log": [],
        "interaction_refs": [],
        "summary_dirty": False,
        "open_questions": [],
        "next_prompt_to_operator": f"Take up this {kind} thread, suppress it, or save as workflow guidance?",
        "sensitivity": candidate.get("sensitivity", "private"),
        "allowed_surfaces": candidate.get("allowed_surfaces", ["local"]),
        "created_at": now,
        "updated_at": now,
        "expires_at": expires,
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
            "action": "would_promote",
            "dry_run": True,
            "candidate_id": selected["id"],
            "candidate_pressure": selected.get("pressure"),
            "thread_preview": thread,
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
