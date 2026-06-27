"""Helpers for live-turn salience receipts and cheap residue review.

This module stays instance-neutral: it does not know about named agents,
chat channels, private policy cards, or any deployment-specific routing. It only
codifies the reusable boundary between foreground-owned work and compact
unresolved residue.
"""

from __future__ import annotations

from typing import Any

from .schemas import new_id, truncate_text, utc_now_iso

FOREGROUND_RESOLUTIONS = frozenset({"full", "partial", "none", "explicit_no_action"})
RESIDUE_KINDS = frozenset({"none", "watch", "later_review", "pattern_pressure"})
DURABLE_CAPTURES = frozenset({"none", "memory", "skill", "docs", "task", "artifact"})
TURN_REVIEW_DECISIONS = frozenset({
    "no_action",
    "sensorium_residue_candidate",
    "memory_candidate",
    "skill_candidate",
    "docs_candidate",
    "followup_review_needed",
})


def _safe_enum(value: Any, allowed: frozenset[str], default: str) -> str:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in allowed:
            return normalized
    return default


def _safe_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off"}:
            return False
    return default


def normalize_live_turn_intent(
    *,
    foreground_action_taken: Any = False,
    foreground_resolution: Any = "none",
    residue: Any = "later_review",
    durable_capture: Any = "none",
    background_action_allowed: Any = False,
) -> dict[str, Any]:
    """Return a closed-vocabulary live-turn intent dict.

    Omitted residue defaults to ``later_review`` for backward compatibility with
    the original live ingest tool: an agent that calls ingest with only text/kind
    is still explicitly asking to record deferred salience. Callers that want the
    new anti-duplicate behavior should send ``residue='none'`` with a full or
    explicit-no-action foreground resolution.
    """

    return {
        "foreground_action_taken": _safe_bool(foreground_action_taken, False),
        "foreground_resolution": _safe_enum(foreground_resolution, FOREGROUND_RESOLUTIONS, "none"),
        "residue": _safe_enum(residue, RESIDUE_KINDS, "later_review"),
        "durable_capture": _safe_enum(durable_capture, DURABLE_CAPTURES, "none"),
        "background_action_allowed": _safe_bool(background_action_allowed, False),
    }


def build_live_ingest_receipt(
    *,
    text: str,
    kind: str,
    surface: str,
    intent: dict[str, Any],
    signal_id: str = "",
    ingested: bool = False,
    skipped_reason: str = "",
) -> dict[str, Any]:
    """Build a compact receipt for a live-turn ingest decision."""

    return {
        "id": new_id("lturn"),
        "type": "live_turn.ingest_decision",
        "ts": utc_now_iso(),
        "surface": surface or "local",
        "kind": kind or "operator_salience",
        "summary": truncate_text(text, 240),
        "foreground_action_taken": bool(intent.get("foreground_action_taken")),
        "foreground_resolution": intent.get("foreground_resolution", "none"),
        "residue": intent.get("residue", "later_review"),
        "durable_capture": intent.get("durable_capture", "none"),
        "background_action_allowed": bool(intent.get("background_action_allowed")),
        "ingested": bool(ingested),
        "signal_id": signal_id,
        "skipped_reason": skipped_reason,
    }


def should_ingest_live_residue(intent: dict[str, Any]) -> tuple[bool, str]:
    """Decide whether a live-turn ingest call should create a signal.

    The key guard is anti-duplication: when the foreground fully handled the
    issue and no residue remains, do not create a Sensorium signal merely because
    the issue was important.
    """

    residue = intent.get("residue", "later_review")
    resolution = intent.get("foreground_resolution", "none")
    durable_capture = intent.get("durable_capture", "none")
    foreground = bool(intent.get("foreground_action_taken"))

    if residue != "none":
        return True, "residue_present"
    if foreground and resolution in {"full", "explicit_no_action"}:
        return False, "foreground_owned_no_residue"
    if durable_capture != "none":
        return False, "captured_elsewhere_no_residue"
    if foreground and resolution == "partial":
        return False, "partial_foreground_without_residue"
    return False, "no_residue"


def review_turn_for_residue(
    *,
    user_text: str = "",
    assistant_text: str = "",
    tool_actions: list[str] | None = None,
    memory_written: bool = False,
    skill_updated: bool = False,
    docs_updated: bool = False,
    sensorium_ingested: bool = False,
    patch_or_artifact_written: bool = False,
    explicit_no_action: bool = False,
) -> dict[str, Any]:
    """Cheap deterministic review of whether a turn may have uncaptured residue.

    This is intentionally conservative and bounded. It is not a full-session
    ingestion mechanism and it performs no writes. A caller may use the returned
    decision to decide whether a model-backed or operator review is worthwhile.
    """

    tools = tool_actions or []
    durable_capture = any([
        memory_written,
        skill_updated,
        docs_updated,
        sensorium_ingested,
        patch_or_artifact_written,
        bool(tools),
    ])
    combined = f"{user_text}\n{assistant_text}".lower()
    salience_cues = [
        "that's wrong",
        "this matters",
        "important",
        "remember",
        "document",
        "before we start",
        "unresolved",
        "watch",
        "later",
        "residue",
        "starvation",
        "over-ingestion",
        "over ingestion",
    ]
    has_salience_cue = any(cue in combined for cue in salience_cues)

    if explicit_no_action:
        decision = "no_action"
        reason = "explicit_no_action"
    elif sensorium_ingested:
        decision = "no_action"
        reason = "sensorium_already_ingested"
    elif durable_capture and has_salience_cue:
        decision = "no_action"
        reason = "salience_captured_elsewhere"
    elif has_salience_cue:
        decision = "sensorium_residue_candidate"
        reason = "salience_cue_without_capture"
    else:
        decision = "no_action"
        reason = "no_salience_cue"

    return {
        "decision": decision,
        "reason": reason,
        "has_salience_cue": has_salience_cue,
        "durable_capture_seen": durable_capture,
        "inputs": {
            "user_chars": len(user_text or ""),
            "assistant_chars": len(assistant_text or ""),
            "tool_action_count": len(tools),
        },
    }
