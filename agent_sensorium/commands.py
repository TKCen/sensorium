"""Command handler for /sensorium pull command — pure function, no Hermes runtime."""

import json

from .tools import (
    handle_sensorium_attention_pointer,
    handle_sensorium_compact,
    handle_sensorium_status,
    handle_sensorium_thread_open,
    handle_sensorium_thread_update,
)


def handle_sensorium_command(
    raw_args: str, *, instance: str = "default", state_dir: str | None = None
) -> str:
    parts = raw_args.strip().split()
    sub = parts[0] if parts else "status"

    kw = {"instance": instance, "state_dir": state_dir}

    if sub == "status":
        return _fmt_status(**kw)
    elif sub == "threads":
        return _fmt_threads(**kw)
    elif sub == "pointer":
        surface = parts[1] if len(parts) > 1 else "local"
        return _fmt_pointer(surface=surface, **kw)
    elif sub == "open":
        thread_id = parts[1] if len(parts) > 1 else "latest"
        surface = parts[2] if len(parts) > 2 else "local"
        return _fmt_open(thread_id=thread_id, surface=surface, **kw)
    elif sub == "thread":
        return _fmt_thread_update(args=parts[1:], **kw)
    elif sub == "compact":
        return _fmt_compact(**kw)
    elif sub == "help":
        return _help()
    else:
        return f"Unknown subcommand: {sub}\n\n{_help()}"


def _fmt_status(*, instance: str, state_dir: str | None) -> str:
    raw = handle_sensorium_status(instance=instance, state_dir=state_dir)
    data = json.loads(raw)["data"]
    counts = data["counts"]
    lines = [
        f"🧠 Sensorium [{instance}]",
        f"  📡 signals: {counts['signals']}  ☄️ events: {counts['events']}",
        f"  ✨ candidates: {counts['active_candidates']}/{counts['candidates']}"
        f" ({counts.get('archived_candidates', 0)} archived)"
        f"  🧵 threads: {counts['dormant_threads']}d {counts['held_threads']}h"
        f" {counts.get('closed_threads', 0)}c {counts.get('archived_threads', 0)}a",
    ]
    if data["top_candidates"]:
        lines.append("  Top candidates:")
        for c in data["top_candidates"]:
            lines.append(f"    [{c['pressure']:.2f}] {c['id']} {c['kind']}: {c['summary']}")
    if data["top_threads"]:
        lines.append("  Visible threads:")
        for t in data["top_threads"]:
            lines.append(f"    [{t['status']}] {t['id']}: {t['title']}")
    if data.get("latest_decision"):
        d = data["latest_decision"]
        ref = d.get("thread_id") or d.get("candidate_id") or ""
        reason = f" — {d['reason']}" if d.get("reason") else ""
        lines.append(f"  Latest decision: {d['type']} {ref} [{d.get('action', '')}]{reason}")
    legacy = data.get("legacy_state_latest") or {}
    if legacy.get("exists"):
        lines.append(
            "  Freshness: legacy state.latest "
            f"{legacy.get('mtime') or legacy.get('updated_at') or 'unknown'} "
            "(deprecated; excluded from canonical freshness)"
        )
    dispatch_budget = (data.get("budgets") or {}).get("dispatch")
    if dispatch_budget:
        lines.append(
            "  Dispatch budget: "
            f"{dispatch_budget.get('remaining')}/{dispatch_budget.get('capacity')} "
            f"resets {dispatch_budget.get('reset_at')}"
        )
    return "\n".join(lines)


def _fmt_threads(*, instance: str, state_dir: str | None) -> str:
    raw = handle_sensorium_status(instance=instance, state_dir=state_dir)
    data = json.loads(raw)["data"]
    if not data["top_threads"]:
        return f"Sensorium [{instance}]: no visible threads."
    lines = [f"Sensorium [{instance}] threads:"]
    for t in data["top_threads"]:
        lines.append(f"  [{t['status']}] {t['id']}: {t['title']}")
        lines.append(f"    origin: {t['origin_candidate_id']}  created: {t['created_at']}")
    return "\n".join(lines)


def _fmt_pointer(*, instance: str, state_dir: str | None, surface: str) -> str:
    raw = handle_sensorium_attention_pointer(instance=instance, state_dir=state_dir, surface=surface)
    data = json.loads(raw)["data"]
    if data.get("action") != "pointer_available":
        return f"Sensorium [{instance}] pointer: none for {surface} ({data.get('reason')})."
    return (
        f"Sensorium [{instance}] pointer for {surface}:\n"
        f"  {data['thread_id']}: {data['title']}\n"
        f"  {data['invitation']}"
    )


def _fmt_open(*, instance: str, state_dir: str | None, thread_id: str, surface: str) -> str:
    raw = handle_sensorium_thread_open(
        instance=instance, state_dir=state_dir, thread_id=thread_id, surface=surface
    )
    result = json.loads(raw)
    if not result["success"]:
        return f"Sensorium [{instance}] open: {result['error']}"
    data = result["data"]
    lines = [
        f"Sensorium [{instance}] thread {data['thread_id']} [{data['status']}]",
        f"  {data['title']}",
        f"  why: {data['conscious_task'].get('why')}",
    ]
    if data.get("continuity_summary"):
        lines.append("  continuity:")
        for item in data["continuity_summary"][:4]:
            lines.append(f"    - {item}")
    if data.get("open_questions"):
        lines.append("  open questions:")
        for item in data["open_questions"][:3]:
            lines.append(f"    - {item}")
    if data.get("next_prompt_to_operator"):
        lines.append(f"  next: {data['next_prompt_to_operator']}")
    return "\n".join(lines)


def _fmt_thread_update(*, instance: str, state_dir: str | None, args: list[str]) -> str:
    if len(args) < 2:
        return "Usage: /sensorium thread <thread_id|latest> <close|hold|resume|archive|pin|unpin|mark_reviewed> [reason]"
    thread_id, action = args[0], args[1]
    reason = " ".join(args[2:])
    raw = handle_sensorium_thread_update(
        instance=instance, state_dir=state_dir, thread_id=thread_id, action=action, reason=reason
    )
    result = json.loads(raw)
    if not result["success"]:
        return f"Sensorium [{instance}] thread update: {result['error']}"
    data = result["data"]
    return (
        f"Sensorium [{instance}] thread {data['thread_id']}: {data['action']} "
        f"({data['old_status']} -> {data['new_status']})"
    )


def _fmt_compact(*, instance: str, state_dir: str | None) -> str:
    raw = handle_sensorium_compact(instance=instance, state_dir=state_dir)
    data = json.loads(raw)["data"]
    n_cand = len(data.get("archived_candidates", []))
    n_thread = len(data.get("archived_threads", []))
    n_receipts = data.get("receipts_written", 0)
    return (
        f"Sensorium [{instance}] compact: "
        f"{n_cand} candidates, {n_thread} threads archived ({n_receipts} receipts)."
    )


def _help() -> str:
    return (
        "Usage: /sensorium [subcommand]\n"
        "\n"
        "Subcommands:\n"
        "  status         Compact status overview (default) 🧠\n"
        "  threads        Top visible dormant/held threads 🧵\n"
        "  pointer [surf] Show the active-session pointer for a surface 📡\n"
        "  open [id] [surf] Open a compact thread capsule 🔓\n"
        "  thread <id> <action> [reason] Update thread lifecycle/pin state ⚙️\n"
        "  compact        Archive expired items 🧹\n"
        "  help           This message ❓"
    )
