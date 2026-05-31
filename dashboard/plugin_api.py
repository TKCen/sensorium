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
    "thread_actions": "thread_actions.jsonl",
    "artifacts": "artifacts.jsonl",
}
ACTIVE_THREAD_STATUSES = {"dormant", "held"}
ACTIVE_CANDIDATE_STATUS = "candidate"
ACTIVE_ACTION_STATUSES = {"proposed", "prepared", "offered"}
DIRECT_DELIVERY_MODES = {"discord_channel_thread", "discord_dm_bound_session"}
OPEN_OUTBOX_STATUSES = {"prepared", "failed"}
TERMINAL_THREAD_STATUSES = {"closed", "archived"}


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


def _freshness_mtime(root: Path) -> str | None:
    """Return the newest mtime for live Sensorium state, not only state.latest.

    `state.latest.json` is dispatcher-oriented and can stay unchanged while the
    Kanban activation substrate, sensor JSONL files, and quiet tick receipts are
    current. The dashboard uses this value as its freshness indicator.
    """
    candidates = [root / "state.latest.json"]
    candidates.extend(root / rel for rel in STATE_NAMES.values())
    kanban_root = root.parent / "kanban"
    candidates.extend(
        [
            kanban_root / "last_sensorium_kanban_tick.json",
            kanban_root / "sensor_kanban_state.json",
            kanban_root / "sera_tick_quiet.latest.json",
        ]
    )
    newest: float | None = None
    for path in candidates:
        try:
            mtime = path.stat().st_mtime
        except Exception:
            continue
        newest = mtime if newest is None else max(newest, mtime)
    if newest is None:
        return None
    return datetime.fromtimestamp(newest, timezone.utc).astimezone().isoformat(timespec="seconds")


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


def _sort_key(row: dict[str, Any]) -> str:
    return str(row.get("updated_at") or row.get("created_at") or row.get("ts") or "")


def _index_by_id(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(row.get("id")): row for row in rows if row.get("id")}


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


def _action_attachment_ids(action: dict[str, Any], kind: str) -> list[str]:
    refs: list[str] = []
    for attachment in action.get("attachments") or []:
        if isinstance(attachment, dict) and attachment.get("kind") == kind and attachment.get("ref_id"):
            refs.append(str(attachment.get("ref_id")))
    return refs


def _action_item(action: dict[str, Any]) -> dict[str, Any]:
    attachments = [a for a in action.get("attachments") or [] if isinstance(a, dict)]
    attachment_kinds = Counter(str(a.get("kind") or "unknown") for a in attachments)
    return {
        "id": action.get("id"),
        "status": action.get("status"),
        "outcome": action.get("outcome"),
        "intent": action.get("intent"),
        "title": _truncate(action.get("title") or action.get("intent"), 140),
        "summary": _truncate(action.get("summary"), 180),
        "origin_thread_id": action.get("origin_thread_id"),
        "origin_candidate_id": action.get("origin_candidate_id"),
        "artifact_refs": _action_attachment_ids(action, "artifact_ref"),
        "outbox_refs": _action_attachment_ids(action, "outbox_request"),
        "attachment_count": len(attachments),
        "attachment_kinds": dict(attachment_kinds),
        "result_summary": _truncate(action.get("result_summary"), 180),
        "updated_at": action.get("updated_at") or action.get("ts"),
    }


def _artifact_item(artifact: dict[str, Any]) -> dict[str, Any]:
    raw_source_refs = artifact.get("source_refs")
    source_refs: dict[str, Any] = raw_source_refs if isinstance(raw_source_refs, dict) else {}
    ref_path = str(artifact.get("ref_path") or "")
    return {
        "id": artifact.get("id"),
        "kind": artifact.get("kind"),
        "status": artifact.get("status"),
        "delivery_state": artifact.get("delivery_state"),
        "handoff_mode": artifact.get("intended_handoff_mode"),
        "ref_name": Path(ref_path).name if ref_path else "",
        "ref_path": ref_path,
        "why_created": _truncate(artifact.get("why_created"), 180),
        "thread_id": source_refs.get("thread_id"),
        "candidate_id": source_refs.get("candidate_id"),
        "action_id": source_refs.get("action_id"),
        "sensitivity": artifact.get("sensitivity") or artifact.get("privacy"),
        "allowed_surfaces": artifact.get("allowed_surfaces") or [],
        "updated_at": artifact.get("updated_at") or artifact.get("ts"),
    }


def _find_action_for_outbox(actions: list[dict[str, Any]]) -> dict[str, str]:
    action_for_outbox: dict[str, str] = {}
    for action in actions:
        action_id = str(action.get("id") or "")
        if not action_id:
            continue
        for ref_id in _action_attachment_ids(action, "outbox_request"):
            action_for_outbox[ref_id] = action_id
    return action_for_outbox


def _outbox_safety(
    req: dict[str, Any],
    *,
    thread_by_id: dict[str, dict[str, Any]],
    action_by_id: dict[str, dict[str, Any]],
    action_for_outbox: dict[str, str],
    config: dict[str, Any],
) -> dict[str, Any]:
    status = str(req.get("status") or "unknown")
    mode = str(req.get("delivery_mode") or "")
    thread_id = str(req.get("origin_thread_id") or "")
    thread = thread_by_id.get(thread_id, {})
    thread_status = str(thread.get("status") or "missing")
    action_id = action_for_outbox.get(str(req.get("id") or ""), "")
    action = action_by_id.get(action_id, {})
    action_status = str(action.get("status") or "")
    raw_outbox_cfg = config.get("outbox")
    outbox_cfg: dict[str, Any] = raw_outbox_cfg if isinstance(raw_outbox_cfg, dict) else {}
    direct_enabled = bool(outbox_cfg.get("direct_modes_enabled") or outbox_cfg.get("enable_direct_discord"))
    direct_mode = mode in DIRECT_DELIVERY_MODES

    safety = {
        "band": "neutral",
        "label": "recorded",
        "detail": "Historical or non-actionable outbox record.",
        "outbound_delivery": direct_mode,
        "direct_delivery_enabled": direct_enabled,
        "dispatch_requires_execute": status == "prepared",
        "origin_thread_status": thread_status,
        "attached_action_id": action_id,
        "attached_action_status": action_status,
        "actionable": False,
    }

    if status == "failed":
        safety.update(
            band="red",
            label="dispatch_failed",
            detail="Previous dispatch attempt failed and needs review.",
            actionable=True,
        )
    elif status == "dispatched":
        safety.update(
            band="green",
            label="dispatched",
            detail="Outbox dispatch receipt exists.",
            dispatch_requires_execute=False,
        )
    elif status == "prepared" and direct_mode and not direct_enabled:
        safety.update(
            band="yellow",
            label="direct_delivery_disabled",
            detail="Direct outbound delivery is disabled; this cannot send without policy/config changes.",
            actionable=False,
        )
    elif status == "prepared" and direct_mode:
        safety.update(
            band="yellow",
            label="direct_ready_requires_execute",
            detail="Direct mode is configured but still requires explicit execute=True.",
            actionable=True,
        )
    elif status == "prepared" and mode in {"context_pointer", "peripheral_reference"}:
        if thread_status in TERMINAL_THREAD_STATUSES and action_status == "acted":
            safety.update(
                band="neutral",
                label="historical_prepared_pointer",
                detail="Prepared pointer is attached to a completed action on a closed thread; no outbound send path.",
            )
        elif thread_status in TERMINAL_THREAD_STATUSES:
            safety.update(
                band="yellow",
                label="prepared_pointer_closed_thread",
                detail="Prepared pointer belongs to a closed/archived thread and should be settled or marked historical.",
                actionable=True,
            )
        else:
            safety.update(
                band="green",
                label="prepared_pointer_only",
                detail="Review pointer is prepared; not direct outbound delivery.",
            )
    return safety


def _outbox_item(
    req: dict[str, Any],
    *,
    thread_by_id: dict[str, dict[str, Any]],
    action_by_id: dict[str, dict[str, Any]],
    action_for_outbox: dict[str, str],
    config: dict[str, Any],
) -> dict[str, Any]:
    raw_target = req.get("target")
    target: dict[str, Any] = raw_target if isinstance(raw_target, dict) else {}
    target_keys = {
        k: target[k]
        for k in ("channel_id", "thread_id", "dm_channel_id", "session_ref", "session", "recipient")
        if target.get(k)
    }
    safety = _outbox_safety(
        req,
        thread_by_id=thread_by_id,
        action_by_id=action_by_id,
        action_for_outbox=action_for_outbox,
        config=config,
    )
    return {
        "id": req.get("id"),
        "status": req.get("status"),
        "origin_thread_id": req.get("origin_thread_id"),
        "origin_candidate_id": req.get("origin_candidate_id"),
        "request_type": req.get("request_type"),
        "surface": req.get("surface"),
        "delivery_mode": req.get("delivery_mode"),
        "title": _truncate(req.get("title"), 120),
        "message_preview": _truncate(req.get("message_preview"), 180),
        "target": target_keys,
        "media_refs": req.get("media_refs") or [],
        "platform_refs": req.get("platform_refs") or {},
        "created_at": req.get("created_at"),
        "updated_at": req.get("updated_at"),
        "safety": safety,
    }


def _decision_item(decision: dict[str, Any]) -> dict[str, Any]:
    return {
        "ts": decision.get("ts"),
        "type": decision.get("type"),
        "thread_id": decision.get("thread_id"),
        "candidate_id": decision.get("candidate_id"),
        "outbox_id": decision.get("outbox_id"),
        "action_id": decision.get("action_id"),
        "artifact_id": decision.get("artifact_id"),
        "action": decision.get("action"),
        "reason": _truncate(decision.get("reason") or decision.get("detail") or decision.get("error"), 140),
    }


def _lifecycle_warnings(
    *,
    outbox: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    thread_by_id: dict[str, dict[str, Any]],
    action_by_id: dict[str, dict[str, Any]],
    action_for_outbox: dict[str, str],
    artifact_by_id: dict[str, dict[str, Any]],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    for req in outbox:
        req_id = str(req.get("id") or "")
        safety = _outbox_safety(
            req,
            thread_by_id=thread_by_id,
            action_by_id=action_by_id,
            action_for_outbox=action_for_outbox,
            config=config,
        )
        if safety.get("actionable"):
            warnings.append(
                {
                    "kind": "outbox",
                    "id": req_id,
                    "band": safety.get("band"),
                    "label": safety.get("label"),
                    "detail": safety.get("detail"),
                }
            )
        if req.get("status") == "prepared" and not action_for_outbox.get(req_id):
            warnings.append(
                {
                    "kind": "outbox",
                    "id": req_id,
                    "band": "yellow",
                    "label": "prepared_outbox_unattached",
                    "detail": "Prepared outbox record is not attached to any thread action.",
                }
            )
        for media_ref in req.get("media_refs") or []:
            if media_ref and str(media_ref) not in artifact_by_id:
                warnings.append(
                    {
                        "kind": "artifact",
                        "id": str(media_ref),
                        "band": "yellow",
                        "label": "missing_media_ref",
                        "detail": f"Outbox {req_id} references a missing artifact.",
                    }
                )
    for action in actions:
        if action.get("status") in ACTIVE_ACTION_STATUSES and _action_attachment_ids(action, "outbox_request"):
            warnings.append(
                {
                    "kind": "action",
                    "id": action.get("id"),
                    "band": "yellow",
                    "label": "outbox_action_not_completed",
                    "detail": "Action has an outbox attachment but is still open.",
                }
            )
    for artifact in artifacts:
        raw_source_refs = artifact.get("source_refs")
        source_refs: dict[str, Any] = raw_source_refs if isinstance(raw_source_refs, dict) else {}
        action_id = str(source_refs.get("action_id") or "")
        if action_id and action_id not in action_by_id:
            warnings.append(
                {
                    "kind": "artifact",
                    "id": artifact.get("id"),
                    "band": "yellow",
                    "label": "artifact_action_missing",
                    "detail": f"Artifact references missing action {action_id}.",
                }
            )
    return warnings[:12]


def _health(
    counts: dict[str, int],
    active_threads: int,
    actionable_outbox: int,
    open_actions: int,
    warning_count: int,
    state: dict[str, Any],
) -> dict[str, Any]:
    if counts.get("corrupt_lines", 0):
        band = "red"
        status = "corrupt_state"
    elif actionable_outbox or warning_count or open_actions:
        band = "yellow"
        status = "needs_review"
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
    actions = rows["thread_actions"]
    artifacts = rows["artifacts"]

    thread_by_id = _index_by_id(threads)
    action_by_id = _index_by_id(actions)
    artifact_by_id = _index_by_id(artifacts)
    action_for_outbox = _find_action_for_outbox(actions)

    active_candidates = [c for c in candidates if c.get("status") == ACTIVE_CANDIDATE_STATUS]
    active_candidates.sort(key=lambda c: float(c.get("pressure") or 0), reverse=True)

    visible_threads = [t for t in threads if t.get("status") in ACTIVE_THREAD_STATUSES]
    visible_threads.sort(key=_sort_key, reverse=True)

    recent_outbox = sorted(outbox, key=_sort_key, reverse=True)
    recent_actions = sorted(actions, key=_sort_key, reverse=True)
    recent_artifacts = sorted(artifacts, key=_sort_key, reverse=True)
    open_outbox = [r for r in outbox if r.get("status") in OPEN_OUTBOX_STATUSES]
    open_actions = [a for a in actions if a.get("status") in ACTIVE_ACTION_STATUSES]

    outbox_items = [
        _outbox_item(
            r,
            thread_by_id=thread_by_id,
            action_by_id=action_by_id,
            action_for_outbox=action_for_outbox,
            config=config,
        )
        for r in recent_outbox[:12]
    ]
    actionable_outbox = sum(1 for item in outbox_items if item.get("safety", {}).get("actionable"))
    warnings = _lifecycle_warnings(
        outbox=outbox,
        actions=actions,
        artifacts=artifacts,
        thread_by_id=thread_by_id,
        action_by_id=action_by_id,
        action_for_outbox=action_for_outbox,
        artifact_by_id=artifact_by_id,
        config=config,
    )

    counts = {
        "signals": len(rows["signals"]),
        "events": len(rows["events"]),
        "candidates": len(candidates),
        "active_candidates": len(active_candidates),
        "threads": len(threads),
        "active_threads": len(visible_threads),
        "actions": len(actions),
        "open_actions": len(open_actions),
        "artifacts": len(artifacts),
        "held_artifacts": sum(1 for a in artifacts if a.get("delivery_state") == "held_for_review"),
        "outbox": len(outbox),
        "prepared_outbox": sum(1 for o in outbox if o.get("status") == "prepared"),
        "open_outbox": len(open_outbox),
        "actionable_outbox": actionable_outbox,
        "lifecycle_warnings": len(warnings),
        "decisions": len(decisions),
        "corrupt_lines": corrupt,
    }

    return {
        "ok": True,
        "generated_at": _now(),
        "instance": effective_instance,
        "state_dir": str(root),
        "state_exists": root.exists(),
        "state_mtime": _freshness_mtime(root),
        "state_latest_mtime": _mtime(root / "state.latest.json"),
        "kanban_tick_mtime": _mtime(root.parent / "kanban" / "last_sensorium_kanban_tick.json"),
        "config": {
            "instance_name": config.get("instance_name") or effective_instance,
            "allowed_surfaces": config.get("allowed_surfaces") or ["local"],
            "max_sensitivity": config.get("max_sensitivity") or "private",
            "policy_card_ref": config.get("policy_card_ref"),
            "outbox": config.get("outbox") if isinstance(config.get("outbox"), dict) else {},
        },
        "counts": counts,
        "health": _health(
            counts,
            len(visible_threads),
            actionable_outbox,
            len(open_actions),
            len(warnings),
            state,
        ),
        "status_breakdown": {
            "threads": dict(Counter(str(t.get("status") or "unknown") for t in threads)),
            "candidates": dict(Counter(str(c.get("status") or "unknown") for c in candidates)),
            "actions": dict(Counter(str(a.get("status") or "unknown") for a in actions)),
            "artifacts": dict(Counter(str(a.get("delivery_state") or "unknown") for a in artifacts)),
            "outbox": dict(Counter(str(o.get("status") or "unknown") for o in outbox)),
        },
        "top_candidates": [_candidate_item(c) for c in active_candidates[:6]],
        "threads": [_thread_item(t) for t in visible_threads[:8]],
        "actions": [_action_item(a) for a in recent_actions[:10]],
        "artifacts": [_artifact_item(a) for a in recent_artifacts[:10]],
        "outbox": outbox_items,
        "lifecycle_warnings": warnings,
        "decisions": [_decision_item(d) for d in decisions[-14:]][::-1],
        "budgets": state.get("budgets") if isinstance(state.get("budgets"), dict) else {},
    }
