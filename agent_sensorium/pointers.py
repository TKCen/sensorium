"""Active-session pointer selection for Sensorium conscious threads.

Pointers are tiny context door-handles, not full awareness capsules. They are
safe to inject into an active turn because they reveal only that an eligible
thread exists and how the operator can open it.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from .actions import count_active_actions_for_thread
from .config import load_instance_config, visible_on_surface
from .schemas import truncate_text, utc_now_iso
from .store import SensoriumStore

DEFAULT_POINTER_CONFIG: dict = {
    "enabled": True,
    "cooldown_minutes": 120,
    "max_title_chars": 96,
    # Foreground injection safety rails for non-openable doorways. These are
    # evaluated before any pointer text is rendered into the live prompt.
    "max_cards_per_turn": 1,
    "min_turn_gap": 2,
    "relevance_min_score": 0.35,
    "high_urgency_pressure": 0.9,
    # Cooldown is a de-spam preference, not an invisibility rule. If every
    # visible thread was recently injected, still return one door handle so
    # the operator can pick it up instead of blacking out attention for hours.
    "fallback_when_all_visible_on_cooldown": True,
    # Threads are the preferred doorway, but active candidates must not vanish
    # from the live context just because no thread has been minted yet. This is
    # deliberately a shallow pointer to the attention inbox, not a capsule leak.
    "candidate_fallback_enabled": True,
    # Saved-residue is the *honest* fallback for archived candidates that carry
    # a Kanban SAVE/PROMOTE_CONSCIOUS settlement. They are no longer active,
    # but they were saved for a reason and conscious access should not be
    # silently lost. This surfaces them with copy that clearly says "this is a
    # saved residue, not an openable thread".
    "saved_residue_fallback_enabled": True,
    # Optional anti-eagerness knobs for the saved-residue pathway. Defaults are
    # `None` (no cap) to preserve existing operator behaviour. When set:
    #   * saved_residue_max_age_days — drop residues whose `settled_at` is
    #     older than N days. Without this, an archive full of stale saved
    #     residue can keep surfacing forever (the "archive-confetti" smell).
    #   * saved_residue_max_items — keep only the top-N after the freshness
    #     sort. This bounds rotation through archived residue across many
    #     consecutive pointer turns when no active candidate exists.
    "saved_residue_max_age_days": None,
    "saved_residue_max_items": None,
}


def _parse_utc(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _recent_pointer_receipts(
    store: SensoriumStore,
    *,
    thread_id: str = "",
    candidate_id: str = "",
) -> list[dict]:
    rows = [d for d in store.read_jsonl("decisions") if d.get("type") == "pointer.presented"]
    if thread_id:
        return [d for d in rows if d.get("thread_id") == thread_id]
    if candidate_id:
        return [d for d in rows if d.get("candidate_id") == candidate_id]
    return []


def _cooldown_open(
    store: SensoriumStore,
    item_id: str,
    cooldown_minutes: int,
    *,
    id_field: str = "thread_id",
) -> tuple[bool, str]:
    if id_field == "candidate_id":
        receipts = _recent_pointer_receipts(store, candidate_id=item_id)
    else:
        receipts = _recent_pointer_receipts(store, thread_id=item_id)
    if not receipts:
        return True, "never_presented"
    last = max(receipts, key=lambda r: r.get("ts", ""))
    last_dt = _parse_utc(last.get("ts"))
    if last_dt is None:
        return True, "last_receipt_unparseable"
    now = datetime.now(timezone.utc)
    next_ok = last_dt + timedelta(minutes=cooldown_minutes)
    if now >= next_ok:
        return True, "cooldown_elapsed"
    return False, f"cooldown_until:{next_ok.strftime('%Y-%m-%dT%H:%M:%SZ')}"


def _compact_ws(value: str | None) -> str:
    return " ".join(str(value or "").split())


_TOKEN_RE = re.compile(r"[a-z0-9_]{3,}")
_GENERIC_RELEVANCE_TOKENS = {
    "check", "look", "latest", "open", "pointer", "residue", "saved",
    "sensorium", "status", "take", "thread", "view",
}


def _extract_latest_user_text(current_text: str | None = None, messages: object | None = None) -> str:
    text = _compact_ws(current_text)
    if text:
        return text
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").lower()
        if role not in {"user", "human"}:
            continue
        content = message.get("content")
        if isinstance(content, str):
            text = _compact_ws(content)
            if text:
                return text
        elif isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if isinstance(part, str):
                    parts.append(part)
                elif isinstance(part, dict) and isinstance(part.get("text"), str):
                    parts.append(part["text"])
            text = _compact_ws(" ".join(parts))
            if text:
                return text
    return ""


def _tokenize_relevance_text(value: str | None) -> set[str]:
    return set(_TOKEN_RE.findall(str(value or "").lower()))


def _explicit_pointer_request(user_text: str, pointer: dict) -> bool:
    if not user_text:
        return False
    text = user_text.lower()
    pointer_type = str(pointer.get("pointer_type") or "")
    subject_ids = {
        str(pointer.get("thread_id") or "").lower(),
        str(pointer.get("candidate_id") or "").lower(),
    }
    subject_ids.discard("")
    if any(subject_id in text for subject_id in subject_ids):
        return True
    if pointer_type == "saved_residue":
        return (
            "check saved residue" in text
            or "saved residue" in text
            or "saved-residue" in text
        )
    if pointer_type == "candidate":
        return (
            "take a look" in text
            or "check the inbox" in text
            or "check inbox" in text
            or ("look" in text and "sensorium" in text)
        )
    return False


def _pointer_relevance_score(pointer: dict, user_text: str) -> float:
    user_tokens = _tokenize_relevance_text(user_text)
    if not user_tokens:
        return 0.0
    source_tokens = (
        _tokenize_relevance_text(pointer.get("title"))
        | _tokenize_relevance_text(pointer.get("kind"))
        | _tokenize_relevance_text(pointer.get("invitation"))
    ) - _GENERIC_RELEVANCE_TOKENS
    if not source_tokens:
        return 0.0
    overlap = user_tokens & source_tokens
    if not overlap:
        return 0.0
    return round(len(overlap) / max(1, min(len(user_tokens), len(source_tokens))), 3)


def _pointer_presented_receipts(store: SensoriumStore) -> list[dict]:
    return [d for d in store.read_jsonl("decisions") if d.get("type") == "pointer.presented"]


def _foreground_turn_index(
    *,
    messages: object | None = None,
    current_text: str | None = None,
    session_id: str = "",
    store: SensoriumStore | None = None,
) -> int:
    """Best-effort user/foreground turn index for the current live call.

    Preferred source is the conversation transcript: count user/human messages.
    That advances even on turns where pointers were previously suppressed, which
    keeps `min_turn_gap` aligned to conversation turns instead of receipt count.

    If no transcript is available, fall back to a per-session monotonic counter
    derived from prior recorded turn indices. This is intentionally permissive:
    it may over-advance, but it will not starve non-openable pointers forever.
    """
    if isinstance(messages, list):
        user_turns = sum(
            1
            for message in messages
            if isinstance(message, dict)
            and str(message.get("role") or "").lower() in {"user", "human"}
        )
        if user_turns > 0:
            return user_turns

    if _compact_ws(current_text):
        if store is not None and session_id:
            prior = [
                receipt for receipt in store.read_jsonl("decisions")
                if receipt.get("session_id") == session_id
            ]
            prior_indices = [int(receipt.get("foreground_turn_index") or 0) for receipt in prior]
            return max(prior_indices, default=0) + 1
        return 1

    if store is not None and session_id:
        prior_presented = [
            receipt for receipt in _pointer_presented_receipts(store)
            if receipt.get("session_id") == session_id
        ]
        prior_indices = [int(receipt.get("foreground_turn_index") or 0) for receipt in prior_presented]
        return max(prior_indices, default=0) + 1
    return 1


def _foreground_injection_gate(
    store: SensoriumStore,
    pointer: dict,
    *,
    session_id: str = "",
    current_text: str = "",
    foreground_turn_index: int = 0,
    config: dict | None = None,
) -> tuple[bool, dict]:
    """Return whether this pointer may be injected into the live prompt."""
    cfg = config if isinstance(config, dict) else {}
    pointer_type = str(pointer.get("pointer_type") or "")
    if pointer_type not in {"thread", "candidate", "saved_residue"}:
        return False, {"reason": "unknown_pointer_type"}

    prior_presented = _pointer_presented_receipts(store)
    explicit = _explicit_pointer_request(current_text, pointer)
    if pointer_type == "thread":
        return True, {"reason": "openable_thread", "explicit": explicit}

    current_turn_index = max(1, int(foreground_turn_index or 0))

    session_receipts = [
        receipt for receipt in prior_presented
        if session_id and receipt.get("session_id") == session_id
    ]
    same_turn_receipts = [
        receipt for receipt in session_receipts
        if int(receipt.get("foreground_turn_index") or 0) == current_turn_index
    ]
    max_cards_per_turn = max(0, int(cfg.get("max_cards_per_turn", 1) or 0))
    if not explicit and session_id and len(same_turn_receipts) >= max_cards_per_turn:
        return False, {
            "reason": "max_cards_per_turn",
            "session_id": session_id,
            "foreground_turn_index": current_turn_index,
            "count": len(same_turn_receipts),
        }

    try:
        pressure = float(pointer.get("pressure") or 0.0)
    except (TypeError, ValueError):
        pressure = 0.0
    high_urgency = pressure >= float(cfg.get("high_urgency_pressure", 0.9) or 0.9)
    relevance_score = _pointer_relevance_score(pointer, current_text)
    relevance_min_score = float(cfg.get("relevance_min_score", 0.35) or 0.35)
    if not explicit and not high_urgency and relevance_score < relevance_min_score:
        return False, {
            "reason": "relevance_gate",
            "relevance_score": relevance_score,
            "relevance_min_score": relevance_min_score,
            "pressure": pressure,
        }

    min_turn_gap = max(0, int(cfg.get("min_turn_gap", 2) or 0))
    if not explicit and min_turn_gap > 0:
        last_presented = next(
            (
                receipt for receipt in reversed(prior_presented)
                if receipt.get("pointer_type") in {"candidate", "saved_residue"}
            ),
            None,
        )
        if last_presented is not None:
            last_index = int(last_presented.get("foreground_turn_index") or 0)
            if last_index > 0 and current_turn_index - last_index < min_turn_gap:
                return False, {
                    "reason": "min_turn_gap",
                    "min_turn_gap": min_turn_gap,
                    "foreground_turn_index": current_turn_index,
                    "last_pointer_turn_index": last_index,
                }

    return True, {
        "reason": "explicit_request" if explicit else ("high_urgency" if high_urgency else "relevance_gate_passed"),
        "explicit": explicit,
        "relevance_score": relevance_score,
        "pressure": pressure,
    }


def _pointer_title_matches(pointer_title: str, source_title: str) -> bool:
    """Return True when the injected pointer title still names its substrate.

    Pointer titles may be truncated by an operator-configured max length, so the
    guard compares against the source text at both the default pointer length and
    the concrete presented-title length. A mismatch means the doorway the model
    is about to show no longer corresponds to the row it claims to represent.
    """
    presented = _compact_ws(pointer_title)
    source = _compact_ws(source_title)
    if not presented or not source:
        return False
    if presented == source:
        return True
    if presented == truncate_text(source, DEFAULT_POINTER_CONFIG["max_title_chars"]):
        return True
    return presented == truncate_text(source, len(presented))


def _pointer_subject_guard(store: SensoriumStore, pointer: dict) -> tuple[bool, dict]:
    """Validate a pointer against the substrate row before recording receipt.

    This is the runtime wire for the Conscious Aperture false-doorway doctrine:
    a `pointer.presented` receipt should only exist when the pointer's subject id
    and displayed title/body are still consistent with the candidate/thread row
    the agent can later open. On mismatch, callers write a guard receipt instead
    and suppress injection for that turn.
    """
    pointer_type = str(pointer.get("pointer_type") or "")
    pointer_title = str(pointer.get("title") or "")

    if pointer_type == "thread":
        thread_id = pointer.get("thread_id")
        thread = next((t for t in store.read_jsonl("threads") if t.get("id") == thread_id), None)
        if thread is None:
            return False, {"reason": "missing_thread_subject", "subject_id": thread_id, "subject_kind": "thread"}
        if thread.get("status") not in ("dormant", "held"):
            return False, {"reason": "thread_not_openable", "subject_id": thread_id, "subject_kind": "thread"}
        source_title = (
            thread.get("conscious_task", {}).get("title")
            or thread.get("next_prompt_to_operator")
            or "Sensorium thread"
        )
        if not _pointer_title_matches(pointer_title, source_title):
            return False, {
                "reason": "thread_title_mismatch",
                "subject_id": thread_id,
                "subject_kind": "thread",
                "expected_title": truncate_text(source_title, DEFAULT_POINTER_CONFIG["max_title_chars"]),
                "presented_title": _compact_ws(pointer_title),
            }
        return True, {"subject_id": thread_id, "subject_kind": "thread"}

    if pointer_type in {"candidate", "saved_residue"}:
        candidate_id = pointer.get("candidate_id")
        candidate = next((c for c in store.read_jsonl("candidates") if c.get("id") == candidate_id), None)
        if candidate is None:
            return False, {"reason": "missing_candidate_subject", "subject_id": candidate_id, "subject_kind": pointer_type}
        if pointer_type == "candidate" and candidate.get("status") != "candidate":
            return False, {"reason": "candidate_status_mismatch", "subject_id": candidate_id, "subject_kind": pointer_type}
        if pointer_type == "saved_residue":
            settlement = candidate.get("kanban_settlement") or {}
            if str(settlement.get("decision") or "") not in {"SAVE", "PROMOTE_CONSCIOUS"} or not settlement.get("intake_task_id"):
                return False, {"reason": "saved_residue_settlement_missing", "subject_id": candidate_id, "subject_kind": pointer_type}
        source_title = candidate.get("summary", "") or (
            "Saved Sensorium residue" if pointer_type == "saved_residue" else "Sensorium salience"
        )
        if not _pointer_title_matches(pointer_title, source_title):
            return False, {
                "reason": "candidate_title_mismatch",
                "subject_id": candidate_id,
                "subject_kind": pointer_type,
                "expected_title": truncate_text(source_title, DEFAULT_POINTER_CONFIG["max_title_chars"]),
                "presented_title": _compact_ws(pointer_title),
            }
        return True, {"subject_id": candidate_id, "subject_kind": pointer_type}

    return False, {"reason": "unknown_pointer_type", "subject_kind": pointer_type}


def select_attention_pointer(
    store: SensoriumStore,
    *,
    surface: str = "local",
    config: dict | None = None,
    instance_config: dict | None = None,
) -> dict:
    """Return a candidate pointer without mutating state."""
    pointer_cfg = dict(instance_config.get("pointer") or {}) if isinstance(instance_config, dict) else {}
    cfg = {**DEFAULT_POINTER_CONFIG, **pointer_cfg, **(config or {})}
    if not cfg.get("enabled", True):
        return {"action": "no_pointer", "reason": "disabled"}

    inst_cfg = instance_config
    if inst_cfg is None:
        inst_cfg, _ = load_instance_config(state_dir=str(store.root))

    threads = [
        t for t in store.read_jsonl("threads")
        if t.get("status") in ("dormant", "held") and not t.get("summary_dirty")
    ]
    threads.sort(key=lambda t: t.get("created_at", ""), reverse=True)

    def _build_pointer(thread: dict, reason: str, *, cooldown_bypassed: bool = False) -> dict:
        title = thread.get("conscious_task", {}).get("title") or thread.get("next_prompt_to_operator") or "Sensorium thread"
        title = truncate_text(title, int(cfg.get("max_title_chars", 96)))
        action_count = count_active_actions_for_thread(store, thread.get("id", ""))
        invitation = (
            f"I have a conscious thread waiting: {title}."
        )
        if action_count:
            invitation += f" ({action_count} prepared action{'s' if action_count != 1 else ''}.)"
        invitation += " Say ‘take it up’ if you want me to open it."
        pointer = {
            "action": "pointer_available",
            "pointer_type": "thread",
            "thread_id": thread.get("id"),
            "task_id": thread.get("conscious_task", {}).get("id"),
            "origin_candidate_id": thread.get("origin_candidate_id"),
            "status": thread.get("status"),
            "title": title,
            "surface": surface or "local",
            "sensitivity": thread.get("sensitivity", "private"),
            "allowed_surfaces": thread.get("allowed_surfaces", []),
            "reason": reason,
            "invitation": invitation,
        }
        if action_count:
            pointer["action_count"] = action_count
        if cooldown_bypassed:
            pointer["cooldown_bypassed"] = True
        return pointer

    def _candidate_sort_key(candidate: dict) -> tuple:
        try:
            pressure = float(candidate.get("pressure") or 0.0)
        except (TypeError, ValueError):
            pressure = 0.0
        return (-pressure, str(candidate.get("created_at") or ""), str(candidate.get("id") or ""))

    def _build_candidate_pointer(candidate: dict, reason: str) -> dict:
        title = truncate_text(candidate.get("summary", "") or "Sensorium salience", int(cfg.get("max_title_chars", 96)))
        # Honest wording: a candidate pointer is NOT an openable thread.
        # Always make this distinction explicit in the human-facing copy so
        # the agent does not improvise a "thread X is waiting for you" line
        # that ends up failing when the user takes it up. The correct next
        # step for a presented candidate pointer is to open this exact
        # candidate id and read the compact candidate capsule; calling status
        # first can rotate the doorway to a different item after cooldown.
        invitation = (
            f"I have a salience candidate waiting (not an openable thread): {title}. "
            "Say ‘take a look’ or ‘check the inbox’ if you want me to surface it."
        )
        return {
            "action": "pointer_available",
            "pointer_type": "candidate",
            "candidate_id": candidate.get("id"),
            "status": candidate.get("status"),
            "kind": candidate.get("kind", ""),
            "pressure": candidate.get("pressure"),
            "title": title,
            "surface": surface or "local",
            "sensitivity": candidate.get("sensitivity", "private"),
            "allowed_surfaces": candidate.get("allowed_surfaces", []),
            "reason": reason,
            "invitation": invitation,
        }

    def _build_saved_residue_pointer(candidate: dict, reason: str) -> dict:
        """Honest doorway for archived candidates that were Kanban-saved.

        These candidates are no longer 'active' (status archived), but they
        carry a kanban_settlement with decision in {SAVE, PROMOTE_CONSCIOUS}
        and an intake/review task pointer. Conscious access must remain
        possible: an honest 'saved-residue' pointer surfaces them with copy
        that clearly states they are saved, not openable, so the user (and
        the LLM) does not mistake them for a live thread.
        """
        title = truncate_text(
            candidate.get("summary", "") or "Saved Sensorium residue",
            int(cfg.get("max_title_chars", 96)),
        )
        settlement = candidate.get("kanban_settlement") or {}
        settlement_decision = str(settlement.get("decision") or "")
        intake_task_id = str(settlement.get("intake_task_id") or "")
        review_task_id = str(settlement.get("review_task_id") or "")
        invitation = (
            f"I previously saved a salience residue (Kanban {settlement_decision or 'SAVE'}): {title}. "
            "This is not an openable thread — say ‘check saved residue’ if you want me to recap the intake."
        )
        return {
            "action": "pointer_available",
            "pointer_type": "saved_residue",
            "candidate_id": candidate.get("id"),
            "status": candidate.get("status"),
            "kind": candidate.get("kind", ""),
            "pressure": candidate.get("pressure"),
            "title": title,
            "surface": surface or "local",
            "sensitivity": candidate.get("sensitivity", "private"),
            "allowed_surfaces": candidate.get("allowed_surfaces", []),
            "settlement_decision": settlement_decision,
            "intake_task_id": intake_task_id,
            "review_task_id": review_task_id,
            "kanban_settlement": {
                "decision": settlement_decision,
                "intake_task_id": intake_task_id,
                "review_task_id": review_task_id,
                "settled_at": settlement.get("settled_at", ""),
                "reason_label": settlement.get("reason_label", ""),
            },
            "reason": reason,
            "invitation": invitation,
        }

    cooldown_blocked: list[tuple[dict, str]] = []
    for thread in threads:
        if not visible_on_surface(thread, surface, inst_cfg):
            continue
        open_, reason = _cooldown_open(store, thread.get("id", ""), int(cfg.get("cooldown_minutes", 120)))
        if not open_:
            cooldown_blocked.append((thread, reason))
            continue
        return _build_pointer(thread, reason)

    if cooldown_blocked:
        if cfg.get("fallback_when_all_visible_on_cooldown", True):
            thread, reason = cooldown_blocked[0]
            return _build_pointer(
                thread,
                f"cooldown_bypassed_all_visible_threads:{reason}",
                cooldown_bypassed=True,
            )
        thread, reason = cooldown_blocked[0]
        return {
            "action": "no_pointer",
            "reason": reason,
            "thread_id": thread.get("id"),
            "task_id": thread.get("conscious_task", {}).get("id"),
            "origin_candidate_id": thread.get("origin_candidate_id"),
            "surface": surface or "local",
        }
    if cfg.get("candidate_fallback_enabled", True):
        candidates = [
            c for c in store.read_jsonl("candidates")
            if c.get("status") == "candidate" and visible_on_surface(c, surface, inst_cfg)
        ]
        for candidate in sorted(candidates, key=_candidate_sort_key):
            open_, reason = _cooldown_open(
                store,
                candidate.get("id", ""),
                int(cfg.get("cooldown_minutes", 120)),
                id_field="candidate_id",
            )
            if open_:
                return _build_candidate_pointer(candidate, reason)

    if cfg.get("saved_residue_fallback_enabled", True):
        saved_candidates = _saved_residue_candidates(store, surface, inst_cfg, cfg=cfg)
        for candidate in saved_candidates:
            open_, reason = _cooldown_open(
                store,
                candidate.get("id", ""),
                int(cfg.get("cooldown_minutes", 120)),
                id_field="candidate_id",
            )
            if open_:
                return _build_saved_residue_pointer(candidate, reason)

    return {"action": "no_pointer", "reason": "no_visible_thread_for_surface", "surface": surface or "local"}


def _saved_residue_candidates(
    store: SensoriumStore,
    surface: str,
    inst_cfg: dict | None,
    *,
    cfg: dict | None = None,
) -> list[dict]:
    """Return archived candidates with a Kanban SAVE/PROMOTE_CONSCIOUS settlement.

    Acceptance criterion (c): relevant research_source_signal candidates must
    not be silently suppressed away from conscious access purely because the
    Kanban settlement was SAVE. They may not be 'openable threads', but they
    are honest saved-residue items and conscious access should remain possible
    via a doorway that clearly says so.

    Anti-eagerness: when the operator does not cap them, archived residue can
    keep rotating forever (the "archive-confetti" smell). To keep this honest
    without breaking existing operators, the function honours two opt-in knobs
    from the merged pointer config:

      * ``saved_residue_max_age_days`` — drop residues whose
        ``kanban_settlement.settled_at`` is older than N days.
      * ``saved_residue_max_items`` — keep only the top-N after the freshness
        sort, so consecutive pointer turns cannot march through the entire
        archive when no active candidate exists.

    Defaults for both knobs are ``None`` (no cap) which preserves the existing
    behaviour exactly.
    """
    # Fails closed when no instance config is supplied; the saved-residue
    # pathway needs a config to honor max_sensitivity.
    cfg_for_visibility = inst_cfg if isinstance(inst_cfg, dict) else {}
    merged_cfg = cfg if isinstance(cfg, dict) else {}
    now = datetime.now(timezone.utc)
    max_age_days = merged_cfg.get("saved_residue_max_age_days")
    max_items = merged_cfg.get("saved_residue_max_items")
    saved = []
    for candidate in store.read_jsonl("candidates"):
        if not visible_on_surface(candidate, surface, cfg_for_visibility):
            continue
        settlement = candidate.get("kanban_settlement") or {}
        decision = str(settlement.get("decision") or "")
        if decision not in {"SAVE", "PROMOTE_CONSCIOUS"}:
            continue
        if not settlement.get("intake_task_id"):
            # A settlement without a linked intake does not qualify as
            # honest saved residue: there is no external trace the conscious
            # layer can rely on.
            continue
        # Optional freshness cap: settled_at is the explicit moment the
        # operator decided this residue was worth saving. If the operator
        # asks for a recency window, honor it.
        if max_age_days is not None:
            settled_dt = _parse_utc(settlement.get("settled_at"))
            if settled_dt is not None:
                age = now - settled_dt
                if age > timedelta(days=float(max_age_days)):
                    continue
        saved.append(candidate)

    def _recency_ts(candidate: dict) -> int:
        """Unix epoch for the freshest explicit signal we have for this residue."""
        settled_dt = _parse_utc((candidate.get("kanban_settlement") or {}).get("settled_at"))
        if settled_dt is not None:
            return int(settled_dt.timestamp())
        updated_dt = _parse_utc(candidate.get("updated_at"))
        if updated_dt is not None:
            return int(updated_dt.timestamp())
        created_dt = _parse_utc(candidate.get("created_at"))
        if created_dt is not None:
            return int(created_dt.timestamp())
        return 0

    # surface-pressure is still the visible-degradation hint we care about,
    # but the freshest settled residue must win when pressure ties — that's
    # the rotation-through-archive guard. Falling back to created_at keeps
    # the sort fully deterministic for residues without any timestamps.
    saved.sort(key=lambda c: (
        -(float(c.get("pressure") or 0.0)),
        -_recency_ts(c),
        c.get("created_at", "") or "",
        c.get("id", "") or "",
    ))
    if max_items is not None and max_items >= 0:
        saved = saved[: int(max_items)]
    return saved


def record_pointer_presented(
    store: SensoriumStore,
    pointer: dict,
    *,
    session_id: str = "",
    surface: str = "local",
    foreground_turn_index: int = 0,
) -> dict:
    """Record a cooldown receipt after injecting/presenting a pointer.

    Before writing `pointer.presented`, verify the doorway still matches the
    row it names. A mismatch writes a guard receipt instead; callers should not
    inject that pointer into the live model turn.
    """
    ok, guard = _pointer_subject_guard(store, pointer)
    if not ok:
        receipt = {
            "ts": utc_now_iso(),
            "type": "pointer.presented.guard",
            "outcome": "blocked",
            "reason": guard.get("reason", "pointer_subject_mismatch"),
            "pointer_type": pointer.get("pointer_type"),
            "thread_id": pointer.get("thread_id"),
            "candidate_id": pointer.get("candidate_id"),
            "surface": surface or pointer.get("surface") or "local",
            "session_id": session_id,
            "subject_kind": guard.get("subject_kind", ""),
            "subject_id": guard.get("subject_id", ""),
            "expected_title": guard.get("expected_title", ""),
            "presented_title": guard.get("presented_title", _compact_ws(pointer.get("title"))),
        }
        store.append_jsonl("decisions", receipt)
        return receipt

    receipt = {
        "ts": utc_now_iso(),
        "type": "pointer.presented",
        "foreground_turn_index": max(
            1,
            int(foreground_turn_index or 0),
        ),
        "pointer_type": pointer.get("pointer_type"),
        "thread_id": pointer.get("thread_id"),
        "candidate_id": pointer.get("candidate_id"),
        "task_id": pointer.get("task_id"),
        "origin_candidate_id": pointer.get("origin_candidate_id"),
        "surface": surface or pointer.get("surface") or "local",
        "session_id": session_id,
        "subject_kind": guard.get("subject_kind", ""),
        "subject_id": guard.get("subject_id", ""),
        "presented_title": _compact_ws(pointer.get("title")),
    }
    store.append_jsonl("decisions", receipt)
    return receipt


def pointer_context_for_llm(pointer: dict) -> str:
    """Render the minimal injected context for the active turn."""
    surface = pointer.get("surface") or "local"
    title = pointer.get("title") or "Sensorium pointer"
    pointer_type = pointer.get("pointer_type")
    if pointer_type == "candidate":
        candidate_id = pointer.get("candidate_id")
        return (
            "[Sensorium Pointer]\n"
            f"Pointer type: candidate (NOT an openable thread) — {candidate_id} — {title}\n"
            f"Human-facing doorway: {pointer.get('invitation')}\n"
            "Internal instruction: This is a salience candidate, not a thread. "
            "If the user wants to inspect it, use the exact candidate id from this "
            "pointer — do not switch to a different status pointer after cooldown "
            "selection advances. Call "
            f"sensorium(action=\"open\", surface=\"{surface}\", id=\"{candidate_id}\") "
            "and surface the compact candidate capsule. Be honest: there is no "
            "openable thread; only a salience candidate in the inbox.\n"
            "Do not mark it reviewed merely because it was shown. Leave meaningful salience open "
            "until it is answered, held with a trigger, or deliberately settled."
        )
    if pointer_type == "saved_residue":
        candidate_id = pointer.get("candidate_id")
        intake_task_id = pointer.get("intake_task_id") or ""
        decision = pointer.get("settlement_decision") or "SAVE"
        return (
            "[Sensorium Pointer]\n"
            f"Pointer type: saved_residue (Kanban {decision}; NOT an openable thread) — "
            f"{candidate_id} — {title}\n"
            f"Linked intake task id: {intake_task_id or '(none)'}\n"
            f"Human-facing doorway: {pointer.get('invitation')}\n"
            "Internal instruction: This was previously saved (Kanban settlement); "
            "conscious access is preserved via an honest doorway. If the user wants "
            "a recap, use the exact candidate id from this pointer — do not call status "
            "as the primary lookup, because cooldown selection may advance and return a "
            "different saved residue. Call "
            f"sensorium(action=\"open\", surface=\"{surface}\", id=\"{candidate_id}\") "
            "and report the compact candidate capsule plus kanban intake/review task ids. "
            "Be honest: this is saved residue, not an openable thread.\n"
            "Do not mark it reviewed merely because it was shown. The Kanban intake "
            "is the durable trace; preserve that linkage."
        )

    thread_id = pointer.get("thread_id")
    return (
        "[Sensorium Pointer]\n"
        f"Pointer type: thread — {thread_id} — {title}\n"
        f"Human-facing doorway: {pointer.get('invitation')}\n"
        "Internal instruction: If the user says “open it”, “take it up”, "
        "or similar, call "
        f"sensorium(action=\"open\", surface=\"{surface}\", id=\"{thread_id}\").\n"
        "Do not reveal capsule content unless opened. Do not include private capsule fields "
        "in the pointer itself."
    )


def handle_pointer_pre_llm(
    *,
    instance: str,
    platform: str = "",
    session_id: str = "",
    state_dir: str | None = None,
    config: dict | None = None,
    config_path: str | None = None,
    current_text: str = "",
    messages: list[dict] | None = None,
) -> dict | None:
    """pre_llm_call hook entrypoint. Returns {context} or None."""
    surface = platform or "local"
    store = SensoriumStore(instance=instance, state_dir=state_dir)
    store.ensure_dirs()
    instance_config, _ = load_instance_config(
        config_path=config_path, state_dir=str(store.root),
    )
    pointer = select_attention_pointer(
        store, surface=surface, config=config, instance_config=instance_config,
    )
    if pointer.get("action") != "pointer_available":
        return None
    merged_cfg = {**DEFAULT_POINTER_CONFIG, **(instance_config.get("pointer") or {}), **(config or {})}
    user_text = _extract_latest_user_text(current_text=current_text, messages=messages)
    foreground_turn_index = _foreground_turn_index(
        messages=messages,
        current_text=user_text,
        session_id=session_id,
        store=store,
    )
    inject_ok, gate = _foreground_injection_gate(
        store,
        pointer,
        session_id=session_id,
        current_text=user_text,
        foreground_turn_index=foreground_turn_index,
        config=merged_cfg,
    )
    if not inject_ok:
        store.append_jsonl("decisions", {
            "ts": utc_now_iso(),
            "type": "pointer.suppressed",
            "pointer_type": pointer.get("pointer_type"),
            "thread_id": pointer.get("thread_id"),
            "candidate_id": pointer.get("candidate_id"),
            "surface": surface,
            "session_id": session_id,
            "foreground_turn_index": foreground_turn_index,
            "reason": gate.get("reason", "foreground_gate"),
            "gate": gate,
        })
        return None
    receipt = record_pointer_presented(
        store,
        pointer,
        session_id=session_id,
        surface=surface,
        foreground_turn_index=foreground_turn_index,
    )
    if receipt.get("type") == "pointer.presented.guard":
        return None
    return {"context": pointer_context_for_llm(pointer)}
