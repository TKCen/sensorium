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
    "sera-presence",
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
