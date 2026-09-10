"""Canonical privacy-safe Sensorium Desktop presentation projection."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
_POSTURES = frozenset(
    {"quiet", "sensing", "awaiting_review", "held", "prepared", "blocked", "settled", "unavailable"}
)
_LATEST_KINDS = frozenset({"candidate", "aperture", "decision", "reachout", "runtime"})
_SAFE_STATES = frozenset({"open", "held", "prepared", "blocked", "settled", "sensing", "unknown"})
_SAFE_REASONS = frozenset(
    {
        "none",
        "active_candidate",
        "open_aperture",
        "held_aperture",
        "prepared_local_reachout",
        "blocked_lifecycle",
        "runtime_receipt",
        "settled_lifecycle",
        "malformed_state",
        "unavailable_state",
    }
)
_DIRECT_MODES = frozenset({"discord_channel_thread", "discord_dm_bound_session"})
_TERMINAL_OUTBOX = frozenset(
    {
        "dispatched",
        "delivered",
        "cancelled",
        "expired",
        "rejected",
        "settled",
        "partially_presented_foreground",
    }
)
_KNOWN_CANDIDATE_STATUSES = frozenset(
    {
        "archived",
        "candidate",
        "held",
        "in_conscious_aperture",
        "reviewed",
        "suppressed",
        "cancelled",
        "prepared_external_work",
        "blocked",
        "error",
    }
)
_KNOWN_OUTBOX_STATUSES = frozenset(
    {
        "prepared",
        "failed",
        "dispatched",
        "delivered",
        "cancelled",
        "expired",
        "rejected",
        "settled",
        "partially_presented_foreground",
    }
)
MAX_CANDIDATES_BYTES = 8 * 1024 * 1024
MAX_CANDIDATES_ROWS = 10_000
MAX_OUTBOX_BYTES = 1 * 1024 * 1024
MAX_OUTBOX_ROWS = 2_000
MAX_DECISIONS_TAIL_BYTES = 1 * 1024 * 1024
MAX_DECISIONS_TAIL_ROWS = 512
MAX_SIGNALS_TAIL_BYTES = 1 * 1024 * 1024
MAX_SIGNALS_TAIL_ROWS = 512
MAX_BLOCKERS_TAIL_BYTES = 512 * 1024
MAX_BLOCKERS_TAIL_ROWS = 256
MAX_CLOCK_BYTES = 64 * 1024
_STATE_FILES = (
    "candidates.jsonl",
    "outbox.jsonl",
    "decisions.jsonl",
    "signals/inbox.jsonl",
    "inner_life/blockers.jsonl",
    "last_native_clock.json",
    "last_conscious_clock.json",
)
_RECENT_SECONDS = 300
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
_VALID_SURFACES = frozenset({"the-table", "sensorium-dashboard"})
_TERMINAL_DECISION_TYPES = frozenset({"conscious.aperture.settled", "conscious_reachout.decision"})
_TERMINAL_CANDIDATE_STATUSES = frozenset(
    {"reviewed", "archived", "suppressed", "cancelled", "prepared_external_work"}
)


def _iso(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(
            timezone.utc
        )
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(
            timezone.utc
        )
    except (TypeError, ValueError):
        return None


def _rfc3339(value: Any) -> str | None:
    parsed = _iso(value)
    return parsed.isoformat(timespec="seconds").replace("+00:00", "Z") if parsed else None


def _opaque(kind: str, value: Any) -> str | None:
    if value is None or value == "":
        return None
    digest = hashlib.sha256(
        (f"sensorium.desktop.presentation.v1\0{kind}\0{value}").encode()
    ).hexdigest()[:16]
    return f"{kind}#{digest}"


def _artifact() -> dict[str, Any]:
    return {
        "available": False,
        "verified": False,
        "content_hash": None,
        "content_length": 0,
        "allowed_surface": None,
    }


def _latest(
    *,
    kind: str,
    state: str,
    reason: str,
    identifier: Any,
    timestamp: Any,
    artifact: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind if kind in _LATEST_KINDS else None,
        "opaque_ref": _opaque(kind, identifier) if kind in _LATEST_KINDS and identifier else None,
        "state": state if state in _SAFE_STATES else "unknown",
        "reason_code": reason if reason in _SAFE_REASONS else "malformed_state",
        "updated_at": _rfc3339(timestamp),
        "artifact": artifact if isinstance(artifact, dict) else _artifact(),
    }


def _envelope(
    *,
    instance: str,
    profile: str,
    surface: str,
    generated: str,
    posture: str,
    headline: str,
    detail: str,
    freshness: dict[str, Any],
    counts: dict[str, int],
    latest: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": posture != "unavailable",
        "instance": instance,
        "profile": profile,
        "surface": surface,
        "policy": {
            "read_only": True,
            "content_included": False,
            "may_prepare": False,
            "may_deliver": False,
            "may_create_work": False,
        },
        "generated_at": generated,
        "freshness": freshness,
        "posture": posture,
        "headline_code": headline,
        "detail_code": detail,
        "counts": counts,
        "latest": latest,
        "links": {"dashboard_path": "/sensorium"},
    }


def _unavailable(
    *, instance: str, profile: str, surface: str, generated: str, reason: str = "unavailable_state"
) -> dict[str, Any]:
    reason = reason if reason in {"malformed_state", "unavailable_state"} else "unavailable_state"
    return _envelope(
        instance=instance,
        profile=profile,
        surface=surface,
        generated=generated,
        posture="unavailable",
        headline="unavailable_state",
        detail=reason,
        freshness={
            "state": "unavailable",
            "observed_at": None,
            "age_seconds": 0,
            "reason_code": reason,
        },
        counts={
            "unresolved_candidates": 0,
            "open_apertures": 0,
            "held_apertures": 0,
            "verified_prepared_reachouts": 0,
            "blocked_items": 0,
        },
        latest=_latest(
            kind="runtime",
            state="unknown",
            reason="unavailable_state",
            identifier=None,
            timestamp=None,
        ),
    )


def _read_jsonl(
    path: Path, *, max_bytes: int, max_rows: int, metrics: dict[str, int] | None = None
) -> tuple[list[dict[str, Any]], bool]:
    if not path.exists():
        return [], True
    try:
        if path.stat().st_size > max_bytes:
            return [], False
        with path.open("rb") as stream:
            data = stream.read(max_bytes + 1)
        if metrics is not None:
            metrics[str(path)] = len(data)
        if len(data) > max_bytes:
            return [], False
        rows = []
        for line in data.decode("utf-8").splitlines():
            if line.strip():
                item = json.loads(line)
                if not isinstance(item, dict):
                    return [], False
                rows.append(item)
                if len(rows) > max_rows:
                    return [], False
        return rows, True
    except (OSError, UnicodeError, json.JSONDecodeError):
        return [], False


def _read_tail_jsonl(
    path: Path, *, max_bytes: int, max_rows: int, metrics: dict[str, int] | None = None
) -> tuple[list[dict[str, Any]], bool]:
    if not path.exists():
        return [], True
    try:
        size = path.stat().st_size
        start = max(0, size - max_bytes)
        with path.open("rb") as stream:
            stream.seek(start)
            data = stream.read(max_bytes)
        if metrics is not None:
            metrics[str(path)] = len(data)
        if start:
            _, separator, data = data.partition(b"\n")
            if not separator:
                return [], True
        if data and not data.endswith(b"\n"):
            data = data.rsplit(b"\n", 1)[0] + (b"\n" if b"\n" in data else b"")
        rows = []
        for line in data.decode("utf-8").splitlines():
            if line.strip():
                item = json.loads(line)
                if not isinstance(item, dict):
                    return [], False
                rows.append(item)
        return rows[-max_rows:], True
    except (OSError, UnicodeError, json.JSONDecodeError):
        return [], False


def _read_clock(
    path: Path, *, metrics: dict[str, int] | None = None
) -> tuple[list[dict[str, Any]], bool]:
    if not path.exists():
        return [], True
    try:
        if path.stat().st_size > MAX_CLOCK_BYTES:
            return [], False
        with path.open("rb") as stream:
            data = stream.read(MAX_CLOCK_BYTES + 1)
        if metrics is not None:
            metrics[str(path)] = len(data)
        if len(data) > MAX_CLOCK_BYTES:
            return [], False
        value = json.loads(data.decode("utf-8"))
        return ([value], True) if isinstance(value, dict) else ([], False)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return [], False


def _read_state(
    root: Path, *, metrics: dict[str, int] | None = None
) -> tuple[dict[str, list[dict[str, Any]]], bool]:
    paths = {
        "candidates": root / "candidates.jsonl",
        "decisions": root / "decisions.jsonl",
        "outbox": root / "outbox.jsonl",
        "signals": root / "signals/inbox.jsonl",
        "blockers": root / "inner_life/blockers.jsonl",
    }
    specs = {
        "candidates": (MAX_CANDIDATES_BYTES, MAX_CANDIDATES_ROWS),
        "outbox": (MAX_OUTBOX_BYTES, MAX_OUTBOX_ROWS),
        "decisions": (MAX_DECISIONS_TAIL_BYTES, MAX_DECISIONS_TAIL_ROWS),
        "signals": (MAX_SIGNALS_TAIL_BYTES, MAX_SIGNALS_TAIL_ROWS),
        "blockers": (MAX_BLOCKERS_TAIL_BYTES, MAX_BLOCKERS_TAIL_ROWS),
    }
    result: dict[str, list[dict[str, Any]]] = {}
    valid = True
    for name, path in paths.items():
        byte_cap, row_cap = specs[name]
        reader = _read_jsonl if name in {"candidates", "outbox"} else _read_tail_jsonl
        result[name], current = reader(path, max_bytes=byte_cap, max_rows=row_cap, metrics=metrics)
        valid &= current
    for name in ("last_native_clock", "last_conscious_clock"):
        result[name], current = _read_clock(root / f"{name}.json", metrics=metrics)
        valid &= current
    return result, valid


def _schema_is_known(rows: dict[str, list[dict[str, Any]]]) -> bool:
    return all(
        "status" in row and str(row.get("status") or "") in allowed
        for key, allowed in (
            ("candidates", _KNOWN_CANDIDATE_STATUSES),
            ("outbox", _KNOWN_OUTBOX_STATUSES),
        )
        for row in rows[key]
    )


def _content_length(row: dict[str, Any]) -> int | None:
    preview = row.get("message_preview")
    content_hash = str(row.get("content_hash") or "").lower()
    if not isinstance(preview, str) or not preview or len(content_hash) not in {16, 64}:
        return None
    digest = hashlib.sha256(preview.encode()).hexdigest()
    if content_hash != (digest if len(content_hash) == 64 else digest[:16]):
        return None
    for key in ("content_length", "message_chars"):
        if key in row:
            try:
                if int(row[key]) != len(preview):
                    return None
            except (TypeError, ValueError):
                return None
    return len(preview)


def _verified_reachout(row: dict[str, Any]) -> bool:
    if row.get("status") != "prepared":
        return False
    if row.get("surface") != "local" or row.get("delivery_mode") != "context_pointer":
        return False
    if "origin_thread_id" not in row or row["origin_thread_id"] != "":
        return False
    if not isinstance(row.get("origin_candidate_id"), str) or not row["origin_candidate_id"]:
        return False
    if "target" not in row or row["target"] != {}:
        return False
    if row.get("allowed_surfaces") != ["local"]:
        return False
    content_hash = str(row.get("content_hash") or "").lower()
    if (
        len(content_hash) not in {16, 64}
        or any(ch not in "0123456789abcdef" for ch in content_hash)
        or _content_length(row) is None
    ):
        return False
    if str(row.get("delivery_state") or "").lower() in _TERMINAL_OUTBOX:
        return False
    return True


def _row_time(row: dict[str, Any]) -> datetime | None:
    for key in ("ts", "finished_at", "updated_at", "created_at", "opened_at"):
        parsed = _iso(row.get(key))
        if parsed:
            return parsed
    return None


def _identity(instance: Any, profile: Any, surface: Any) -> tuple[str, str, str, bool]:
    valid = isinstance(instance, str) and bool(_SAFE_NAME.fullmatch(instance))
    valid &= isinstance(profile, str) and bool(_SAFE_NAME.fullmatch(profile))
    valid &= isinstance(surface, str) and surface in _VALID_SURFACES
    return (
        instance if valid else "default",
        profile if valid else "default",
        surface if valid else "the-table",
        valid,
    )


def _settled_receipt(row: dict[str, Any]) -> bool:
    kind = str(row.get("type") or "")
    if kind in _TERMINAL_DECISION_TYPES:
        return True
    return (
        kind == "candidate.updated"
        and str(row.get("new_status") or "") in _TERMINAL_CANDIDATE_STATUSES
    )


def project_desktop_presentation(
    root: str | Path,
    *,
    instance: str = "default",
    profile: str = "default",
    surface: str = "the-table",
    now: str | None = None,
) -> dict[str, Any]:
    instance, profile, surface, identity_valid = _identity(instance, profile, surface)
    root_path = Path(root)
    generated = _rfc3339(now) or datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )
    if not identity_valid:
        return _unavailable(
            instance=instance,
            profile=profile,
            surface=surface,
            generated=generated,
            reason="malformed_state",
        )
    if not root_path.exists() or not root_path.is_dir():
        return _unavailable(
            instance=instance, profile=profile, surface=surface, generated=generated
        )
    try:
        rows, valid = _read_state(root_path)
    except (OSError, ValueError):
        return _unavailable(
            instance=instance, profile=profile, surface=surface, generated=generated
        )
    if not valid or not _schema_is_known(rows):
        return _unavailable(
            instance=instance,
            profile=profile,
            surface=surface,
            generated=generated,
            reason="malformed_state",
        )
    now_dt = _iso(generated) or datetime.now(timezone.utc)

    def recent(row):
        return (stamp := _row_time(row)) is not None and 0 <= (
            now_dt - stamp
        ).total_seconds() <= _RECENT_SECONDS

    candidates = rows["candidates"]
    prepared = [x for x in rows["outbox"] if _verified_reachout(x)]
    open_apertures = [
        x
        for x in candidates
        if x.get("status") == "in_conscious_aperture"
        and isinstance(x.get("conscious_aperture"), dict)
        and x["conscious_aperture"].get("state", "open") == "open"
    ]
    held = [
        x
        for x in candidates
        if x.get("status") == "held" and x.get("kind") == "subconscious_advisory"
    ]
    review = [
        x
        for x in candidates
        if x.get("status") == "candidate" and isinstance(x.get("conscious_task"), dict)
    ]
    blocked = [
        x for x in candidates if str(x.get("status") or "").lower() in {"blocked", "error"}
    ] + [x for x in rows["blockers"] if x.get("blocked") is True and recent(x)]
    unresolved = [x for x in candidates if x.get("status") == "candidate"]
    posture, headline, detail = "quiet", "none", "none"
    latest = _latest(
        kind="runtime", state="unknown", reason="none", identifier=None, timestamp=None
    )
    if blocked:
        item = max(blocked, key=lambda x: _row_time(x) or datetime.min.replace(tzinfo=timezone.utc))
        latest = _latest(
            kind="decision",
            state="blocked",
            reason="blocked_lifecycle",
            identifier=item.get("id") or item.get("type"),
            timestamp=_row_time(item),
        )
        posture, headline, detail = "blocked", "blocked_lifecycle", "blocked_lifecycle"
    elif prepared:
        item = max(
            prepared, key=lambda x: _row_time(x) or datetime.min.replace(tzinfo=timezone.utc)
        )
        length = _content_length(item) or 0
        artifact = {
            "available": True,
            "verified": True,
            "content_hash": str(item.get("content_hash")),
            "content_length": length,
            "allowed_surface": "local",
        }
        latest = _latest(
            kind="reachout",
            state="prepared",
            reason="prepared_local_reachout",
            identifier=item.get("id"),
            timestamp=_row_time(item),
            artifact=artifact,
        )
        posture, headline, detail = "prepared", "prepared_local_reachout", "prepared_local_reachout"
    elif held:
        item = max(held, key=lambda x: _row_time(x) or datetime.min.replace(tzinfo=timezone.utc))
        aperture = item.get("conscious_aperture") or {}
        latest = _latest(
            kind="aperture",
            state="held",
            reason="held_aperture",
            identifier=aperture.get("id") or item.get("id"),
            timestamp=aperture.get("settled_at") or _row_time(item),
        )
        posture, headline, detail = "held", "held_aperture", "held_aperture"
    elif open_apertures:
        item = max(
            open_apertures, key=lambda x: _row_time(x) or datetime.min.replace(tzinfo=timezone.utc)
        )
        aperture = item.get("conscious_aperture") or {}
        latest = _latest(
            kind="aperture",
            state="open",
            reason="open_aperture",
            identifier=aperture.get("id") or item.get("id"),
            timestamp=aperture.get("opened_at") or _row_time(item),
        )
        posture, headline, detail = "awaiting_review", "open_aperture", "open_aperture"
    elif review:
        item = max(review, key=lambda x: _row_time(x) or datetime.min.replace(tzinfo=timezone.utc))
        latest = _latest(
            kind="candidate",
            state="open",
            reason="active_candidate",
            identifier=item.get("id"),
            timestamp=_row_time(item),
        )
        posture, headline, detail = "awaiting_review", "active_candidate", "active_candidate"
    else:
        runtime = [
            x
            for x in rows["signals"] + rows["last_native_clock"] + rows["last_conscious_clock"]
            if recent(x)
        ]
        if runtime:
            item = max(
                runtime, key=lambda x: _row_time(x) or datetime.min.replace(tzinfo=timezone.utc)
            )
            latest = _latest(
                kind="runtime",
                state="sensing",
                reason="runtime_receipt",
                identifier=item.get("sensor") or item.get("action") or item.get("id") or "clock",
                timestamp=_row_time(item),
            )
            posture, headline, detail = "sensing", "runtime_receipt", "runtime_receipt"
        else:
            terminal = [x for x in rows["decisions"] if recent(x) and _settled_receipt(x)]
            if terminal:
                item = max(
                    terminal,
                    key=lambda x: _row_time(x) or datetime.min.replace(tzinfo=timezone.utc),
                )
                latest = _latest(
                    kind="decision",
                    state="settled",
                    reason="settled_lifecycle",
                    identifier=item.get("type") or item.get("id"),
                    timestamp=_row_time(item),
                )
                posture, headline, detail = "settled", "settled_lifecycle", "settled_lifecycle"
    try:
        owned_paths = []
        for relative in _STATE_FILES:
            path = root_path / relative
            try:
                if path.stat().st_mode:
                    owned_paths.append(path)
            except FileNotFoundError:
                continue
        observed_dt = (
            datetime.fromtimestamp(max(path.stat().st_mtime for path in owned_paths), timezone.utc)
            if owned_paths
            else None
        )
    except (OSError, ValueError, OverflowError):
        return _unavailable(
            instance=instance, profile=profile, surface=surface, generated=generated
        )
    # A directory by itself is not an initialized Sensorium state root.  Do
    # not let the default quiet posture mask the absence of any owned state.
    if observed_dt is None:
        return _unavailable(
            instance=instance, profile=profile, surface=surface, generated=generated
        )
    age = max(0, int((now_dt - observed_dt).total_seconds()))
    freshness_state = (
        "unavailable"
        if observed_dt is None
        else ("fresh" if age <= 90 else "aging" if age <= 300 else "stale")
    )
    freshness = {
        "state": freshness_state,
        "observed_at": observed_dt.isoformat(timespec="seconds").replace("+00:00", "Z")
        if observed_dt
        else None,
        "age_seconds": age,
        "reason_code": "runtime_receipt" if observed_dt else "unavailable_state",
    }
    return _envelope(
        instance=instance,
        profile=profile,
        surface=surface,
        generated=generated,
        posture=posture,
        headline=headline,
        detail=detail,
        freshness=freshness,
        counts={
            "unresolved_candidates": len(unresolved),
            "open_apertures": len(open_apertures),
            "held_apertures": len(held),
            "verified_prepared_reachouts": len(prepared),
            "blocked_items": len(blocked),
        },
        latest=latest,
    )


def project_instance_desktop_presentation(
    *,
    instance: str = "default",
    profile: str = "default",
    surface: str = "the-table",
    state_dir: str | Path | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Resolve an instance root without creating directories, then project it."""
    safe_instance, safe_profile, safe_surface, valid = _identity(instance, profile, surface)
    generated = _rfc3339(now) or datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )
    if not valid:
        return _unavailable(
            instance=safe_instance,
            profile=safe_profile,
            surface=safe_surface,
            generated=generated,
            reason="malformed_state",
        )
    from .store import SensoriumStore

    root = SensoriumStore(
        instance=safe_instance, state_dir=str(state_dir) if state_dir is not None else None
    ).root
    return project_desktop_presentation(
        root, instance=safe_instance, profile=safe_profile, surface=safe_surface, now=now
    )
