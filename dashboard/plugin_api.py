"""Read-only Hermes dashboard API for Agent Sensorium.

Mounted by Hermes dashboard under /api/plugins/agent-sensorium/.
This deliberately reads compact JSONL state only and never mutates Sensorium state.
"""
from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter

router = APIRouter()


def _default_instance() -> str:
    try:
        import sys

        plugin_root = Path(__file__).resolve().parents[1]
        if str(plugin_root) not in sys.path:
            sys.path.insert(0, str(plugin_root))
        from agent_sensorium.config import default_instance_name

        return default_instance_name("default")
    except Exception:
        return os.environ.get("AGENT_SENSORIUM_DEFAULT_INSTANCE") or os.environ.get("SENSORIUM_INSTANCE") or "default"


DEFAULT_INSTANCE = _default_instance()
DEFAULT_ROOT = Path(os.environ.get("SENSORIUM_STATE_DIR", Path.home() / ".hermes" / "agent-sensorium" / DEFAULT_INSTANCE))
STATE_NAMES = {
    "signals": "signals/inbox.jsonl",
    "events": "events.jsonl",
    "candidates": "candidates.jsonl",
    "threads": "threads.jsonl",
    "decisions": "decisions.jsonl",
    "outbox": "outbox.jsonl",
}
ACTIVE_THREAD_STATUSES = {"dormant", "held"}
ACTIVE_CANDIDATE_STATUS = "candidate"
OPEN_OUTBOX_STATUSES = {"prepared", "failed"}


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _truncate(value: Any, limit: int = 160) -> str:
    text = str(value or "").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(errors="ignore"))
    except Exception:
        return default


def _read_jsonl(root: Path, name: str, limit: int | None = None) -> tuple[list[dict[str, Any]], int]:
    rel = STATE_NAMES[name]
    path = root / rel
    if not path.exists():
        return [], 0
    try:
        lines = path.read_text(errors="ignore").splitlines()
    except Exception:
        return [], 0
    if limit is not None:
        lines = lines[-limit:]
    rows: list[dict[str, Any]] = []
    bad = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except Exception:
            bad += 1
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows, bad


def _mtime(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).astimezone().isoformat(timespec="seconds")
    except Exception:
        return None


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _thread_title(thread: dict[str, Any]) -> str:
    raw_task = thread.get("conscious_task")
    task: dict[str, Any] = raw_task if isinstance(raw_task, dict) else {}
    return _truncate(task.get("title") or thread.get("title") or thread.get("id"), 140)


def _thread_item(thread: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": thread.get("id"),
        "status": thread.get("status") or "dormant",
        "title": _thread_title(thread),
        "origin_candidate_id": thread.get("origin_candidate_id"),
        "sensitivity": thread.get("sensitivity"),
        "allowed_surfaces": thread.get("allowed_surfaces") or [],
        "created_at": thread.get("created_at"),
        "updated_at": thread.get("updated_at"),
        "expires_at": thread.get("expires_at"),
        "pinned": bool(thread.get("pinned")),
        "dirty": bool(thread.get("dirty_since")),
        "interaction_refs": len(thread.get("interaction_refs") or []),
    }


def _candidate_item(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": candidate.get("id"),
        "status": candidate.get("status"),
        "kind": candidate.get("kind"),
        "pressure": candidate.get("pressure"),
        "summary": _truncate(candidate.get("summary"), 180),
        "sensitivity": candidate.get("sensitivity"),
        "allowed_surfaces": candidate.get("allowed_surfaces") or [],
        "updated_at": candidate.get("updated_at") or candidate.get("created_at"),
    }


def _outbox_item(req: dict[str, Any]) -> dict[str, Any]:
    raw_target = req.get("target")
    target: dict[str, Any] = raw_target if isinstance(raw_target, dict) else {}
    target_keys = {k: target[k] for k in ("channel_id", "thread_id", "dm_channel_id", "session_ref") if target.get(k)}
    return {
        "id": req.get("id"),
        "status": req.get("status"),
        "origin_thread_id": req.get("origin_thread_id"),
        "request_type": req.get("request_type"),
        "surface": req.get("surface"),
        "delivery_mode": req.get("delivery_mode"),
        "title": _truncate(req.get("title"), 120),
        "message_preview": _truncate(req.get("message_preview"), 180),
        "target": target_keys,
        "platform_refs": req.get("platform_refs") or {},
        "created_at": req.get("created_at"),
        "updated_at": req.get("updated_at"),
    }


def _decision_item(decision: dict[str, Any]) -> dict[str, Any]:
    return {
        "ts": decision.get("ts"),
        "type": decision.get("type"),
        "thread_id": decision.get("thread_id"),
        "candidate_id": decision.get("candidate_id"),
        "outbox_id": decision.get("outbox_id"),
        "action": decision.get("action"),
        "reason": _truncate(decision.get("reason") or decision.get("detail") or decision.get("error"), 140),
    }


def _health(counts: dict[str, int], active_threads: int, open_outbox: int, state: dict[str, Any]) -> dict[str, Any]:
    if counts.get("corrupt_lines", 0):
        band = "red"
        status = "corrupt_state"
    elif open_outbox:
        band = "yellow"
        status = "pending_outbox"
    elif active_threads:
        band = "green"
        status = "threads_visible"
    else:
        band = "neutral"
        status = "quiet"
    raw_last_dispatch = state.get("last_dispatch_result")
    last_dispatch: dict[str, Any] = raw_last_dispatch if isinstance(raw_last_dispatch, dict) else {}
    return {
        "status": status,
        "band": band,
        "last_dispatch_action": last_dispatch.get("action"),
        "last_dispatch_reason": _truncate(last_dispatch.get("reason"), 120),
    }


@router.get("/attention")
async def attention(instance: str | None = None, surface: str = "local", limit: int = 50) -> dict[str, Any]:
    """Read-only attention inbox: candidates and threads filtered by surface."""
    try:
        import sys

        plugin_root = Path(__file__).resolve().parents[1]
        if str(plugin_root) not in sys.path:
            sys.path.insert(0, str(plugin_root))
        from agent_sensorium.attention import build_attention_inbox
        from agent_sensorium.store import SensoriumStore
    except ImportError:
        return {"ok": False, "error": "agent_sensorium not importable"}

    effective_instance = instance or DEFAULT_INSTANCE
    root = DEFAULT_ROOT if effective_instance in {"default", DEFAULT_INSTANCE} else DEFAULT_ROOT.parent / effective_instance
    store = SensoriumStore(instance=effective_instance, state_dir=str(root))
    try:
        # Read-only dashboard endpoint: SensoriumStore.read_jsonl returns empty
        # rows for missing files, so avoid ensure_dirs() here.
        inbox = build_attention_inbox(store, surface=surface, limit=limit)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "generated_at": _now(), "instance": effective_instance, **inbox}


@router.get("/snapshot")
async def snapshot(instance: str | None = None) -> dict[str, Any]:
    # `instance` is surfaced for multi-instance UI without allowing arbitrary
    # path traversal. Omitted and legacy `default` requests both resolve to the
    # configured default instance.
    effective_instance = instance or DEFAULT_INSTANCE
    root = DEFAULT_ROOT if effective_instance in {"default", DEFAULT_INSTANCE} else DEFAULT_ROOT.parent / effective_instance
    state = _read_json(root / "state.latest.json", {})
    config = _read_json(root / "instance.config.json", {})

    rows: dict[str, list[dict[str, Any]]] = {}
    corrupt = 0
    for name in STATE_NAMES:
        rows[name], bad = _read_jsonl(root, name, limit=5000)
        corrupt += bad

    candidates = rows["candidates"]
    threads = rows["threads"]
    outbox = rows["outbox"]
    decisions = rows["decisions"]

    active_candidates = [c for c in candidates if c.get("status") == ACTIVE_CANDIDATE_STATUS]
    active_candidates.sort(key=lambda c: float(c.get("pressure") or 0), reverse=True)

    visible_threads = [t for t in threads if t.get("status") in ACTIVE_THREAD_STATUSES]
    visible_threads.sort(key=lambda t: t.get("updated_at") or t.get("created_at") or "", reverse=True)

    recent_outbox = sorted(outbox, key=lambda r: r.get("updated_at") or r.get("created_at") or "", reverse=True)
    open_outbox = [r for r in outbox if r.get("status") in OPEN_OUTBOX_STATUSES]

    counts = {
        "signals": len(rows["signals"]),
        "events": len(rows["events"]),
        "candidates": len(candidates),
        "active_candidates": len(active_candidates),
        "threads": len(threads),
        "active_threads": len(visible_threads),
        "outbox": len(outbox),
        "open_outbox": len(open_outbox),
        "decisions": len(decisions),
        "corrupt_lines": corrupt,
    }

    return {
        "ok": True,
        "generated_at": _now(),
        "instance": effective_instance,
        "state_dir": str(root),
        "state_exists": root.exists(),
        "state_mtime": _mtime(root / "state.latest.json"),
        "config": {
            "instance_name": config.get("instance_name") or effective_instance,
            "allowed_surfaces": config.get("allowed_surfaces") or ["local"],
            "max_sensitivity": config.get("max_sensitivity") or "private",
            "policy_card_ref": config.get("policy_card_ref"),
        },
        "counts": counts,
        "health": _health(counts, len(visible_threads), len(open_outbox), state),
        "status_breakdown": {
            "threads": dict(Counter(str(t.get("status") or "unknown") for t in threads)),
            "candidates": dict(Counter(str(c.get("status") or "unknown") for c in candidates)),
            "outbox": dict(Counter(str(o.get("status") or "unknown") for o in outbox)),
        },
        "top_candidates": [_candidate_item(c) for c in active_candidates[:6]],
        "threads": [_thread_item(t) for t in visible_threads[:8]],
        "outbox": [_outbox_item(r) for r in recent_outbox[:8]],
        "decisions": [_decision_item(d) for d in decisions[-12:]][::-1],
        "budgets": state.get("budgets") if isinstance(state.get("budgets"), dict) else {},
    }
