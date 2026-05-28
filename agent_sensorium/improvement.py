"""Sensorium self-improvement harness primitives.

The harness is deliberately deterministic. It watches Sensorium's own receipts and
Kanban review outcomes for crisp evidence that the attention policy may need a
bounded conscious review, then creates at most one ``attention_policy_review``
candidate. It never mutates wake behavior directly; conscious decisions are
recorded as receipts with explicit future-tendency/verification/rollback fields.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from typing import Any

from .schemas import new_id, truncate_text, utc_now_iso
from .store import SensoriumStore

ATTENTION_POLICY_KIND = "attention_policy_review"
ATTENTION_POLICY_KEY = "attention-policy-review"

DEFAULT_IMPROVEMENT_CONFIG: dict[str, Any] = {
    "repeated_drop_threshold": 3,
    "settlement_gap_threshold": 1,
    "manual_intervention_threshold": 1,
    "completed_outcome_threshold": 2,
    "decision_limit": 500,
    "candidate_pressure": 0.74,
    "max_evidence_items": 8,
    # Low-information historical validation residue often lacks both candidate
    # kind and correlation keys. Treat those rows as evidence-quality debt, not
    # as a wake-worthy policy-review cluster by default; actionable reviews need
    # a stable kind/key so Conscious can tune the right threshold/coalescing path.
    "include_unkeyed_drop_evidence": False,
}

VALID_ATTENTION_DECISIONS = {
    "NO_CHANGE",
    "THRESHOLD_COALESCING_TWEAK_PROPOSAL",
    "SENSOR_ADDITION_TASK",
    "PRIORITY_MAP_CHANGE",
    "MEMORY_SKILL_PATCH",
    "HOLD",
}

_REVIEWED_TASK_STATUSES = {"done", "completed", "archived"}


def _merged_config(config: dict | None = None) -> dict:
    cfg = dict(DEFAULT_IMPROVEMENT_CONFIG)
    if config:
        cfg.update({k: v for k, v in config.items() if v is not None})
    return cfg


def _short_hash(payload: Any) -> str:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _decision_kind(decision: dict) -> str:
    for key in ("kind", "candidate_kind", "event_kind"):
        value = decision.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "unknown"


def _decision_keys(decision: dict) -> list[str]:
    keys = decision.get("correlation_keys")
    if isinstance(keys, list):
        out = [str(k) for k in keys if str(k or "").strip()]
        if out:
            return out[:6]
    kind = _decision_kind(decision)
    return [kind] if kind != "unknown" else []


def _evidence_key(kind: str, keys: list[str]) -> str:
    stable = ":".join([kind, *sorted(keys)])
    return stable[:180] if stable else "unknown"


def _attention_review_fingerprint(evidence_key: str) -> str:
    return _short_hash({"kind": ATTENTION_POLICY_KIND, "evidence_key": evidence_key})


def _active_attention_policy_review_exists(candidates: list[dict], evidence_key: str) -> dict | None:
    fingerprint = _attention_review_fingerprint(evidence_key)
    for candidate in candidates:
        if candidate.get("kind") != ATTENTION_POLICY_KIND:
            continue
        # Reviewed/held review candidates still represent routed conscious work;
        # do not re-mint while the final conscious decision receipt may still be pending.
        if candidate.get("status", "candidate") in {"cancelled", "archived", "closed", "suppressed"}:
            continue
        meta = candidate.get("improvement_meta") or {}
        if meta.get("evidence_key") == evidence_key or candidate.get("fingerprint") == fingerprint:
            return candidate
    return None


def _recent_decision_for_evidence(decisions: list[dict], evidence_key: str) -> dict | None:
    for decision in reversed(decisions):
        if decision.get("type") != "attention_policy_review.decision":
            continue
        if (decision.get("improvement_evidence") or {}).get("evidence_key") == evidence_key:
            return decision
    return None


def _collect_repeated_drop_evidence(
    decisions: list[dict], threshold: int, max_items: int, *, include_unkeyed: bool = False
) -> list[dict]:
    groups: dict[str, dict[str, Any]] = defaultdict(lambda: {"items": [], "count": 0})
    for decision in decisions:
        dtype = decision.get("type")
        is_drop = (
            dtype == "kanban.settlement.applied" and decision.get("decision") == "DROP"
        ) or (
            dtype == "candidate.updated" and decision.get("action") == "suppress"
        )
        if not is_drop:
            continue
        kind = _decision_kind(decision)
        keys = _decision_keys(decision)
        if kind == "unknown" and not keys and not include_unkeyed:
            continue
        key = _evidence_key(kind, keys)
        group = groups[key]
        group["count"] += 1
        if len(group["items"]) < max_items:
            group["items"].append({
                "ts": decision.get("ts"),
                "type": dtype,
                "candidate_id": decision.get("candidate_id"),
                "decision": decision.get("decision") or decision.get("action"),
                "reason": truncate_text(str(decision.get("reason") or ""), 160),
            })
        group["kind"] = kind
        group["correlation_keys"] = keys
    out: list[dict] = []
    for key, group in groups.items():
        if int(group["count"]) < threshold:
            continue
        out.append({
            "type": "repeated_suppression_or_drop",
            "evidence_key": key,
            "count": group["count"],
            "kind": group.get("kind", "unknown"),
            "correlation_keys": group.get("correlation_keys", []),
            "summary": (
                f"{group['count']} DROP/suppress receipts clustered around "
                f"{group.get('kind', 'unknown')} suggest noisy salience or a threshold/coalescing issue."
            ),
            "items": group["items"],
            "suggested_review": "decide whether to tune threshold/coalescing, hold, or record NO_CHANGE",
        })
    out.sort(key=lambda item: int(item.get("count") or 0), reverse=True)
    return out


def _collect_settlement_gap_evidence(
    decisions: list[dict], bridge_state: dict | None, threshold: int, max_items: int
) -> list[dict]:
    gaps: list[dict] = []
    for decision in decisions:
        if decision.get("type") == "kanban.settlement.unresolved":
            gaps.append({
                "ts": decision.get("ts"),
                "candidate_id": decision.get("candidate_id"),
                "event_id": decision.get("event_id"),
                "intake_task_id": decision.get("intake_task_id"),
                "reason": truncate_text(str(decision.get("reason") or ""), 160),
            })
    for gap in ((bridge_state or {}).get("last_post_review_settlement") or {}).get("gaps", []) or []:
        if isinstance(gap, dict):
            gaps.append({
                "ts": (bridge_state or {}).get("updated_at"),
                "candidate_id": gap.get("candidate_id"),
                "intake_task_id": gap.get("intake_task_id"),
                "reason": gap.get("reason"),
            })
    if len(gaps) < threshold:
        return []
    return [{
        "type": "settlement_gap",
        "evidence_key": "settlement_gap",
        "count": len(gaps),
        "kind": "settlement_gap",
        "correlation_keys": ["settlement-gap", "feedback-integrity"],
        "summary": f"{len(gaps)} settlement gap/unresolved receipts indicate feedback integrity may need attention.",
        "items": gaps[-max_items:],
        "suggested_review": "decide whether the settlement enforcement policy needs another guard or NO_CHANGE",
    }]


def _collect_manual_intervention_evidence(decisions: list[dict], threshold: int, max_items: int) -> list[dict]:
    interventions = [
        d for d in decisions
        if d.get("type") in {"attention.manual_intervention", "sensorium.manual_intervention"}
        or d.get("manual_intervention") is True
    ]
    if len(interventions) < threshold:
        return []
    return [{
        "type": "manual_intervention",
        "evidence_key": "manual_intervention",
        "count": len(interventions),
        "kind": "manual_intervention",
        "correlation_keys": ["manual-intervention", "attention-policy"],
        "summary": f"{len(interventions)} manual Sensorium intervention receipts suggest the attention policy missed a repair opportunity.",
        "items": [
            {
                "ts": d.get("ts"),
                "type": d.get("type"),
                "reason": truncate_text(str(d.get("reason") or d.get("summary") or ""), 160),
            }
            for d in interventions[-max_items:]
        ],
        "suggested_review": "decide whether to add a guard, sensor, or task-shape correction",
    }]


def _collect_completed_task_outcome_evidence(
    kanban_tasks: list[dict] | None, threshold: int, max_items: int
) -> list[dict]:
    if not kanban_tasks:
        return []
    interesting: list[dict] = []
    for task in kanban_tasks:
        if not isinstance(task, dict):
            continue
        status = str(task.get("status") or "")
        title = str(task.get("title") or task.get("name") or "")
        if status not in _REVIEWED_TASK_STATUSES:
            continue
        text = " ".join(
            str(task.get(k) or "") for k in ("title", "result", "summary", "body")
        ).lower()
        if title.startswith("conscious:review:") or any(
            marker in text for marker in ("false positive", "no_change", "threshold", "coalesc")
        ):
            interesting.append({
                "task_id": task.get("id"),
                "status": status,
                "title": truncate_text(title, 140),
                "result": truncate_text(str(task.get("result") or task.get("summary") or ""), 180),
            })
    if len(interesting) < threshold:
        return []
    return [{
        "type": "completed_task_outcomes",
        "evidence_key": "completed_task_outcomes",
        "count": len(interesting),
        "kind": "kanban_outcome",
        "correlation_keys": ["kanban-outcome", "attention-policy"],
        "summary": f"{len(interesting)} completed review/task outcomes mention attention-policy relevance.",
        "items": interesting[-max_items:],
        "suggested_review": "decide whether task outcomes imply an attention policy change or NO_CHANGE",
    }]


def collect_improvement_evidence(
    store: SensoriumStore,
    *,
    bridge_state: dict | None = None,
    kanban_tasks: list[dict] | None = None,
    config: dict | None = None,
) -> list[dict]:
    """Return bounded evidence groups that may warrant attention-policy review."""
    cfg = _merged_config(config)
    decisions = store.read_jsonl("decisions", limit=int(cfg["decision_limit"]))
    max_items = int(cfg["max_evidence_items"])
    evidence: list[dict] = []
    evidence.extend(_collect_settlement_gap_evidence(
        decisions, bridge_state, int(cfg["settlement_gap_threshold"]), max_items
    ))
    evidence.extend(_collect_repeated_drop_evidence(
        decisions,
        int(cfg["repeated_drop_threshold"]),
        max_items,
        include_unkeyed=bool(cfg.get("include_unkeyed_drop_evidence")),
    ))
    evidence.extend(_collect_manual_intervention_evidence(
        decisions, int(cfg["manual_intervention_threshold"]), max_items
    ))
    evidence.extend(_collect_completed_task_outcome_evidence(
        kanban_tasks, int(cfg["completed_outcome_threshold"]), max_items
    ))
    rank = {"settlement_gap": 0, "manual_intervention": 1, "repeated_suppression_or_drop": 2, "completed_task_outcomes": 3}
    evidence.sort(key=lambda e: (rank.get(str(e.get("type")), 99), -int(e.get("count") or 0)))
    return evidence


def attention_policy_review_candidate(evidence: dict, *, config: dict | None = None) -> dict:
    """Build a candidate for conscious attention-policy review from one evidence group."""
    cfg = _merged_config(config)
    evidence_key = str(evidence.get("evidence_key") or evidence.get("type") or "attention-policy")
    fingerprint = _attention_review_fingerprint(evidence_key)
    now = utc_now_iso()
    summary = truncate_text(
        f"Attention policy review: {evidence.get('summary') or evidence_key}", 220
    )
    keys = sorted(set([
        ATTENTION_POLICY_KEY,
        "sensorium-self-improvement",
        str(evidence.get("type") or "evidence"),
        *[str(k) for k in (evidence.get("correlation_keys") or []) if str(k or "").strip()],
    ]))
    return {
        "id": new_id("cand"),
        "status": "candidate",
        "kind": ATTENTION_POLICY_KIND,
        "pressure": _safe_float(cfg.get("candidate_pressure"), 0.74),
        "novelty": 0.45,
        "repetition": min(1.0, max(0.0, int(evidence.get("count") or 1) / 5)),
        "identity_relevance": 0.8,
        "relationship_relevance": 0.0,
        "actionability": 0.75,
        "time_sensitivity": 0.15,
        "summary": summary,
        "event_ids": [],
        "correlation_keys": keys,
        "sensitivity": "local_only",
        "allowed_surfaces": ["local"],
        "created_at": now,
        "updated_at": now,
        "expires_at": "",
        "fingerprint": fingerprint,
        "conscious_task": {
            "id": new_id("ctask"),
            "request_type": "THINK",
            "title": "Review Sensorium attention policy evidence",
            "why": truncate_text(str(evidence.get("summary") or evidence_key), 240),
            "expected_decision": (
                "Choose NO_CHANGE, threshold/coalescing tweak proposal, sensor addition task, "
                "priority-map change, memory/skill patch, or HOLD; include future_tendency_delta, "
                "verification_condition, and rollback_condition."
            ),
        },
        "improvement_meta": {
            "schema_version": 1,
            "evidence_key": evidence_key,
            "evidence_type": evidence.get("type"),
            "evidence_count": evidence.get("count"),
            "evidence": evidence,
            "allowed_decisions": sorted(VALID_ATTENTION_DECISIONS),
            "authority_boundary": "conscious_decision_required_before_wake_behavior_mutation",
        },
    }


def run_improvement_collector(
    store: SensoriumStore,
    *,
    bridge_state: dict | None = None,
    kanban_tasks: list[dict] | None = None,
    dry_run: bool = False,
    config: dict | None = None,
) -> dict:
    """Collect Sensorium-internal evidence and create at most one review candidate."""
    store.ensure_dirs()
    evidence = collect_improvement_evidence(
        store, bridge_state=bridge_state, kanban_tasks=kanban_tasks, config=config
    )
    if not evidence:
        result = {"action": "no_evidence", "created": False, "evidence_count": 0}
        return result

    chosen = evidence[0]
    candidates = store.read_jsonl("candidates")
    prior_decision = _recent_decision_for_evidence(store.read_jsonl("decisions"), str(chosen.get("evidence_key") or ""))
    if prior_decision:
        result = {
            "action": "recently_decided",
            "created": False,
            "prior_decision": {
                "ts": prior_decision.get("ts"),
                "decision": prior_decision.get("decision"),
                "future_tendency_delta": prior_decision.get("future_tendency_delta"),
            },
            "evidence": chosen,
        }
        return result

    existing = _active_attention_policy_review_exists(candidates, str(chosen.get("evidence_key") or ""))
    if existing:
        result = {
            "action": "already_exists",
            "created": False,
            "candidate_id": existing.get("id"),
            "evidence": chosen,
        }
        return result

    candidate = attention_policy_review_candidate(chosen, config=config)
    result = {
        "action": "would_create" if dry_run else "created_candidate",
        "created": not dry_run,
        "candidate_id": candidate.get("id"),
        "candidate_preview": candidate if dry_run else None,
        "evidence": chosen,
    }
    if not dry_run:
        store.append_jsonl("candidates", candidate)
        store.append_jsonl("decisions", {
            "ts": utc_now_iso(),
            "type": "attention_policy_review.collector",
            "action": "created_candidate",
            "created": True,
            "candidate_id": candidate.get("id"),
            "improvement_evidence": {
                "evidence_key": chosen.get("evidence_key"),
                "type": chosen.get("type"),
                "count": chosen.get("count"),
            },
            "reason": truncate_text(str(chosen.get("summary") or ""), 240),
        })
        result.pop("candidate_preview", None)
    return result


def record_attention_policy_decision(
    store: SensoriumStore,
    *,
    candidate_id: str,
    decision: str,
    reason: str,
    future_tendency_delta: str,
    verification_condition: str,
    rollback_condition: str,
    decided_by: str = "conscious",
    decision_ref: str = "",
    implementation_ref: str = "",
) -> dict:
    """Record a Conscious attention-policy decision receipt.

    This does not apply threshold/coalescing/sensor changes. It records the
    authorized tendency delta and marks/holds the review candidate so later
    collectors can treat the evidence as consciously settled.
    """
    decision_upper = str(decision or "").upper()
    if decision_upper not in VALID_ATTENTION_DECISIONS:
        raise ValueError(f"decision must be one of {sorted(VALID_ATTENTION_DECISIONS)}")
    required = {
        "reason": reason,
        "future_tendency_delta": future_tendency_delta,
        "verification_condition": verification_condition,
        "rollback_condition": rollback_condition,
    }
    missing = [name for name, value in required.items() if not isinstance(value, str) or not value.strip()]
    if missing:
        raise ValueError(f"attention-policy decision missing required fields: {missing}")

    store.ensure_dirs()
    candidates = store.read_jsonl("candidates")
    target = None
    for candidate in candidates:
        if candidate.get("id") == candidate_id:
            target = candidate
            break
    if target is None:
        raise ValueError(f"candidate '{candidate_id}' not found")
    if target.get("kind") != ATTENTION_POLICY_KIND:
        raise ValueError(f"candidate '{candidate_id}' is not {ATTENTION_POLICY_KIND}")

    old_status = target.get("status", "candidate")
    new_status = "held" if decision_upper == "HOLD" else "reviewed"
    now = utc_now_iso()
    target["status"] = new_status
    target["updated_at"] = now
    meta = target.setdefault("attention_policy_decision", {})
    meta.update({
        "decision": decision_upper,
        "decided_at": now,
        "reason": truncate_text(reason, 240),
        "future_tendency_delta": truncate_text(future_tendency_delta, 300),
        "verification_condition": truncate_text(verification_condition, 300),
        "rollback_condition": truncate_text(rollback_condition, 300),
        "decided_by": truncate_text(decided_by, 80),
    })
    if decision_ref:
        meta["decision_ref"] = truncate_text(decision_ref, 200)
    if implementation_ref:
        meta["implementation_ref"] = truncate_text(implementation_ref, 200)

    path = store._resolve("candidates")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for candidate in candidates:
            f.write(json.dumps(candidate, separators=(",", ":")) + "\n")

    evidence = (target.get("improvement_meta") or {}).get("evidence") or {}
    receipt = {
        "ts": now,
        "type": "attention_policy_review.decision",
        "candidate_id": candidate_id,
        "old_status": old_status,
        "new_status": new_status,
        "decision": decision_upper,
        "reason": truncate_text(reason, 240),
        "future_tendency_delta": truncate_text(future_tendency_delta, 300),
        "verification_condition": truncate_text(verification_condition, 300),
        "rollback_condition": truncate_text(rollback_condition, 300),
        "decided_by": truncate_text(decided_by, 80),
        "decision_ref": truncate_text(decision_ref, 200),
        "implementation_ref": truncate_text(implementation_ref, 200),
        "improvement_evidence": {
            "evidence_key": (target.get("improvement_meta") or {}).get("evidence_key"),
            "type": evidence.get("type"),
            "count": evidence.get("count"),
        },
    }
    store.append_jsonl("decisions", receipt)
    return {
        "action": "recorded",
        "candidate_id": candidate_id,
        "decision": decision_upper,
        "old_status": old_status,
        "new_status": new_status,
        "receipt": receipt,
    }


def summarize_improvement_state(store: SensoriumStore) -> dict:
    candidates = [c for c in store.read_jsonl("candidates") if c.get("kind") == ATTENTION_POLICY_KIND]
    decisions = [d for d in store.read_jsonl("decisions") if str(d.get("type") or "").startswith("attention_policy_review")]
    counts = Counter(c.get("status", "candidate") for c in candidates)
    return {
        "attention_policy_review_candidates": len(candidates),
        "candidate_status_counts": dict(counts),
        "recent_candidates": [
            {
                "id": c.get("id"),
                "status": c.get("status"),
                "summary": truncate_text(str(c.get("summary") or ""), 120),
                "evidence_key": (c.get("improvement_meta") or {}).get("evidence_key"),
            }
            for c in candidates[-5:]
        ],
        "recent_decisions": decisions[-5:],
    }
