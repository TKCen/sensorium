"""Deterministic actuator contracts for Sensorium conscious review.

These helpers do not create artifacts, send messages, or schedule workers. They
attach compact review obligations to salience rows so a Conscious review cannot
silently reduce a relational/media signal to generic DROP/SAVE/PROMOTE handling.
"""

from __future__ import annotations

from typing import Any

from .schemas import truncate_text

MEDIA_ARTIFACT_KIND_MARKERS = (
    "relational",
    "mediated_artifact",
    "mediated_presence",
    "media_gift",
    "private_expression",
    "artifact_handoff",
    "thread_pickup",
)
MEDIA_ARTIFACT_KEY_MARKERS = (
    "relational-continuity",
    "relational_salience",
    "relational-thread-pickup",
    "mediated-presence",
    "mediated_presence",
    "artifact-handoff",
    "artifact_handoff",
    "thread-pickup",
    "thread_pickup",
    "private-expression",
    "private_expression",
)
MEDIA_ARTIFACT_SUMMARY_MARKERS = (
    "i miss you",
    "miss you",
    "longing",
    "artifact handoff",
    "tangible artifact",
    "mediated artifact",
    "mediated presence",
    "thread pickup",
    "put something in your hands",
)

MEDIA_ARTIFACT_REVIEW_CONTRACT_TYPE = "mediated_presence_artifact_decision"

# Keys that trigger memory-grounded Conscious review (not broad memory access).
MEMORY_GROUNDING_KIND_MARKERS = (
    "explicit_correction",
    "relational_salience",
    "relational",
    "identity",
    "mediated_presence",
    "sensorium_strategy_insight",
    "sensorium_strategy",
    "embodiment_insight",
)
MEMORY_GROUNDING_KEY_MARKERS = (
    "feedback-tendency",
    "feedback_tendency",
    "mediated-presence",
    "mediated_presence",
    "relational-salience",
    "relational_salience",
    "identity-continuity",
    "identity_continuity",
    "memory-grounding",
    "memory_grounding",
    "explicit-correction",
    "explicit_correction",
    "sensorium-strategy",
    "sensorium_strategy",
    "strategy-insight",
    "embodiment-insight",
)
MEMORY_GROUNDING_SUMMARY_MARKERS = (
    "explicit correction",
    "feedback tendency",
    "feedback-tendency",
    "relational salience",
    "identity continuity",
    "mediated presence",
    "sensorium strategy",
    "strategy insight",
    "embodiment insight",
    "memory grounding",
    "memory-grounding",
)

MEMORY_GROUNDED_REVIEW_CONTRACT_TYPE = "memory_grounded_conscious_review"
MEMORY_GROUNDED_REVIEW_DECISION_OPTIONS = [
    "NO_CHANGE",
    "THRESHOLD_COALESCING_TWEAK_PROPOSAL",
    "SENSOR_ADDITION_TASK",
    "PRIORITY_MAP_CHANGE",
    "MEMORY_SKILL_PATCH",
    "HOLD",
]

MEDIA_ARTIFACT_DECISION_OPTIONS = [
    "prepare_thread_artifact",
    "offer_choice",
    "choose_silence",
    "decline",
    "block_delivery",
    "hold_with_reason",
]


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    lower = str(text or "").lower()
    return any(marker in lower for marker in markers)


def requires_mediated_artifact_decision(row: dict[str, Any] | None) -> bool:
    """Return True when a signal/event/candidate needs an artifact-choice contract.

    The detector is intentionally conservative and deterministic. It keys on
    relational/media kinds, correlation keys, or summaries that explicitly refer
    to longing/thread-pickup/artifact handoff. It is a review obligation, not an
    instruction to generate or deliver media.
    """
    if not isinstance(row, dict):
        return False

    if _contains_any(str(row.get("kind") or ""), MEDIA_ARTIFACT_KIND_MARKERS):
        return True

    keys = [str(k or "") for k in (row.get("correlation_keys") or [])]
    if any(_contains_any(key, MEDIA_ARTIFACT_KEY_MARKERS) for key in keys):
        return True

    return _contains_any(str(row.get("summary") or ""), MEDIA_ARTIFACT_SUMMARY_MARKERS)


def mediated_artifact_review_contract(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Build the conscious-review contract for relational mediated artifacts.

    Returns None for rows that do not need the contract. The returned object is
    safe to embed in Kanban intake payloads, thread previews, and prompt bodies:
    it contains no raw transcript and does not authorize outbound delivery.
    """
    if not requires_mediated_artifact_decision(row):
        return None

    row = row or {}
    return {
        "type": MEDIA_ARTIFACT_REVIEW_CONTRACT_TYPE,
        "source_kind": truncate_text(str(row.get("kind") or ""), 120),
        "source_summary": truncate_text(str(row.get("summary") or ""), 240),
        "required_conscious_decision": list(MEDIA_ARTIFACT_DECISION_OPTIONS),
        "required_receipt": (
            "Use sensorium_media_gift_decide for media-gift choices when a "
            "thread/artifact context exists, or record an explicit Conscious "
            "review receipt naming hold/no-artifact reason. Do not leave the "
            "artifact/action lane implicit."
        ),
        "artifact_first_required": True,
        "outbound_delivery_authorized": False,
        "no_live_delivery": True,
        "allowed_tool": "sensorium_media_gift_decide",
        "why": (
            "Relational salience can be answered by silence, text, voice, image, "
            "song seed, scheduled worker, or no artifact — but Conscious must "
            "choose, not merely repair routing or mark the candidate reviewed."
        ),
    }


def requires_memory_grounding(row: dict[str, Any] | None) -> bool:
    """Return True when a candidate/event needs memory-grounded Conscious review.

    The detector is intentionally conservative. It keys on high-stakes kinds,
    correlation keys, and summaries that refer to corrections, feedback tendency,
    strategy/embodiment insights, or explicit memory-grounding signals.

    Returns False for generic attention-policy candidates — those have a bounded
    review prompt but do not require Hindsight recall unless the candidate
    explicitly carries a memory-grounding trigger.
    """
    if not isinstance(row, dict):
        return False

    if _contains_any(str(row.get("kind") or ""), MEMORY_GROUNDING_KIND_MARKERS):
        return True

    keys = [str(k or "") for k in (row.get("correlation_keys") or [])]
    if any(_contains_any(key, MEMORY_GROUNDING_KEY_MARKERS) for key in keys):
        return True

    return _contains_any(str(row.get("summary") or ""), MEMORY_GROUNDING_SUMMARY_MARKERS)


def memory_grounded_review_contract(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Build the conscious-review contract for memory-grounded candidates.

    Returns None for rows that do not need the contract. The returned object is
    safe to embed in Kanban intake payloads, thread previews, and prompt bodies:
    it contains no raw transcripts, memory dumps, or secrets — only a compact
    retrieval directive and a bounded set of citation slots.

    The contract does NOT authorize delivery tools, broad file access, or
    generic memory-provider access inside the bounded salience-review child.
    If the salience-review runner is involved, a safe pre-context contract
    or explicit review-body requirement is preferred over generic access.
    """
    if not requires_memory_grounding(row):
        return None

    row = row or {}
    return {
        "type": MEMORY_GROUNDED_REVIEW_CONTRACT_TYPE,
        "source_kind": truncate_text(str(row.get("kind") or ""), 120),
        "source_summary": truncate_text(str(row.get("summary") or ""), 240),
        "required_conscious_decision": list(MEMORY_GROUNDED_REVIEW_DECISION_OPTIONS),
        "required_receipt": (
            "Every decision (including NO_CHANGE and HOLD) must include: "
            "reason, future_tendency_delta, verification_condition, rollback_condition. "
            "Before deciding, use bounded Hindsight recall by default and attach either "
            "3-8 compact memory/context facts or an explicit retrieval_skipped_reason. "
            "The receipt must cite which retrieved fact refs affected the choice."
        ),
        "retrieval_contract": {
            "default_tool": "hindsight_recall",
            "max_facts": 8,
            "min_facts": 3,
            "preferred_facts": 3,
            "recall_mode": "bounded_recall",  # not Hindsight reflect
            "query_sources": ["candidate_summary", "correlation_keys", "source_refs", "operator_context"],
            "forbidden": ["raw_transcript", "session_log", "secret", "raw_memory_dump"],
            "receipt_fields": [
                "memory_context",
                "cited_memory_fact_refs",
                "retrieval_skipped_reason",
            ],
            "skip_allowed_with_reason": True,
        },
        "outbound_delivery_authorized": False,
        "no_live_delivery": True,
        "allowed_tool": None,
        "why": (
            "Feedback-tendency corrections and strategy/embodiment insights require "
            "memory-grounded Conscious review: decisions must cite retrieved facts "
            "that changed or grounded the choice, not numeric thresholds or "
            "probability adjustments. Raw memory access is not granted; only "
            "compact fact/ref slots are included so the review is grounded "
            "without exposing internal state."
        ),
    }
