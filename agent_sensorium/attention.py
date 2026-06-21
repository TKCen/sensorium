"""Read-only Attention Inbox builder — the conscious aperture surface.

Collects active candidates and visible conscious threads into a compact
review surface filtered by surface and sensitivity policy. Never mutates state.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import re

from .config import load_instance_config, visible_on_surface
from .schemas import truncate_text, utc_now_iso
from .store import SensoriumStore

CANDIDATE_DECISIONS = ["open", "suppress", "hold", "mark_reviewed"]
THREAD_DECISIONS: dict[str, list[str]] = {
    "dormant": ["open", "hold", "close", "archive", "mark_reviewed"],
    "held": ["open", "resume", "close", "archive", "mark_reviewed"],
}

_SAFE_HASH_LABEL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*#[0-9a-f]{16}$")
_SAFE_ATOM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$")
_PRIVATE_MARKER_RE = re.compile(
    r"sk-|api[_-]?key|private[_-]?key|password|passwd|oauth|bearer|secret|token|raw[_ -]?transcript|raw[_ -]?log|do[_ -]?not[_ -]?leak",
    re.I,
)


def _opaque_label(prefix: str, value: object) -> str:
    digest = hashlib.sha256(str(value or "").encode("utf-8", errors="ignore")).hexdigest()[:16]
    return f"{prefix}#{digest}"


def _safe_text(prefix: str, value: object, *, limit: int = 160, atom_only: bool = False) -> str:
    text = truncate_text(str(value or "").replace("\n", " ").strip(), limit)
    if not text:
        return ""
    if _SAFE_HASH_LABEL_RE.fullmatch(text):
        return text
    if _PRIVATE_MARKER_RE.search(text) or (atom_only and not _SAFE_ATOM_RE.fullmatch(text)):
        return _opaque_label(prefix, value)
    return text


def _safe_atom(prefix: str, value: object, *, limit: int = 96) -> str:
    return _safe_text(prefix, value, limit=limit, atom_only=True)


def _safe_atom_list(prefix: str, values: object, *, limit: int = 8) -> list[str]:
    if not isinstance(values, list):
        return []
    out: list[str] = []
    for value in values:
        label = _safe_atom(prefix, value)
        if label:
            out.append(label)
        if len(out) >= limit:
            break
    return out


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


def _candidate_item(candidate: dict, now: datetime) -> dict:
    return {
        "id": _safe_atom("candidate", candidate.get("id", "")),
        "type": "candidate",
        "status": _safe_atom("candidate_status", candidate.get("status", "candidate")),
        "kind": _safe_atom("candidate_kind", candidate.get("kind", "")),
        "title": _safe_text("candidate_title", candidate.get("summary", ""), limit=100),
        "summary": _safe_text("candidate_summary", candidate.get("summary", ""), limit=200),
        "pressure": candidate.get("pressure", 0),
        "age_hours": _age_hours(candidate.get("created_at"), now),
        "freshness": _freshness(candidate.get("updated_at") or candidate.get("created_at"), now),
        "sensitivity": _safe_atom("sensitivity", candidate.get("sensitivity", "private")),
        "allowed_surfaces": _safe_atom_list("surface", candidate.get("allowed_surfaces", [])),
        "source_refs": [],
        "allowed_decisions": list(CANDIDATE_DECISIONS),
        "created_at": candidate.get("created_at", ""),
        "updated_at": candidate.get("updated_at", ""),
    }


def _thread_item(thread: dict, now: datetime) -> dict:
    task = thread.get("conscious_task") or {}
    status = str(thread.get("status", "dormant") or "dormant")
    safe_status = status if status in THREAD_DECISIONS else _safe_atom("thread_status", status)
    continuity = thread.get("continuity_summary") or []
    summary = task.get("why", "") or (continuity[0] if continuity else "")
    return {
        "id": _safe_atom("thread", thread.get("id", "")),
        "type": "thread",
        "status": safe_status,
        "kind": _safe_atom("request_type", task.get("request_type", "")),
        "title": _safe_text("thread_title", task.get("title", ""), limit=100),
        "summary": _safe_text("thread_summary", summary, limit=200),
        "pressure": None,
        "age_hours": _age_hours(thread.get("created_at"), now),
        "freshness": _freshness(thread.get("updated_at") or thread.get("created_at"), now),
        "sensitivity": _safe_atom("sensitivity", thread.get("sensitivity", "private")),
        "allowed_surfaces": _safe_atom_list("surface", thread.get("allowed_surfaces", [])),
        "source_refs": _safe_atom_list("source_ref", thread.get("source_refs", [])),
        "origin_candidate_id": _safe_atom("candidate", thread.get("origin_candidate_id", "")),
        "hold_reason": _safe_text("hold_reason", thread.get("hold_reason", ""), limit=160),
        "resume_trigger": _safe_text("resume_trigger", thread.get("resume_trigger", ""), limit=160),
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
