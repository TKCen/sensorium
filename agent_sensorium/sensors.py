"""Deterministic compact sensor helpers — stdlib-only, no model calls.

Each helper returns a compact signal dict suitable for sensorium_ingest_signal.
Signals contain only metadata, summaries, refs, hashes, and sizes — never raw
file contents, full transcripts, or unbounded data.
"""

import hashlib

from .schemas import VALID_SENSITIVITIES, truncate_text

MAX_SUMMARY_CHARS = 200
MAX_REF_CHARS = 256


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        numeric = 0.5
    return max(lo, min(hi, numeric))


def _safe_list(values: list[str] | None, *, default: list[str], max_chars: int = MAX_REF_CHARS) -> list[str]:
    if not values:
        return list(default)
    sanitized = [truncate_text(v.strip(), max_chars) for v in values if isinstance(v, str) and v.strip()]
    return sanitized or list(default)


def _safe_sensitivity(value: str) -> str:
    return value if value in VALID_SENSITIVITIES else "private"


def session_event_signal(
    *,
    kind: str,
    summary: str,
    session_ref: str = "",
    strength_hint: float = 0.5,
    sensitivity: str = "private",
    allowed_surfaces: list[str] | None = None,
    correlation_keys: list[str] | None = None,
) -> dict:
    return {
        "sensor": "sensorium.session_event",
        "source": "hermes_session",
        "kind": kind,
        "summary": truncate_text(summary, MAX_SUMMARY_CHARS),
        "session_ref": truncate_text(session_ref, MAX_REF_CHARS),
        "strength_hint": _clamp(strength_hint),
        "sensitivity": _safe_sensitivity(sensitivity),
        "allowed_surfaces": _safe_list(allowed_surfaces, default=["local"]),
        "correlation_keys": _safe_list(correlation_keys, default=[]),
    }


def artifact_signal(
    *,
    path: str,
    summary: str,
    size: int | None = None,
    content_hash: str = "",
    artifact_ref: str = "",
    kind: str = "artifact_created",
    strength_hint: float = 0.6,
    sensitivity: str = "private",
    allowed_surfaces: list[str] | None = None,
    correlation_keys: list[str] | None = None,
) -> dict:
    return {
        "sensor": "sensorium.artifact",
        "source": "artifact",
        "kind": kind,
        "summary": truncate_text(summary, MAX_SUMMARY_CHARS),
        "path": truncate_text(path, MAX_REF_CHARS),
        "artifact_ref": truncate_text(artifact_ref, MAX_REF_CHARS),
        "size": size,
        "content_hash": content_hash[:64] if content_hash else "",
        "strength_hint": _clamp(strength_hint),
        "sensitivity": _safe_sensitivity(sensitivity),
        "allowed_surfaces": _safe_list(allowed_surfaces, default=["local"]),
        "correlation_keys": _safe_list(correlation_keys, default=[]),
    }


def operator_signal(
    *,
    summary: str,
    kind: str = "user_correction",
    strength_hint: float = 0.8,
    sensitivity: str = "private",
    allowed_surfaces: list[str] | None = None,
    correlation_keys: list[str] | None = None,
    source_ref: str = "",
) -> dict:
    return {
        "sensor": "sensorium.explicit_operator",
        "source": "manual",
        "kind": kind,
        "summary": truncate_text(summary, MAX_SUMMARY_CHARS),
        "actor": "operator",
        "strength_hint": _clamp(strength_hint),
        "sensitivity": _safe_sensitivity(sensitivity),
        "allowed_surfaces": _safe_list(allowed_surfaces, default=["local"]),
        "correlation_keys": _safe_list(correlation_keys, default=[]),
        "source_ref": truncate_text(source_ref, MAX_REF_CHARS),
    }


def file_content_hash(path: str) -> str:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()
    except (OSError, IOError):
        return ""
