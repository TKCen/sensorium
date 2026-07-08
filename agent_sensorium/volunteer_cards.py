"""Read-only Sensorium volunteer context cards.

These cards are compact orientation handles assembled only from Sensorium-native
state. They never call Hindsight, Hermes LCM/session memory, or external
services, and they avoid raw transcript/message/prompt/artifact body text.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .attention import _freshness, _safe_atom, _safe_atom_list, _safe_text
from .config import load_instance_config, visible_on_surface
from .pointers import DEFAULT_POINTER_CONFIG, _saved_residue_candidates
from .schemas import truncate_text
from .store import SensoriumStore

_THREAD_TYPE_PRIORITY = 0
_CANDIDATE_TYPE_PRIORITY = 1
_SAVED_RESIDUE_TYPE_PRIORITY = 2
_ARTIFACT_TYPE_PRIORITY = 3

_ARTIFACT_STATE_PRIORITY = {
    "held_for_review": 0,
    "prepared": 1,
    "not_delivered": 2,
    "delivery_blocked": 3,
    "silenced": 4,
    "delivery_cancelled": 5,
}


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _ts_rank(*values: str | None) -> int:
    for value in values:
        dt = _parse_ts(value)
        if dt is not None:
            return int(dt.timestamp())
    return 0


def _confidence(card_type: str, freshness: str) -> str:
    if card_type in {"thread", "artifact"}:
        return "high"
    if card_type == "candidate":
        return "medium"
    if card_type == "saved_residue" and freshness in {"fresh", "recent"}:
        return "medium"
    return "low"


def _card_id(card_type: str, subject_id: str) -> str:
    return f"vcard:{card_type}:{subject_id}"


def _base_card(
    *,
    card_type: str,
    subject_kind: str,
    subject_id: str,
    summary: str,
    why_now: str,
    freshness: str,
    sensitivity: str,
    allowed_surfaces: list[str],
    reference_id: str,
    source_type: str,
    suggested_action: str,
) -> dict:
    safe_summary = _safe_text(f"{card_type}_summary", summary, limit=160)
    safe_why = _safe_text(f"{card_type}_why_now", why_now, limit=160)
    # reference_id / subject ids must remain stable and openable; preserve the
    # exact Sensorium-native ids rather than hashing them through secret-marker
    # heuristics intended for free text.
    safe_ref = truncate_text(str(reference_id or ""), 96)
    safe_subject_id = truncate_text(str(subject_id or ""), 96)
    safe_surfaces = _safe_atom_list("surface", allowed_surfaces, limit=8)
    safe_sensitivity = _safe_atom("sensitivity", sensitivity or "private", limit=32)
    return {
        "card_id": _card_id(card_type, safe_subject_id),
        "card_type": card_type,
        "subject_ref": {
            "kind": subject_kind,
            "id": safe_subject_id,
        },
        "reference_id": safe_ref,
        "openable_ref": {
            "action": "status",
            "reference_id": safe_ref,
        },
        "source": {
            "layer": "sensorium",
            "source_type": source_type,
        },
        "why_now": safe_why,
        "confidence": _confidence(card_type, freshness),
        "freshness": freshness,
        "privacy_scope": safe_sensitivity,
        "allowed_surfaces": safe_surfaces,
        "summary": safe_summary,
        "suggested_action": suggested_action,
        "dedupe_key": f"{card_type}:{safe_subject_id}:{safe_ref}",
    }


def _thread_card(thread: dict, now: datetime) -> tuple[tuple, dict]:
    task = thread.get("conscious_task") or {}
    title = task.get("title") or thread.get("next_prompt_to_operator") or "Sensorium thread"
    freshness = _freshness(thread.get("updated_at") or thread.get("created_at"), now)
    status = str(thread.get("status") or "dormant")
    card = _base_card(
        card_type="thread",
        subject_kind="thread",
        subject_id=str(thread.get("id") or ""),
        summary=title,
        why_now="A visible conscious thread already exists and can be reopened exactly.",
        freshness=freshness,
        sensitivity=str(thread.get("sensitivity") or "private"),
        allowed_surfaces=list(thread.get("allowed_surfaces") or []),
        reference_id=str(thread.get("id") or ""),
        source_type="thread",
        suggested_action="open_before_answer",
    )
    card["status"] = _safe_atom("thread_status", status, limit=32)
    return (
        _THREAD_TYPE_PRIORITY,
        0 if status == "dormant" else 1,
        -_ts_rank(thread.get("updated_at"), thread.get("created_at")),
        str(thread.get("id") or ""),
    ), card


def _candidate_card(candidate: dict, now: datetime) -> tuple[tuple, dict]:
    summary = candidate.get("summary", "") or "Sensorium salience"
    freshness = _freshness(candidate.get("updated_at") or candidate.get("created_at"), now)
    try:
        pressure = float(candidate.get("pressure") or 0.0)
    except (TypeError, ValueError):
        pressure = 0.0
    card = _base_card(
        card_type="candidate",
        subject_kind="candidate",
        subject_id=str(candidate.get("id") or ""),
        summary=summary,
        why_now="An active unresolved salience candidate is still visible on this surface.",
        freshness=freshness,
        sensitivity=str(candidate.get("sensitivity") or "private"),
        allowed_surfaces=list(candidate.get("allowed_surfaces") or []),
        reference_id=str(candidate.get("id") or ""),
        source_type="candidate",
        suggested_action="open_before_answer",
    )
    card["pressure"] = pressure
    card["kind"] = _safe_atom("candidate_kind", candidate.get("kind") or "", limit=64)
    return (
        _CANDIDATE_TYPE_PRIORITY,
        -pressure,
        -_ts_rank(candidate.get("updated_at"), candidate.get("created_at")),
        str(candidate.get("id") or ""),
    ), card


def _saved_residue_card(candidate: dict, now: datetime) -> tuple[tuple, dict]:
    settlement = candidate.get("kanban_settlement") or {}
    summary = candidate.get("summary", "") or "Saved Sensorium residue"
    freshness = _freshness(
        settlement.get("settled_at") or candidate.get("updated_at") or candidate.get("created_at"),
        now,
    )
    try:
        pressure = float(candidate.get("pressure") or 0.0)
    except (TypeError, ValueError):
        pressure = 0.0
    decision = str(settlement.get("decision") or "SAVE")
    card = _base_card(
        card_type="saved_residue",
        subject_kind="candidate",
        subject_id=str(candidate.get("id") or ""),
        summary=summary,
        why_now="A previously saved residue remains the best continuity handle even though no thread is open.",
        freshness=freshness,
        sensitivity=str(candidate.get("sensitivity") or "private"),
        allowed_surfaces=list(candidate.get("allowed_surfaces") or []),
        reference_id=str(candidate.get("id") or ""),
        source_type="decision_receipt",
        suggested_action="use_context",
    )
    card["kind"] = _safe_atom("candidate_kind", candidate.get("kind") or "", limit=64)
    card["pressure"] = pressure
    card["kanban_settlement"] = {
        "decision": _safe_atom("settlement_decision", decision, limit=32),
        "intake_task_id": _safe_atom("intake_task", settlement.get("intake_task_id") or "", limit=96),
        "review_task_id": _safe_atom("review_task", settlement.get("review_task_id") or "", limit=96),
    }
    return (
        _SAVED_RESIDUE_TYPE_PRIORITY,
        -pressure,
        -_ts_rank(settlement.get("settled_at"), candidate.get("updated_at"), candidate.get("created_at")),
        str(candidate.get("id") or ""),
    ), card


def _artifact_parent_ref(artifact: dict) -> str:
    source_refs = artifact.get("source_refs") or {}
    return str(
        source_refs.get("thread_id")
        or source_refs.get("candidate_id")
        or source_refs.get("action_id")
        or ""
    )


def _artifact_card(artifact: dict, now: datetime) -> tuple[tuple, dict] | None:
    parent_ref = _artifact_parent_ref(artifact)
    if not parent_ref:
        return None
    delivery_state = str(artifact.get("delivery_state") or "not_delivered")
    summary = artifact.get("why_created", "") or f"{artifact.get('kind', 'artifact')} artifact"
    freshness = _freshness(artifact.get("created_at") or artifact.get("updated_at"), now)
    card = _base_card(
        card_type="artifact",
        subject_kind="artifact",
        subject_id=str(artifact.get("id") or ""),
        summary=summary,
        why_now="A linked Sensorium artifact already exists for review and points to an exact subject.",
        freshness=freshness,
        sensitivity=str(artifact.get("sensitivity") or "private"),
        allowed_surfaces=list(artifact.get("allowed_surfaces") or []),
        reference_id=parent_ref,
        source_type="artifact",
        suggested_action="hold_for_review",
    )
    card["kind"] = _safe_atom("artifact_kind", artifact.get("kind") or "", limit=32)
    card["delivery_state"] = _safe_atom("artifact_state", delivery_state, limit=32)
    card["summary"] = _safe_text("artifact_summary", truncate_text(summary, 120), limit=120)
    return (
        _ARTIFACT_TYPE_PRIORITY,
        _ARTIFACT_STATE_PRIORITY.get(delivery_state, 99),
        -_ts_rank(artifact.get("created_at"), artifact.get("updated_at")),
        str(artifact.get("id") or ""),
    ), card


def build_volunteer_cards(
    store: SensoriumStore,
    *,
    surface: str = "local",
    instance_config: dict | None = None,
    config_path: str | None = None,
    limit: int = 3,
) -> list[dict]:
    """Return up to ``limit`` compact volunteer cards for the given surface."""
    if limit <= 0:
        return []

    inst_cfg = instance_config
    if inst_cfg is None:
        inst_cfg, _ = load_instance_config(config_path=config_path, state_dir=str(store.root))

    now = datetime.now(timezone.utc)
    ranked_cards: list[tuple[tuple, dict]] = []

    threads = [
        t for t in store.read_jsonl("threads")
        if t.get("status") in {"dormant", "held"} and visible_on_surface(t, surface, inst_cfg)
    ]
    for thread in threads:
        ranked_cards.append(_thread_card(thread, now))

    candidates = [
        c for c in store.read_jsonl("candidates")
        if c.get("status") == "candidate" and visible_on_surface(c, surface, inst_cfg)
    ]
    for candidate in candidates:
        ranked_cards.append(_candidate_card(candidate, now))

    pointer_cfg = dict(DEFAULT_POINTER_CONFIG)
    if isinstance(inst_cfg, dict):
        pointer_cfg.update(inst_cfg.get("pointer") or {})
    for candidate in _saved_residue_candidates(store, surface, inst_cfg, cfg=pointer_cfg):
        ranked_cards.append(_saved_residue_card(candidate, now))

    for artifact in store.read_jsonl("artifacts"):
        if not visible_on_surface(artifact, surface, inst_cfg):
            continue
        artifact_card = _artifact_card(artifact, now)
        if artifact_card is not None:
            ranked_cards.append(artifact_card)

    ranked_cards.sort(key=lambda item: item[0])

    chosen: list[dict] = []
    seen_subject_refs: set[tuple[str, str]] = set()
    seen_reference_ids: set[str] = set()
    for _, card in ranked_cards:
        subject = card.get("subject_ref") or {}
        subject_key = (str(subject.get("kind") or ""), str(subject.get("id") or ""))
        reference_id = str(card.get("reference_id") or "")
        if not subject_key[1] or not reference_id:
            continue
        if subject_key in seen_subject_refs:
            continue
        if card.get("card_type") == "artifact" and reference_id in seen_reference_ids:
            continue
        chosen.append(card)
        seen_subject_refs.add(subject_key)
        seen_reference_ids.add(reference_id)
        if len(chosen) >= limit:
            break

    return chosen
