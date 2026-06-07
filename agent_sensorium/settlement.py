"""Kanban Subconscious settlement propagation and incident coalescing.

Bridges Kanban Subconscious decisions back into Sensorium candidate/event truth
so a subconscious-reviewer profile that DROP/SAVE/PROMOTE_CONSCIOUS-settles an
intake also settles the underlying Sensorium candidate. Also exposes the
deterministic incident-key derivation the Kanban bridge uses to coalesce
repeated jitter samples without re-spending Subconscious calls.

This module is intentionally stdlib-only and side-effect-free apart from the
explicit JSONL writes performed by `apply_kanban_settlement`.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .gate import is_feedback_self_loop
from .schemas import truncate_text, utc_now_iso
from .store import SensoriumStore

VALID_SETTLEMENT_DECISIONS = {"DROP", "SAVE", "PROMOTE_CONSCIOUS"}

# Mirror of the dispatcher's default `dispatch_pressure` threshold. The
# reconciliation pass treats any active candidate at or above this pressure as a
# pending activation that must be visible in Kanban or settled with a receipt.
DEFAULT_DISPATCH_PRESSURE_THRESHOLD = 0.5

# Bounded fan-out: never mint more than this many reconciliation intakes in a
# single tick. Overflow is reported (never silently dropped) so a backlog spike
# cannot flood the board, and the next tick drains the remainder.
MAX_RECONCILE_INTAKES_PER_TICK = 25

# Status applied to the originating candidate per decision. DROP suppresses so
# the dispatcher's `select_candidate` filter (status == "candidate") cannot
# later promote it. SAVE/PROMOTE_CONSCIOUS mark the candidate reviewed so its
# audit trail is preserved while it leaves the active promotion pool.
DECISION_TO_CANDIDATE_STATUS = {
    "DROP": "suppressed",
    "SAVE": "reviewed",
    "PROMOTE_CONSCIOUS": "reviewed",
}

ACTIONABLE_DASHBOARD_MARKERS = (
    "service not running",
    "dashboard root failed",
    "dashboard proxy failed",
    "plugin registry failed",
    "plugin api failures",
    "session token not found",
    "zombie dashboard children",
    "slash_worker count high",
    "embedded tui child count high",
    "dashboard task count high",
)


def event_incident_key(event: dict) -> str:
    """Stable incident key for coalescing repeated sensor samples.

    Sensors say "threshold crossed". Subconscious contextualizes the first/new
    crossing. Repeated jitter from the same unresolved situation should update
    continuity state instead of spending another MiniMax call. A materially
    distinct structural marker (e.g. a new dashboard failure mode) still
    derives a different key and is treated as a new incident.
    """
    kind = str(event.get("kind") or "event")
    summary = str(event.get("summary") or "")
    lower = summary.lower()
    if kind == "dashboard_memory_pressure":
        markers = [marker for marker in ACTIONABLE_DASHBOARD_MARKERS if marker in lower]
        if markers:
            return f"{kind}:" + ",".join(sorted(markers))
        if "dashboard cgroup memory high" in lower or "dashboard watchdog alert" in lower:
            return f"{kind}:cgroup_memory_high"
    fingerprint = str(event.get("fingerprint") or "")
    if fingerprint:
        return f"{kind}:{fingerprint}"
    return f"{kind}:{hashlib.sha256(summary.encode('utf-8')).hexdigest()[:12]}"


def coalesce_suppression_reason(event: dict, state: dict | None) -> str | None:
    """Return a reason when this event is a repeat of an already-contextualized incident."""
    if not state:
        return None
    key = event_incident_key(event)
    incidents = state.get("incident_context") or {}
    incident = incidents.get(key) if isinstance(incidents, dict) else None
    if not incident:
        return None
    if incident.get("intake_task_id") or incident.get("review_task_id"):
        return f"coalesced_repeat:{key}"
    return None


def _matches_correlation_keys(candidate: dict, keys: list[str]) -> bool:
    cand_keys = candidate.get("correlation_keys") or []
    if not isinstance(cand_keys, list):
        return False
    return any(k in cand_keys for k in keys)


def _resolve_candidates(
    candidates: list[dict],
    *,
    candidate_id: str = "",
    event_id: str = "",
    fingerprint: str = "",
    correlation_keys: list[str] | None = None,
) -> list[dict]:
    """Return candidates matching the most specific identifier provided.

    Resolution order is strict: candidate_id → event_id → fingerprint →
    correlation_keys. Returns an empty list when nothing matches. Correlation
    keys can match multiple candidates; the caller decides how to apply.
    """
    if candidate_id:
        return [c for c in candidates if c.get("id") == candidate_id]
    if event_id:
        return [
            c for c in candidates
            if event_id in (c.get("event_ids") or [])
        ]
    if fingerprint:
        return [c for c in candidates if c.get("fingerprint") == fingerprint]
    if correlation_keys:
        return [c for c in candidates if _matches_correlation_keys(c, correlation_keys)]
    return []


def _rewrite_candidates(store: SensoriumStore, candidates: list[dict]) -> None:
    store.rewrite_jsonl("candidates", candidates)


def _normalize_conscious_task_ref(ref: Any) -> dict:
    if not isinstance(ref, dict):
        return {}
    out: dict[str, str] = {}
    # Preserve both legacy Kanban task refs and the newer internal
    # conscious_task candidate refs. Conscious promotion is no longer required
    # to mean "spawn a conscious:review Kanban worker"; a settlement may point
    # at an internal candidate/thread that the bounded Conscious aperture will
    # inspect later.
    for key in (
        "task_id",
        "thread_id",
        "board",
        "kanban_task_id",
        "candidate_id",
        "conscious_task_id",
        "kind",
        "promoted_at",
    ):
        value = ref.get(key)
        if isinstance(value, str) and value.strip():
            out[key] = truncate_text(value, 200)
    return out


def apply_kanban_settlement(
    store: SensoriumStore,
    *,
    decision: str,
    candidate_id: str = "",
    event_id: str = "",
    fingerprint: str = "",
    correlation_keys: list[str] | None = None,
    intake_task_id: str = "",
    review_task_id: str = "",
    conscious_task_ref: dict | None = None,
    reason: str = "",
    record_receipt: bool = True,
) -> dict:
    """Propagate a Kanban Subconscious decision into Sensorium candidate truth.

    The call is idempotent: re-applying the same DROP/SAVE/PROMOTE_CONSCIOUS to
    an already-settled candidate is a no-op and never duplicate-promotes. The
    function never writes threads, never opens any platform surface, and never
    mutates outbox or worker state. Receipts go into the standard decisions
    JSONL so existing tooling and audits can read them.
    """
    decision_upper = (decision or "").upper()
    if decision_upper not in VALID_SETTLEMENT_DECISIONS:
        return {
            "action": "invalid_decision",
            "decision": decision,
            "reason": f"decision must be one of {sorted(VALID_SETTLEMENT_DECISIONS)}",
        }

    store.ensure_dirs()
    candidates = store.read_jsonl("candidates")
    matched = _resolve_candidates(
        candidates,
        candidate_id=candidate_id,
        event_id=event_id,
        fingerprint=fingerprint,
        correlation_keys=correlation_keys,
    )

    if not matched:
        receipt = {
            "ts": utc_now_iso(),
            "type": "kanban.settlement.unresolved",
            "decision": decision_upper,
            "candidate_id": candidate_id,
            "event_id": event_id,
            "fingerprint": fingerprint,
            "correlation_keys": list(correlation_keys or []),
            "intake_task_id": intake_task_id,
            "review_task_id": review_task_id,
            "reason": truncate_text(reason, 240),
        }
        if record_receipt:
            store.append_jsonl("decisions", receipt)
        return {
            "action": "no_candidate_match",
            "decision": decision_upper,
            "matched_candidate_ids": [],
            "receipts": [receipt] if record_receipt else [],
        }

    target_status = DECISION_TO_CANDIDATE_STATUS[decision_upper]
    now = utc_now_iso()
    receipts: list[dict] = []
    updated_ids: list[str] = []
    already_settled_ids: list[str] = []
    conscious_ref = _normalize_conscious_task_ref(conscious_task_ref)

    for candidate in matched:
        cand_id = candidate.get("id", "")
        old_status = candidate.get("status", "candidate")
        existing_settlement = candidate.get("kanban_settlement") or {}
        already_marked = (
            existing_settlement.get("decision") == decision_upper
            and old_status == target_status
        )
        if already_marked:
            already_settled_ids.append(cand_id)
            continue

        if old_status == "candidate":
            candidate["status"] = target_status
        # If the candidate is already terminal in some other way (cancelled,
        # archived) we leave its status alone but still record the Kanban
        # settlement evidence so audit can trace the link.
        candidate["updated_at"] = now
        settlement_meta = {
            "decision": decision_upper,
            "intake_task_id": intake_task_id,
            "review_task_id": review_task_id,
            "settled_at": now,
            "reason": truncate_text(reason, 240),
        }
        if conscious_ref:
            settlement_meta["conscious_task_ref"] = conscious_ref
        candidate["kanban_settlement"] = settlement_meta
        updated_ids.append(cand_id)

        receipt = {
            "ts": now,
            "type": "kanban.settlement.applied",
            "decision": decision_upper,
            "candidate_id": cand_id,
            "old_status": old_status,
            "new_status": candidate["status"],
            "intake_task_id": intake_task_id,
            "review_task_id": review_task_id,
            "reason": truncate_text(reason, 240),
        }
        if conscious_ref:
            receipt["conscious_task_ref"] = conscious_ref
        receipts.append(receipt)
        if record_receipt:
            store.append_jsonl("decisions", receipt)

    if updated_ids:
        _rewrite_candidates(store, candidates)

    if updated_ids:
        action = "settled"
    elif already_settled_ids:
        action = "already_settled"
    else:
        action = "no_change"

    return {
        "action": action,
        "decision": decision_upper,
        "matched_candidate_ids": [c.get("id", "") for c in matched],
        "updated_candidate_ids": updated_ids,
        "already_settled_candidate_ids": already_settled_ids,
        "receipts": receipts,
    }


def apply_settlement_record(store: SensoriumStore, record: dict) -> dict:
    """Apply a structured settlement record as produced by the Kanban bridge.

    The record is the single source-of-truth shape that the subconscious
    reviewer's completion summaries (or the Kanban bridge wrapper) must emit. Keeping it
    here lets the parser stay deterministic and tested without parsing free
    Kanban comment prose.
    """
    if not isinstance(record, dict):
        return {"action": "invalid_record", "reason": "record must be a dict"}
    return apply_kanban_settlement(
        store,
        decision=str(record.get("decision") or ""),
        candidate_id=str(record.get("candidate_id") or ""),
        event_id=str(record.get("event_id") or ""),
        fingerprint=str(record.get("fingerprint") or ""),
        correlation_keys=list(record.get("correlation_keys") or []) or None,
        intake_task_id=str(record.get("intake_task_id") or ""),
        review_task_id=str(record.get("review_task_id") or ""),
        conscious_task_ref=record.get("conscious_task_ref") if isinstance(record.get("conscious_task_ref"), dict) else None,
        reason=str(record.get("reason") or ""),
    )


CLOSED_INTAKE_STATUSES = {"done", "archived", "completed"}
_SETTLEMENT_DECISION_RE = re.compile(
    r"(?:\bdecision\s*:\s*|\bsettled\s+as\s+)?\b(DROP|SAVE|PROMOTE_CONSCIOUS|PROMOTE|SUPPRESSED|SUPPRESS)\b",
    re.IGNORECASE,
)


def _normalise_decision_token(token: str) -> str:
    upper = (token or "").upper()
    if upper == "PROMOTE":
        return "PROMOTE_CONSCIOUS"
    if upper in {"SUPPRESSED", "SUPPRESS"}:
        return "DROP"
    return upper


def extract_kanban_intake_payload(body: str) -> dict:
    """Extract the compact event/candidate JSON payload from an intake body.

    The bridge emits human-readable Kanban bodies with a single JSON object after
    either ``Compact candidate payload:`` or ``Compact event payload:``. This
    parser deliberately ignores free prose and returns ``{}`` on malformed input
    so reconciliation can flag a visible gap instead of guessing.
    """
    text = str(body or "")
    marker_positions = [
        text.find("Compact candidate payload:"),
        text.find("Compact event payload:"),
    ]
    marker_positions = [p for p in marker_positions if p >= 0]
    if not marker_positions:
        return {}
    start = min(marker_positions)
    brace = text.find("{", start)
    if brace < 0:
        return {}
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[brace:])
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


def _task_texts_for_decision(task: dict) -> list[str]:
    texts: list[str] = []
    for key in ("result", "summary"):
        value = task.get(key)
        if isinstance(value, str) and value.strip():
            texts.append(value)
    for comment in task.get("comments") or []:
        if isinstance(comment, dict):
            body = comment.get("body")
            if isinstance(body, str) and body.strip():
                if body.startswith("BLOCKED: Sensorium substrate intake:"):
                    continue
                texts.append(body)
    for run in task.get("runs") or []:
        if isinstance(run, dict):
            for key in ("summary", "result"):
                value = run.get(key)
                if isinstance(value, str) and value.strip():
                    texts.append(value)
    for event in task.get("events") or []:
        if not isinstance(event, dict):
            continue
        payload = event.get("payload")
        if isinstance(payload, dict):
            for key in ("summary", "result"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    texts.append(value)
    return texts


def infer_kanban_settlement_decision(task: dict) -> str | None:
    """Infer a closed intake's explicit settlement decision from review evidence.

    Only comments, run summaries/results, task result/summary, and completion
    event summaries are considered. The intake body is intentionally excluded
    because it contains instructional text such as ``DROP/SAVE/PROMOTE`` that is
    not evidence the reviewer actually decided anything.
    """
    for text in _task_texts_for_decision(task if isinstance(task, dict) else {}):
        match = _SETTLEMENT_DECISION_RE.search(text)
        if not match:
            continue
        decision = _normalise_decision_token(match.group(1))
        if decision in VALID_SETTLEMENT_DECISIONS:
            return decision
    return None


def _settlement_receipt_exists(decisions: list[dict], intake_task_id: str) -> bool:
    if not intake_task_id:
        return False
    for decision in decisions:
        if not isinstance(decision, dict):
            continue
        if (
            decision.get("type") == "kanban.settlement.applied"
            and str(decision.get("intake_task_id") or "") == intake_task_id
        ):
            return True
    return False


def plan_completed_intake_settlements(
    tasks: list[dict],
    *,
    decisions: list[dict],
    active_candidate_ids: set[str] | list[str] | None = None,
) -> dict:
    """Plan recovery settlements for completed intakes missing Sensorium receipts.

    This closes the failure class where a cheap Kanban reviewer comments/completes
    an intake as DROP/SAVE/PROMOTE but forgets to run the settlement CLI. Given
    full Kanban task details, return deterministic settlement records for closed
    intakes that claimed a decision but have no ``kanban.settlement.applied``
    receipt. Closed intakes with no explicit decision are returned as gaps so the
    bridge can make the failure visible rather than silently treating comments as
    truth.
    """
    active_filter = {str(c) for c in active_candidate_ids} if active_candidate_ids is not None else None
    records: list[dict] = []
    gaps: list[dict] = []
    already_settled: list[str] = []

    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        intake_task_id = str(task.get("id") or "")
        title = str(task.get("title") or task.get("name") or "")
        status = str(task.get("status") or "")
        if not intake_task_id or not title.startswith("sensor:intake:"):
            continue
        if status not in CLOSED_INTAKE_STATUSES:
            continue
        payload = extract_kanban_intake_payload(str(task.get("body") or ""))
        candidate_id = str(payload.get("candidate_id") or "")
        if active_filter is not None and candidate_id and candidate_id not in active_filter:
            continue
        if _settlement_receipt_exists(decisions, intake_task_id):
            already_settled.append(intake_task_id)
            continue
        decision = infer_kanban_settlement_decision(task)
        if not decision:
            gaps.append(
                {
                    "intake_task_id": intake_task_id,
                    "candidate_id": candidate_id,
                    "reason": "closed_intake_missing_decision",
                }
            )
            continue
        event_ids = payload.get("event_ids") or []
        event_id = str(payload.get("event_id") or "")
        if not event_id and isinstance(event_ids, list) and event_ids:
            event_id = str(event_ids[0] or "")
        correlation_keys = payload.get("correlation_keys") or []
        if not isinstance(correlation_keys, list):
            correlation_keys = []
        records.append(
            {
                "decision": decision,
                "candidate_id": candidate_id,
                "event_id": event_id,
                "fingerprint": str(payload.get("fingerprint") or ""),
                "correlation_keys": correlation_keys,
                "intake_task_id": intake_task_id,
                "review_task_id": str(task.get("review_task_id") or ""),
                "reason": truncate_text(
                    "Recovered missing Sensorium settlement from completed Kanban intake; "
                    f"review evidence claimed {decision}.",
                    240,
                ),
            }
        )

    return {
        "records": records,
        "gaps": gaps,
        "already_settled": already_settled,
    }


def plan_reviewed_open_intake_settlements(
    tasks: list[dict],
    *,
    decisions: list[dict],
    active_candidate_ids: set[str] | list[str] | None = None,
) -> dict:
    """Plan settlements for open intake tasks that already carry review evidence.

    This closes the live failure class where the cheap reviewer comments a
    DROP/SAVE/PROMOTE decision but cannot complete/archive the substrate row
    because worker scope does not include board-level cleanup authority. Fresh
    untouched open intakes are ignored; reviewed open intakes without an explicit
    decision are returned as visible gaps.
    """
    active_filter = {str(c) for c in active_candidate_ids} if active_candidate_ids is not None else None
    records: list[dict] = []
    gaps: list[dict] = []
    already_settled: list[str] = []
    cleanup_task_ids: list[str] = []

    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        intake_task_id = str(task.get("id") or "")
        title = str(task.get("title") or task.get("name") or "")
        status = str(task.get("status") or "")
        if not intake_task_id or not title.startswith("sensor:intake:"):
            continue
        if status in CLOSED_INTAKE_STATUSES:
            continue

        payload = extract_kanban_intake_payload(str(task.get("body") or ""))
        candidate_id = str(payload.get("candidate_id") or "")
        if active_filter is not None and candidate_id and candidate_id not in active_filter:
            continue

        if _settlement_receipt_exists(decisions, intake_task_id):
            already_settled.append(intake_task_id)
            cleanup_task_ids.append(intake_task_id)
            continue

        review_texts = _task_texts_for_decision(task)
        if not review_texts:
            continue

        decision = infer_kanban_settlement_decision(task)
        if not decision:
            gaps.append(
                {
                    "intake_task_id": intake_task_id,
                    "candidate_id": candidate_id,
                    "reason": "reviewed_open_intake_missing_decision",
                }
            )
            continue

        event_ids = payload.get("event_ids") or []
        event_id = str(payload.get("event_id") or "")
        if not event_id and isinstance(event_ids, list) and event_ids:
            event_id = str(event_ids[0] or "")
        correlation_keys = payload.get("correlation_keys") or []
        if not isinstance(correlation_keys, list):
            correlation_keys = []
        records.append(
            {
                "decision": decision,
                "candidate_id": candidate_id,
                "event_id": event_id,
                "fingerprint": str(payload.get("fingerprint") or ""),
                "correlation_keys": correlation_keys,
                "intake_task_id": intake_task_id,
                "review_task_id": str(task.get("review_task_id") or ""),
                "reason": truncate_text(
                    "Recovered missing Sensorium settlement from reviewed open Kanban intake; "
                    f"review evidence claimed {decision} but worker could not close the row.",
                    240,
                ),
            }
        )
        cleanup_task_ids.append(intake_task_id)

    return {
        "records": records,
        "gaps": gaps,
        "already_settled": already_settled,
        "cleanup_task_ids": cleanup_task_ids,
    }


# ---------------------------------------------------------------------------

# Stale-candidate reconciliation (pure routing/idempotency layer)
#
# The event-driven bridge only ever mirrors *freshly emitted* Sensorium events
# into Kanban. A candidate that crossed the dispatch threshold earlier -- before
# the bridge's event watermark, or while no fresh sample was emitted -- can
# therefore sit active and above-threshold while the board shows nothing. That
# is the split-brain regression in another guise: a quiet pending activation the
# dispatcher would still surface as `kanban_review_required` with no Kanban
# representation.
#
# These helpers are pure (no I/O, no `hermes` CLI). The live tick script feeds
# them the candidate list plus the set of candidate ids that are already
# represented by an open intake, and executes the resulting plan: mint a
# `sensor:intake:*` task for routable kinds, or settle non-routable kinds with a
# DROP receipt. Idempotency keys are derived from the candidate id so re-minting
# the same candidate is a server-side no-op even if tick state is lost.
# ---------------------------------------------------------------------------


def candidate_intake_idempotency_key(candidate: dict) -> str:
    """Stable Kanban idempotency key for a candidate's reconciliation intake.

    Keyed on the candidate id (the most specific stable identifier) so the same
    unresolved candidate never mints a duplicate intake across ticks, even if
    the bridge's local reconciliation state is lost.
    """
    return f"sensorium:intake:candidate:{str(candidate.get('id') or '')}"


def represented_candidate_ids(
    reconciled_candidates: dict,
    open_intake_task_ids: set[str] | list[str] | None,
) -> set[str]:
    """Candidate ids whose reconciliation intake is still open on the board.

    The bridge persists a ``reconciled_candidates`` map (candidate id ->
    ``{intake_task_id, ...}``). A candidate counts as represented only when its
    recorded intake task is still open: if a review archived/completed the intake
    without settling the candidate, the candidate is *not* represented and must
    be re-mirrored rather than left as a quiet pending activation. Settle-drop
    entries carry no ``intake_task_id`` and never count as represented (the
    candidate they settled has already left the active pool by status).
    """
    open_ids = {str(t) for t in (open_intake_task_ids or [])}
    out: set[str] = set()
    for cand_id, rec in (reconciled_candidates or {}).items():
        if not isinstance(rec, dict):
            continue
        if str(rec.get("intake_task_id") or "") in open_ids and str(rec.get("intake_task_id") or ""):
            out.add(str(cand_id))
    return out


def candidate_route(candidate: dict) -> str:
    """Routing policy for an active above-threshold candidate.

    Returns ``"intake"`` when the candidate should be mirrored into a Kanban
    intake task, or ``"settle_drop"`` when policy says it must not be
    Kanban-routed (and so must instead be deterministically settled with a
    receipt). Feedback self-loops are internal echoes the dispatcher already
    refuses to promote; routing them to a human/Subconscious intake would be
    noise, so they are settled with an auditable DROP instead of left pending.
    """
    if is_feedback_self_loop(candidate):
        return "settle_drop"
    return "intake"


def select_active_above_threshold(
    candidates: list[dict],
    *,
    threshold: float = DEFAULT_DISPATCH_PRESSURE_THRESHOLD,
) -> list[dict]:
    """Active (``status == "candidate"``) candidates at/above the dispatch threshold.

    Mirrors ``dispatcher.select_candidate``'s eligibility filter but returns the
    whole set (highest pressure first) rather than only the top one, because
    every one of them is an activation the bridge must account for.
    """
    eligible = [
        c
        for c in candidates
        if c.get("status") == "candidate"
        and float(c.get("pressure", 0) or 0) >= threshold
    ]
    eligible.sort(key=lambda c: float(c.get("pressure", 0) or 0), reverse=True)
    return eligible


def plan_candidate_reconciliation(
    candidates: list[dict],
    *,
    threshold: float = DEFAULT_DISPATCH_PRESSURE_THRESHOLD,
    represented_candidate_ids: set[str] | list[str] | None = None,
    max_intakes: int = MAX_RECONCILE_INTAKES_PER_TICK,
) -> dict:
    """Plan the reconciliation of stale active above-threshold candidates.

    Pure: returns *what* the bridge should do without doing it. The live script
    executes the plan (mint intakes via ``hermes kanban create``, settle drops
    via ``apply_kanban_settlement``).

    - ``mint``: candidates that need a fresh Kanban intake. Each entry carries
      the stable ``idempotency_key`` plus the fields the intake body needs.
    - ``settle``: candidates policy excludes from Kanban; the bridge DROP-settles
      them so they leave the active promotion pool with a receipt.
    - ``skip``: candidates already represented by an open intake (coalesced).
    - ``truncated``: how many mint candidates exceeded ``max_intakes`` this tick
      (surfaced, not silently dropped; drained on the next tick).
    """
    represented = {str(c) for c in (represented_candidate_ids or [])}
    active = select_active_above_threshold(candidates, threshold=threshold)

    mint: list[dict] = []
    settle: list[dict] = []
    skip: list[dict] = []

    for candidate in active:
        cand_id = str(candidate.get("id") or "")
        if cand_id in represented:
            skip.append({"candidate_id": cand_id, "reason": "already_represented_in_kanban"})
            continue
        if candidate_route(candidate) == "settle_drop":
            settle.append(
                {
                    "candidate_id": cand_id,
                    "kind": candidate.get("kind", ""),
                    "reason": "policy_not_kanban_routed:feedback_self_loop",
                }
            )
            continue
        mint.append(
            {
                "candidate_id": cand_id,
                "kind": candidate.get("kind", ""),
                "pressure": candidate.get("pressure"),
                "summary": truncate_text(str(candidate.get("summary") or ""), 200),
                "idempotency_key": candidate_intake_idempotency_key(candidate),
                "fingerprint": str(candidate.get("fingerprint") or ""),
                "event_ids": list(candidate.get("event_ids") or []),
                "correlation_keys": list(candidate.get("correlation_keys") or []),
            }
        )

    truncated = 0
    if max_intakes >= 0 and len(mint) > max_intakes:
        truncated = len(mint) - max_intakes
        mint = mint[:max_intakes]

    return {
        "mint": mint,
        "settle": settle,
        "skip": skip,
        "truncated": truncated,
        "active_count": len(active),
    }
