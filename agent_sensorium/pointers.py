"""Active-session pointer selection for Sensorium conscious threads.

Pointers are tiny context door-handles, not full awareness capsules. They are
safe to inject into an active turn because they reveal only that an eligible
thread exists and how the operator can open it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .schemas import truncate_text, utc_now_iso
from .store import SensoriumStore

DEFAULT_POINTER_CONFIG: dict = {
    "enabled": True,
    "cooldown_minutes": 120,
    "max_title_chars": 96,
}


def _parse_utc(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _surface_allowed(thread: dict, surface: str) -> bool:
    allowed = set(thread.get("allowed_surfaces") or [])
    if not allowed:
        return False
    normalized = surface or "local"
    return normalized in allowed


def _recent_pointer_receipts(store: SensoriumStore, thread_id: str) -> list[dict]:
    return [
        d for d in store.read_jsonl("decisions")
        if d.get("type") == "pointer.presented" and d.get("thread_id") == thread_id
    ]


def _cooldown_open(store: SensoriumStore, thread_id: str, cooldown_minutes: int) -> tuple[bool, str]:
    receipts = _recent_pointer_receipts(store, thread_id)
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
) -> dict:
    """Return a candidate pointer without mutating state."""
    cfg = {**DEFAULT_POINTER_CONFIG, **(config or {})}
    if not cfg.get("enabled", True):
        return {"action": "no_pointer", "reason": "disabled"}

    threads = [
        t for t in store.read_jsonl("threads")
        if t.get("status") in ("dormant", "held") and not t.get("summary_dirty")
    ]
    threads.sort(key=lambda t: t.get("created_at", ""), reverse=True)

    for thread in threads:
        if not _surface_allowed(thread, surface):
            continue
        open_, reason = _cooldown_open(store, thread.get("id", ""), int(cfg.get("cooldown_minutes", 120)))
        if not open_:
            continue
        title = thread.get("conscious_task", {}).get("title") or thread.get("next_prompt_to_operator") or "Sensorium thread"
        title = truncate_text(title, int(cfg.get("max_title_chars", 96)))
        return {
            "action": "pointer_available",
            "thread_id": thread.get("id"),
            "task_id": thread.get("conscious_task", {}).get("id"),
            "origin_candidate_id": thread.get("origin_candidate_id"),
            "status": thread.get("status"),
            "title": title,
            "surface": surface or "local",
            "sensitivity": thread.get("sensitivity", "private"),
            "allowed_surfaces": thread.get("allowed_surfaces", []),
            "reason": reason,
            "invitation": f"Sensorium has a pending thread: {title}. Say ‘take it up’ if you want me to open it.",
        }

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
        "task_id": pointer.get("task_id"),
        "origin_candidate_id": pointer.get("origin_candidate_id"),
        "surface": surface or pointer.get("surface") or "local",
        "session_id": session_id,
    }
    store.append_jsonl("decisions", receipt)
    return receipt


def pointer_context_for_llm(pointer: dict) -> str:
    """Render the minimal injected context for the active turn."""
    return (
        "Sensorium active-session pointer:\n"
        f"- Thread: {pointer.get('thread_id')} — {pointer.get('title')}\n"
        "- Do not dump the full thread capsule into this chat.\n"
        "- If relevant, mention only this doorway: "
        f"{pointer.get('invitation')}"
    )


def handle_pointer_pre_llm(
    *,
    instance: str,
    platform: str = "",
    session_id: str = "",
    state_dir: str | None = None,
    config: dict | None = None,
) -> dict | None:
    """pre_llm_call hook entrypoint. Returns {context} or None."""
    surface = platform or "local"
    store = SensoriumStore(instance=instance, state_dir=state_dir)
    store.ensure_dirs()
    pointer = select_attention_pointer(store, surface=surface, config=config)
    if pointer.get("action") != "pointer_available":
        return None
    record_pointer_presented(store, pointer, session_id=session_id, surface=surface)
    return {"context": pointer_context_for_llm(pointer)}
