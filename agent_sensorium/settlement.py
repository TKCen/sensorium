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
from datetime import datetime, timezone
from typing import Any

from .gate import is_feedback_self_loop
from .schemas import truncate_text, utc_now_iso
from .store import SensoriumStore

VALID_SETTLEMENT_DECISIONS = {"DROP", "SAVE", "PROMOTE_CONSCIOUS"}
RECEIPT_SCHEMA = "sensorium.decision_receipt.v1"
SETTLEMENT_RECEIPT_TYPES = {
    "kanban.settlement.applied",
    "kanban.settlement.unresolved",
    "kanban.settlement.no_candidate_match",
}

# Mirror of the dispatcher's default `dispatch_pressure` threshold. The
# reconciliation pass treats any active candidate at or above this pressure as a
# pending activation that must be visible in Kanban or settled with a receipt.
DEFAULT_DISPATCH_PRESSURE_THRESHOLD = 0.5

# Bounded fan-out: never mint more than this many reconciliation intakes in a
# single tick. Overflow is reported (never silently dropped) so a backlog spike
# cannot flood the board, and the next tick drains the remainder.
MAX_RECONCILE_INTAKES_PER_TICK = 25

# Projection-only vocabulary. These values deliberately do not replace the
# entity-specific persisted status enums: canonical owners remain the only
# transition writers.
LIVENESS_STATES = frozenset({
    "active", "reviewing", "blocked", "held", "prepared", "settled",
    "stale", "error", "quiet", "unknown",
})
LIVENESS_REASON_CODES = frozenset({
    "above_threshold_unrepresented", "already_represented_in_kanban",
    "feedback_self_loop", "truncated_intake_capacity", "reviewed_open_intake",
    "reviewed_open_intake_missing_decision", "stale_aperture",
    "historical_prepared_pointer", "candidate_below_threshold",
    "candidate_unknown_status", "outbox_prepared", "outbox_failed",
    "outbox_dispatched", "outbox_unknown_status",
})
LIVENESS_RECEIPT_SCHEMA = "sensorium.liveness_receipt.v1"
LIVENESS_RECEIPT_KIND = "liveness_reconciliation"
LIVENESS_RECEIPT_VERSION = 1

# Status applied to the originating candidate per decision. DROP suppresses so
# the dispatcher's `select_candidate` filter (status == "candidate") cannot
# later promote it. SAVE/PROMOTE_CONSCIOUS mark the candidate reviewed so its
# audit trail is preserved while it leaves the active promotion pool.
DECISION_TO_CANDIDATE_STATUS = {
    "DROP": "suppressed",
    "SAVE": "reviewed",
    "PROMOTE_CONSCIOUS": "reviewed",
}


def _safe_scalar_label(prefix: str, value: Any) -> str | None:
    """Deterministic, non-reversible label for free-form receipt scalars.

    Settlement evidence can originate in Kanban task text or operator comments,
    so receipt rows must never persist raw prose/log/transcript/secret content.
    IDs that are needed for joins remain in their legacy compact fields; every
    evidence/reason/detail scalar in the normalized receipt body is hash-labeled.
    """
    if value is None:
        return None
    try:
        seed = json.dumps(value, default=str, sort_keys=True, separators=(",", ":"))
    except TypeError:
        seed = str(value)
    digest = hashlib.sha256(seed.encode("utf-8", errors="ignore")).hexdigest()[:16]
    return f"{prefix}#{digest}"


def _receipt_idempotency_key(*, receipt_type: str, subject_id: str, decision: str, evidence: dict) -> str:
    material = {
        "type": receipt_type,
        "subject": subject_id,
        "decision": decision,
        "intake_task_id": evidence.get("intake_task_id") or "",
        "review_task_id": evidence.get("review_task_id") or "",
        "event_id": evidence.get("event_id") or "",
        "fingerprint": evidence.get("fingerprint") or "",
        "correlation_keys": sorted(str(k) for k in (evidence.get("correlation_keys") or []) if k),
    }
    digest = hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return f"receipt:{digest}"


def _evidence_ref(ref_type: str, value: Any) -> dict | None:
    label = _safe_scalar_label(ref_type, value)
    if label is None:
        return None
    return {"type": ref_type, "ref": label}


def candidate_ref_label(candidate_id: str) -> str:
    """Deterministic opaque label for a candidate id.

    Used both as the receipt `subject_ref.id` and as the join key dashboard
    projections use to re-attach candidate metadata without ever echoing the
    raw (potentially corrupted/secret-shaped) candidate id.
    """
    return _safe_scalar_label("candidate", candidate_id) or _safe_scalar_label("candidate", "") or "candidate#0"


def evidence_ref_label(ref_type: str, value: Any) -> str:
    """Public deterministic label for an evidence scalar, for cross-module joins."""
    return _safe_scalar_label(ref_type, value) or _safe_scalar_label(ref_type, "") or f"{ref_type}#0"


# Candidate `kind` is attacker/sensor-controlled free text (it is copied verbatim
# from the originating event's `kind`; see `gate.event_to_candidate`). There is no
# existing closed vocabulary for it project-wide, so this is a small explicit safe
# vocab of known-benign builtin kinds. Anything outside this set is hash-labeled
# rather than echoed raw into any privacy-safe projection (e.g. the dashboard graph).
SAFE_CANDIDATE_KINDS = {
    "dashboard_memory_pressure",
    "budget_pressure",
    "inference_budget_pressure",
    "process_pressure",
    "body_pressure",
    "hindsight_pressure",
    "kanban_pressure",
    "media_capacity",
    "rate_window",
    "temporal_trend",
    "pressure_spike",
    "pressure_event",
    "relational_salience",
    "relational_thread_pickup_request",
    "internal_conscious_task_candidate",
    "gateway_error",
    "runtime_heartbeat",
    "tts_sidecar_pressure",
    "user_correction",
    "explicit_correction",
    "embodiment_insight",
    "design_insight",
    "design_decision",
    "sensorium_strategy_insight",
    "llm_reflect",
    "kanban_outcome",
    "manual_intervention",
    "settlement_gap",
    "identity",
    "note",
    "subconscious_advisory",
    "anomaly",
    "noise_event",
    "test",
    "test_event",
    "test_signal",
    "unknown",
}


def safe_candidate_kind_label(kind: Any) -> str:
    """Closed-vocab-or-hash label for a candidate `kind` value.

    Never echoes raw kind text outside the safe vocab, since `kind` is copied
    verbatim from sensor/event input and can carry secret-shaped or corrupted
    content.
    """
    text = str(kind or "unknown")
    if text in SAFE_CANDIDATE_KINDS:
        return text
    return _safe_scalar_label("kind", text) or "kind#0"


# conscious_task_ref id-like subfields are Kanban task/thread/board/candidate ids
# supplied by the caller (the Conscious-promotion bridge) and are therefore just
# as corruptible/secret-shaped as candidate_id/intake_task_id/review_task_id.
# Every one of them must be opaque before it is persisted into a normalized
# receipt row.
_CONSCIOUS_REF_ID_KEYS = ("task_id", "thread_id", "board", "kanban_task_id", "candidate_id", "conscious_task_id")
_ISO_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def safe_conscious_task_ref(ref: Any) -> dict:
    """Privacy-safe projection of a conscious_task_ref for a normalized receipt.

    Every id-like subfield (`task_id`, `thread_id`, `board`, `kanban_task_id`,
    `candidate_id`, `conscious_task_id`) is hash-labeled rather than echoed raw.
    `kind` reuses the same closed-vocab-or-hash treatment as candidate kind.
    `promoted_at` is only passed through when it looks like a validated ISO
    timestamp; anything else is dropped rather than risk echoing free text.
    """
    if not isinstance(ref, dict):
        return {}
    out: dict[str, str] = {}
    for key in _CONSCIOUS_REF_ID_KEYS:
        value = ref.get(key)
        if isinstance(value, str) and value.strip():
            out[key] = evidence_ref_label(f"conscious_{key}", value)
    kind = ref.get("kind")
    if isinstance(kind, str) and kind.strip():
        out["kind"] = safe_candidate_kind_label(kind)
    promoted_at = ref.get("promoted_at")
    if isinstance(promoted_at, str) and _ISO_TIMESTAMP_RE.match(promoted_at):
        out["promoted_at"] = promoted_at
    return out


def _settlement_evidence_refs(
    *,
    candidate_id: str = "",
    event_id: str = "",
    fingerprint: str = "",
    correlation_keys: list[str] | None = None,
    intake_task_id: str = "",
    review_task_id: str = "",
    conscious_task_ref: dict | None = None,
    reason: str = "",
) -> list[dict]:
    refs: list[dict] = []
    for ref_type, value in (
        ("candidate", candidate_id),
        ("event", event_id),
        ("fingerprint", fingerprint),
        ("intake_task", intake_task_id),
        ("review_task", review_task_id),
        ("reason", reason),
    ):
        ref = _evidence_ref(ref_type, value)
        if ref is not None:
            refs.append(ref)
    for key in correlation_keys or []:
        ref = _evidence_ref("correlation", key)
        if ref is not None:
            refs.append(ref)
    if conscious_task_ref:
        ref = _evidence_ref("conscious_task", conscious_task_ref)
        if ref is not None:
            refs.append(ref)
    # Stable de-duplication without leaking raw values.
    unique: dict[tuple[str, str], dict] = {}
    for ref in refs:
        unique[(ref["type"], ref["ref"])] = ref
    return list(unique.values())[:24]


def normalize_settlement_receipt(
    *,
    receipt_type: str,
    decision: str,
    subject_id: str,
    outcome: str,
    created_at: str,
    candidate_id: str = "",
    event_id: str = "",
    fingerprint: str = "",
    correlation_keys: list[str] | None = None,
    intake_task_id: str = "",
    review_task_id: str = "",
    conscious_task_ref: dict | None = None,
    old_status: str = "",
    new_status: str = "",
    reason: str = "",
    sensitivity: str = "private",
    allowed_surfaces: list[str] | None = None,
) -> dict:
    """Return the normalized decisions.jsonl receipt row for a settlement.

    The schema is intentionally compact and review-oriented. It carries enough
    structured refs for dashboard/graph settlement links while replacing raw
    evidence/prose with deterministic labels.
    """
    evidence = {
        "candidate_id": candidate_id,
        "event_id": event_id,
        "fingerprint": fingerprint,
        "correlation_keys": list(correlation_keys or []),
        "intake_task_id": intake_task_id,
        "review_task_id": review_task_id,
    }
    subject = subject_id or candidate_id or event_id or fingerprint or "unknown"
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "receipt_kind": "settlement",
        "ts": created_at,
        "created_at": created_at,
        "type": receipt_type,
        "subject_ref": {"type": "candidate", "id": candidate_ref_label(subject)},
        "decision": decision,
        "outcome": outcome,
        "decided_by": {
            "kind": "kanban_review",
            "ref": _safe_scalar_label("review", review_task_id or intake_task_id or "settlement"),
        },
        "evidence_refs": _settlement_evidence_refs(
            candidate_id=candidate_id or subject,
            event_id=event_id,
            fingerprint=fingerprint,
            correlation_keys=list(correlation_keys or []),
            intake_task_id=intake_task_id,
            review_task_id=review_task_id,
            conscious_task_ref=conscious_task_ref,
            reason=reason,
        ),
        "idempotency_key": _receipt_idempotency_key(
            receipt_type=receipt_type,
            subject_id=subject,
            decision=decision,
            evidence=evidence,
        ),
        "surface": "kanban",
        "allowed_surfaces": list(allowed_surfaces or ["local"]),
        "sensitivity": sensitivity or "private",
        "raw_content": False,
    }
    # Closed-vocab statuses only; raw id-like join material (candidate/event ids,
    # fingerprints, correlation keys, intake/review task ids) is deliberately not
    # persisted here. Those scalars can originate from Kanban task text/comments
    # or compact state and are corruptible/secret-shaped; joins are carried via
    # `evidence_refs` (hash-labeled) and `subject_ref.id` instead.
    if old_status:
        receipt["old_status"] = old_status
    if new_status:
        receipt["new_status"] = new_status
    reason_label = _safe_scalar_label("reason", reason)
    if reason_label:
        receipt["reason_label"] = reason_label
    if conscious_task_ref:
        safe_ref = safe_conscious_task_ref(conscious_task_ref)
        if safe_ref:
            receipt["conscious_task_ref"] = safe_ref
    return receipt


def _find_receipt_by_idempotency(decisions: list[dict], idempotency_key: str) -> dict | None:
    for row in decisions:
        if isinstance(row, dict) and row.get("idempotency_key") == idempotency_key:
            return row
    return None


def _append_decision_receipt_once(store: SensoriumStore, receipt: dict, *, record_receipt: bool) -> dict | None:
    if not record_receipt:
        return receipt
    existing = _find_receipt_by_idempotency(store.read_jsonl("decisions"), str(receipt.get("idempotency_key") or ""))
    if existing is not None:
        return existing
    store.append_jsonl("decisions", receipt)
    return receipt

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
        receipt = normalize_settlement_receipt(
            receipt_type="kanban.settlement.unresolved",
            decision=decision_upper,
            subject_id=candidate_id or event_id or fingerprint or "unresolved",
            outcome="unresolved",
            created_at=utc_now_iso(),
            candidate_id=candidate_id,
            event_id=event_id,
            fingerprint=fingerprint,
            correlation_keys=correlation_keys,
            intake_task_id=intake_task_id,
            review_task_id=review_task_id,
            reason=reason,
        )
        written = _append_decision_receipt_once(store, receipt, record_receipt=record_receipt)
        return {
            "action": "no_candidate_match",
            "decision": decision_upper,
            "matched_candidate_ids": [],
            "receipts": [written] if written is not None else [],
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
            "reason_label": _safe_scalar_label("reason", truncate_text(reason, 240)),
        }
        if conscious_ref:
            settlement_meta["conscious_task_ref"] = conscious_ref
        candidate["kanban_settlement"] = settlement_meta
        updated_ids.append(cand_id)

        receipt = normalize_settlement_receipt(
            receipt_type="kanban.settlement.applied",
            decision=decision_upper,
            subject_id=cand_id,
            outcome=candidate["status"],
            created_at=now,
            candidate_id=cand_id,
            event_id=event_id,
            fingerprint=fingerprint or str(candidate.get("fingerprint") or ""),
            correlation_keys=correlation_keys or list(candidate.get("correlation_keys") or []),
            intake_task_id=intake_task_id,
            review_task_id=review_task_id,
            conscious_task_ref=conscious_ref,
            old_status=old_status,
            new_status=candidate["status"],
            reason=reason,
            sensitivity=str(candidate.get("sensitivity") or "private"),
            allowed_surfaces=list(candidate.get("allowed_surfaces") or ["local"]),
        )
        written = _append_decision_receipt_once(store, receipt, record_receipt=record_receipt)
        if written is not None:
            receipts.append(written)

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
    # New-format receipts no longer carry a raw `intake_task_id` field (it is a
    # corruptible/secret-shaped join scalar); match via the hash-labeled
    # `evidence_refs` entry instead. Legacy rows with a raw field are still
    # honored for back-compat with receipts written before this change.
    target_ref = evidence_ref_label("intake_task", intake_task_id)
    for decision in decisions:
        if not isinstance(decision, dict):
            continue
        if decision.get("type") != "kanban.settlement.applied":
            continue
        if str(decision.get("intake_task_id") or "") == intake_task_id:
            return True
        for ref in decision.get("evidence_refs") or []:
            if isinstance(ref, dict) and ref.get("type") == "intake_task" and ref.get("ref") == target_ref:
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
    def pressure(candidate: dict) -> float:
        try:
            return float(candidate.get("pressure", 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    eligible = [c for c in candidates if c.get("status") == "candidate" and pressure(c) >= threshold]
    # Stable pressure-desc/id-asc ordering is an idempotency property: capacity
    # truncation must choose the same candidates on every identical re-run.
    eligible.sort(key=lambda c: (-pressure(c), str(c.get("id") or "")))
    return eligible


def _derived_stale_aperture_ids(candidates: list[dict], now: str, stale_after_minutes: int = 180) -> set[str]:
    """Fail-safe stale aperture detection for the pure reconciliation snapshot."""
    try:
        now_dt = datetime.fromisoformat(now.replace("Z", "+00:00"))
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return set()
    stale: set[str] = set()
    for candidate in candidates:
        if candidate.get("status") != "in_conscious_aperture":
            continue
        aperture = (candidate.get("conscious_aperture") if isinstance(candidate.get("conscious_aperture"), dict) else {}) or {}
        opened = aperture.get("opened_at") or candidate.get("updated_at")
        try:
            opened_dt = datetime.fromisoformat(str(opened).replace("Z", "+00:00"))
            if opened_dt.tzinfo is None:
                opened_dt = opened_dt.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            # An unparseable open aperture is unsafe to replace, so it is stale.
            stale.add(str(candidate.get("id") or ""))
            continue
        if (now_dt - opened_dt.astimezone(timezone.utc)).total_seconds() >= stale_after_minutes * 60:
            stale.add(str(candidate.get("id") or ""))
    return stale


def classify_liveness_snapshot(
    candidates: list[dict],
    *,
    now: str,
    threshold: float = DEFAULT_DISPATCH_PRESSURE_THRESHOLD,
    represented_candidate_ids: set[str] | list[str] | None = None,
    stale_aperture_ids: set[str] | list[str] | None = None,
    historical_outbox_ids: set[str] | list[str] | None = None,
    outbox: list[dict] | None = None,
) -> dict:
    """Return a pure, bounded liveness projection with opaque subject refs."""
    represented = {str(value) for value in (represented_candidate_ids or [])}
    stale = _derived_stale_aperture_ids(candidates, now) | {str(value) for value in (stale_aperture_ids or [])}
    historical = {str(value) for value in (historical_outbox_ids or [])}
    findings: list[dict] = []
    candidate_statuses = {
        "candidate", "in_conscious_aperture", "held", "prepared_external_work",
        "reviewed", "suppressed", "cancelled", "archived",
    }
    for candidate in sorted(candidates, key=lambda row: str(row.get("id") or "")):
        candidate_id = str(candidate.get("id") or "")
        status = str(candidate.get("status") or "")
        if not candidate_id or status not in candidate_statuses:
            state, reason = "unknown", "candidate_unknown_status"
        elif candidate_id in stale:
            state, reason = "stale", "stale_aperture"
        elif status == "in_conscious_aperture":
            state, reason = "reviewing", "stale_aperture"
        elif status == "held":
            state, reason = "held", "candidate_below_threshold"
        elif status == "prepared_external_work":
            state, reason = "prepared", "candidate_below_threshold"
        elif status in {"reviewed", "suppressed", "cancelled", "archived"}:
            state, reason = "settled", "candidate_below_threshold"
        else:
            try:
                above = float(candidate.get("pressure", 0) or 0) >= threshold
            except (TypeError, ValueError):
                above = False
            if candidate_id in represented:
                state, reason = "blocked", "already_represented_in_kanban"
            elif above:
                state, reason = "active", "above_threshold_unrepresented"
            else:
                state, reason = "quiet", "candidate_below_threshold"
        findings.append({
            "subject_ref": {"type": "candidate", "id": candidate_ref_label(candidate_id)},
            "state": state, "reason_code": reason, "observed_at": now,
            "source": "liveness_snapshot", "actionable": state in {"active", "blocked", "stale"},
            "terminal": state == "settled", "related_refs": [],
        })
    for row in sorted(outbox or [], key=lambda item: str(item.get("id") or "")):
        outbox_id = str(row.get("id") or "")
        status = str(row.get("status") or "").lower()
        if outbox_id in historical:
            state, reason, actionable, terminal = "settled", "historical_prepared_pointer", False, True
        elif status == "prepared":
            state, reason, actionable, terminal = "prepared", "outbox_prepared", True, False
        elif status == "failed":
            state, reason, actionable, terminal = "error", "outbox_failed", True, False
        elif status == "dispatched":
            state, reason, actionable, terminal = "settled", "outbox_dispatched", False, True
        else:
            state, reason, actionable, terminal = "unknown", "outbox_unknown_status", False, False
        findings.append({
            "subject_ref": {"type": "outbox", "id": evidence_ref_label("outbox", outbox_id)},
            "state": state, "reason_code": reason, "observed_at": now,
            "source": "outbox_lineage", "actionable": actionable, "terminal": terminal,
            "related_refs": [],
        })
    return {"version": 1, "now": now, "findings": findings}


def _liveness_receipt_key(subject_ref: dict[str, Any], reason_code: str) -> str:
    """Stable opaque identity for a classification-only reconciliation receipt."""
    material = {
        "subject_type": subject_ref["type"],
        "subject_id": subject_ref["id"],
        "reason_code": reason_code,
        "version": LIVENESS_RECEIPT_VERSION,
    }
    digest = hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return f"liveness:{digest}"


def append_liveness_receipts(store: SensoriumStore, findings: list[dict[str, Any]]) -> dict[str, int]:
    """Append compact liveness findings once, without owning any transition.

    Findings are already a privacy-safe classification boundary. This writer
    validates each field again so legacy/corrupt callers fail closed rather than
    turning a receipt into a raw state projection.
    """
    existing = {
        str(row.get("idempotency_key") or "")
        for row in store.read_jsonl("decisions")
        if row.get("schema") == LIVENESS_RECEIPT_SCHEMA
    }
    written = skipped = 0
    for finding in findings:
        raw_subject = finding.get("subject_ref")
        subject: dict[str, Any] = raw_subject if isinstance(raw_subject, dict) else {}
        subject_type = str(subject.get("type") or "")
        subject_id = str(subject.get("id") or "")
        state = str(finding.get("state") or "")
        reason_code = str(finding.get("reason_code") or "")
        source = str(finding.get("source") or "")
        if (
            subject_type not in {"candidate", "outbox"}
            or not re.fullmatch(r"[a-z_]+#[0-9a-f]{16}", subject_id)
            or state not in LIVENESS_STATES
            or reason_code not in LIVENESS_REASON_CODES
            or source not in {"liveness_snapshot", "outbox_lineage"}
        ):
            skipped += 1
            continue
        safe_subject = {"type": subject_type, "id": subject_id}
        key = _liveness_receipt_key(safe_subject, reason_code)
        if key in existing:
            skipped += 1
            continue
        observed_at = finding.get("observed_at")
        receipt = {
            "schema": LIVENESS_RECEIPT_SCHEMA,
            "receipt_kind": LIVENESS_RECEIPT_KIND,
            "idempotency_key": key,
            "ts": observed_at if isinstance(observed_at, str) and len(observed_at) <= 60 else utc_now_iso(),
            "subject_ref": safe_subject,
            "old_liveness": "unknown",
            "new_liveness": state,
            "reason_code": reason_code,
            "source": source,
            "action": "none",
            "related_refs": [],
            "version": LIVENESS_RECEIPT_VERSION,
        }
        store.append_jsonl("decisions", receipt)
        existing.add(key)
        written += 1
    return {"written": written, "skipped": skipped}


def plan_liveness_reconciliation(
    candidates: list[dict],
    *,
    now: str,
    threshold: float = DEFAULT_DISPATCH_PRESSURE_THRESHOLD,
    represented_candidate_ids: set[str] | list[str] | None = None,
    stale_aperture_ids: set[str] | list[str] | None = None,
    max_intakes: int = MAX_RECONCILE_INTAKES_PER_TICK,
) -> dict:
    """Build the deterministic first-slice plan without mutating any store."""
    resolved_stale = _derived_stale_aperture_ids(candidates, now) | {str(value) for value in (stale_aperture_ids or [])}
    candidate_plan = plan_candidate_reconciliation(
        candidates, threshold=threshold, represented_candidate_ids=represented_candidate_ids, max_intakes=max_intakes
    )
    return {
        "version": 1,
        "classification": classify_liveness_snapshot(
            candidates, now=now, threshold=threshold,
            represented_candidate_ids=represented_candidate_ids, stale_aperture_ids=resolved_stale,
        ),
        "candidate_reconciliation": candidate_plan,
        "summary": {
            "active": candidate_plan["active_count"], "mint": len(candidate_plan["mint"]),
            "settle": len(candidate_plan["settle"]), "truncated": candidate_plan["truncated"],
            "stale_apertures": len(resolved_stale),
        },
    }


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
