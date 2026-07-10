"""Pure helpers for IDs, timestamps, validation, and normalization."""

import re
from datetime import datetime, timezone
from uuid import uuid4

SIGNAL_REQUIRED_FIELDS = {"sensor", "source", "kind", "summary"}
VALID_SENSITIVITIES = {"local_only", "private", "public_safe"}
VALID_SOURCES = {"manual", "hermes_session", "artifact", "feedback", "machine", "memory", "kanban"}
VALID_ACTORS = {"operator", "agent", "tool"}

SENSITIVITY_RANK = {"local_only": 0, "private": 1, "public_safe": 2}

FEEDBACK_REQUIRED_FIELDS = {"caused_by", "outcome", "feedback_scope"}
VALID_FEEDBACK_SCOPES = {"thread", "candidate", "delivery", "operator_evaluation", "system_action"}

DELIVERY_ONLY_OUTCOMES = frozenset({
    "delivered", "sent", "posted", "dispatched", "queued",
})

PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
UTC_Z_CHECKPOINT_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)


def sanitize_profile_name(name: str) -> str:
    """Validate and return a safe profile (instance) name, else raise ValueError.

    Names address direct children of the Sensorium state root, never nested or
    parent paths.
    """
    candidate = (name or "").strip()
    if not candidate:
        raise ValueError("profile name must not be blank")
    if len(candidate) > 64:
        raise ValueError("profile name must be at most 64 characters")
    if candidate.startswith(".") or candidate in {".", ".."}:
        raise ValueError(f"invalid profile name: {name!r}")
    if "/" in candidate or "\\" in candidate or "\x00" in candidate:
        raise ValueError(f"invalid profile name: {name!r}")
    if not PROFILE_NAME_RE.match(candidate):
        raise ValueError(
            "profile name may only contain letters, digits, '.', '_', '-': "
            f"{name!r}"
        )
    return candidate


SUCCESS_OUTCOMES = frozenset({
    "operator_approved", "completed", "response_received", "acknowledged",
})

FAILURE_OUTCOMES = frozenset({
    "operator_rejected", "failed", "no_response", "expired_no_response",
})


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc_z_checkpoint(value: object) -> tuple[str, datetime] | None:
    """Return canonical UTC-Z text and time, rejecting every other timestamp form."""
    if not isinstance(value, str):
        return None
    text = value
    if UTC_Z_CHECKPOINT_RE.fullmatch(text) is None:
        return None
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError:
        return None
    parsed_utc = parsed.astimezone(timezone.utc)
    canonical = parsed_utc.isoformat(timespec="auto").replace("+00:00", "Z")
    return canonical, parsed_utc


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def truncate_text(text: str, max_chars: int = 120) -> str:
    """Truncate at a word boundary when possible, avoiding mid-word guillotines."""
    value = " ".join(str(text or "").split())
    if len(value) <= max_chars:
        return value
    if max_chars <= 1:
        return value[:max_chars]
    cut = value[: max_chars - 1].rstrip()
    boundary = max(cut.rfind(" "), cut.rfind("—"), cut.rfind(":"), cut.rfind(";"), cut.rfind(","))
    if boundary >= max(12, int(max_chars * 0.55)):
        cut = cut[:boundary].rstrip(" —:;,")
    return cut + "…"


def normalize_signal(raw: dict) -> dict:
    sig = dict(raw)
    if "id" not in sig:
        sig["id"] = new_id("sig")
    if "ts" not in sig:
        sig["ts"] = utc_now_iso()
    sig.setdefault("actor", "operator")
    sig.setdefault("strength_hint", 0.5)
    sig.setdefault("correlation_keys", [])
    sig.setdefault("sensitivity", "private")
    sig.setdefault("allowed_surfaces", ["local"])
    sig.setdefault("ttl_hours", 72)
    sig.setdefault("source_ref", "")
    return sig


def classify_feedback_outcome(outcome: str) -> str:
    if outcome in DELIVERY_ONLY_OUTCOMES:
        return "delivery_only"
    if outcome in SUCCESS_OUTCOMES:
        return "success"
    if outcome in FAILURE_OUTCOMES:
        return "failure"
    return "inconclusive"


def validate_signal(signal: dict) -> None:
    missing = SIGNAL_REQUIRED_FIELDS - signal.keys()
    if missing:
        raise ValueError(f"Signal missing required fields: {missing}")
    if signal.get("sensitivity") and signal["sensitivity"] not in VALID_SENSITIVITIES:
        raise ValueError(
            f"Invalid sensitivity: {signal['sensitivity']}. Must be one of {VALID_SENSITIVITIES}"
        )
    if signal.get("source") == "feedback":
        fb_missing = FEEDBACK_REQUIRED_FIELDS - signal.keys()
        if fb_missing:
            raise ValueError(f"Feedback signal missing required fields: {fb_missing}")

        caused_by = signal.get("caused_by")
        if not isinstance(caused_by, dict) or not caused_by:
            raise ValueError("Feedback signal caused_by must be a non-empty dict")

        outcome = signal.get("outcome")
        if not isinstance(outcome, str) or not outcome.strip():
            raise ValueError("Feedback signal outcome must be a non-empty string")

        scope = signal.get("feedback_scope")
        if scope is None:
            raise ValueError("Feedback signal feedback_scope must not be None")
        if isinstance(scope, str):
            scope = [scope]
        elif not isinstance(scope, list):
            raise ValueError("Feedback signal feedback_scope must be a string or list")
        if not scope:
            raise ValueError("Feedback signal feedback_scope must not be empty")
        if any(not isinstance(item, str) for item in scope):
            raise ValueError("Feedback signal feedback_scope values must be strings")
        invalid = set(scope) - VALID_FEEDBACK_SCOPES
        if invalid:
            raise ValueError(f"Invalid feedback_scope values: {invalid}")


def validate_event(event: dict) -> None:
    required = {"id", "ts", "type", "kind", "summary"}
    missing = required - event.keys()
    if missing:
        raise ValueError(f"Event missing required fields: {missing}")
    if event.get("sensitivity") and event["sensitivity"] not in VALID_SENSITIVITIES:
        raise ValueError(
            f"Invalid sensitivity: {event['sensitivity']}. Must be one of {VALID_SENSITIVITIES}"
        )


def merge_sensitivity(values: list[str]) -> str:
    if not values:
        return "private"
    best_rank = min(SENSITIVITY_RANK.get(v, 1) for v in values)
    for name, rank in SENSITIVITY_RANK.items():
        if rank == best_rank:
            return name
    return "private"


def intersect_allowed_surfaces(items: list[list[str]]) -> list[str]:
    if not items:
        return []
    result = set(items[0])
    for surfaces in items[1:]:
        result &= set(surfaces)
    return sorted(result)
