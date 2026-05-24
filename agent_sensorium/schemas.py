"""Pure helpers for IDs, timestamps, validation, and normalization."""

from datetime import datetime, timezone
from uuid import uuid4

SIGNAL_REQUIRED_FIELDS = {"sensor", "source", "kind", "summary"}
VALID_SENSITIVITIES = {"local_only", "private", "public_safe"}
VALID_SOURCES = {"manual", "hermes_session", "artifact", "feedback"}
VALID_ACTORS = {"operator", "agent", "tool"}

SENSITIVITY_RANK = {"local_only": 0, "private": 1, "public_safe": 2}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def validate_signal(signal: dict) -> None:
    missing = SIGNAL_REQUIRED_FIELDS - signal.keys()
    if missing:
        raise ValueError(f"Signal missing required fields: {missing}")
    if signal.get("sensitivity") and signal["sensitivity"] not in VALID_SENSITIVITIES:
        raise ValueError(
            f"Invalid sensitivity: {signal['sensitivity']}. Must be one of {VALID_SENSITIVITIES}"
        )


def validate_event(event: dict) -> None:
    required = {"id", "ts", "type", "kind", "summary"}
    missing = required - event.keys()
    if missing:
        raise ValueError(f"Event missing required fields: {missing}")


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
