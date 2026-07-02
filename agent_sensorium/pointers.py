"""Active-session pointer selection for Sensorium conscious threads.

Pointers are tiny context door-handles, not full awareness capsules. They are
safe to inject into an active turn because they reveal only that an eligible
thread exists and how the operator can open it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .actions import count_active_actions_for_thread
from .config import load_instance_config, visible_on_surface
from .schemas import truncate_text, utc_now_iso
from .store import SensoriumStore

DEFAULT_POINTER_CONFIG: dict = {
    "enabled": True,
    "cooldown_minutes": 120,
    "max_title_chars": 96,
    # Cooldown is a de-spam preference, not an invisibility rule. If every
    # visible thread was recently injected, still return one door handle so
    # the operator can pick it up instead of blacking out attention for hours.
    "fallback_when_all_visible_on_cooldown": True,
    # Threads are the preferred doorway, but active candidates must not vanish
    # from the live context just because no thread has been minted yet. This is
    # deliberately a shallow pointer to the attention inbox, not a capsule leak.
    "candidate_fallback_enabled": True,
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
        invitation = f"🧵 I have something for you: {title}."
        if action_count:
            invitation += f" ({action_count} prepared action{'s' if action_count != 1 else ''}.)"
        invitation += ' Say "take it up" if you want me to open it.'
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
        invitation = (
            f"✨ I have something for you: {title}. "
            'Say "open it" or "take it up" if you want me to inspect the attention inbox.'
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

    return {"action": "no_pointer", "reason": "no_visible_thread_for_surface", "surface": surface or "local"}


def record_pointer_presented(
    store: SensoriumStore,
    pointer: dict,
    *,
    session_id: str = "",
    surface: str = "local",
) -> dict:
    """Record a cooldown receipt after injecting/presenting a pointer."""
    receipt = {
        "ts": utc_now_iso(),
        "type": "pointer.presented",
        "thread_id": pointer.get("thread_id"),
        "candidate_id": pointer.get("candidate_id"),
        "task_id": pointer.get("task_id"),
        "origin_candidate_id": pointer.get("origin_candidate_id"),
        "surface": surface or pointer.get("surface") or "local",
        "session_id": session_id,
    }
    store.append_jsonl("decisions", receipt)
    return receipt


def pointer_context_for_llm(pointer: dict) -> str:
    """Render the minimal injected context for the active turn."""
    surface = pointer.get("surface") or "local"
    title = pointer.get("title") or "Sensorium thread"
    if pointer.get("pointer_type") == "candidate":
        candidate_id = pointer.get("candidate_id")
        return (
            "[Sensorium Pointer]\n"
            f"Candidate salience: {candidate_id} — {title}\n"
            f"Human-facing doorway: {pointer.get('invitation')}\n"
            'Internal instruction: If the user says "open it", "take it up", '
            "or similar, call "
            f"sensorium(action=\"status\", surface=\"{surface}\").\n"
            "Do not mark it reviewed merely because it was shown. Leave meaningful salience open "
            "until it is answered, held with a trigger, or deliberately settled."
        )

    thread_id = pointer.get("thread_id")
    return (
        "[Sensorium Pointer]\n"
        f"Conscious thread: {thread_id} — {title}\n"
        f"Human-facing doorway: {pointer.get('invitation')}\n"
        'Internal instruction: If the user says "open it", "take it up", '
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
    record_pointer_presented(store, pointer, session_id=session_id, surface=surface)
    return {"context": pointer_context_for_llm(pointer)}
