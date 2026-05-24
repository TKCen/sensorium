"""Command handler for /sensorium pull command — pure function, no Hermes runtime."""

import json

from .tools import (
    handle_sensorium_compact,
    handle_sensorium_dispatch_once,
    handle_sensorium_status,
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
    elif sub == "dispatch":
        return _fmt_dispatch(**kw)
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
        f"Sensorium [{instance}]",
        f"  signals: {counts['signals']}  events: {counts['events']}",
        f"  candidates: {counts['active_candidates']}/{counts['candidates']}"
        f"  threads: {counts['dormant_threads']}d {counts['held_threads']}h",
    ]
    if data["top_candidates"]:
        lines.append("  Top candidates:")
        for c in data["top_candidates"]:
            lines.append(f"    [{c['pressure']:.2f}] {c['id']} {c['kind']}: {c['summary']}")
    if data["top_threads"]:
        lines.append("  Visible threads:")
        for t in data["top_threads"]:
            lines.append(f"    [{t['status']}] {t['id']}: {t['title']}")
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


def _fmt_dispatch(*, instance: str, state_dir: str | None) -> str:
    raw = handle_sensorium_dispatch_once(
        instance=instance, state_dir=state_dir, dry_run=True
    )
    data = json.loads(raw)["data"]
    action = data["action"]
    if action == "no_candidate":
        return f"Sensorium [{instance}] dispatch: no eligible candidate."
    elif action == "would_promote":
        cid = data["candidate_id"]
        pressure = data.get("candidate_pressure", "?")
        preview = data.get("thread_preview", {})
        title = preview.get("conscious_task", {}).get("title", "")
        return (
            f"Sensorium [{instance}] dispatch would promote:\n"
            f"  {cid} (pressure {pressure})\n"
            f"  -> {title}"
        )
    elif action == "already_exists":
        return (
            f"Sensorium [{instance}] dispatch: thread {data['thread_id']}"
            f" already exists for {data['candidate_id']}."
        )
    return f"Sensorium [{instance}] dispatch: {action}"


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
        "  status         Compact status overview (default)\n"
        "  threads        Top visible dormant/held threads\n"
        "  dispatch       Dry-run dispatch preview\n"
        "  compact        Archive expired items\n"
        "  help           This message"
    )
