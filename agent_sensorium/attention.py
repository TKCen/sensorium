"""Read-only Attention Inbox builder — the conscious aperture surface.

Collects active candidates and visible conscious threads into a compact
review surface filtered by surface and sensitivity policy. Never mutates state.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .config import load_instance_config
from .schemas import SENSITIVITY_RANK, truncate_text, utc_now_iso
from .store import SensoriumStore

CANDIDATE_DECISIONS = ["open", "suppress", "hold", "mark_reviewed"]
THREAD_DECISIONS: dict[str, list[str]] = {
    "dormant": ["open", "hold", "close", "archive", "mark_reviewed"],
    "held": ["open", "resume", "close", "archive", "mark_reviewed"],
}


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _age_hours(created_at: str | None, now: datetime) -> float | None:
    dt = _parse_ts(created_at)
    if dt is None:
        return None
    return round(max(0.0, (now - dt).total_seconds() / 3600), 1)


def _freshness(updated_at: str | None, now: datetime) -> str:
    dt = _parse_ts(updated_at)
    if dt is None:
        return "unknown"
    hours = (now - dt).total_seconds() / 3600
    if hours < 1:
        return "fresh"
    if hours < 24:
        return "recent"
    return "stale"


def visible_on_surface(item: dict, surface: str, config: dict) -> bool:
    """Return True only if item is allowed on the requested surface per policy.

    Fails closed: missing surfaces or sensitivity data hides the item.
    """
    if not surface:
        return False
    item_surfaces = set(item.get("allowed_surfaces") or [])
    config_surfaces = set(config.get("allowed_surfaces") or [])
    if surface not in item_surfaces or surface not in config_surfaces:
        return False
    item_rank = SENSITIVITY_RANK.get(item.get("sensitivity", "private"), 1)
    max_rank = SENSITIVITY_RANK.get(config.get("max_sensitivity", "private"), 1)
    return item_rank <= max_rank


def _candidate_item(candidate: dict, now: datetime) -> dict:
    return {
        "id": candidate.get("id", ""),
        "type": "candidate",
        "status": candidate.get("status", "candidate"),
        "kind": candidate.get("kind", ""),
        "title": truncate_text(candidate.get("summary", ""), 100),
        "summary": truncate_text(candidate.get("summary", ""), 200),
        "pressure": candidate.get("pressure", 0),
        "age_hours": _age_hours(candidate.get("created_at"), now),
        "freshness": _freshness(candidate.get("updated_at") or candidate.get("created_at"), now),
        "sensitivity": candidate.get("sensitivity", "private"),
        "allowed_surfaces": candidate.get("allowed_surfaces", []),
        "source_refs": [],
        "allowed_decisions": list(CANDIDATE_DECISIONS),
        "created_at": candidate.get("created_at", ""),
        "updated_at": candidate.get("updated_at", ""),
    }


def _thread_item(thread: dict, now: datetime) -> dict:
    task = thread.get("conscious_task") or {}
    status = thread.get("status", "dormant")
    continuity = thread.get("continuity_summary") or []
    summary = task.get("why", "") or (continuity[0] if continuity else "")
    return {
        "id": thread.get("id", ""),
        "type": "thread",
        "status": status,
        "kind": task.get("request_type", ""),
        "title": truncate_text(task.get("title", ""), 100),
        "summary": truncate_text(summary, 200),
        "pressure": None,
        "age_hours": _age_hours(thread.get("created_at"), now),
        "freshness": _freshness(thread.get("updated_at") or thread.get("created_at"), now),
        "sensitivity": thread.get("sensitivity", "private"),
        "allowed_surfaces": thread.get("allowed_surfaces", []),
        "source_refs": thread.get("source_refs", []),
        "origin_candidate_id": thread.get("origin_candidate_id", ""),
        "hold_reason": thread.get("hold_reason", ""),
        "resume_trigger": thread.get("resume_trigger", ""),
        "allowed_decisions": list(THREAD_DECISIONS.get(status, [])),
        "created_at": thread.get("created_at", ""),
        "updated_at": thread.get("updated_at", ""),
        "expires_at": thread.get("expires_at", ""),
    }


def build_attention_inbox(
    store: SensoriumStore,
    *,
    surface: str = "local",
    config: dict | None = None,
    config_path: str | None = None,
    limit: int = 50,
) -> dict:
    """Build a compact attention inbox for the given surface. Read-only."""
    instance_config, config_diag = load_instance_config(
        config_path=config_path, state_dir=str(store.root),
    )
    if config:
        for k, v in config.items():
            instance_config[k] = v

    now = datetime.now(timezone.utc)

    candidates = store.read_jsonl("candidates")
    threads = store.read_jsonl("threads")

    active_candidates = [c for c in candidates if c.get("status") == "candidate"]
    visible_threads = [t for t in threads if t.get("status") in ("dormant", "held")]

    items: list[dict] = []
    filtered_out = 0

    for c in active_candidates:
        if visible_on_surface(c, surface, instance_config):
            items.append(_candidate_item(c, now))
        else:
            filtered_out += 1

    for t in visible_threads:
        if visible_on_surface(t, surface, instance_config):
            items.append(_thread_item(t, now))
        else:
            filtered_out += 1

    items.sort(key=lambda x: (0 if x["type"] == "thread" else 1, -(x.get("pressure") or 0)))

    if limit and len(items) > limit:
        items = items[:limit]

    candidate_count = sum(1 for i in items if i["type"] == "candidate")
    thread_count = sum(1 for i in items if i["type"] == "thread")

    return {
        "items": items,
        "counts": {
            "total": len(items),
            "candidates": candidate_count,
            "threads": thread_count,
            "filtered_out": filtered_out,
        },
        "config_diagnostics": {
            "surface": surface,
            "instance_name": config_diag.get("instance_name", "default"),
            "allowed_surfaces": config_diag.get("allowed_surfaces", ["local"]),
            "max_sensitivity": config_diag.get("max_sensitivity", "private"),
            "policy_card_ref": config_diag.get("policy_card_ref"),
        },
        "ts": utc_now_iso(),
    }
