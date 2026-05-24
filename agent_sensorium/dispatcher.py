"""Dispatcher: select candidate, create dormant thread capsule."""

from datetime import datetime, timezone, timedelta

from .schemas import new_id, utc_now_iso
from .store import SensoriumStore

DEFAULT_DISPATCH_CONFIG: dict = {
    "thresholds": {
        "dispatch_pressure": 0.5,
    },
    "thread_ttl_hours": 168,
}


def select_candidate(candidates: list[dict], config: dict | None = None) -> dict | None:
    cfg = config or DEFAULT_DISPATCH_CONFIG
    threshold = cfg.get("thresholds", {}).get("dispatch_pressure", 0.5)
    eligible = [
        c for c in candidates
        if c.get("status") == "candidate" and c.get("pressure", 0) >= threshold
    ]
    if not eligible:
        return None
    eligible.sort(key=lambda c: c.get("pressure", 0), reverse=True)
    return eligible[0]


def candidate_to_thread(candidate: dict, config: dict | None = None) -> dict:
    cfg = config or DEFAULT_DISPATCH_CONFIG
    ttl_hours = cfg.get("thread_ttl_hours", 168)
    now = utc_now_iso()
    now_dt = datetime.now(timezone.utc)
    expires = (now_dt + timedelta(hours=ttl_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")

    summary = candidate.get("summary", "")
    kind = candidate.get("kind", "")

    return {
        "id": new_id("sth"),
        "status": "dormant",
        "origin": "candidate",
        "conscious_task": {
            "id": new_id("ctask"),
            "request_type": "THINK",
            "title": f"Review {kind}: {summary[:80]}",
            "why": f"Candidate pressure {candidate.get('pressure', 0)} crossed dispatch threshold.",
            "expected_decision": "Suppress, hold for later, save as workflow guidance, or create bounded follow-up.",
        },
        "origin_candidate_id": candidate.get("id", ""),
        "continuity_summary": _derive_continuity(candidate),
        "decision_log": [],
        "interaction_refs": [],
        "summary_dirty": False,
        "open_questions": [],
        "next_prompt_to_operator": f"Take up this {kind} thread, suppress it, or save as workflow guidance?",
        "sensitivity": candidate.get("sensitivity", "private"),
        "allowed_surfaces": candidate.get("allowed_surfaces", ["local"]),
        "created_at": now,
        "updated_at": now,
        "expires_at": expires,
    }


def _derive_continuity(candidate: dict) -> list[str]:
    bullets = []
    summary = candidate.get("summary", "")
    if summary:
        bullets.append(summary)
    keys = candidate.get("correlation_keys", [])
    if keys:
        bullets.append(f"Correlation keys: {', '.join(keys)}")
    kind = candidate.get("kind", "")
    if kind:
        bullets.append(f"Kind: {kind}")
    return bullets[:4]


def dispatch_once(
    store: SensoriumStore,
    *,
    dry_run: bool = True,
    config: dict | None = None,
) -> dict:
    cfg = config or DEFAULT_DISPATCH_CONFIG
    candidates = store.read_jsonl("candidates")
    selected = select_candidate(candidates, cfg)

    if selected is None:
        return {"action": "no_candidate", "reason": "No eligible candidate above threshold."}

    existing_threads = store.read_jsonl("threads")
    for t in existing_threads:
        if t.get("origin_candidate_id") == selected["id"]:
            return {
                "action": "already_exists",
                "thread_id": t["id"],
                "candidate_id": selected["id"],
                "status": t.get("status", "dormant"),
            }

    thread = candidate_to_thread(selected, cfg)

    if dry_run:
        return {
            "action": "would_promote",
            "dry_run": True,
            "candidate_id": selected["id"],
            "candidate_pressure": selected.get("pressure"),
            "thread_preview": thread,
        }

    store.append_jsonl("threads", thread)

    receipt = {
        "ts": utc_now_iso(),
        "type": "dispatch.promoted_to_thread",
        "candidate_id": selected["id"],
        "thread_id": thread["id"],
        "dry_run": False,
    }
    store.append_jsonl("decisions", receipt)

    return {
        "action": "promoted",
        "dry_run": False,
        "candidate_id": selected["id"],
        "thread_id": thread["id"],
    }
