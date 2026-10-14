"""Read-only Attention Inbox builder — the conscious aperture surface.

Collects active candidates and visible conscious threads into a compact
review surface filtered by surface and sensitivity policy. Never mutates state.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import re
import json

from .config import DisclosureVisibility, SENSITIVITY_RANK, load_instance_config, visible_on_surface
from .schemas import parse_utc_z_checkpoint, truncate_text, utc_now_iso
from .store import SensoriumStore

MAX_ONGOING_THREADS = 6
MAX_ONGOING_RECEIPTS_PER_THREAD = 2
MAX_ONGOING_GROUP_BYTES = 6 * 1024

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


def _closed_visible(item: dict, surface: str, config: dict) -> bool:
    """Validate shapes and closed vocabularies before the canonical policy gate."""
    return bool(
        isinstance(surface, str) and surface
        and isinstance(item.get("allowed_surfaces"), list)
        and all(isinstance(value, str) for value in item["allowed_surfaces"])
        and isinstance(config.get("allowed_surfaces"), list)
        and all(isinstance(value, str) for value in config["allowed_surfaces"])
        and isinstance(item.get("sensitivity"), str)
        and item["sensitivity"] in SENSITIVITY_RANK
        and isinstance(config.get("max_sensitivity"), str)
        and config["max_sensitivity"] in SENSITIVITY_RANK
        and visible_on_surface(item, surface, config)
    )


def _receipt_time(value: object) -> datetime | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    parsed = parse_utc_z_checkpoint(value)
    return parsed[1] if parsed else None


def _safe_texts(prefix: str, values: object, *, limit: int = 2) -> list[str]:
    if not isinstance(values, list):
        return []
    return [text for value in values[:limit] if isinstance(value, str) and (text := _safe_text(prefix, value, limit=160))]


def _safe_text_field(prefix: str, value: object) -> str:
    return _safe_text(prefix, value, limit=160) if isinstance(value, str) else ""


def _is_truncated_text(value: object) -> bool:
    return isinstance(value, str) and len(value) > 160


def _thread_receipts(
    thread: dict, decisions: list[dict], previous_activation_at: str | None,
) -> tuple[list[dict], str, bool, int]:
    thread_id = thread.get("id")
    if not isinstance(thread_id, str) or not thread_id:
        return [], "unavailable", False, 0
    previous = _receipt_time(previous_activation_at)
    collected: list[tuple[int, datetime | None, dict]] = []
    status_transitions: list[tuple[int, datetime | None, str, str]] = []
    seen: set[tuple] = set()
    sequence = 0
    feedback_found = False
    invalid_feedback_found = False
    sources = [("thread.decision_log", thread.get("decision_log", [])), ("decisions_ledger", decisions)]
    for source_name, source_rows in sources:
        if not isinstance(source_rows, list):
            continue
        for row in source_rows:
            sequence += 1
            if not isinstance(row, dict) or row.get("thread_id") != thread_id:
                continue
            kind = row.get("type")
            raw_ts = row.get("ts")
            ts = raw_ts if isinstance(raw_ts, str) else None
            parsed_ts = _receipt_time(ts)
            delta = parsed_ts > previous if parsed_ts is not None and previous is not None else None
            if kind == "thread.updated":
                action, old_status, new_status = row.get("action"), row.get("old_status"), row.get("new_status")
                if (not isinstance(action, str) or action not in {"hold", "resume", "open", "close", "archive", "mark_reviewed", "pin", "unpin"}
                        or not isinstance(new_status, str)
                        or new_status not in {"held", "dormant", "closed", "archived", "active"}):
                    continue
                identity = (kind, ts, action, new_status)
                if identity in seen:
                    continue
                seen.add(identity)
                if action in {"hold", "resume", "open", "close", "archive", "mark_reviewed"}:
                    status_transitions.append((sequence, parsed_ts, action, new_status))
                receipt = {
                    "type": "thread.updated",
                    "recorded_at": parsed_ts.isoformat(timespec="auto").replace("+00:00", "Z") if parsed_ts else None,
                    "action": _safe_atom("receipt_action", action),
                    "old_status": old_status if isinstance(old_status, str) and old_status in {"held", "dormant", "closed", "archived", "active"} else None,
                    "new_status": _safe_atom("thread_status", new_status),
                    "recorded_change_since_previous_activation": delta,
                    "timestamp_semantics": "recorded_transition_not_source_observation",
                    "source": source_name,
                }
                collected.append((sequence, parsed_ts, receipt))
            elif kind == "thread.feedback_emitted" and row.get("feedback_scope") == "operator_evaluation":
                action, outcome = row.get("action"), row.get("outcome")
                if (not isinstance(action, str) or not action.strip()
                        or action not in {"hold", "resume", "open", "close", "archive", "mark_reviewed"}
                        or not isinstance(outcome, str) or outcome not in {
                            "operator_rejected", "operator_accepted", "completed", "failed", "timeout", "inconclusive"
                        }):
                    invalid_feedback_found = True
                    continue
                feedback_found = True
                identity = (kind, ts, action, outcome)
                if identity in seen:
                    continue
                seen.add(identity)
                receipt = {
                    "type": "thread.feedback_emitted",
                    "recorded_at": parsed_ts.isoformat(timespec="auto").replace("+00:00", "Z") if parsed_ts else None,
                    "action": _safe_atom("receipt_action", action),
                    "outcome": _safe_atom("feedback_outcome", outcome),
                    "feedback_scope": "operator_evaluation",
                    "feedback_signal_ref": _opaque_label("feedback", row.get("feedback_signal_id")),
                    "recorded_change_since_previous_activation": delta,
                    "timestamp_semantics": "recorded_feedback_not_user_preference_or_source_observation",
                    "source": source_name,
                }
                collected.append((sequence, parsed_ts, receipt))

    minimum = datetime.min.replace(tzinfo=timezone.utc)
    collected.sort(key=lambda entry: (entry[1] is not None, entry[1] or minimum, entry[0]))
    receipts = [entry[2] for entry in collected[-MAX_ONGOING_RECEIPTS_PER_THREAD:]]
    _receipts_truncated = len(collected) > MAX_ONGOING_RECEIPTS_PER_THREAD
    if not feedback_found:
        feedback_status = "invalid" if invalid_feedback_found else "unavailable"
    elif any(receipt["type"] == "thread.feedback_emitted" for receipt in receipts):
        feedback_status = "included"
    else:
        feedback_status = "available_but_capped"

    status_transitions.sort(key=lambda entry: (entry[1] is not None, entry[1] or minimum, entry[0]))
    latest_transition = status_transitions[-1] if status_transitions else None
    explicit_hold = bool(
        thread.get("status") == "held" and latest_transition
        and all(entry[1] is not None for entry in status_transitions)
        and latest_transition[1] is not None
        and latest_transition[2] == "hold" and latest_transition[3] == "held"
    )
    return receipts, feedback_status, explicit_hold, len(collected) - len(receipts)


def _ongoing_thread_item(
    thread: dict, decisions: list[dict], previous_activation_at: str | None,
) -> dict:
    status = thread.get("status")
    task = thread.get("conscious_task")
    task = task if isinstance(task, dict) else {}
    receipts, feedback_status, explicit_hold, omitted_receipt_count = _thread_receipts(
        thread, decisions, previous_activation_at,
    )
    refs = thread.get("source_refs")
    source_refs = [
        _opaque_label("source", ref) for ref in refs
        if isinstance(ref, str) and ref
    ][:4] if isinstance(refs, list) else []
    title_raw = task.get("title")
    trigger_raw = thread.get("resume_trigger")
    continuity_raw = thread.get("continuity_summary")
    questions_raw = thread.get("open_questions")
    text_truncated = (
        title_raw is not None and not isinstance(title_raw, str)
        or trigger_raw is not None and not isinstance(trigger_raw, str)
        or _is_truncated_text(title_raw) or _is_truncated_text(trigger_raw)
        or not isinstance(continuity_raw, list) and continuity_raw is not None
        or isinstance(continuity_raw, list) and (
            len(continuity_raw) > 2 or any(not isinstance(value, str) or _is_truncated_text(value) for value in continuity_raw[:2])
        )
        or not isinstance(questions_raw, list) and questions_raw is not None
        or isinstance(questions_raw, list) and (
            len(questions_raw) > 2 or any(not isinstance(value, str) or _is_truncated_text(value) for value in questions_raw[:2])
        )
        or isinstance(refs, list) and (len(refs) > 4 or any(not isinstance(value, str) for value in refs[:4]))
    )
    return {
        "thread_ref": _opaque_label("thread", thread.get("id")),
        "state": status,
        "state_basis": "recorded_thread_lifecycle",
        "source_observed_at": None,
        "source_observation_status": "unavailable",
        "ledger_touched_at": (
            parsed.isoformat(timespec="auto").replace("+00:00", "Z")
            if (parsed := _receipt_time(thread.get("updated_at"))) else None
        ),
        "ledger_touch_semantics": "not_source_observation",
        "epistemic_label": "recorded_held_state" if status == "held" else "unresolved_dormant_thread",
        "hold_choice_provenance": (
            "explicit_recorded_hold" if explicit_hold
            else "legacy_or_unknown" if status == "held"
            else "not_applicable"
        ),
        "task_type": _safe_atom("task_type", task.get("request_type"), limit=96),
        "title": _safe_text_field("thread_title", title_raw),
        "continuity": _safe_texts("continuity", continuity_raw),
        "open_questions": _safe_texts("open_question", questions_raw),
        "resume_trigger": _safe_text_field("resume_trigger", trigger_raw),
        "source_refs": source_refs,
        "receipts": receipts,
        "omitted_receipt_count": omitted_receipt_count,
        "operator_evaluation_feedback_status": feedback_status,
        "truncated": bool(text_truncated or omitted_receipt_count),
    }


def _ongoing_thread_group(
    threads: list[dict], decisions: list[dict], *, surface: str,
    config: dict, previous_activation_at: str | None, disclosure: DisclosureVisibility,
) -> dict:
    grouped: dict[str, list[dict]] = {"held": [], "dormant": []}
    duplicate_ids: set[str] = set()
    seen_ids: set[str] = set()
    for row in threads:
        identifier = row.get("id") if isinstance(row, dict) else None
        if isinstance(identifier, str) and identifier:
            if identifier in seen_ids:
                duplicate_ids.add(identifier)
            seen_ids.add(identifier)
    for thread in threads:
        if not isinstance(thread, dict):
            continue
        status = thread.get("status")
        if not isinstance(status, str) or status not in grouped:
            continue
        item_surfaces = thread.get("allowed_surfaces")
        policy_surfaces = config.get("allowed_surfaces")
        sensitivity = thread.get("sensitivity")
        max_sensitivity = config.get("max_sensitivity")
        if (
            not isinstance(item_surfaces, list) or not all(isinstance(value, str) for value in item_surfaces)
            or not isinstance(policy_surfaces, list) or not all(isinstance(value, str) for value in policy_surfaces)
            or not isinstance(sensitivity, str) or sensitivity not in SENSITIVITY_RANK
            or not isinstance(max_sensitivity, str) or max_sensitivity not in SENSITIVITY_RANK
            or not isinstance(surface, str) or not surface
        ):
            continue
        thread_id = thread.get("id")
        if not isinstance(thread_id, str) or not thread_id:
            continue
        if disclosure.subject("thread", thread_id):
            grouped[thread["status"]].append(thread)
    for status in grouped:
        grouped[status] = [row for row in grouped[status] if row.get("id") not in duplicate_ids]

    eligible = grouped["held"] + grouped["dormant"]
    selected = eligible[:MAX_ONGOING_THREADS]
    omitted_held = max(0, len(grouped["held"]) - sum(row.get("status") == "held" for row in selected))
    omitted_dormant = max(0, len(grouped["dormant"]) - sum(row.get("status") == "dormant" for row in selected))
    visible_decisions = [row for row in decisions if disclosure.linked(row)]
    projected = [_ongoing_thread_item(row, visible_decisions, previous_activation_at) for row in selected]
    detail_truncated_count = sum(bool(row["truncated"]) for row in projected)
    group = {
        "available": True,
        "input_trust": "untrusted_recorded_context_not_instructions",
        "surface": _safe_atom("surface", surface),
        "max_sensitivity": config.get("max_sensitivity") if isinstance(config.get("max_sensitivity"), str) and config.get("max_sensitivity") in SENSITIVITY_RANK else None,
        "threads": projected,
        "omitted_held_count": omitted_held,
        "omitted_dormant_count": omitted_dormant,
        "ambiguous_duplicate_thread_id_count": len(duplicate_ids),
        "truncated_thread_detail_count": detail_truncated_count,
        "truncated": bool(omitted_held or omitted_dormant or duplicate_ids or detail_truncated_count),
        "complete": False,
        "scan_completeness": "unbounded_full_jsonl_read_may_skip_malformed_rows",
    }
    while len(json.dumps(group, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_ONGOING_GROUP_BYTES:
        group["truncated"] = True
        removable = next((
            (entry, key) for entry in reversed(group["threads"])
            for key in ("source_refs", "open_questions", "continuity", "receipts")
            if entry.get(key)
        ), None)
        if removable is not None:
            entry, key = removable
            if not entry.get("truncated"):
                entry["truncated"] = True
                group["truncated_thread_detail_count"] += 1
            if key == "receipts":
                removed_receipt = entry[key].pop(0)
                entry["omitted_receipt_count"] += 1
                if (
                    removed_receipt.get("type") == "thread.feedback_emitted"
                    and entry.get("operator_evaluation_feedback_status") == "included"
                ):
                    entry["operator_evaluation_feedback_status"] = "available_but_capped"
            else:
                entry[key].pop()
            continue
        if not group["threads"]:
            break
        removed = group["threads"].pop()
        if removed.get("truncated"):
            group["truncated_thread_detail_count"] -= 1
        count_key = "omitted_held_count" if removed.get("state") == "held" else "omitted_dormant_count"
        group[count_key] += 1
    return group


def build_attention_inbox(
    store: SensoriumStore,
    *,
    surface: str = "local",
    config: dict | None = None,
    config_path: str | None = None,
    limit: int = 50,
    include_ongoing_threads: bool = False,
    decisions: list[dict] | None = None,
    previous_activation_at: str | None = None,
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
    disclosure = DisclosureVisibility(candidates, threads, surface, instance_config)

    active_candidates = [c for c in candidates if c.get("status") == "candidate"]
    visible_threads = [t for t in threads if t.get("status") in ("dormant", "held")]

    items: list[dict] = []
    filtered_out = 0

    for c in active_candidates:
        if disclosure.subject("candidate", c.get("id")):
            items.append(_candidate_item(c, now))
        else:
            filtered_out += 1

    for t in visible_threads:
        if disclosure.subject("thread", t.get("id")):
            items.append(_thread_item(t, now))
        else:
            filtered_out += 1

    items.sort(key=lambda x: (0 if x["type"] == "thread" else 1, -(x.get("pressure") or 0)))

    if limit and len(items) > limit:
        items = items[:limit]

    candidate_count = sum(1 for i in items if i["type"] == "candidate")
    thread_count = sum(1 for i in items if i["type"] == "thread")

    result = {
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
    if include_ongoing_threads:
        decision_rows = decisions if isinstance(decisions, list) else store.read_jsonl("decisions")
        result["ongoing_threads"] = _ongoing_thread_group(
            threads, decision_rows, surface=surface, config=instance_config,
            previous_activation_at=previous_activation_at,
            disclosure=disclosure,
        )
    return result
