"""Read-only Hermes dashboard API for Agent Sensorium.

Mounted by Hermes dashboard under /api/plugins/agent-sensorium/.
This deliberately reads compact JSONL state only and never mutates Sensorium state.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
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
METRICS_DIR = Path(os.environ.get("SENSORIUM_METRICS_DIR", Path.home() / ".hermes" / "ops" / "sensorium-metrics"))
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

_INSTANCE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_CANDIDATE_REF_LABEL_RE = re.compile(r"^candidate#[0-9a-f]{16}$")
_RECEIPT_EVIDENCE_REF_TYPES = {
    "candidate",
    "event",
    "fingerprint",
    "intake_task",
    "review_task",
    "reason",
    "correlation",
    "conscious_task",
}
_SAFE_HASH_LABEL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*#[0-9a-f]{16}$")
_SAFE_SURFACE_ATOM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$")
_SECRET_SHAPED_RE = re.compile(
    r"sk-|api[_-]?key|private[_-]?key|password|passwd|oauth|bearer|secret|token|raw[_ -]?transcript|raw[_ -]?log|do[_ -]?not[_ -]?leak",
    re.I,
)


def _resolve_instance(instance: str | None) -> tuple[str, Path] | None:
    """Resolve a request `instance` to (effective_instance, root), or None if invalid.

    Omitted instance and the legacy/default instance name both resolve to
    DEFAULT_ROOT. Any other instance must be a plain name (letters, digits,
    underscore, hyphen) — no path separators, dot-segments, blank/whitespace,
    or hidden/dot names — so it can't escape DEFAULT_ROOT.parent via traversal.
    """
    effective_instance = instance if instance is not None else DEFAULT_INSTANCE
    if effective_instance in {"default", DEFAULT_INSTANCE}:
        return effective_instance, DEFAULT_ROOT
    if not _INSTANCE_NAME_RE.fullmatch(effective_instance):
        return None
    return effective_instance, DEFAULT_ROOT.parent / effective_instance


async def _world_model_read(instance: str | None, request: dict[str, Any]) -> dict[str, Any]:
    """Serve a bounded configured World Model read without exposing setup details."""
    resolved = _resolve_instance(instance)
    if resolved is None:
        return {"ok": False, "degradation_state": "unavailable", "error": "world_model_unavailable"}
    _, root = resolved
    config = _read_json(root / "instance.config.json", {})
    allowed_surfaces = config.get("allowed_surfaces", ["local"]) if isinstance(config, dict) else []
    max_sensitivity = config.get("max_sensitivity", "private") if isinstance(config, dict) else "private"
    # The dashboard is a local surface. Never use this route to weaken an
    # instance's configured surface or sensitivity policy.
    if not isinstance(allowed_surfaces, list) or "local" not in allowed_surfaces or max_sensitivity not in {"private", "public_safe"}:
        return {"ok": False, "degradation_state": "unavailable", "error": "world_model_unavailable"}
    try:
        from agent_sensorium.world_model_dashboard import (
            WorldModelDashboardUnavailable,
            configured_world_model_service,
        )

        response = await configured_world_model_service(config).read_bounded(request)
        return {"ok": True, **response}
    except Exception:
        # Dashboard errors are deliberately non-diagnostic: provider paths,
        # configuration and source details remain private implementation state.
        return {"ok": False, "degradation_state": "unavailable", "error": "world_model_unavailable"}


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _truncate(value: Any, limit: int = 160) -> str:
    text = str(value or "").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _looks_secret_shaped(value: Any) -> bool:
    """Heuristic guard for hostile legacy/corrupt GET-projected strings."""
    return bool(_SECRET_SHAPED_RE.search(str(value or "")))


def _safe_surface_text(prefix: str, value: Any, *, limit: int = 160, atom_only: bool = False) -> str:
    """Project a scalar for dashboard GET output without echoing secret-shaped text.

    Existing compact dashboard rows legitimately carry short free-form summaries.
    Those remain visible unless a legacy/corrupt value looks like key/password
    material. Atom/classification fields additionally must be compact atoms; if
    not, they are replaced with deterministic opaque labels.
    """
    text = _truncate(value, limit)
    if not text:
        return ""
    if _SAFE_HASH_LABEL_RE.fullmatch(text):
        return text
    if _looks_secret_shaped(text) or (atom_only and not _SAFE_SURFACE_ATOM_RE.fullmatch(text)):
        return _opaque_join_label(prefix, value)
    return text


def _safe_surface_atom(prefix: str, value: Any, *, limit: int = 96) -> str:
    return _safe_surface_text(prefix, value, limit=limit, atom_only=True)


def _safe_surface_projection(prefix: str, value: Any, *, limit: int = 160) -> Any:
    """Recursively sanitize caller-controlled actuator metadata for GET output."""
    if isinstance(value, dict):
        projected: dict[str, Any] = {}
        for key, child in list(value.items())[:24]:
            projected[_safe_surface_atom(f"{prefix}_key", key)] = _safe_surface_projection(prefix, child, limit=limit)
        return projected
    if isinstance(value, list):
        return [_safe_surface_projection(prefix, child, limit=limit) for child in value[:24]]
    if value is None or isinstance(value, bool | int | float):
        return value
    return _safe_surface_text(prefix, value, limit=limit)


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(errors="ignore"))
    except Exception:
        return default


def _read_plain_jsonl(path: Path, limit: int | None = None) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(errors="ignore").splitlines()
    except Exception:
        return []
    if limit is not None:
        lines = lines[-limit:]
    rows: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except Exception:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _metrics_snapshot(limit: int = 144) -> dict[str, Any]:
    latest = _read_json(METRICS_DIR / "latest.json", {})
    series = _read_plain_jsonl(METRICS_DIR / "timeseries.jsonl", limit=limit)
    return {
        "ok": bool(latest),
        "dir": _safe_surface_text("metrics_dir", str(METRICS_DIR), limit=240),
        "latest_mtime": _mtime(METRICS_DIR / "latest.json"),
        "timeseries_path": _safe_surface_text("metrics_path", str(METRICS_DIR / "timeseries.jsonl"), limit=240),
        "series_count": len(series),
        "latest": _safe_surface_projection("metric", latest),
        "series": _safe_surface_projection("metric", series),
    }


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


def _mtime_ts(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except Exception:
        return None


def _source_info(path: Path, *, deprecated: bool = False, excluded_from_canonical: bool = False) -> dict[str, Any]:
    info: dict[str, Any] = {
        "path": _safe_surface_text("source_path", str(path), limit=240),
        "exists": path.exists(),
        "mtime": _mtime(path),
    }
    if deprecated:
        info["deprecated"] = True
    if excluded_from_canonical:
        info["excluded_from_canonical"] = True
    return info


def _tick_quiet_filename(root: Path) -> str:
    """Resolve the dashboard quiet-tick freshness filename from instance config."""
    try:
        from agent_sensorium.config import load_instance_config

        config, _ = load_instance_config(state_dir=str(root))
        name = config.get("tick_quiet_filename")
        if isinstance(name, str) and name.strip():
            return name.strip()
    except Exception:
        pass
    return "sensorium_tick_quiet.latest.json"


def _freshness_snapshot(root: Path) -> dict[str, Any]:
    kanban_root = root.parent / "kanban"
    tick_quiet_path = kanban_root / _tick_quiet_filename(root)
    jsonl = {name: _source_info(root / rel) for name, rel in STATE_NAMES.items()}
    sources = {
        "jsonl": jsonl,
        "last_sensorium_kanban_tick": _source_info(kanban_root / "last_sensorium_kanban_tick.json"),
        "sensor_kanban_state": _source_info(kanban_root / "sensor_kanban_state.json"),
        "tick_quiet_latest": _source_info(tick_quiet_path),
        "metrics_latest": _source_info(METRICS_DIR / "latest.json"),
        "legacy_state_latest": _source_info(
            root / "state.latest.json",
            deprecated=True,
            excluded_from_canonical=True,
        ),
    }
    canonical_paths = [root / rel for rel in STATE_NAMES.values()]
    canonical_paths.extend([
        kanban_root / "last_sensorium_kanban_tick.json",
        kanban_root / "sensor_kanban_state.json",
        tick_quiet_path,
        METRICS_DIR / "latest.json",
    ])
    latest_ts = max((ts for ts in (_mtime_ts(path) for path in canonical_paths) if ts is not None), default=None)
    sources["canonical_latest_mtime"] = (
        datetime.fromtimestamp(latest_ts, timezone.utc).astimezone().isoformat(timespec="seconds")
        if latest_ts is not None
        else None
    )
    sources["canonical_excludes"] = ["legacy_state_latest"]
    return sources


def _freshness_mtime(root: Path) -> str | None:
    """Return newest canonical Sensorium freshness mtime, excluding state.latest.

    `state.latest.json` is a legacy dispatcher snapshot and can stay unchanged
    while JSONL, Kanban tick receipts, quiet tick receipts, and metrics are
    current. Keep the legacy file visible separately, but never let it define
    dashboard freshness.
    """
    return _freshness_snapshot(root).get("canonical_latest_mtime")


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


def _safe_liveness_timestamp(value: Any) -> str | None:
    """Project only bounded offset-aware ISO-8601 timestamps onto GET output."""
    if not isinstance(value, str) or not 1 <= len(value) <= 40:
        return None
    try:
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None or not 1970 <= parsed.year <= 2100:
        return None
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _current_budgets(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    try:
        import sys

        plugin_root = Path(__file__).resolve().parents[1]
        if str(plugin_root) not in sys.path:
            sys.path.insert(0, str(plugin_root))
        from agent_sensorium.dispatcher import current_budget_state
        from agent_sensorium.store import SensoriumStore

        store = SensoriumStore(instance="dashboard", state_dir=str(root))
        return current_budget_state(store, config if isinstance(config, dict) else None)
    except Exception:
        # If the runtime package is not importable, avoid echoing stale expired
        # reset windows as healthy. The dashboard will show no budget snapshot
        # rather than replaying legacy state.latest indefinitely.
        return {}


def _live_turn_metrics(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        import sys

        plugin_root = Path(__file__).resolve().parents[1]
        if str(plugin_root) not in sys.path:
            sys.path.insert(0, str(plugin_root))
        from agent_sensorium.live_turn import live_turn_receipt_metrics

        return live_turn_receipt_metrics(decisions)
    except Exception:
        return {
            "receipt_type": "live_turn.ingest_decision",
            "receipt_count": 0,
            "ingested_count": 0,
            "skipped_count": 0,
            "error": "unavailable",
        }


def _live_turn_review_metrics(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        import sys

        plugin_root = Path(__file__).resolve().parents[1]
        if str(plugin_root) not in sys.path:
            sys.path.insert(0, str(plugin_root))
        from agent_sensorium.live_turn import live_turn_review_metrics

        return live_turn_review_metrics(decisions)
    except Exception:
        return {
            "receipt_type": "live_turn.review_decision",
            "receipt_count": 0,
            "pending_review_count": 0,
            "error": "unavailable",
        }


def _conscious_reachout_metrics(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        import sys

        plugin_root = Path(__file__).resolve().parents[1]
        if str(plugin_root) not in sys.path:
            sys.path.insert(0, str(plugin_root))
        from agent_sensorium.conscious_reachout import conscious_reachout_metrics

        return conscious_reachout_metrics(decisions)
    except Exception:
        return {
            "receipt_type": "conscious_reachout.decision",
            "receipt_count": 0,
            "prepared_count": 0,
            "authorized_count": 0,
            "delivered_count": 0,
            "blocked_count": 0,
            "error": "unavailable",
        }


def _sort_key(row: dict[str, Any]) -> str:
    return str(row.get("updated_at") or row.get("created_at") or row.get("ts") or "")


def _index_by_id(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(row.get("id")): row for row in rows if row.get("id")}


def _thread_title(thread: dict[str, Any]) -> str:
    raw_task = thread.get("conscious_task")
    task: dict[str, Any] = raw_task if isinstance(raw_task, dict) else {}
    return _truncate(task.get("title") or thread.get("title") or thread.get("id"), 140)


def _liveness_item(*, state: str, reason_code: str, observed_at: Any, source: str, actionable: bool, terminal: bool) -> dict[str, Any]:
    """Closed, bounded GET projection for reconciliation state."""
    allowed_states = {"active", "reviewing", "blocked", "held", "prepared", "settled", "stale", "error", "quiet", "unknown"}
    allowed_reasons = {
        "above_threshold_unrepresented", "candidate_below_threshold", "candidate_unknown_status",
        "reviewing_open_aperture", "stale_aperture", "candidate_held", "candidate_prepared",
        "candidate_settled", "historical_prepared_pointer", "outbox_prepared", "outbox_failed",
        "outbox_dispatched", "outbox_unknown_status",
        "thread_dormant", "thread_held", "thread_closed", "thread_archived", "thread_unknown_status",
        "action_proposed", "action_prepared", "action_offered", "action_acted", "action_closed",
        "action_expired", "action_cancelled", "action_rejected", "action_unknown_status",
    }
    return {
        "state": state if state in allowed_states else "unknown",
        "reason_code": reason_code if reason_code in allowed_reasons else "candidate_unknown_status",
        "observed_at": _safe_liveness_timestamp(observed_at),
        "source": source if source in {"candidate_status", "thread_status", "action_status", "outbox_lineage"} else "candidate_status",
        "actionable": bool(actionable), "terminal": bool(terminal), "related_refs": [],
    }


def _candidate_liveness(candidate: dict[str, Any]) -> dict[str, Any]:
    status = str(candidate.get("status") or "")
    observed_at = candidate.get("updated_at") or candidate.get("created_at")
    if status == "in_conscious_aperture":
        aperture = (candidate.get("conscious_aperture") if isinstance(candidate.get("conscious_aperture"), dict) else {}) or {}
        opened = _parse_dt(aperture.get("opened_at") or candidate.get("updated_at"))
        stale = aperture.get("state") == "stale" or opened is None or (datetime.now(timezone.utc) - opened).total_seconds() >= 180 * 60
        state = "stale" if stale else "reviewing"
        return _liveness_item(state=state, reason_code="stale_aperture" if stale else "reviewing_open_aperture", observed_at=observed_at, source="candidate_status", actionable=stale, terminal=False)
    if status == "candidate":
        try:
            above = float(candidate.get("pressure", 0) or 0) >= 0.5
        except (TypeError, ValueError):
            above = False
        return _liveness_item(state="active" if above else "quiet", reason_code="above_threshold_unrepresented" if above else "candidate_below_threshold", observed_at=observed_at, source="candidate_status", actionable=above, terminal=False)
    if status == "held":
        return _liveness_item(state="held", reason_code="candidate_held", observed_at=observed_at, source="candidate_status", actionable=False, terminal=False)
    if status == "prepared_external_work":
        return _liveness_item(state="prepared", reason_code="candidate_prepared", observed_at=observed_at, source="candidate_status", actionable=False, terminal=False)
    if status in {"reviewed", "suppressed", "cancelled", "archived"}:
        return _liveness_item(state="settled", reason_code="candidate_settled", observed_at=observed_at, source="candidate_status", actionable=False, terminal=True)
    return _liveness_item(state="unknown", reason_code="candidate_unknown_status", observed_at=observed_at, source="candidate_status", actionable=False, terminal=False)


def _thread_liveness(thread: dict[str, Any]) -> dict[str, Any]:
    status = str(thread.get("status") or "").lower()
    observed_at = thread.get("updated_at") or thread.get("created_at")
    mapped = {
        "dormant": ("active", "thread_dormant", True, False),
        "held": ("held", "thread_held", False, False),
        "closed": ("settled", "thread_closed", False, True),
        "archived": ("settled", "thread_archived", False, True),
    }.get(status, ("unknown", "thread_unknown_status", False, False))
    return _liveness_item(state=mapped[0], reason_code=mapped[1], observed_at=observed_at, source="thread_status", actionable=mapped[2], terminal=mapped[3])


def _action_liveness(action: dict[str, Any]) -> dict[str, Any]:
    status = str(action.get("status") or "").lower()
    observed_at = action.get("updated_at") or action.get("ts")
    mapped = {
        "proposed": ("active", "action_proposed", True, False),
        "prepared": ("prepared", "action_prepared", True, False),
        "offered": ("held", "action_offered", True, False),
        "acted": ("settled", "action_acted", False, True),
        "closed": ("settled", "action_closed", False, True),
        "expired": ("settled", "action_expired", False, True),
        "cancelled": ("settled", "action_cancelled", False, True),
        "rejected": ("settled", "action_rejected", False, True),
    }.get(status, ("unknown", "action_unknown_status", False, False))
    return _liveness_item(state=mapped[0], reason_code=mapped[1], observed_at=observed_at, source="action_status", actionable=mapped[2], terminal=mapped[3])


def _thread_item(thread: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _safe_surface_atom("thread", thread.get("id")),
        "status": _safe_surface_atom("thread_status", thread.get("status") or "dormant"),
        "title": _safe_surface_text("thread_title", _thread_title(thread), limit=140),
        "origin_candidate_id": _safe_surface_atom("candidate", thread.get("origin_candidate_id")),
        "sensitivity": _safe_surface_atom("sensitivity", thread.get("sensitivity")),
        "allowed_surfaces": [_safe_surface_atom("surface", s) for s in (thread.get("allowed_surfaces") or [])][:8],
        "created_at": thread.get("created_at"),
        "updated_at": thread.get("updated_at"),
        "expires_at": thread.get("expires_at"),
        "pinned": bool(thread.get("pinned")),
        "dirty": bool(thread.get("dirty_since")),
        "interaction_refs": len(thread.get("interaction_refs") or []),
        "liveness": _thread_liveness(thread),
    }


def _candidate_item(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _safe_trace_candidate_id(candidate.get("id")),
        "status": _safe_surface_atom("candidate_status", candidate.get("status")),
        "kind": _safe_surface_atom("candidate_kind", candidate.get("kind")),
        "pressure": candidate.get("pressure"),
        "summary": _safe_surface_text("candidate_summary", candidate.get("summary"), limit=180),
        "sensitivity": _safe_surface_atom("sensitivity", candidate.get("sensitivity")),
        "allowed_surfaces": [_safe_surface_atom("surface", s) for s in (candidate.get("allowed_surfaces") or [])][:8],
        "updated_at": candidate.get("updated_at") or candidate.get("created_at"),
        "liveness": _candidate_liveness(candidate),
    }


def _signal_item(signal: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _safe_trace_ref("signal", signal.get("id")),
        "sensor": _safe_surface_atom("sensor", signal.get("sensor")),
        "source": _safe_surface_atom("source", signal.get("source")),
        "kind": _safe_surface_atom("signal_kind", signal.get("kind")),
        "summary": _safe_surface_text("signal_summary", signal.get("summary"), limit=180),
        "strength_hint": signal.get("strength_hint"),
        "pressure_level": _safe_surface_atom("pressure_level", signal.get("pressure_level")),
        "transition": _safe_surface_atom("transition", signal.get("transition")),
        "correlation_keys": _safe_trace_ref_list("correlation", signal.get("correlation_keys") or [], limit=8),
        "sensitivity": _safe_surface_atom("sensitivity", signal.get("sensitivity")),
        "allowed_surfaces": [_safe_surface_atom("surface", s) for s in (signal.get("allowed_surfaces") or [])][:8],
        "ts": signal.get("ts") or signal.get("created_at") or signal.get("updated_at"),
    }


def _action_attachment_ids(action: dict[str, Any], kind: str) -> list[str]:
    refs: list[str] = []
    for attachment in action.get("attachments") or []:
        if isinstance(attachment, dict) and attachment.get("kind") == kind and attachment.get("ref_id"):
            refs.append(str(attachment.get("ref_id")))
    return refs


def _action_item(action: dict[str, Any]) -> dict[str, Any]:
    attachments = [a for a in action.get("attachments") or [] if isinstance(a, dict)]
    attachment_kinds = Counter(_safe_surface_atom("attachment_kind", a.get("kind") or "unknown") for a in attachments)
    return {
        "id": _safe_surface_atom("action", action.get("id")),
        "status": _safe_surface_atom("action_status", action.get("status")),
        "outcome": _safe_surface_atom("action_outcome", action.get("outcome")),
        "intent": _safe_surface_atom("action_intent", action.get("intent")),
        "title": _safe_surface_text("action_title", action.get("title") or action.get("intent"), limit=140),
        "summary": _safe_surface_text("action_summary", action.get("summary"), limit=180),
        "origin_thread_id": _safe_surface_atom("thread", action.get("origin_thread_id")),
        "origin_candidate_id": _safe_surface_atom("candidate", action.get("origin_candidate_id")),
        "artifact_refs": [_safe_surface_atom("artifact", ref) for ref in _action_attachment_ids(action, "artifact_ref")],
        "outbox_refs": [_safe_surface_atom("outbox", ref) for ref in _action_attachment_ids(action, "outbox_request")],
        "attachment_count": len(attachments),
        "attachment_kinds": dict(attachment_kinds),
        "result_summary": _safe_surface_text("action_result", action.get("result_summary"), limit=180),
        "updated_at": action.get("updated_at") or action.get("ts"),
        "liveness": _action_liveness(action),
    }


def _verify_artifact_file(ref_path: str, kind: str) -> dict[str, Any]:
    if kind not in {"text", "image"}:
        return {"status": "UNVERIFIED", "error_details": None}
    if not ref_path:
        return {"status": "UNVERIFIED", "error_details": None}

    # Path traversal and approved directory check
    # Check for parent directory components ("..")
    path_parts = Path(ref_path).parts
    if ".." in path_parts or ".." in ref_path:
        return {"status": "UNVERIFIED", "error_details": "security_warning"}

    try:
        resolved_path = Path(ref_path).resolve()
    except Exception:
        return {"status": "UNVERIFIED", "error_details": "security_warning"}

    # Must be located strictly within approved directories (e.g. /home/entity/.hermes/ or project workspace)
    allowed_prefixes = [
        Path("/home/entity/.hermes").resolve(),
        Path("/home/entity/.gemini/antigravity-cli/scratch").resolve(),
        Path("/tmp").resolve(),
    ]
    is_allowed = False
    for prefix in allowed_prefixes:
        try:
            resolved_path.relative_to(prefix)
            is_allowed = True
            break
        except ValueError:
            continue
    if not is_allowed:
        return {"status": "UNVERIFIED", "error_details": "security_warning"}

    # File existence check
    if not resolved_path.is_file():
        return {"status": "MISSING_FILE", "error_details": "file_not_found"}

    # Read compliance check
    try:
        with open(resolved_path, "r", encoding="utf-8", errors="ignore") as f:
            line1 = f.readline()
            line2 = f.readline()
        
        # Strip trailing newlines and whitespace
        line1 = line1.strip()
        line2 = line2.strip()
        
        # Check first line has STATUS: and second line has ARTIFACT:
        if line1.startswith("STATUS:") and line2.startswith("ARTIFACT:"):
            return {"status": "VERIFIED_COMPLIANT", "error_details": None}
        else:
            return {"status": "NONCOMPLIANT", "error_details": "missing_status_marker"}
    except Exception:
        # Catch concurrent write/read lock exceptions as UNVERIFIED to handle race conditions
        return {"status": "UNVERIFIED", "error_details": "read_error"}


def _artifact_item(artifact: dict[str, Any]) -> dict[str, Any]:
    raw_source_refs = artifact.get("source_refs")
    source_refs: dict[str, Any] = raw_source_refs if isinstance(raw_source_refs, dict) else {}
    ref_path = str(artifact.get("ref_path") or "")
    verification = _verify_artifact_file(ref_path, str(artifact.get("kind") or ""))
    return {
        "id": _safe_surface_atom("artifact", artifact.get("id")),
        "kind": _safe_surface_atom("artifact_kind", artifact.get("kind")),
        "status": _safe_surface_atom("artifact_status", artifact.get("status")),
        "delivery_state": _safe_surface_atom("delivery_state", artifact.get("delivery_state")),
        "handoff_mode": _safe_surface_atom("handoff_mode", artifact.get("intended_handoff_mode")),
        "ref_name": _safe_surface_text("artifact_ref_name", Path(ref_path).name if ref_path else "", limit=120),
        "ref_path": _safe_surface_text("artifact_ref_path", ref_path, limit=180),
        "why_created": _safe_surface_text("artifact_why", artifact.get("why_created"), limit=180),
        "thread_id": _safe_surface_atom("thread", source_refs.get("thread_id")),
        "candidate_id": _safe_surface_atom("candidate", source_refs.get("candidate_id")),
        "action_id": _safe_surface_atom("action", source_refs.get("action_id")),
        "sensitivity": _safe_surface_atom("sensitivity", artifact.get("sensitivity") or artifact.get("privacy")),
        "allowed_surfaces": [_safe_surface_atom("surface", s) for s in (artifact.get("allowed_surfaces") or [])][:8],
        "updated_at": artifact.get("updated_at") or artifact.get("ts"),
        "verification": verification,
    }


def _artifact_group_key(artifact: dict[str, Any]) -> tuple[str, str]:
    raw_source_refs = artifact.get("source_refs")
    source_refs: dict[str, Any] = raw_source_refs if isinstance(raw_source_refs, dict) else {}
    raw_provenance = artifact.get("provenance")
    provenance: dict[str, Any] = raw_provenance if isinstance(raw_provenance, dict) else {}

    for group_type, value in (
        ("action", source_refs.get("action_id")),
        ("thread", source_refs.get("thread_id")),
        ("candidate", source_refs.get("candidate_id")),
        ("task", provenance.get("task_id")),
        ("task", provenance.get("kanban_task")),
        ("fingerprint", provenance.get("fingerprint")),
    ):
        if value:
            return group_type, str(value)

    ref_path = str(artifact.get("ref_path") or "")
    if ref_path:
        return "ref", ref_path
    return "artifact", str(artifact.get("id") or "unknown")


def _artifact_group_title(
    group_type: str,
    group_id: str,
    items: list[dict[str, Any]],
    *,
    thread_by_id: dict[str, dict[str, Any]],
    action_by_id: dict[str, dict[str, Any]],
    candidate_by_id: dict[str, dict[str, Any]],
) -> str:
    if group_type == "action":
        action = action_by_id.get(group_id, {})
        return _safe_surface_text("artifact_group_title", action.get("title") or action.get("intent") or f"Action {group_id}", limit=120)
    if group_type == "thread":
        thread = thread_by_id.get(group_id, {})
        return _safe_surface_text("artifact_group_title", _thread_title(thread) if thread else f"Thread {group_id}", limit=120)
    if group_type == "candidate":
        candidate = candidate_by_id.get(group_id, {})
        return _safe_surface_text("artifact_group_title", candidate.get("summary") or f"Candidate {group_id}", limit=120)
    if group_type == "task":
        return _safe_surface_text("artifact_group_title", f"Kanban task {group_id}", limit=120)
    if group_type == "fingerprint":
        return _safe_surface_text("artifact_group_title", f"Signal lineage {group_id}", limit=120)
    if group_type == "ref":
        return _safe_surface_text("artifact_group_title", Path(group_id).name, limit=120)
    first = items[0] if items else {}
    return _safe_surface_text("artifact_group_title", first.get("why_created") or first.get("id") or "Artifact group", limit=120)


def _artifact_groups(
    artifacts: list[dict[str, Any]],
    *,
    thread_by_id: dict[str, dict[str, Any]],
    action_by_id: dict[str, dict[str, Any]],
    candidate_by_id: dict[str, dict[str, Any]],
    limit: int = 10,
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for artifact in artifacts:
        grouped.setdefault(_artifact_group_key(artifact), []).append(artifact)

    groups: list[dict[str, Any]] = []
    for (group_type, group_id), rows in grouped.items():
        rows.sort(key=_sort_key, reverse=True)
        item_cards = [_artifact_item(row) for row in rows]
        delivery_states = Counter(_safe_surface_atom("delivery_state", row.get("delivery_state") or "unknown") for row in rows)
        kinds = Counter(_safe_surface_atom("artifact_kind", row.get("kind") or "unknown") for row in rows)
        latest = rows[0] if rows else {}
        safe_group_id = _safe_surface_atom(group_type, group_id)
        groups.append(
            {
                "id": f"{group_type}:{safe_group_id}",
                "group_type": _safe_surface_atom("artifact_group_type", group_type),
                "group_id": safe_group_id,
                "title": _artifact_group_title(
                    group_type,
                    group_id,
                    rows,
                    thread_by_id=thread_by_id,
                    action_by_id=action_by_id,
                    candidate_by_id=candidate_by_id,
                ),
                "count": len(rows),
                "kinds": dict(kinds),
                "delivery_states": dict(delivery_states),
                "held_count": delivery_states.get("held_for_review", 0),
                "prepared_count": delivery_states.get("prepared", 0),
                "latest_updated_at": latest.get("updated_at") or latest.get("ts"),
                "items": item_cards[:5],
            }
        )
    groups.sort(key=lambda g: str(g.get("latest_updated_at") or ""), reverse=True)
    return groups[:limit]


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
        "origin_thread_status": _safe_surface_atom("thread_status", thread_status),
        "attached_action_id": _safe_surface_atom("action", action_id),
        "attached_action_status": _safe_surface_atom("action_status", action_status),
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
                dispatch_requires_execute=False,
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
        _safe_surface_atom("target_key", k): _safe_surface_projection("target", target[k], limit=120)
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
        "id": _safe_surface_atom("outbox", req.get("id")),
        "status": _safe_surface_atom("outbox_status", req.get("status")),
        "origin_thread_id": _safe_surface_atom("thread", req.get("origin_thread_id")),
        "origin_candidate_id": _safe_surface_atom("candidate", req.get("origin_candidate_id")),
        "request_type": _safe_surface_atom("request_type", req.get("request_type")),
        "surface": _safe_surface_atom("surface", req.get("surface")),
        "delivery_mode": _safe_surface_atom("delivery_mode", req.get("delivery_mode")),
        "title": _safe_surface_text("outbox_title", req.get("title"), limit=120),
        "message_preview": _safe_surface_text("message_preview", req.get("message_preview"), limit=180),
        "target": target_keys,
        "media_refs": [_safe_surface_atom("media_ref", ref) for ref in (req.get("media_refs") or [])][:12],
        "platform_refs": _safe_surface_projection("platform_ref", req.get("platform_refs") or {}, limit=160),
        "created_at": req.get("created_at"),
        "updated_at": req.get("updated_at"),
        "safety": safety,
        "liveness": _liveness_item(
            state=("settled" if safety.get("label") == "historical_prepared_pointer" else {"prepared": "prepared", "failed": "error", "dispatched": "settled"}.get(str(req.get("status") or "").lower(), "unknown")),
            reason_code=("historical_prepared_pointer" if safety.get("label") == "historical_prepared_pointer" else {"prepared": "outbox_prepared", "failed": "outbox_failed", "dispatched": "outbox_dispatched"}.get(str(req.get("status") or "").lower(), "outbox_unknown_status")),
            observed_at=req.get("updated_at") or req.get("created_at"), source="outbox_lineage",
            actionable=bool(safety.get("actionable")), terminal=safety.get("label") == "historical_prepared_pointer" or str(req.get("status") or "").lower() == "dispatched",
        ),
    }


def _outbox_counts_as_open(req: dict[str, Any], *, safety: dict[str, Any] | None = None) -> bool:
    """Human-facing open/outbox counters exclude settled historical pointers.

    Preserve raw receipts in `outbox` and status breakdowns, but do not let a
    prepared context-pointer attached to a completed action on a terminal thread
    inflate live/open dashboard pressure forever.
    """
    status = str(req.get("status") or "").strip().lower()
    if status not in OPEN_OUTBOX_STATUSES:
        return False
    safety = safety or {}
    return safety.get("label") != "historical_prepared_pointer"


def _outbox_counts_as_prepared(req: dict[str, Any], *, safety: dict[str, Any] | None = None) -> bool:
    status = str(req.get("status") or "").strip().lower()
    if status != "prepared":
        return False
    safety = safety or {}
    return safety.get("label") != "historical_prepared_pointer"


def _decision_item(decision: dict[str, Any]) -> dict[str, Any]:
    return {
        "ts": decision.get("ts"),
        "created_at": decision.get("created_at") or decision.get("ts"),
        "type": _safe_surface_atom("decision_type", decision.get("type")),
        "schema": _safe_surface_atom("schema", decision.get("schema")),
        "receipt_kind": _safe_surface_atom("receipt_kind", decision.get("receipt_kind")),
        "subject_ref": _safe_receipt_subject_ref(decision),
        "decision": _safe_surface_atom("decision", decision.get("decision")),
        "outcome": _safe_surface_atom("outcome", decision.get("outcome")),
        "thread_id": _safe_decision_join_field(decision, "thread_id", "thread"),
        "candidate_id": _safe_decision_candidate_id(decision),
        "outbox_id": _safe_decision_join_field(decision, "outbox_id", "outbox"),
        "action_id": _safe_decision_join_field(decision, "action_id", "action"),
        "artifact_id": _safe_decision_join_field(decision, "artifact_id", "artifact"),
        "action": _safe_surface_atom("decision_action", decision.get("action")),
        "reason": _safe_decision_reason(decision),
    }


def _settlement_label_helpers():
    """Import the settlement module's opaque-label helpers for cross-module joins.

    Receipts persist `candidate_id`/`subject_ref.id` as deterministic hash labels
    (see `agent_sensorium.settlement.candidate_ref_label`); the dashboard must use
    the same function to re-attach candidate metadata without ever echoing a raw
    candidate id into graph/perception-trace output.
    """
    import sys

    plugin_root = Path(__file__).resolve().parents[1]
    if str(plugin_root) not in sys.path:
        sys.path.insert(0, str(plugin_root))
    from agent_sensorium.settlement import candidate_ref_label, evidence_ref_label, safe_candidate_kind_label

    return candidate_ref_label, safe_candidate_kind_label, evidence_ref_label


def _opaque_join_label(prefix: str, value: Any) -> str:
    """Hash-label a join scalar for GET output; empty input stays empty.

    Used for any id-like settlement field (intake/review task ids, conscious
    task/thread ids, …) that the dashboard projects into a privacy-safe GET
    response — these can originate from Kanban task text/comments or compact
    state and must never be echoed raw.
    """
    text = str(value or "")
    if not text:
        return ""
    _, _, evidence_ref_label = _settlement_label_helpers()
    return evidence_ref_label(prefix, text)


def _is_receipt_evidence_label(ref_type: str, value: Any) -> bool:
    text = str(value or "")
    return bool(re.fullmatch(rf"{re.escape(ref_type)}#[0-9a-f]{{16}}", text))


def _safe_receipt_evidence_refs(receipt: dict[str, Any]) -> list[dict[str, str]]:
    """Fail-closed projection of receipt evidence refs for GET /graph.

    Older/corrupt receipt rows can carry raw candidate/event/fingerprint/task
    scalars even with raw_content=false. Trust only the closed receipt evidence
    vocabulary and demonstrable hash labels; hash-label anything else and drop
    malformed refs rather than echoing persisted raw material.
    """
    refs = receipt.get("evidence_refs")
    if not isinstance(refs, list):
        return []
    _, _, evidence_ref_label = _settlement_label_helpers()
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        ref_type = str(ref.get("type") or "").strip()
        if ref_type not in _RECEIPT_EVIDENCE_REF_TYPES:
            continue
        raw_ref = ref.get("ref")
        ref_text = str(raw_ref or "").strip()
        if not ref_text:
            continue
        safe_ref = ref_text if _is_receipt_evidence_label(ref_type, ref_text) else evidence_ref_label(ref_type, raw_ref)
        key = (ref_type, safe_ref)
        if key in seen:
            continue
        seen.add(key)
        out.append({"type": ref_type, "ref": safe_ref})
    return out[:24]


def _safe_decision_reason(decision: dict[str, Any]) -> str:
    """Privacy-safe reason/detail/error label for GET-exposed decision rows."""
    for key in ("reason_label", "detail_label", "error_label"):
        label = decision.get(key)
        if label:
            prefix = key.removesuffix("_label")
            if _is_receipt_evidence_label(prefix, label) or _SAFE_HASH_LABEL_RE.fullmatch(str(label)):
                return _truncate(label, 140)
            return _truncate(_opaque_join_label(prefix, label), 140)
    for key in ("reason", "detail", "error"):
        raw = decision.get(key)
        if raw:
            return _truncate(_opaque_join_label(key, raw), 140)
    return ""


def _is_settlement_receipt(decision: dict[str, Any]) -> bool:
    if decision.get("type") in SETTLEMENT_RECEIPT_TYPES:
        return True
    return (
        decision.get("schema") == "sensorium.decision_receipt.v1"
        and decision.get("receipt_kind") == "settlement"
    )


def _safe_decision_candidate_id(decision: dict[str, Any]) -> Any:
    """Privacy-safe candidate id projection for GET-exposed decision rows.

    Legacy settlement receipts may still carry raw `candidate_id` even though new
    receipts use an opaque `subject_ref.id`. For receipt rows, never echo the raw
    join scalar: trust only a demonstrable candidate hash label, otherwise derive
    the same opaque candidate label used by the receipt writer.
    """
    raw = decision.get("candidate_id")
    if not _is_settlement_receipt(decision):
        return raw
    text = str(raw or "")
    if text:
        if _CANDIDATE_REF_LABEL_RE.fullmatch(text):
            return text
        candidate_ref_label, _, _ = _settlement_label_helpers()
        return candidate_ref_label(text)
    return _receipt_subject_label(decision) or None


def _safe_decision_join_field(decision: dict[str, Any], key: str, prefix: str) -> Any:
    """Hash-label id-like receipt fields before exposing them through /snapshot."""
    raw = decision.get(key)
    if not raw or not _is_settlement_receipt(decision):
        return raw
    return _opaque_join_label(prefix, raw)


def _safe_trace_reason(meta: dict[str, Any], latest_applied: dict[str, Any]) -> str:
    """Privacy-safe settlement reason for GET-exposed perception traces.

    New normalized receipts/candidate metadata carry `reason_label`, which is
    already a deterministic opaque label. Legacy rows may still carry raw
    `reason` prose copied from Kanban comments, transcripts, logs, or secrets;
    `/snapshot` must hash-label that fallback instead of echoing it.
    """
    reason_label = meta.get("reason_label") or latest_applied.get("reason_label")
    if reason_label:
        if _is_receipt_evidence_label("reason", reason_label) or _SAFE_HASH_LABEL_RE.fullmatch(str(reason_label)):
            return _truncate(reason_label, 200)
        return _truncate(_opaque_join_label("reason", reason_label), 200)
    raw_reason = meta.get("reason") or latest_applied.get("reason")
    if not raw_reason:
        return ""
    return _truncate(_opaque_join_label("reason", raw_reason), 200)


def _candidate_label_indexes(
    candidates: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Build candidate-by-label and label-to-raw-id indexes for receipt joins.

    Returns ({label: candidate_row}, {label: raw_candidate_id}).
    """
    candidate_ref_label, _, _ = _settlement_label_helpers()
    by_label: dict[str, dict[str, Any]] = {}
    label_to_id: dict[str, str] = {}
    for candidate in candidates:
        cand_id = str(candidate.get("id") or "")
        if not cand_id:
            continue
        label = candidate_ref_label(cand_id)
        by_label[label] = candidate
        label_to_id[label] = cand_id
    return by_label, label_to_id


def _receipt_subject_label(receipt: dict[str, Any]) -> str:
    """Opaque candidate label for a settlement receipt, new- or legacy-format.

    New-format receipts carry `candidate#<sha16>` in `subject_ref.id`. Legacy or
    corrupt rows may carry a raw subject id there, or in the older `candidate_id`
    field; every non-demonstrably-opaque value is hash-labeled before it can be
    used as a join key or projected into GET output.
    """
    subject_ref = receipt.get("subject_ref") if isinstance(receipt.get("subject_ref"), dict) else {}
    candidate_ref_label, _, _ = _settlement_label_helpers()
    label = str(subject_ref.get("id") or "")
    if label:
        if _CANDIDATE_REF_LABEL_RE.fullmatch(label):
            return label
        return candidate_ref_label(label)
    legacy_candidate_id = str(receipt.get("candidate_id") or "")
    if legacy_candidate_id:
        return candidate_ref_label(legacy_candidate_id)
    return ""


def _receipt_subject_type(receipt: dict[str, Any]) -> str:
    """Closed-vocabulary subject-ref type for GET output.

    `subject_ref.type` is persisted state and can be corrupt or secret-shaped, so
    do not echo it unless it is a known public discriminator.
    """
    subject_ref = receipt.get("subject_ref") if isinstance(receipt.get("subject_ref"), dict) else {}
    raw_type = str(subject_ref.get("type") or "candidate").strip()
    return raw_type if raw_type == "candidate" else "unknown"


def _safe_receipt_subject_ref(receipt: dict[str, Any]) -> dict[str, str] | None:
    subject_id = _receipt_subject_label(receipt)
    if not subject_id:
        return None
    return {"type": _receipt_subject_type(receipt), "id": subject_id}


_SAFE_TRACE_REF_RE = re.compile(r"^[a-z][a-z0-9_:-]{0,79}$")
_SAFE_TRACE_CANDIDATE_RE = re.compile(r"^cand_[A-Za-z0-9_-]{1,75}$")
_SAFE_TRACE_EVENT_RE = re.compile(r"^(evt|event)_[A-Za-z0-9_-]{1,74}$")
_SAFE_TRACE_SIGNAL_RE = re.compile(r"^(sig|signal)_[A-Za-z0-9_-]{1,74}$")


def _has_private_trace_marker(text: str) -> bool:
    lowered = text.lower()
    private_markers = (
        "...",
        "secret",
        "token",
        "password",
        "passwd",
        "api_key",
        "apikey",
        "private_key",
        "privatekey",
    )
    return text.startswith(("sk-", "***")) or any(marker in lowered for marker in private_markers)


def _is_safe_trace_ref(prefix: str, value: Any) -> bool:
    """Return true only for compact dashboard refs safe to echo verbatim."""
    text = str(value or "")
    if not text or _has_private_trace_marker(text):
        return False
    if prefix == "candidate":
        return bool(_SAFE_TRACE_CANDIDATE_RE.fullmatch(text))
    if prefix == "event":
        return bool(_SAFE_TRACE_EVENT_RE.fullmatch(text))
    if prefix == "signal":
        return bool(_SAFE_TRACE_SIGNAL_RE.fullmatch(text))
    if prefix == "correlation":
        return bool(_SAFE_TRACE_REF_RE.fullmatch(text))
    return bool(_SAFE_TRACE_REF_RE.fullmatch(text))


def _safe_trace_candidate_id(value: Any) -> str:
    """Privacy-safe candidate identifier for GET /snapshot perception traces."""
    text = str(value or "")
    if not text:
        return ""
    if _is_safe_trace_ref("candidate", text):
        return text
    candidate_ref_label, _, _ = _settlement_label_helpers()
    return candidate_ref_label(text)


def _safe_trace_ref(prefix: str, value: Any) -> str:
    """Privacy-safe lineage reference for GET /snapshot perception traces."""
    text = str(value or "")
    if not text:
        return ""
    if _looks_secret_shaped(text):
        return _opaque_join_label(prefix, text)
    if _is_safe_trace_ref(prefix, text):
        return text
    return _opaque_join_label(prefix, text)


def _safe_trace_ref_list(prefix: str, values: Any, *, limit: int) -> list[str]:
    if not isinstance(values, list):
        return []
    out: list[str] = []
    for value in values:
        label = _safe_trace_ref(prefix, value)
        if label:
            out.append(label)
        if len(out) >= limit:
            break
    return out


def _receipt_node_id(receipt: dict[str, Any]) -> str:
    idem = str(receipt.get("idempotency_key") or "")
    if idem:
        import hashlib

        return "receipt:" + hashlib.sha256(idem.encode("utf-8", errors="ignore")).hexdigest()[:24]
    seed = json.dumps(receipt, sort_keys=True, default=str, separators=(",", ":"))
    import hashlib

    return "receipt:" + hashlib.sha256(seed.encode("utf-8", errors="ignore")).hexdigest()[:24]


def _receipt_graph(root: Path, *, limit_nodes: int = 200, limit_edges: int = 300) -> dict[str, Any]:
    """Project compact settlement receipts into a read-only graph fragment.

    T8 receipt normalization uses `decisions.jsonl` as the authoritative receipt
    store. This projection adds receipt nodes and `settles` edges without
    exposing raw reason/prose/log content; evidence refs are normalized hash
    labels from the receipt writer.
    """
    limit_nodes = max(1, min(int(limit_nodes or 200), 500))
    limit_edges = max(1, min(int(limit_edges or 300), 1000))
    candidates, _ = _read_jsonl(root, "candidates", limit=5000)
    decisions, _ = _read_jsonl(root, "decisions", limit=5000)
    candidate_by_label, _ = _candidate_label_indexes(candidates)
    _, safe_candidate_kind_label, _ = _settlement_label_helpers()
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    seen_nodes: set[str] = set()

    def add_node(node: dict[str, Any]) -> None:
        node_id = str(node.get("id") or "")
        if not node_id or node_id in seen_nodes or len(nodes) >= limit_nodes:
            return
        seen_nodes.add(node_id)
        nodes.append(node)

    receipt_rows = [
        row for row in decisions
        if isinstance(row, dict) and row.get("type") in SETTLEMENT_RECEIPT_TYPES
    ]
    for receipt in receipt_rows[-limit_nodes:]:
        # `subject_label` is always an opaque hash label, never a raw candidate
        # id: `_receipt_subject_label` trusts only demonstrable candidate hash
        # labels and hashes any raw/corrupt subject_ref.id or legacy
        # `candidate_id` before either can become a node id or output field.
        subject_label = _receipt_subject_label(receipt)
        if subject_label:
            candidate = candidate_by_label.get(subject_label, {})
            add_node(
                {
                    "id": f"candidate:{subject_label}",
                    "kind": "candidate",
                    "status": _safe_surface_atom("candidate_status", candidate.get("status") or "unknown"),
                    "candidate_kind": safe_candidate_kind_label(candidate.get("kind")),
                    "sensitivity": _safe_surface_atom(
                        "sensitivity", candidate.get("sensitivity") or receipt.get("sensitivity") or "private"
                    ),
                    "allowed_surfaces": [
                        _safe_surface_atom("surface", surface)
                        for surface in (candidate.get("allowed_surfaces") or receipt.get("allowed_surfaces") or ["local"])
                    ][:8],
                }
            )
        receipt_id = _receipt_node_id(receipt)
        add_node(
            {
                "id": receipt_id,
                "kind": "receipt",
                "receipt_kind": _safe_surface_atom("receipt_kind", receipt.get("receipt_kind") or "settlement"),
                "receipt_type": _safe_surface_atom("receipt_type", receipt.get("type")),
                "schema": _safe_surface_atom("schema", receipt.get("schema")),
                "subject_ref": _safe_receipt_subject_ref(receipt),
                "decision": _safe_surface_atom("decision", receipt.get("decision")),
                "outcome": _safe_surface_atom("outcome", receipt.get("outcome") or receipt.get("new_status")),
                "created_at": receipt.get("created_at") or receipt.get("ts"),
                "surface": _safe_surface_atom("surface", receipt.get("surface") or "kanban"),
                "sensitivity": _safe_surface_atom("sensitivity", receipt.get("sensitivity") or "private"),
                "allowed_surfaces": [
                    _safe_surface_atom("surface", surface) for surface in (receipt.get("allowed_surfaces") or ["local"])
                ][:8],
                "evidence_refs": _safe_receipt_evidence_refs(receipt),
            }
        )
        if subject_label and len(edges) < limit_edges:
            edges.append(
                {
                    "id": f"edge:{subject_label}:{receipt_id}",
                    "kind": "settles",
                    "from": f"candidate:{subject_label}",
                    "to": receipt_id,
                    "receipt_type": receipt.get("type"),
                }
            )
    return {
        "nodes": nodes,
        "edges": edges,
        "truncated": len(receipt_rows) > limit_nodes or len(edges) >= limit_edges,
    }


PERCEPTION_TRACE_LIMIT = 8
SETTLEMENT_RECEIPT_TYPES = {
    "kanban.settlement.applied",
    "kanban.settlement.unresolved",
    "kanban.settlement.no_candidate_match",
}
DISPATCH_PRESSURE_THRESHOLD = 0.5
DROP_CANDIDATE_STATUSES = {"suppressed", "dropped"}


def _trace_event_item(event: dict[str, Any]) -> dict[str, Any]:
    signal_ids = _safe_trace_ref_list("signal", event.get("source_signal_ids") or [], limit=6)
    return {
        "id": _safe_trace_ref("event", event.get("id")),
        "kind": _safe_surface_atom("event_kind", event.get("kind")),
        "summary": _safe_surface_text("event_summary", event.get("summary"), limit=160),
        "strength": event.get("strength"),
        "signal_ids": signal_ids,
        "ts": event.get("ts") or event.get("created_at"),
    }


def _trace_signal_item(signal: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _safe_trace_ref("signal", signal.get("id")),
        "kind": _safe_surface_atom("signal_kind", signal.get("kind")),
        "sensor": _safe_surface_atom("sensor", signal.get("sensor")),
        "summary": _safe_surface_text("signal_summary", signal.get("summary"), limit=160),
        "strength_hint": signal.get("strength_hint"),
        "pressure_level": _safe_surface_atom("pressure_level", signal.get("pressure_level")),
        "transition": _safe_surface_atom("transition", signal.get("transition")),
        "ts": signal.get("ts") or signal.get("created_at"),
    }


def _trace_settlement(candidate: dict[str, Any], receipts: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Compact Kanban settlement view from candidate truth plus decision receipts.

    The candidate's own ``kanban_settlement`` is authoritative; settlement
    receipts from ``decisions.jsonl`` backfill any fields missing on the
    candidate and surface unresolved/no-match attempts (the literal
    "interpretation went wrong" case). Reason labels are surfaced when already
    normalized; legacy raw reasons are hash-labeled here because `/snapshot` is
    a GET-exposed privacy-safe surface.
    """
    raw_meta = candidate.get("kanban_settlement")
    meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
    applied = [r for r in receipts if r.get("type") == "kanban.settlement.applied"]
    unresolved = [
        r for r in receipts
        if r.get("type") in {"kanban.settlement.unresolved", "kanban.settlement.no_candidate_match"}
    ]
    latest_applied = applied[-1] if applied else {}
    if not meta and not applied and not unresolved:
        return None
    raw_ref = meta.get("conscious_task_ref")
    ref: dict[str, Any] = raw_ref if isinstance(raw_ref, dict) else {}
    # `meta` (the candidate's own internal `kanban_settlement` block) and
    # `latest_applied` (a `decisions.jsonl` receipt) can both still carry raw
    # id-like join scalars (intake/review task ids, conscious task/thread ids)
    # — `meta` because it is internal storage, `latest_applied` for legacy rows
    # written before receipt normalization hashed these fields. `/snapshot` is a
    # GET-exposed privacy-safe surface, so every one of those scalars is
    # hash-labeled here before being placed in the trace, never echoed raw.
    intake_task_id = meta.get("intake_task_id") or latest_applied.get("intake_task_id") or ""
    review_task_id = meta.get("review_task_id") or latest_applied.get("review_task_id") or ""
    conscious_task_id = ref.get("task_id") or ref.get("kanban_task_id") or ""
    conscious_thread_id = ref.get("thread_id") or ""
    raw_decision = meta.get("decision") or latest_applied.get("decision")
    return {
        "decision": _safe_surface_atom("decision", raw_decision),
        "reason": _safe_trace_reason(meta, latest_applied),
        "intake_task_id": _opaque_join_label("intake_task", intake_task_id),
        "review_task_id": _opaque_join_label("review_task", review_task_id),
        "settled_at": meta.get("settled_at") or latest_applied.get("ts"),
        "conscious_task_id": _opaque_join_label("conscious_task_id", conscious_task_id),
        "conscious_thread_id": _opaque_join_label("conscious_thread_id", conscious_thread_id),
        "unresolved": bool(unresolved) and not applied,
    }


def _trace_lifecycle(
    candidate: dict[str, Any],
    events: list[dict[str, Any]],
    signals: list[dict[str, Any]],
    settlement: dict[str, Any] | None,
) -> dict[str, Any]:
    """Lifecycle-stage flags plus a compact wrong-turn band for one candidate.

    Stages mirror the inner movement the dashboard exists to expose:
    signal -> event -> candidate -> kanban -> decision -> settled.
    """
    settlement = settlement or {}
    raw_status = str(candidate.get("status") or "")
    status = _safe_surface_atom("candidate_status", raw_status)
    pressure = float(candidate.get("pressure") or 0)
    decision = settlement.get("decision")
    intake = bool(settlement.get("intake_task_id") or settlement.get("review_task_id"))
    settled = bool(settlement.get("settled_at") and decision)

    stages = [
        {"key": "signal", "label": "Signal", "reached": bool(signals), "detail": str(len(signals))},
        {"key": "event", "label": "Event", "reached": bool(events), "detail": str(len(events))},
        {"key": "candidate", "label": "Candidate", "reached": True, "detail": status or "candidate"},
        {"key": "kanban", "label": "Kanban", "reached": intake, "detail": "intake/review" if intake else ""},
        {"key": "decision", "label": "Decision", "reached": bool(decision), "detail": decision or ""},
        {"key": "settled", "label": "Settled", "reached": settled, "detail": status if settled else ""},
    ]

    flags: list[str] = []
    if not events:
        flags.append("no_source_events")
    if not signals and events:
        flags.append("no_source_signals")
    if settlement.get("unresolved"):
        flags.append("settlement_unresolved")
    if status in DROP_CANDIDATE_STATUSES:
        flags.append("dropped")
    pending = status == ACTIVE_CANDIDATE_STATUS and pressure >= DISPATCH_PRESSURE_THRESHOLD and not intake
    if pending:
        flags.append("pending_unsettled")
    reviewed_without_settlement = status == "reviewed" and not settled
    if reviewed_without_settlement:
        flags.append("reviewed_without_settlement")

    if settlement.get("unresolved") or "no_source_events" in flags:
        band = "red"
    elif status in DROP_CANDIDATE_STATUSES or pending or reviewed_without_settlement:
        band = "yellow"
    elif decision in {"SAVE", "PROMOTE_CONSCIOUS"}:
        band = "green"
    else:
        band = "neutral"

    return {"stages": stages, "flags": flags, "band": band}


def _perception_traces(
    candidates: list[dict[str, Any]],
    *,
    event_by_id: dict[str, dict[str, Any]],
    signal_by_id: dict[str, dict[str, Any]],
    decisions: list[dict[str, Any]],
    limit: int = PERCEPTION_TRACE_LIMIT,
) -> list[dict[str, Any]]:
    """Assemble bounded, read-only candidate lifecycle traces.

    Each trace stitches the inner movement back together so a wrong turn is
    legible: what the system noticed (signals/events), what became a candidate, what
    Subconscious decided in Kanban, the compact evidence/reason, and where it
    settled. Pure: it only reads already-loaded rows and never mutates state.
    """
    _, label_to_candidate_id = _candidate_label_indexes(candidates)
    receipts_by_candidate: dict[str, list[dict[str, Any]]] = {}
    for row in decisions:
        if not isinstance(row, dict) or row.get("type") not in SETTLEMENT_RECEIPT_TYPES:
            continue
        # Prefer the normalized subject-ref label whenever it resolves to a
        # loaded candidate. Legacy/corrupt rows may still carry a raw
        # `candidate_id`; keep it only as the no-subject/no-match fallback so it
        # cannot override a valid subject_ref join.
        subject_label = _receipt_subject_label(row)
        cand_id = label_to_candidate_id.get(subject_label, "")
        if not cand_id:
            cand_id = str(row.get("candidate_id") or "")
        if cand_id:
            receipts_by_candidate.setdefault(cand_id, []).append(row)

    ordered = sorted(candidates, key=_sort_key, reverse=True)[:limit]
    traces: list[dict[str, Any]] = []
    for candidate in ordered:
        cand_id = str(candidate.get("id") or "")
        event_ids = [str(e) for e in (candidate.get("event_ids") or []) if e]
        events = [event_by_id[eid] for eid in event_ids if eid in event_by_id][:4]
        signal_ids: list[str] = []
        for event in events:
            for sid in event.get("source_signal_ids") or []:
                sid = str(sid)
                if sid and sid not in signal_ids:
                    signal_ids.append(sid)
        signals = [signal_by_id[sid] for sid in signal_ids if sid in signal_by_id][:6]

        receipts = sorted(receipts_by_candidate.get(cand_id, []), key=lambda r: str(r.get("ts") or ""))
        settlement = _trace_settlement(candidate, receipts)
        lifecycle = _trace_lifecycle(candidate, events, signals, settlement)

        traces.append(
            {
                "candidate_id": _safe_trace_candidate_id(cand_id),
                "kind": _safe_surface_atom("candidate_kind", candidate.get("kind")),
                "status": _safe_surface_atom("candidate_status", candidate.get("status")),
                "pressure": candidate.get("pressure"),
                "summary": _safe_surface_text("candidate_summary", candidate.get("summary"), limit=200),
                "correlation_keys": _safe_trace_ref_list(
                    "correlation", candidate.get("correlation_keys") or [], limit=4
                ),
                "created_at": candidate.get("created_at"),
                "updated_at": candidate.get("updated_at") or candidate.get("created_at"),
                "events": [_trace_event_item(e) for e in events],
                "signals": [_trace_signal_item(s) for s in signals],
                "missing_event_ids": [
                    _safe_trace_ref("event", eid) for eid in event_ids if eid not in event_by_id
                ][:4],
                "settlement": settlement,
                "stages": lifecycle["stages"],
                "flags": lifecycle["flags"],
                "band": lifecycle["band"],
            }
        )
    return traces


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
                    "id": _safe_surface_atom("outbox", req_id),
                    "band": safety.get("band"),
                    "label": safety.get("label"),
                    "detail": safety.get("detail"),
                }
            )
        if req.get("status") == "prepared" and not action_for_outbox.get(req_id):
            warnings.append(
                {
                    "kind": "outbox",
                    "id": _safe_surface_atom("outbox", req_id),
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
                        "id": _safe_surface_atom("artifact", media_ref),
                        "band": "yellow",
                        "label": "missing_media_ref",
                        "detail": _safe_surface_text("warning_detail", f"Outbox {req_id} references a missing artifact.", limit=160),
                    }
                )
    for action in actions:
        if action.get("status") in ACTIVE_ACTION_STATUSES and _action_attachment_ids(action, "outbox_request"):
            warnings.append(
                {
                    "kind": "action",
                    "id": _safe_surface_atom("action", action.get("id")),
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
                    "id": _safe_surface_atom("artifact", artifact.get("id")),
                    "band": "yellow",
                    "label": "artifact_action_missing",
                    "detail": _safe_surface_text("warning_detail", f"Artifact references missing action {action_id}.", limit=160),
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
        "last_dispatch_action": _safe_surface_atom("dispatch_action", last_dispatch.get("action")),
        "last_dispatch_reason": _safe_surface_text("dispatch_reason", last_dispatch.get("reason"), limit=120),
    }


def _view_card(view_id: str, label: str, summary: str, count: int, band: str) -> dict[str, Any]:
    """Compact dashboard view metadata; read-only navigation, not control."""
    return {
        "id": view_id,
        "label": label,
        "summary": summary,
        "count": int(count or 0),
        "band": band,
    }


def _attention_footprint(counts: dict[str, int], *, metrics: dict[str, Any]) -> dict[str, Any]:
    """Separate live attention from historical/residue substrate.

    The dashboard should not make old held artifacts or historical pointers feel
    like immediate work. Keep the arithmetic explicit so UI and tests can show
    "now" separately from archive/museum residue.
    """
    raw_latest = metrics.get("latest")
    latest = raw_latest if isinstance(raw_latest, dict) else {}
    raw_open = latest.get("open")
    open_block = raw_open if isinstance(raw_open, dict) else {}
    open_reviews = (
        int(open_block.get("open_conscious_review_tasks") or 0)
        + int(open_block.get("open_subconscious_review_tasks") or 0)
        + int(open_block.get("open_intake_tasks") or 0)
    )
    live_items = (
        counts.get("lifecycle_warnings", 0)
        + counts.get("active_candidates", 0)
        + counts.get("active_threads", 0)
        + counts.get("open_actions", 0)
        + counts.get("actionable_outbox", 0)
        + open_reviews
    )
    residue_items = (
        counts.get("held_artifacts", 0)
        + counts.get("historical_outbox", 0)
        + counts.get("closed_actions", 0)
    )
    return {
        "live_items": live_items,
        "residue_items": residue_items,
        "open_reviews": open_reviews,
        "live_breakdown": {
            "active_candidates": counts.get("active_candidates", 0),
            "active_threads": counts.get("active_threads", 0),
            "open_reviews": open_reviews,
            "open_actions": counts.get("open_actions", 0),
            "actionable_outbox": counts.get("actionable_outbox", 0),
            "lifecycle_warnings": counts.get("lifecycle_warnings", 0),
        },
        "residue_breakdown": {
            "held_artifacts": counts.get("held_artifacts", 0),
            "historical_outbox": counts.get("historical_outbox", 0),
            "closed_actions": counts.get("closed_actions", 0),
        },
    }


def _dashboard_views(counts: dict[str, int], *, recent_signals: int, footprint: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Create posture-aligned dashboard views from already-computed counts.

    The views reflect the active-session salience policy: immediate work stays in
    Overview, retained compact residue and interpretation traces live in
    Perception, substrate state lives in Substrate, and possible motor/output
    material is isolated in Actuators. This keeps the cockpit from becoming one
    flat JSONL pile while preserving the no-mutation safety boundary.
    """
    footprint = footprint or {}
    overview_count = int(footprint.get("live_items") or 0)
    overview_band = "red" if counts.get("corrupt_lines", 0) else ("yellow" if overview_count else "green")
    perception_count = counts.get("perception_wrong_turns", 0) + recent_signals
    perception_band = "yellow" if counts.get("perception_wrong_turns", 0) else ("green" if recent_signals else "neutral")
    substrate_count = counts.get("active_candidates", 0) + counts.get("active_threads", 0)
    substrate_band = "yellow" if counts.get("active_candidates", 0) else ("green" if counts.get("active_threads", 0) else "neutral")
    actuator_count = counts.get("open_actions", 0) + counts.get("actionable_outbox", 0)
    actuator_band = "yellow" if actuator_count else "neutral"
    return [
        _view_card(
            "overview",
            "Overview",
            "Live pressure, freshness, and efficiency before any detailed drilldown.",
            overview_count,
            overview_band,
        ),
        _view_card(
            "perception",
            "Perception",
            "Compact residue plus signal → event → candidate interpretation trace.",
            perception_count,
            perception_band,
        ),
        _view_card(
            "substrate",
            "Substrate",
            "Candidates, threads, receipts, and lifecycle state breakdowns.",
            substrate_count,
            substrate_band,
        ),
        _view_card(
            "actuators",
            "Actuators",
            "Thread actions, outbox pointers, and artifact groups without raw bodies.",
            actuator_count,
            actuator_band,
        ),
    ]


def _sanitize_registry_block(block_id: str, block: Any) -> dict[str, Any]:
    data = block if isinstance(block, dict) else {}
    return {
        "id": _safe_surface_atom("block", block_id),
        "type": _safe_surface_atom("block_type", data.get("type") or data.get("kind")),
        "status": _safe_surface_atom("block_status", data.get("status") or "active"),
        "enabled": data.get("enabled") if isinstance(data.get("enabled"), bool) else None,
    }


def _sanitize_edge(edge: Any) -> dict[str, Any]:
    data = edge if isinstance(edge, dict) else {}
    return {
        "from": _safe_surface_atom("block", data.get("from") or data.get("source")),
        "to": _safe_surface_atom("block", data.get("to") or data.get("target")),
        "kind": _safe_surface_atom("edge_kind", data.get("kind") or data.get("type")),
    }


_TOPOLOGY_NODE_KINDS = {"sensor", "emitter", "processor", "gate", "queue", "router", "review", "receipt", "sink"}
_TOPOLOGY_NODE_KIND_ALIASES = {
    # Inner-life registry block kinds are more specific than the public Flow DAG
    # display vocabulary. Map them into stable stage categories instead of
    # rendering valid configured organs as `unknown`.
    "temporal_sensor": "sensor",
    "memory_reflector": "processor",
    "normalizer": "processor",
    "aggregator": "queue",
    "dampener": "gate",
    "blocker": "gate",
    "consciousness_review": "review",
    "receipt_writer": "receipt",
    "tendency_proposer": "processor",
    "dashboard_projection": "sink",
}
_TOPOLOGY_STATUSES = {"active", "disabled", "degraded"}
_TOPOLOGY_EDGE_KINDS = {"configured_flow", "dependency", "fallback"}


def _topology_node_kind(raw: Any) -> str:
    """Closed-vocabulary node kind; missing type defaults to sensor (registry.json is sensor-scoped)."""
    if raw is None or raw == "":
        return "sensor"
    text = str(raw).strip().lower()
    text = _TOPOLOGY_NODE_KIND_ALIASES.get(text, text)
    return text if text in _TOPOLOGY_NODE_KINDS else "unknown"


def _topology_edge_kind(raw: Any) -> str:
    text = str(raw or "").strip().lower()
    return text if text in _TOPOLOGY_EDGE_KINDS else "configured_flow"


def _topology_status(enabled: Any, raw_status: Any) -> str:
    status = str(raw_status or "").strip().lower()
    if status in _TOPOLOGY_STATUSES:
        return status
    if enabled is False:
        return "disabled"
    if enabled is True:
        return "active"
    return "unknown"


def _topology_safe_id(value: Any) -> str:
    return _safe_surface_atom("topology_node", value)


def _topology_edge_status(raw_status: Any) -> str | None:
    """Closed-vocabulary edge status; never echo free-form/raw config status text."""
    if raw_status is None or raw_status == "":
        return None
    text = str(raw_status).strip().lower()
    return text if text in _TOPOLOGY_STATUSES else "unknown"


_PATH_SHAPED_RE = re.compile(r"[\\/]")


def _topology_safe_label(value: Any, *, limit: int = 80) -> str:
    """Privacy-safe topology display label, stricter than generic surface text.

    sera-ck9.3.2 label policy: `/topology` labels are the primary flow-DAG UI
    affordance (rendered directly as node text), unlike most compact
    summaries elsewhere in this module which stay free-form. `_safe_surface_text`
    alone only catches secret-shaped strings (api keys, tokens, …), not local
    filesystem paths that could leak into a registry `label`/`name` field by
    operator mistake. Hash-label anything path-shaped (contains `/` or `\\`)
    before falling through to the normal secret-shaped guard, so a registry
    label can never echo a raw path/hostile marker verbatim in the primary UI.
    """
    text = _truncate(value, limit)
    if not text:
        return ""
    if _PATH_SHAPED_RE.search(text):
        return _opaque_join_label("topology_label", value)
    return _safe_surface_text("topology_label", value, limit=limit)


def _sanitize_topology_node(raw_id: str, block: Any) -> tuple[str, dict[str, Any]]:
    data = block if isinstance(block, dict) else {}
    kind = _topology_node_kind(data.get("type") or data.get("kind"))
    node_id = f"{kind}:{_topology_safe_id(raw_id)}"
    enabled = data.get("enabled") if isinstance(data.get("enabled"), bool) else None
    label = _topology_safe_label(data.get("label") or data.get("name") or raw_id)
    return node_id, {
        "id": node_id,
        "kind": kind,
        "label": label,
        "enabled": enabled,
        "configured_status": _topology_status(enabled, data.get("status")),
    }


def _sanitize_topology_edge(edge: Any, node_lookup: dict[str, str]) -> dict[str, Any]:
    data = edge if isinstance(edge, dict) else {}
    raw_from = data.get("from") or data.get("source")
    raw_to = data.get("to") or data.get("target")
    from_id = node_lookup.get(str(raw_from)) or f"unknown:{_topology_safe_id(raw_from)}"
    to_id = node_lookup.get(str(raw_to)) or f"unknown:{_topology_safe_id(raw_to)}"
    kind = _topology_edge_kind(data.get("kind") or data.get("type"))
    enabled = data.get("enabled") if isinstance(data.get("enabled"), bool) else None
    status = _topology_edge_status(data.get("status"))
    return {
        "from": from_id,
        "to": to_id,
        "kind": kind,
        "enabled": enabled,
        "status": status,
    }


def _sanitize_topology_edges(raw_edge_list: list[Any], node_lookup: dict[str, str]) -> list[dict[str, Any]]:
    """Sanitize configured topology edges with collision-safe ids.

    Two configured edges can legitimately share the same `from`/`to` pair with
    a different `kind` (e.g. a primary `configured_flow` plus a `fallback`
    edge between the same two blocks), or even be exact duplicates in a sloppy
    config. A bare `from->to` id collides in the first case and silently
    drops the second occurrence in any id-keyed frontend map. Fold `kind` into
    the id and append a deterministic occurrence index for any remaining
    duplicate (from, to, kind) triples so every edge id is unique.
    """
    edges: list[dict[str, Any]] = []
    seen_counts: dict[tuple[str, str, str], int] = {}
    for raw_edge in raw_edge_list:
        edge = _sanitize_topology_edge(raw_edge, node_lookup)
        key = (edge["from"], edge["to"], edge["kind"])
        occurrence = seen_counts.get(key, 0)
        seen_counts[key] = occurrence + 1
        edge_id = f"{edge['from']}->{edge['to']}#{edge['kind']}"
        if occurrence:
            edge_id += f":{occurrence}"
        edges.append({"id": edge_id, **edge})
    return edges


def _topology_signal_inbox_lineage(
    blocks_obj: dict[str, Any], raw_edge_list: list[Any], node_lookup: dict[str, str]
) -> dict[str, Any]:
    """Compact read-only completeness check for sensor -> signal_inbox paths.

    This does not mutate config or infer runtime traversal. It only flags
    enabled emitting sensor blocks that are present in the configured Flow-DAG
    without any enabled configured edge path to ``signal_inbox``. A sensor block
    can explicitly opt out by declaring ``emits_signals: false``.
    """

    try:
        from agent_sensorium.inner_life import signal_inbox_lineage_violations

        raw_violations = signal_inbox_lineage_violations(
            {"version": 2, "blocks": blocks_obj},
            {"version": 1, "edges": raw_edge_list},
        )
    except Exception:
        return {
            "ok": False,
            "violation_count": 0,
            "violations": [],
            "allowlist": "emits_signals:false",
            "error": "lineage_checker_unavailable",
        }

    violations: list[dict[str, Any]] = []
    for violation in raw_violations[:25]:
        sensor = str(violation.get("sensor") or "")
        required_target = str(violation.get("required_target") or "signal_inbox")
        safe_sensor = _topology_safe_id(sensor)
        violations.append(
            {
                "sensor": safe_sensor,
                "node_id": node_lookup.get(sensor) or f"sensor:{safe_sensor}",
                "reason": _safe_surface_atom("lineage_reason", violation.get("reason")),
                "required_target": _topology_safe_id(required_target),
            }
        )
    return {
        "ok": not raw_violations,
        "violation_count": len(raw_violations),
        "violations": violations,
        "allowlist": "emits_signals:false",
    }


def _topology_config_version(root: Path) -> str:
    """Hash of effective topology/config/registry sources; changes when any source's content changes."""
    parts: list[bytes] = []
    for rel in ("sensors/registry.json", "sensors/edges.json", "instance.config.json"):
        path = root / rel
        try:
            parts.append(path.read_bytes() if path.exists() else b"")
        except Exception:
            parts.append(b"")
    digest = hashlib.sha256(b"\x00".join(parts)).hexdigest()
    return f"sha256:{digest}"


_RUNTIME_STATUSES = {
    "active", "quiet", "degraded", "error", "processing", "waiting",
    "reviewing", "blocked", "held", "settled", "stale",
}
# sera-ck9.3 runtime-status vocabulary polish: not every member is emitted by
# this slice yet. Documenting why, rather than emitting a guessed value:
#   - "degraded": reserved for a future honest per-node processor/gate
#     degradation signal; today only the instance-wide `is_stale` freshness
#     check exists, surfaced as "stale", not "degraded".
#   - "blocked": reserved for a future blocker-effect overlay (see
#     `/blockers`); blocker rows are not yet joined onto runtime-status nodes.
#   - "settled": is emitted for reviewed candidates. The detailed settlement
#     receipt still lives in `/snapshot` perception traces and `/graph`, but a
#     reviewed candidate must not look like live review pressure in the Flow DAG.
# "waiting" for candidates also covers the `candidate -> waiting` semantics
# noted in sera-ck9.2: an unreviewed `candidate` status row means "awaiting
# Subconscious review", not literally idle/quiet, which is why it maps to
# "waiting" instead of falling through to the "quiet" default.
_RUNTIME_STALE_SECONDS = 24 * 60 * 60
_RUNTIME_INSTANCE_LIMIT = 20

# Direct mappings from existing lifecycle status enums (candidates.jsonl /
# threads.jsonl / thread_actions.jsonl / outbox.jsonl `status`) into the closed
# runtime-status vocabulary above. Anything not listed here falls back to the
# safe default "quiet" rather than ever passing free-form status text through.
_RUNTIME_CANDIDATE_STATUS = {
    "candidate": "waiting",
    "reviewed": "settled",
    "suppressed": "stale",
    "dropped": "stale",
}
_RUNTIME_THREAD_STATUS = {
    "held": "held",
    "dormant": "quiet",
}
_RUNTIME_ACTION_STATUS = {
    "proposed": "waiting",
    "offered": "waiting",
    "prepared": "processing",
}
_RUNTIME_OUTBOX_STATUS = {
    "prepared": "waiting",
    "failed": "error",
}


def _runtime_is_stale(root: Path) -> tuple[bool, str | None]:
    """Whether the instance's canonical freshness mtime has gone stale.

    This is the only honest per-instance (not per-node) staleness signal
    available without a runtime trace log; it is applied uniformly to
    topology nodes lacking a more specific honest source (see
    `_runtime_node_overlays`).
    """
    canonical_mtime = _freshness_snapshot(root).get("canonical_latest_mtime")
    if not canonical_mtime:
        return False, canonical_mtime
    parsed = _parse_dt(canonical_mtime)
    if parsed is None:
        return False, canonical_mtime
    age_seconds = (datetime.now(timezone.utc) - parsed).total_seconds()
    return age_seconds > _RUNTIME_STALE_SECONDS, canonical_mtime


def _recent_signal_sensors(root: Path, *, limit: int = 200) -> set[str]:
    """Sensor names with a signal in the most recent window.

    `signals/inbox.jsonl` rows already carry a `sensor` field naming the same
    registry block id /topology projects, so this is the one honest per-node
    runtime-activity signal available today (see sera-ck9-2 task guidance on
    not fabricating processor/gate runtime state).
    """
    rows, _ = _read_jsonl(root, "signals", limit=limit)
    return {str(row.get("sensor")) for row in rows if row.get("sensor")}


def _runtime_node_overlays(
    blocks_obj: dict[str, Any], *, active_sensors: set[str], is_stale: bool
) -> list[dict[str, Any]]:
    """Closed-vocabulary runtime status overlay for every configured topology node.

    Mirrors /topology's own node projection (same registry source, same ids)
    so every topology node gets an explicit overlay entry. Processor/gate
    nodes have no honest runtime-activity source yet and conservatively
    default to quiet/stale; sensor nodes additionally get "active" when a
    recent signal attributes to them.
    """
    overlays: list[dict[str, Any]] = []
    for raw_id, block in sorted(blocks_obj.items(), key=lambda item: str(item[0]))[:250]:
        node_id, topo_node = _sanitize_topology_node(str(raw_id), block)
        if topo_node["kind"] == "sensor" and str(raw_id) in active_sensors:
            status, source = "active", "signal_match"
        elif is_stale:
            status, source = "stale", "freshness_stale"
        else:
            status, source = "quiet", "configured_default"
        overlays.append({"id": node_id, "kind": topo_node["kind"], "status": status, "source": source, "origin": "topology"})
    return overlays


def _runtime_candidate_node(candidate: dict[str, Any]) -> dict[str, Any]:
    status = _RUNTIME_CANDIDATE_STATUS.get(str(candidate.get("status") or "").strip().lower(), "quiet")
    return {
        "id": f"candidate:{_safe_trace_candidate_id(candidate.get('id'))}",
        "kind": "candidate",
        "status": status,
        "source": "field_status",
        "origin": "instance",
        "label": _safe_surface_atom("candidate_kind", candidate.get("kind") or "candidate"),
        "detail": _safe_surface_text("candidate_summary", candidate.get("summary"), limit=160),
        "pressure": candidate.get("pressure"),
        "updated_at": candidate.get("updated_at") or candidate.get("created_at"),
        "contents": _instance_contents("candidate", candidate),
    }


def _runtime_thread_node(thread: dict[str, Any]) -> dict[str, Any]:
    status = _RUNTIME_THREAD_STATUS.get(str(thread.get("status") or "").strip().lower(), "quiet")
    task = thread.get("conscious_task") if isinstance(thread.get("conscious_task"), dict) else {}
    return {
        "id": f"thread:{_safe_surface_atom('thread', thread.get('id'))}",
        "kind": "thread",
        "status": status,
        "source": "field_status",
        "origin": "instance",
        "label": _safe_surface_text("thread_title", task.get("title") or thread.get("title") or "held thread", limit=120),
        "detail": _safe_surface_text("thread_detail", task.get("summary") or thread.get("summary"), limit=160),
        "updated_at": thread.get("updated_at") or thread.get("created_at"),
        "contents": _instance_contents("thread", thread),
    }


def _runtime_action_node(action: dict[str, Any]) -> dict[str, Any]:
    status = _RUNTIME_ACTION_STATUS.get(str(action.get("status") or "").strip().lower(), "quiet")
    return {
        "id": f"action:{_safe_surface_atom('action', action.get('id'))}",
        "kind": "action",
        "status": status,
        "source": "field_status",
        "origin": "instance",
        "label": _safe_surface_text("action_title", action.get("title") or action.get("intent") or "thread action", limit=120),
        "detail": _safe_surface_text("action_summary", action.get("summary") or action.get("result_summary"), limit=160),
        "updated_at": action.get("updated_at") or action.get("ts"),
        "contents": _instance_contents("action", action),
    }


def _runtime_outbox_node(req: dict[str, Any], *, safety: dict[str, Any] | None = None) -> dict[str, Any]:
    status = _RUNTIME_OUTBOX_STATUS.get(str(req.get("status") or "").strip().lower(), "quiet")
    safety = safety or {}
    if safety.get("label") == "historical_prepared_pointer":
        status = "settled"
    return {
        "id": f"outbox:{_safe_surface_atom('outbox', req.get('id'))}",
        "kind": "outbox",
        "status": status,
        "source": "field_status",
        "origin": "instance",
        "label": _safe_surface_text("outbox_title", req.get("title") or req.get("request_type") or "outbox request", limit=120),
        "detail": _safe_surface_text("outbox_preview", req.get("message_preview"), limit=160),
        "updated_at": req.get("updated_at") or req.get("created_at"),
        "contents": _instance_contents("outbox", req),
    }


def _runtime_flow_edges(
    root: Path,
    *,
    visible_node_ids: set[str],
    candidates: list[dict[str, Any]],
    threads: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    outbox: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Compact runtime relation overlay for the Flow DAG.

    These edges are derived from explicit JSONL lineage fields (signal/event IDs,
    origin_candidate_id/origin_thread_id, and action outbox attachments). They
    are a bounded current-state relation overlay, not a complete historical
    traversal log.
    """
    edges: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_edge(from_id: str, to_id: str, kind: str, source: str, ts: Any = None) -> None:
        if not from_id or not to_id or from_id not in visible_node_ids or to_id not in visible_node_ids:
            return
        base = f"runtime:{kind}:{from_id}->{to_id}"
        edge_id = base
        if edge_id in seen:
            edge_id = f"{base}#{len(seen)}"
        if edge_id in seen:
            return
        seen.add(edge_id)
        edges.append({
            "id": edge_id,
            "from": from_id,
            "to": to_id,
            "kind": kind,
            "status": "active",
            "source": source,
            "observed": True,
            "projection": True,
            "ts": ts if isinstance(ts, str) and len(ts) <= 60 else None,
        })

    candidate_node_by_raw = {str(c.get("id") or ""): f"candidate:{_safe_trace_candidate_id(c.get('id'))}" for c in candidates}
    thread_node_by_raw = {str(t.get("id") or ""): f"thread:{_safe_surface_atom('thread', t.get('id'))}" for t in threads}
    action_node_by_raw = {str(a.get("id") or ""): f"action:{_safe_surface_atom('action', a.get('id'))}" for a in actions}
    outbox_node_by_raw = {str(o.get("id") or ""): f"outbox:{_safe_surface_atom('outbox', o.get('id'))}" for o in outbox}

    events, _ = _read_jsonl(root, "events", limit=5000)
    signals, _ = _read_jsonl(root, "signals", limit=5000)
    event_by_id = _index_by_id(events)
    signal_by_id = _index_by_id(signals)
    for candidate in candidates:
        candidate_node_id = candidate_node_by_raw.get(str(candidate.get("id") or ""), "")
        for event_id in [str(e) for e in (candidate.get("event_ids") or []) if e]:
            event = event_by_id.get(event_id, {})
            for signal_id in event.get("source_signal_ids") or []:
                signal = signal_by_id.get(str(signal_id), {})
                sensor = signal.get("sensor")
                if sensor:
                    add_edge(
                        f"sensor:{_topology_safe_id(sensor)}",
                        candidate_node_id,
                        "signal_to_candidate",
                        "event_lineage",
                        signal.get("ts") or signal.get("created_at") or signal.get("updated_at"),
                    )

    for thread in threads:
        candidate_node_id = candidate_node_by_raw.get(str(thread.get("origin_candidate_id") or ""), "")
        thread_node_id = thread_node_by_raw.get(str(thread.get("id") or ""), "")
        add_edge(candidate_node_id, thread_node_id, "candidate_to_thread", "origin_candidate_id", thread.get("updated_at") or thread.get("created_at"))

    for action in actions:
        action_node_id = action_node_by_raw.get(str(action.get("id") or ""), "")
        add_edge(candidate_node_by_raw.get(str(action.get("origin_candidate_id") or ""), ""), action_node_id, "candidate_to_action", "origin_candidate_id", action.get("updated_at") or action.get("ts"))
        add_edge(thread_node_by_raw.get(str(action.get("origin_thread_id") or ""), ""), action_node_id, "thread_to_action", "origin_thread_id", action.get("updated_at") or action.get("ts"))
        for outbox_ref in _action_attachment_ids(action, "outbox_request"):
            add_edge(action_node_id, outbox_node_by_raw.get(str(outbox_ref), ""), "action_to_outbox", "attachment_ref", action.get("updated_at") or action.get("ts"))

    for req in outbox:
        outbox_node_id = outbox_node_by_raw.get(str(req.get("id") or ""), "")
        add_edge(candidate_node_by_raw.get(str(req.get("origin_candidate_id") or ""), ""), outbox_node_id, "candidate_to_outbox", "origin_candidate_id", req.get("updated_at") or req.get("created_at"))
        add_edge(thread_node_by_raw.get(str(req.get("origin_thread_id") or ""), ""), outbox_node_id, "thread_to_outbox", "origin_thread_id", req.get("updated_at") or req.get("created_at"))

    return edges[:80]


def _read_inner_life_rows(root: Path, name: str, limit: int) -> list[dict[str, Any]]:
    path = root / "inner_life" / name
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(errors="ignore").splitlines()[-limit:]:
            try:
                value = json.loads(line)
            except Exception:
                continue
            if isinstance(value, dict):
                rows.append(value)
    except Exception:
        return []
    return rows


def _sanitize_dampener(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": _safe_surface_atom("record_type", row.get("type")),
        "source_node": _safe_surface_atom("node", row.get("source_node")),
        "target_node": _safe_surface_atom("node", row.get("target_node")),
        "mode": _safe_surface_atom("mode", row.get("mode")),
        "reason": _safe_surface_text("reason", row.get("reason"), limit=140),
        "before_pressure": row.get("before_pressure"),
        "after_pressure": row.get("after_pressure"),
        "precedence": _safe_surface_atom("precedence", row.get("precedence")),
        "ts": row.get("ts") or row.get("created_at"),
    }


def _sanitize_blocker(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": _safe_surface_atom("record_type", row.get("type")),
        "source_node": _safe_surface_atom("node", row.get("source_node")),
        "target_node": _safe_surface_atom("node", row.get("target_node")),
        "policy_id": _safe_surface_atom("policy", row.get("policy_id")),
        "schema_ref": _safe_surface_atom("schema", row.get("schema_ref")),
        "authority_ref": _safe_surface_atom("authority", row.get("authority_ref")),
        "reason_kind": _safe_surface_atom("reason_kind", row.get("reason_kind")),
        "reason_label": (
            _truncate(row.get("reason_label"), 140)
            if _is_receipt_evidence_label("reason", row.get("reason_label"))
            else _opaque_join_label("reason", row.get("reason_label"))
        ) if row.get("reason_label") else "",
        "precedence": _safe_surface_atom("precedence", row.get("precedence")),
        "blocked": bool(row.get("blocked")) if "blocked" in row else None,
        "ts": row.get("ts") or row.get("created_at"),
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

    resolved = _resolve_instance(instance)
    if resolved is None:
        return {"ok": False, "error": "invalid_instance"}
    effective_instance, root = resolved
    store = SensoriumStore(instance=effective_instance, state_dir=str(root))
    try:
        # Read-only dashboard endpoint: SensoriumStore.read_jsonl returns empty
        # rows for missing files, so avoid ensure_dirs() here.
        inbox = build_attention_inbox(store, surface=surface, limit=limit)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "generated_at": _now(), "instance": effective_instance, **inbox}


@router.get("/registry")
async def registry(instance: str | None = None) -> dict[str, Any]:
    """Read-only compact sensor/inner-life registry projection."""
    resolved = _resolve_instance(instance)
    if resolved is None:
        return {"ok": False, "error": "invalid_instance"}
    effective_instance, root = resolved
    raw_registry = _read_json(root / "sensors" / "registry.json", {})
    raw_edges = _read_json(root / "sensors" / "edges.json", {})
    blocks_obj = raw_registry.get("blocks") if isinstance(raw_registry, dict) else {}
    if not isinstance(blocks_obj, dict) and isinstance(raw_registry, dict):
        blocks_obj = raw_registry
    blocks = [
        _sanitize_registry_block(str(block_id), block)
        for block_id, block in sorted((blocks_obj or {}).items(), key=lambda item: str(item[0]))[:250]
    ] if isinstance(blocks_obj, dict) else []
    raw_edge_list = raw_edges.get("edges") if isinstance(raw_edges, dict) else []
    edges = [_sanitize_edge(edge) for edge in (raw_edge_list if isinstance(raw_edge_list, list) else [])[:300]]
    return {
        "ok": True,
        "generated_at": _now(),
        "instance": effective_instance,
        "version": _safe_surface_atom("registry_version", raw_registry.get("version")) if isinstance(raw_registry, dict) else None,
        "meta": {"block_count": len(blocks), "edge_count": len(edges), "privacy": "compact_only"},
        "blocks": blocks,
        "edges": edges,
    }


def _build_topology(root: Path) -> dict[str, Any]:
    """Shared compact topology projection used by `/topology` and `/trace`.

    Returns nodes/edges/config_version/meta plus `node_lookup` (raw registry
    block id -> sanitized node id) and `blocks_obj` (raw registry blocks) so
    `/trace` can resolve a selected node back to its registry block without
    re-parsing the registry/edges files itself.
    """
    raw_registry = _read_json(root / "sensors" / "registry.json", {})
    raw_edges = _read_json(root / "sensors" / "edges.json", {})
    blocks_obj = raw_registry.get("blocks") if isinstance(raw_registry, dict) else {}
    if not isinstance(blocks_obj, dict) and isinstance(raw_registry, dict):
        blocks_obj = raw_registry
    if not isinstance(blocks_obj, dict):
        blocks_obj = {}

    node_limit, edge_limit = 250, 300
    node_lookup: dict[str, str] = {}
    nodes: list[dict[str, Any]] = []
    for raw_id, block in sorted(blocks_obj.items(), key=lambda item: str(item[0]))[:node_limit]:
        node_id, node = _sanitize_topology_node(str(raw_id), block)
        node_lookup[str(raw_id)] = node_id
        nodes.append(node)

    raw_edge_list = raw_edges.get("edges") if isinstance(raw_edges, dict) else []
    if not isinstance(raw_edge_list, list):
        raw_edge_list = []
    edges = _sanitize_topology_edges(raw_edge_list[:edge_limit], node_lookup)
    signal_inbox_lineage = _topology_signal_inbox_lineage(blocks_obj, raw_edge_list, node_lookup)

    return {
        "nodes": nodes,
        "edges": edges,
        "config_version": _topology_config_version(root),
        "node_lookup": node_lookup,
        "blocks_obj": blocks_obj,
        "meta": {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "privacy": "compact_only",
            "node_limit": node_limit,
            "edge_limit": edge_limit,
            "truncated_nodes": len(blocks_obj) > node_limit,
            "truncated_edges": len(raw_edge_list) > edge_limit,
            "signal_inbox_lineage": signal_inbox_lineage,
        },
    }


@router.get("/topology")
async def topology(instance: str | None = None) -> dict[str, Any]:
    """Read-only compact-only topology/config projection for the flow DAG.

    Derived from sensors/registry.json + sensors/edges.json (instance.config.json
    additionally feeds config_version). Nodes/edges reflect configured topology,
    not runtime history, so configured-but-quiet sensors appear even with zero
    JSONL rows. Processor/gate internals have no registry representation yet;
    this slice covers sensor registry blocks/edges (sera-ck9.1) and leaves
    richer processor/gate coverage for a follow-up slice.
    """
    resolved = _resolve_instance(instance)
    if resolved is None:
        return {"ok": False, "error": "invalid_instance"}
    effective_instance, root = resolved

    built = _build_topology(root)
    ts = _now()
    return {
        "ok": True,
        "privacy": "compact_only",
        "generated_at": ts,
        "as_of": ts,
        "instance": effective_instance,
        "config_version": built["config_version"],
        "nodes": built["nodes"],
        "edges": built["edges"],
        "meta": built["meta"],
    }


@router.get("/runtime-status")
async def runtime_status(instance: str | None = None) -> dict[str, Any]:
    """Read-only compact-only runtime status overlay for the flow DAG (sera-ck9.2).

    Overlays a closed-vocabulary runtime status onto every /topology node, plus
    a bounded set of active runtime instance nodes (candidates/threads/actions/
    outbox) drawn from the same "active" status filters /snapshot already uses.
    Per-node runtime activity is only honestly derivable for sensor nodes via
    signal->sensor attribution; everything else conservatively defaults to
    quiet/stale rather than fabricating processor/gate runtime state. `edges`
    is always empty in this slice: there is no honest current-traversal source
    yet (left for a future /trace slice).
    """
    resolved = _resolve_instance(instance)
    if resolved is None:
        return {"ok": False, "error": "invalid_instance"}
    effective_instance, root = resolved

    raw_registry = _read_json(root / "sensors" / "registry.json", {})
    blocks_obj = raw_registry.get("blocks") if isinstance(raw_registry, dict) else {}
    if not isinstance(blocks_obj, dict) and isinstance(raw_registry, dict):
        blocks_obj = raw_registry
    if not isinstance(blocks_obj, dict):
        blocks_obj = {}

    is_stale, freshness_mtime = _runtime_is_stale(root)
    active_sensors = _recent_signal_sensors(root)
    topology_nodes = _runtime_node_overlays(blocks_obj, active_sensors=active_sensors, is_stale=is_stale)

    candidates, _ = _read_jsonl(root, "candidates", limit=2000)
    threads, _ = _read_jsonl(root, "threads", limit=2000)
    actions, _ = _read_jsonl(root, "thread_actions", limit=2000)
    outbox, _ = _read_jsonl(root, "outbox", limit=2000)
    thread_by_id = _index_by_id(threads)
    action_by_id = _index_by_id(actions)
    action_for_outbox = _find_action_for_outbox(actions)
    config = _read_json(root / "instance.config.json", {})

    eligible_candidates = [c for c in candidates if str(c.get("status") or "").strip().lower() in _RUNTIME_CANDIDATE_STATUS]
    eligible_threads = [t for t in threads if t.get("status") in ACTIVE_THREAD_STATUSES]
    eligible_actions = [a for a in actions if a.get("status") in ACTIVE_ACTION_STATUSES]
    outbox_safety_by_id = {
        str(o.get("id") or ""): _outbox_safety(
            o,
            thread_by_id=thread_by_id,
            action_by_id=action_by_id,
            action_for_outbox=action_for_outbox,
            config=config,
        )
        for o in outbox
    }
    eligible_outbox = [
        o for o in outbox if _outbox_counts_as_open(o, safety=outbox_safety_by_id.get(str(o.get("id") or "")))
    ]

    candidate_nodes = [
        _runtime_candidate_node(c) for c in sorted(eligible_candidates, key=_sort_key, reverse=True)[:_RUNTIME_INSTANCE_LIMIT]
    ]
    thread_nodes = [
        _runtime_thread_node(t) for t in sorted(eligible_threads, key=_sort_key, reverse=True)[:_RUNTIME_INSTANCE_LIMIT]
    ]
    action_nodes = [
        _runtime_action_node(a) for a in sorted(eligible_actions, key=_sort_key, reverse=True)[:_RUNTIME_INSTANCE_LIMIT]
    ]
    outbox_nodes = [
        _runtime_outbox_node(
            o,
            safety=outbox_safety_by_id.get(str(o.get("id") or "")) or {},
        )
        for o in sorted(eligible_outbox, key=_sort_key, reverse=True)[:_RUNTIME_INSTANCE_LIMIT]
    ]
    instance_nodes = candidate_nodes + thread_nodes + action_nodes + outbox_nodes
    nodes = topology_nodes + instance_nodes
    runtime_edges = _runtime_flow_edges(
        root,
        visible_node_ids={str(n.get("id") or "") for n in nodes},
        candidates=eligible_candidates,
        threads=eligible_threads,
        actions=eligible_actions,
        outbox=eligible_outbox,
    )

    ts = _now()
    return {
        "ok": True,
        "privacy": "compact_only",
        "generated_at": ts,
        "as_of": ts,
        "instance": effective_instance,
        "topology_config_version": _topology_config_version(root),
        "status_vocab": sorted(_RUNTIME_STATUSES),
        "nodes": nodes,
        "edges": runtime_edges,
        "meta": {
            "privacy": "compact_only",
            "node_count": len(nodes),
            "topology_node_count": len(topology_nodes),
            "instance_node_count": len(instance_nodes),
            "edge_count": len(runtime_edges),
            "edges_limitation": "runtime edges are bounded compact relation evidence from JSONL lineage fields, not a complete traversal log",
            "instance_node_limit": _RUNTIME_INSTANCE_LIMIT,
            "truncated_candidates": len(eligible_candidates) > _RUNTIME_INSTANCE_LIMIT,
            "truncated_threads": len(eligible_threads) > _RUNTIME_INSTANCE_LIMIT,
            "truncated_actions": len(eligible_actions) > _RUNTIME_INSTANCE_LIMIT,
            "truncated_outbox": len(eligible_outbox) > _RUNTIME_INSTANCE_LIMIT,
            "is_stale": is_stale,
            "stale_threshold_seconds": _RUNTIME_STALE_SECONDS,
            "freshness_mtime": freshness_mtime,
        },
    }


_TRACE_TOPOLOGY_NODE_KINDS = _TOPOLOGY_NODE_KINDS | {"unknown"}
_TRACE_INSTANCE_NODE_KINDS = {"candidate", "thread", "action", "outbox"}


def _trace_compact_ref(node_id: str, kind: str, relation: str) -> dict[str, str]:
    return {"id": node_id, "kind": kind, "relation": relation}


def _topology_node_contents(root: Path, node_id: str, node: dict[str, Any], overlay: dict[str, Any] | None, *, upstream_count: int, downstream_count: int) -> dict[str, Any]:
    """Compact operator-facing node contents for the Flow DAG detail rail."""
    overlay_obj: dict[str, Any] = overlay if isinstance(overlay, dict) else {}
    contents: dict[str, Any] = {
        "role": _safe_surface_atom("topology_role", node.get("kind")),
        "status": _safe_surface_atom("runtime_status", overlay_obj.get("status")),
        "status_source": _safe_surface_atom("runtime_source", overlay_obj.get("source")),
        "configured_status": _safe_surface_atom("topology_status", node.get("configured_status")),
        "enabled": node.get("enabled") if isinstance(node.get("enabled"), bool) else None,
        "upstream_count": upstream_count,
        "downstream_count": downstream_count,
    }
    if node.get("kind") == "sensor":
        rows, _ = _read_jsonl(root, "signals", limit=300)
        matching = [
            row for row in rows
            if f"sensor:{_topology_safe_id(row.get('sensor'))}" == node_id
        ]
        if matching:
            latest = matching[-1]
            contents.update({
                "recent_signal_count": len(matching),
                "latest_signal_kind": _safe_surface_atom("signal_kind", latest.get("kind")),
                "latest_signal_ts": latest.get("ts") or latest.get("created_at") or latest.get("updated_at"),
                "latest_signal_summary": _safe_surface_text("signal_summary", latest.get("summary"), limit=140),
            })
        else:
            contents["recent_signal_count"] = 0
    return contents


def _safe_list_count(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


def _instance_contents(kind: str, row: dict[str, Any], *, upstream_count: int = 0, downstream_count: int = 0) -> dict[str, Any]:
    contents: dict[str, Any] = {
        "role": _safe_surface_atom("instance_role", kind),
        "upstream_count": upstream_count,
        "downstream_count": downstream_count,
    }
    if kind == "candidate":
        contents.update({
            "kind": _safe_surface_atom("candidate_kind", row.get("kind")),
            "status": _safe_surface_atom("candidate_status", row.get("status")),
            "pressure": row.get("pressure"),
            "summary": _safe_surface_text("candidate_summary", row.get("summary"), limit=180),
            "event_count": _safe_list_count(row.get("event_ids")),
            "correlation_count": _safe_list_count(row.get("correlation_keys")),
        })
    elif kind == "thread":
        task = row.get("conscious_task") if isinstance(row.get("conscious_task"), dict) else {}
        contents.update({
            "status": _safe_surface_atom("thread_status", row.get("status")),
            "title": _safe_surface_text("thread_title", task.get("title") or row.get("title"), limit=160),
            "request_type": _safe_surface_atom("request_type", task.get("request_type")),
            "interaction_ref_count": _safe_list_count(row.get("interaction_refs")),
        })
    elif kind == "action":
        contents.update({
            "status": _safe_surface_atom("action_status", row.get("status")),
            "intent": _safe_surface_atom("action_intent", row.get("intent")),
            "title": _safe_surface_text("action_title", row.get("title"), limit=160),
            "attachment_count": _safe_list_count(row.get("attachments")),
        })
    elif kind == "outbox":
        contents.update({
            "status": _safe_surface_atom("outbox_status", row.get("status")),
            "request_type": _safe_surface_atom("request_type", row.get("request_type")),
            "surface": _safe_surface_atom("surface", row.get("surface")),
            "delivery_mode": _safe_surface_atom("delivery_mode", row.get("delivery_mode")),
            "message_chars": len(str(row.get("message_preview") or "")),
            "media_ref_count": _safe_list_count(row.get("media_refs")),
        })
    return contents


def _trace_topology_node(root: Path, node_id: str) -> dict[str, Any] | None:
    """Trace for a configured `/topology` node: configured neighbors plus its current runtime overlay status.

    Config refs name the sanitized config source/kind/version (sera-ck9.3
    honesty requirement), never a raw file path or config blob. There is no
    honest per-node runtime timestamp source for configured topology nodes
    yet, so `timestamps` stays empty with a `limitations` note instead of
    fabricating one.
    """
    built = _build_topology(root)
    nodes_by_id = {n["id"]: n for n in built["nodes"]}
    node = nodes_by_id.get(node_id)
    if node is None:
        return None
    upstream = [
        _trace_compact_ref(e["from"], "topology_node", e["kind"]) for e in built["edges"] if e["to"] == node_id
    ][:24]
    downstream = [
        _trace_compact_ref(e["to"], "topology_node", e["kind"]) for e in built["edges"] if e["from"] == node_id
    ][:24]
    is_stale, _ = _runtime_is_stale(root)
    active_sensors = _recent_signal_sensors(root)
    overlays = _runtime_node_overlays(built["blocks_obj"], active_sensors=active_sensors, is_stale=is_stale)
    overlay = next((o for o in overlays if o["id"] == node_id), None)
    return {
        "subject": {
            "id": node_id,
            "type": "node",
            "kind": node["kind"],
            "origin": "topology",
            "status": overlay["status"] if overlay else None,
        },
        "upstream": upstream,
        "downstream": downstream,
        "influences": [],
        "contents": _topology_node_contents(
            root,
            node_id,
            node,
            overlay,
            upstream_count=len(upstream),
            downstream_count=len(downstream),
        ),
        "config_refs": [{"kind": "sensor_registry", "ref": node_id, "version": built["config_version"]}],
        "timestamps": {},
        "evidence_refs": [],
        "limitations": ["no honest per-node runtime timestamp source yet for configured topology nodes"],
    }


def _trace_topology_edge(root: Path, edge_id: str) -> dict[str, Any] | None:
    """Trace for a configured `/topology` edge: endpoints plus an honest traversal-evidence note.

    `/runtime-status` edges are always empty in this slice (no honest current-
    traversal source yet), so an edge trace can describe what is configured
    but must not claim runtime evidence it does not have.
    """
    built = _build_topology(root)
    edge = next((e for e in built["edges"] if e["id"] == edge_id), None)
    if edge is None:
        return None
    return {
        "subject": {
            "id": edge_id,
            "type": "edge",
            "kind": edge["kind"],
            "origin": "topology",
            "status": edge.get("status"),
        },
        "upstream": [_trace_compact_ref(edge["from"], "topology_node", "configured_source")],
        "downstream": [_trace_compact_ref(edge["to"], "topology_node", "configured_target")],
        "influences": [],
        "contents": {
            "role": "configured_edge",
            "kind": _safe_surface_atom("edge_kind", edge.get("kind")),
            "status": _safe_surface_atom("edge_status", edge.get("status")),
            "enabled": edge.get("enabled") if isinstance(edge.get("enabled"), bool) else None,
            "upstream_count": 1,
            "downstream_count": 1,
        },
        "config_refs": [{"kind": "topology_edges", "ref": edge_id, "version": built["config_version"]}],
        "timestamps": {},
        "evidence_refs": [],
        "limitations": [
            "no honest current-traversal/runtime evidence for this edge yet; "
            "/runtime-status edges stay empty until a future slice"
        ],
    }


def _trace_runtime_edge(root: Path, edge_id: str) -> dict[str, Any] | None:
    raw_registry = _read_json(root / "sensors" / "registry.json", {})
    blocks_obj = raw_registry.get("blocks") if isinstance(raw_registry, dict) else {}
    if not isinstance(blocks_obj, dict) and isinstance(raw_registry, dict):
        blocks_obj = raw_registry
    if not isinstance(blocks_obj, dict):
        blocks_obj = {}

    is_stale, _ = _runtime_is_stale(root)
    topology_nodes = _runtime_node_overlays(blocks_obj, active_sensors=_recent_signal_sensors(root), is_stale=is_stale)
    candidates, _ = _read_jsonl(root, "candidates", limit=2000)
    threads, _ = _read_jsonl(root, "threads", limit=2000)
    actions, _ = _read_jsonl(root, "thread_actions", limit=2000)
    outbox, _ = _read_jsonl(root, "outbox", limit=2000)
    thread_by_id = _index_by_id(threads)
    action_by_id = _index_by_id(actions)
    action_for_outbox = _find_action_for_outbox(actions)
    config = _read_json(root / "instance.config.json", {})
    eligible_candidates = [c for c in candidates if str(c.get("status") or "").strip().lower() in _RUNTIME_CANDIDATE_STATUS]
    eligible_threads = [t for t in threads if t.get("status") in ACTIVE_THREAD_STATUSES]
    eligible_actions = [a for a in actions if a.get("status") in ACTIVE_ACTION_STATUSES]
    outbox_safety_by_id = {
        str(o.get("id") or ""): _outbox_safety(
            o,
            thread_by_id=thread_by_id,
            action_by_id=action_by_id,
            action_for_outbox=action_for_outbox,
            config=config,
        )
        for o in outbox
    }
    eligible_outbox = [
        o for o in outbox if _outbox_counts_as_open(o, safety=outbox_safety_by_id.get(str(o.get("id") or "")))
    ]
    instance_nodes = (
        [_runtime_candidate_node(c) for c in sorted(eligible_candidates, key=_sort_key, reverse=True)[:_RUNTIME_INSTANCE_LIMIT]]
        + [_runtime_thread_node(t) for t in sorted(eligible_threads, key=_sort_key, reverse=True)[:_RUNTIME_INSTANCE_LIMIT]]
        + [_runtime_action_node(a) for a in sorted(eligible_actions, key=_sort_key, reverse=True)[:_RUNTIME_INSTANCE_LIMIT]]
        + [
            _runtime_outbox_node(
                o,
                safety=outbox_safety_by_id.get(str(o.get("id") or "")) or {},
            )
            for o in sorted(eligible_outbox, key=_sort_key, reverse=True)[:_RUNTIME_INSTANCE_LIMIT]
        ]
    )
    nodes = topology_nodes + instance_nodes
    node_ids = {str(n.get("id") or "") for n in nodes}
    edge = next((e for e in _runtime_flow_edges(
        root,
        visible_node_ids=node_ids,
        candidates=eligible_candidates,
        threads=eligible_threads,
        actions=eligible_actions,
        outbox=eligible_outbox,
    ) if e.get("id") == edge_id), None)
    if edge is None:
        return None
    from_id = str(edge.get("from") or "")
    to_id = str(edge.get("to") or "")
    return {
        "subject": {
            "id": edge_id,
            "type": "edge",
            "kind": _safe_surface_atom("edge_kind", edge.get("kind")),
            "origin": "runtime_projection",
            "status": _safe_surface_atom("runtime_status", edge.get("status")),
        },
        "upstream": [_trace_compact_ref(from_id, from_id.split(":", 1)[0] or "node", "runtime_source")],
        "downstream": [_trace_compact_ref(to_id, to_id.split(":", 1)[0] or "node", "runtime_target")],
        "influences": [],
        "contents": {
            "role": "runtime_projection_edge",
            "kind": _safe_surface_atom("edge_kind", edge.get("kind")),
            "status": _safe_surface_atom("runtime_status", edge.get("status")),
            "source": _safe_surface_atom("runtime_source", edge.get("source")),
            "observed": bool(edge.get("observed")),
            "projection": bool(edge.get("projection")),
            "upstream_count": 1,
            "downstream_count": 1,
        },
        "config_refs": [],
        "timestamps": {"observed_at": edge.get("ts")},
        "evidence_refs": [],
        "limitations": [
            "runtime edges are bounded compact relation evidence from JSONL lineage fields, not a complete traversal log"
        ],
    }


def _trace_candidate(root: Path, candidate_node_id: str) -> dict[str, Any] | None:
    """Trace for a runtime candidate node: upstream perception, downstream effects, settlement evidence.

    Mirrors the candidate_id/receipt join used by `/snapshot`'s perception
    traces (`_candidate_label_indexes` + `_receipt_subject_label`) so this
    stays consistent with the existing settlement-evidence contract instead
    of inventing a second join scheme.
    """
    candidates, _ = _read_jsonl(root, "candidates", limit=5000)
    target = None
    for candidate in candidates:
        if f"candidate:{_safe_trace_candidate_id(candidate.get('id'))}" == candidate_node_id:
            target = candidate
            break
    if target is None:
        return None
    cand_id = str(target.get("id") or "")

    events, _ = _read_jsonl(root, "events", limit=5000)
    signals, _ = _read_jsonl(root, "signals", limit=5000)
    event_by_id = _index_by_id(events)
    signal_by_id = _index_by_id(signals)
    event_ids = [str(e) for e in (target.get("event_ids") or []) if e]
    trace_events = [event_by_id[eid] for eid in event_ids if eid in event_by_id][:6]
    signal_ids: list[str] = []
    for event in trace_events:
        for sid in event.get("source_signal_ids") or []:
            sid = str(sid)
            if sid and sid not in signal_ids:
                signal_ids.append(sid)
    trace_signals = [signal_by_id[sid] for sid in signal_ids if sid in signal_by_id][:8]

    decisions, _ = _read_jsonl(root, "decisions", limit=5000)
    _, label_to_candidate_id = _candidate_label_indexes(candidates)
    receipts = []
    for row in decisions:
        if not isinstance(row, dict) or row.get("type") not in SETTLEMENT_RECEIPT_TYPES:
            continue
        subject_label = _receipt_subject_label(row)
        row_cand_id = label_to_candidate_id.get(subject_label, "") or str(row.get("candidate_id") or "")
        if row_cand_id == cand_id:
            receipts.append(row)
    receipts.sort(key=lambda r: str(r.get("ts") or ""))
    settlement = _trace_settlement(target, receipts)

    actions, _ = _read_jsonl(root, "thread_actions", limit=5000)
    outbox, _ = _read_jsonl(root, "outbox", limit=5000)
    downstream_actions = [a for a in actions if str(a.get("origin_candidate_id") or "") == cand_id]
    downstream_outbox = [o for o in outbox if str(o.get("origin_candidate_id") or "") == cand_id]

    upstream = [
        _trace_compact_ref(_safe_trace_ref("signal", s.get("id")), "signal", "perceived") for s in trace_signals
    ] + [
        _trace_compact_ref(_safe_trace_ref("event", e.get("id")), "event", "raised") for e in trace_events
    ]
    downstream = [
        _trace_compact_ref(f"action:{_safe_surface_atom('action', a.get('id'))}", "action", "downstream_action")
        for a in downstream_actions[:12]
    ] + [
        _trace_compact_ref(f"outbox:{_safe_surface_atom('outbox', o.get('id'))}", "outbox", "downstream_outbox")
        for o in downstream_outbox[:12]
    ]
    if settlement and settlement.get("decision"):
        downstream.append(
            _trace_compact_ref(_safe_surface_atom("decision", settlement["decision"]), "receipt", "settled_by")
        )

    evidence_refs: list[dict[str, str]] = []
    if settlement:
        for key in ("intake_task_id", "review_task_id", "conscious_task_id"):
            ref = settlement.get(key)
            if ref:
                evidence_refs.append({"type": key.removesuffix("_id"), "ref": ref})

    limitations = []
    if not trace_events:
        limitations.append("no source events recorded for this candidate")
    if not settlement:
        limitations.append("no settlement/decision receipt found for this candidate yet")

    return {
        "subject": {
            "id": candidate_node_id,
            "type": "node",
            "kind": "candidate",
            "origin": "instance",
            "status": _safe_surface_atom("candidate_status", target.get("status")),
        },
        "upstream": upstream[:24],
        "downstream": downstream[:24],
        "influences": _safe_trace_ref_list("correlation", target.get("correlation_keys") or [], limit=8),
        "contents": _instance_contents("candidate", target, upstream_count=len(upstream[:24]), downstream_count=len(downstream[:24])),
        "config_refs": [],
        "timestamps": {
            "created_at": target.get("created_at"),
            "updated_at": target.get("updated_at") or target.get("created_at"),
            "settled_at": settlement.get("settled_at") if settlement else None,
        },
        "evidence_refs": evidence_refs[:12],
        "limitations": limitations,
    }


def _trace_thread(root: Path, thread_node_id: str) -> dict[str, Any] | None:
    threads, _ = _read_jsonl(root, "threads", limit=5000)
    target = None
    for thread in threads:
        if f"thread:{_safe_surface_atom('thread', thread.get('id'))}" == thread_node_id:
            target = thread
            break
    if target is None:
        return None
    thread_id = str(target.get("id") or "")
    actions, _ = _read_jsonl(root, "thread_actions", limit=5000)
    downstream_actions = [a for a in actions if str(a.get("origin_thread_id") or "") == thread_id]

    upstream = []
    origin_candidate_id = target.get("origin_candidate_id")
    if origin_candidate_id:
        upstream.append(
            _trace_compact_ref(
                f"candidate:{_safe_trace_candidate_id(origin_candidate_id)}", "candidate", "origin_candidate"
            )
        )
    downstream = [
        _trace_compact_ref(f"action:{_safe_surface_atom('action', a.get('id'))}", "action", "downstream_action")
        for a in downstream_actions[:24]
    ]
    return {
        "subject": {
            "id": thread_node_id,
            "type": "node",
            "kind": "thread",
            "origin": "instance",
            "status": _safe_surface_atom("thread_status", target.get("status")),
        },
        "upstream": upstream,
        "downstream": downstream,
        "influences": [],
        "contents": _instance_contents("thread", target, upstream_count=len(upstream), downstream_count=len(downstream)),
        "config_refs": [],
        "timestamps": {
            "created_at": target.get("created_at"),
            "updated_at": target.get("updated_at"),
            "expires_at": target.get("expires_at"),
        },
        "evidence_refs": [],
        "limitations": [] if downstream_actions else ["no thread actions recorded yet for this thread"],
    }


def _trace_action(root: Path, action_node_id: str) -> dict[str, Any] | None:
    actions, _ = _read_jsonl(root, "thread_actions", limit=5000)
    target = None
    for action in actions:
        if f"action:{_safe_surface_atom('action', action.get('id'))}" == action_node_id:
            target = action
            break
    if target is None:
        return None

    upstream = []
    if target.get("origin_thread_id"):
        upstream.append(
            _trace_compact_ref(
                f"thread:{_safe_surface_atom('thread', target.get('origin_thread_id'))}", "thread", "origin_thread"
            )
        )
    if target.get("origin_candidate_id"):
        upstream.append(
            _trace_compact_ref(
                f"candidate:{_safe_trace_candidate_id(target.get('origin_candidate_id'))}",
                "candidate",
                "origin_candidate",
            )
        )
    outbox_refs = _action_attachment_ids(target, "outbox_request")
    artifact_refs = _action_attachment_ids(target, "artifact_ref")
    downstream = [
        _trace_compact_ref(f"outbox:{_safe_surface_atom('outbox', ref)}", "outbox", "attached_outbox")
        for ref in outbox_refs[:12]
    ] + [
        _trace_compact_ref(_safe_surface_atom("artifact", ref), "artifact", "attached_artifact")
        for ref in artifact_refs[:12]
    ]
    return {
        "subject": {
            "id": action_node_id,
            "type": "node",
            "kind": "action",
            "origin": "instance",
            "status": _safe_surface_atom("action_status", target.get("status")),
        },
        "upstream": upstream,
        "downstream": downstream,
        "influences": [],
        "contents": _instance_contents("action", target, upstream_count=len(upstream), downstream_count=len(downstream)),
        "config_refs": [],
        "timestamps": {"updated_at": target.get("updated_at") or target.get("ts")},
        "evidence_refs": [],
        "limitations": [] if downstream else ["no outbox/artifact attachments recorded for this action"],
    }


def _trace_outbox(root: Path, outbox_node_id: str) -> dict[str, Any] | None:
    outbox, _ = _read_jsonl(root, "outbox", limit=5000)
    target = None
    for req in outbox:
        if f"outbox:{_safe_surface_atom('outbox', req.get('id'))}" == outbox_node_id:
            target = req
            break
    if target is None:
        return None

    threads, _ = _read_jsonl(root, "threads", limit=5000)
    actions, _ = _read_jsonl(root, "thread_actions", limit=5000)
    thread_by_id = _index_by_id(threads)
    action_by_id = _index_by_id(actions)
    action_for_outbox = _find_action_for_outbox(actions)
    config = _read_json(root / "instance.config.json", {})
    safety = _outbox_safety(
        target,
        thread_by_id=thread_by_id,
        action_by_id=action_by_id,
        action_for_outbox=action_for_outbox,
        config=config,
    )
    upstream = []
    if target.get("origin_thread_id"):
        upstream.append(
            _trace_compact_ref(
                f"thread:{_safe_surface_atom('thread', target.get('origin_thread_id'))}", "thread", "origin_thread"
            )
        )
    if target.get("origin_candidate_id"):
        upstream.append(
            _trace_compact_ref(
                f"candidate:{_safe_trace_candidate_id(target.get('origin_candidate_id'))}",
                "candidate",
                "origin_candidate",
            )
        )
    attached_action_id = action_for_outbox.get(str(target.get("id") or ""))
    if attached_action_id:
        upstream.append(
            _trace_compact_ref(f"action:{_safe_surface_atom('action', attached_action_id)}", "action", "attached_action")
        )

    status = str(target.get("status") or "")
    derived_status = "settled" if safety.get("label") == "historical_prepared_pointer" else status
    limitations = []
    if safety.get("label") == "historical_prepared_pointer":
        limitations.append("historical prepared pointer attached to a completed action on a closed thread; no outbound send path")
    elif status not in {"dispatched", "failed"}:
        limitations.append("no dispatch receipt yet; outbound delivery has not been observed for this record")
    target_for_contents = dict(target)
    target_for_contents["status"] = derived_status

    return {
        "subject": {
            "id": outbox_node_id,
            "type": "node",
            "kind": "outbox",
            "origin": "instance",
            "status": _safe_surface_atom("outbox_status", derived_status),
        },
        "upstream": upstream,
        "downstream": [],
        "influences": [],
        "contents": _instance_contents("outbox", target_for_contents, upstream_count=len(upstream), downstream_count=0),
        "config_refs": [],
        "timestamps": {"created_at": target.get("created_at"), "updated_at": target.get("updated_at")},
        "evidence_refs": [],
        "limitations": limitations,
    }


@router.get("/trace")
async def trace(node_id: str | None = None, edge_id: str | None = None, instance: str | None = None) -> dict[str, Any]:
    """Read-only compact-only trace/provenance projection for one selected node or edge (sera-ck9.3).

    Answers "where did this come from, what influenced it, what does it lead
    to" for whatever a user selects in the flow-DAG UI:
      - a configured `/topology` node or edge (kind/status/config_version,
        configured neighbors; no fabricated runtime traversal evidence)
      - a runtime candidate/thread/action/outbox node from `/runtime-status`
        (upstream perception, downstream effects, settlement evidence already
        carried by existing compact state)
    Every id/label passes through the same `_safe_surface_*`/`_safe_trace_*`
    helpers used elsewhere in this module, so this never echoes raw ids,
    config blobs, file paths, or free-form hostile text. GET-only; never
    mutates Sensorium state. If a requested relation cannot be honestly
    derived from existing compact state, the corresponding list stays empty
    and `meta.limitations` says so instead of fabricating causality.
    """
    resolved = _resolve_instance(instance)
    if resolved is None:
        return {"ok": False, "error": "invalid_instance"}
    effective_instance, root = resolved

    built_trace: dict[str, Any] | None = None
    if edge_id:
        built_trace = _trace_runtime_edge(root, edge_id) if edge_id.startswith("runtime:") else _trace_topology_edge(root, edge_id)
    elif node_id:
        prefix = node_id.split(":", 1)[0] if ":" in node_id else ""
        if prefix in _TRACE_TOPOLOGY_NODE_KINDS:
            built_trace = _trace_topology_node(root, node_id)
        elif prefix == "candidate":
            built_trace = _trace_candidate(root, node_id)
        elif prefix == "thread":
            built_trace = _trace_thread(root, node_id)
        elif prefix == "action":
            built_trace = _trace_action(root, node_id)
        elif prefix == "outbox":
            built_trace = _trace_outbox(root, node_id)

    ts = _now()
    base = {
        "ok": True,
        "privacy": "compact_only",
        "generated_at": ts,
        "as_of": ts,
        "instance": effective_instance,
    }
    if built_trace is None:
        return {
            **base,
            "subject": None,
            "upstream": [],
            "downstream": [],
            "influences": [],
            "contents": {},
            "config_refs": [],
            "timestamps": {},
            "evidence_refs": [],
            "meta": {
                "privacy": "compact_only",
                "limitations": ["no node_id/edge_id given, or subject not found in topology/instance state"],
            },
        }

    limitations = built_trace.pop("limitations", [])
    return {
        **base,
        **built_trace,
        "meta": {"privacy": "compact_only", "limitations": limitations},
    }


@router.get("/probe-audit")
async def probe_audit(instance: str | None = None, limit: int = 100) -> dict[str, Any]:
    """Read-only compact run-state/probe audit projection."""
    resolved = _resolve_instance(instance)
    if resolved is None:
        return {"ok": False, "error": "invalid_instance"}
    effective_instance, root = resolved
    run_state_dir = root / "sensors" / "run_state"
    files = sorted(run_state_dir.glob("*.json"), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)[:limit] if run_state_dir.exists() else []
    items: list[dict[str, Any]] = []
    for path in files:
        row = _read_json(path, {})
        data = row if isinstance(row, dict) else {}
        items.append({
            "id": _safe_surface_atom("run_state", path.stem),
            "status": _safe_surface_atom("run_state_status", data.get("status")),
            "reason": _safe_surface_text("reason", data.get("reason") or data.get("last_reason"), limit=140),
            "last_run_at": data.get("last_run_at") or data.get("updated_at") or data.get("ts"),
            "mtime": _mtime(path),
        })
    return {"ok": True, "generated_at": _now(), "instance": effective_instance, "items": items, "meta": {"count": len(items), "privacy": "compact_only"}}


@router.get("/dampeners")
async def dampeners(instance: str | None = None, limit: int = 100) -> dict[str, Any]:
    """Read-only compact dampener audit projection."""
    resolved = _resolve_instance(instance)
    if resolved is None:
        return {"ok": False, "error": "invalid_instance"}
    effective_instance, root = resolved
    rows = _read_inner_life_rows(root, "dampeners.jsonl", limit)
    items = [_sanitize_dampener(row) for row in rows]
    return {"ok": True, "generated_at": _now(), "instance": effective_instance, "items": items, "meta": {"count": len(items), "privacy": "compact_only"}}


@router.get("/blockers")
async def blockers(instance: str | None = None, limit: int = 100) -> dict[str, Any]:
    """Read-only compact blocker audit projection."""
    resolved = _resolve_instance(instance)
    if resolved is None:
        return {"ok": False, "error": "invalid_instance"}
    effective_instance, root = resolved
    rows = _read_inner_life_rows(root, "blockers.jsonl", limit)
    items = [_sanitize_blocker(row) for row in rows]
    return {"ok": True, "generated_at": _now(), "instance": effective_instance, "items": items, "meta": {"count": len(items), "privacy": "compact_only"}}


@router.get("/metrics")
async def metrics() -> dict[str, Any]:
    """Read-only Sensorium efficiency metrics time series."""
    return {"ok": True, "generated_at": _now(), "metrics": _metrics_snapshot(limit=288)}


@router.get("/graph")
async def graph(instance: str | None = None, limit_nodes: int = 200, limit_edges: int = 300) -> dict[str, Any]:
    """Read-only compact graph projection for receipt settlement links."""
    resolved = _resolve_instance(instance)
    if resolved is None:
        return {"ok": False, "error": "invalid_instance"}
    effective_instance, root = resolved
    projected = _receipt_graph(root, limit_nodes=limit_nodes, limit_edges=limit_edges)
    return {
        "ok": True,
        "generated_at": _now(),
        "instance": effective_instance,
        "meta": {
            "node_count": len(projected["nodes"]),
            "edge_count": len(projected["edges"]),
            "truncated": projected["truncated"],
            "privacy": "compact_only",
        },
        "nodes": projected["nodes"],
        "edges": projected["edges"],
    }


@router.get("/explanation")
async def explanation(subject_id: str, instance: str | None = None) -> dict[str, Any]:
    """Read-only deterministic pressure explanation for one candidate/review.

    Answers "why did this candidate surface, and what pushed back?" from compact
    candidate rows plus recent dampener/blocker sidecar evidence. Pure and
    output-boundary safe: the explanation builder hash-labels every free-form
    scalar, so this never echoes raw ids/summaries/secrets. GET-only; it never
    mutates Sensorium state.
    """
    try:
        import sys

        plugin_root = Path(__file__).resolve().parents[1]
        if str(plugin_root) not in sys.path:
            sys.path.insert(0, str(plugin_root))
        from agent_sensorium.explanations import build_explanation
        from agent_sensorium.store import SensoriumStore
    except ImportError:
        return {"ok": False, "error": "agent_sensorium not importable"}

    resolved = _resolve_instance(instance)
    if resolved is None:
        return {"ok": False, "error": "invalid_instance"}
    effective_instance, root = resolved
    store = SensoriumStore(instance=effective_instance, state_dir=str(root))
    try:
        candidates = _index_by_id(store.read_jsonl("candidates"))
        dampeners = store.read_dampener_effects(limit=200)
        blockers = store.read_blocker_effects(limit=200)
        built = build_explanation(
            subject_id,
            records=candidates,
            dampener_evidence=dampeners,
            blocker_evidence=blockers,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "generated_at": _now(), "instance": effective_instance, "explanation": built}


@router.get("/world-model/search")
async def world_model_search(query: str, limit: int = 10, instance: str | None = None) -> dict[str, Any]:
    return await _world_model_read(instance, {"operation": "search", "query": query, "limit": limit})


@router.get("/world-model/pages/{object_id}")
async def world_model_page(object_id: str, instance: str | None = None) -> dict[str, Any]:
    return await _world_model_read(instance, {"operation": "page", "object_id": object_id})


@router.get("/world-model/pages/{object_id}/relations")
async def world_model_relations(object_id: str, hops: int = 1, instance: str | None = None) -> dict[str, Any]:
    return await _world_model_read(instance, {"operation": "relations", "object_id": object_id, "hops": hops})


@router.get("/world-model/pages/{object_id}/trace")
async def world_model_trace(object_id: str, instance: str | None = None) -> dict[str, Any]:
    return await _world_model_read(instance, {"operation": "trace", "object_id": object_id})


@router.get("/snapshot")
async def snapshot(instance: str | None = None) -> dict[str, Any]:
    # `instance` is surfaced for multi-instance UI without allowing arbitrary
    # path traversal. Omitted and legacy `default` requests both resolve to the
    # configured default instance.
    resolved = _resolve_instance(instance)
    if resolved is None:
        return {"ok": False, "error": "invalid_instance"}
    effective_instance, root = resolved
    state = _read_json(root / "state.latest.json", {})
    config = _read_json(root / "instance.config.json", {})
    freshness = _freshness_snapshot(root)

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
    candidate_by_id = _index_by_id(candidates)
    action_by_id = _index_by_id(actions)
    artifact_by_id = _index_by_id(artifacts)
    event_by_id = _index_by_id(rows["events"])
    signal_by_id = _index_by_id(rows["signals"])
    action_for_outbox = _find_action_for_outbox(actions)

    active_candidates = [c for c in candidates if c.get("status") == ACTIVE_CANDIDATE_STATUS]
    active_candidates.sort(key=lambda c: float(c.get("pressure") or 0), reverse=True)

    visible_threads = [t for t in threads if t.get("status") in ACTIVE_THREAD_STATUSES]
    visible_threads.sort(key=_sort_key, reverse=True)

    recent_outbox = sorted(outbox, key=_sort_key, reverse=True)
    recent_signals = sorted(rows["signals"], key=_sort_key, reverse=True)
    recent_actions = sorted(actions, key=_sort_key, reverse=True)
    recent_artifacts = sorted(artifacts, key=_sort_key, reverse=True)
    artifact_groups = _artifact_groups(
        artifacts,
        thread_by_id=thread_by_id,
        action_by_id=action_by_id,
        candidate_by_id=candidate_by_id,
        limit=10,
    )
    open_actions = [a for a in actions if a.get("status") in ACTIVE_ACTION_STATUSES]
    closed_actions = [a for a in actions if a.get("status") not in ACTIVE_ACTION_STATUSES]
    perception_traces = _perception_traces(
        candidates,
        event_by_id=event_by_id,
        signal_by_id=signal_by_id,
        decisions=decisions,
    )

    outbox_items_all = [
        _outbox_item(
            r,
            thread_by_id=thread_by_id,
            action_by_id=action_by_id,
            action_for_outbox=action_for_outbox,
            config=config,
        )
        for r in recent_outbox
    ]
    outbox_items = outbox_items_all[:12]
    outbox_safety_by_id = {
        str(item.get("id") or ""): item.get("safety") if isinstance(item.get("safety"), dict) else {}
        for item in outbox_items_all
    }
    open_outbox = [
        r for r in outbox if _outbox_counts_as_open(r, safety=outbox_safety_by_id.get(str(r.get("id") or "")))
    ]
    prepared_outbox = [
        r for r in outbox if _outbox_counts_as_prepared(r, safety=outbox_safety_by_id.get(str(r.get("id") or "")))
    ]
    actionable_outbox = sum(1 for item in outbox_items_all if item.get("safety", {}).get("actionable"))
    historical_outbox = sum(
        1
        for item in outbox_items_all
        if item.get("safety", {}).get("label") in {"historical_prepared_pointer", "recorded", "dispatched"}
    )
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
        "artifact_groups": len(artifact_groups),
        "held_artifacts": sum(1 for a in artifacts if a.get("delivery_state") == "held_for_review"),
        "outbox": len(outbox),
        "prepared_outbox": len(prepared_outbox),
        "prepared_outbox_raw": sum(1 for o in outbox if str(o.get("status") or "").strip().lower() == "prepared"),
        "open_outbox": len(open_outbox),
        "open_outbox_raw": sum(1 for o in outbox if str(o.get("status") or "").strip().lower() in OPEN_OUTBOX_STATUSES),
        "historical_outbox": historical_outbox,
        "actionable_outbox": actionable_outbox,
        "lifecycle_warnings": len(warnings),
        "decisions": len(decisions),
        "closed_actions": len(closed_actions),
        "perception_traces": len(perception_traces),
        "perception_wrong_turns": sum(1 for t in perception_traces if t.get("band") in {"red", "yellow"}),
        "corrupt_lines": corrupt,
    }

    metrics_data = _metrics_snapshot()
    live_turn_metrics = _live_turn_metrics(decisions)
    live_turn_review_metrics = _live_turn_review_metrics(decisions)
    conscious_reachout_metrics = _conscious_reachout_metrics(decisions)
    attention_footprint = _attention_footprint(counts, metrics=metrics_data)

    return {
        "ok": True,
        "generated_at": _now(),
        "instance": effective_instance,
        "state_dir": _safe_surface_text("state_dir", str(root), limit=240),
        "state_exists": root.exists(),
        "state_mtime": freshness.get("canonical_latest_mtime"),
        "state_latest_mtime": freshness.get("legacy_state_latest", {}).get("mtime"),
        "kanban_tick_mtime": freshness.get("last_sensorium_kanban_tick", {}).get("mtime"),
        "freshness": freshness,
        "config": {
            "instance_name": _safe_surface_atom("instance", config.get("instance_name") or effective_instance),
            "allowed_surfaces": [_safe_surface_atom("surface", s) for s in (config.get("allowed_surfaces") or ["local"])
            ][:8],
            "max_sensitivity": _safe_surface_atom("sensitivity", config.get("max_sensitivity") or "private"),
            "policy_card_ref": _safe_surface_text("policy_card_ref", config.get("policy_card_ref"), limit=160),
            "outbox": _safe_surface_projection(
                "outbox_config", config.get("outbox") if isinstance(config.get("outbox"), dict) else {}, limit=160
            ),
        },
        "counts": counts,
        "attention_footprint": attention_footprint,
        "live_turn_metrics": live_turn_metrics,
        "live_turn_review_metrics": live_turn_review_metrics,
        "conscious_reachout_metrics": conscious_reachout_metrics,
        "health": _health(
            counts,
            len(visible_threads),
            actionable_outbox,
            len(open_actions),
            len(warnings),
            state,
        ),
        "status_breakdown": {
            "threads": dict(Counter(_safe_surface_atom("thread_status", t.get("status") or "unknown") for t in threads)),
            "candidates": dict(Counter(_safe_surface_atom("candidate_status", c.get("status") or "unknown") for c in candidates)),
            "actions": dict(Counter(_safe_surface_atom("action_status", a.get("status") or "unknown") for a in actions)),
            "artifacts": dict(Counter(_safe_surface_atom("delivery_state", a.get("delivery_state") or "unknown") for a in artifacts)),
            "outbox": dict(Counter(_safe_surface_atom("outbox_status", o.get("status") or "unknown") for o in outbox)),
        },
        "views": _dashboard_views(counts, recent_signals=len(recent_signals[:12]), footprint=attention_footprint),
        "perception_traces": perception_traces,
        "top_candidates": [_candidate_item(c) for c in active_candidates[:6]],
        "recent_signals": [_signal_item(s) for s in recent_signals[:12]],
        "threads": [_thread_item(t) for t in visible_threads[:8]],
        "actions": [_action_item(a) for a in recent_actions[:10]],
        "artifacts": [_artifact_item(a) for a in recent_artifacts[:10]],
        "artifact_groups": artifact_groups,
        "outbox": outbox_items,
        "lifecycle_warnings": warnings,
        "decisions": [_decision_item(d) for d in decisions[-14:]][::-1],
        "budgets": _current_budgets(root, config),
        "metrics": metrics_data,
    }

