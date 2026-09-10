"""Detached, fail-closed prospective-evidence recorder.

Canonical owners call :func:`observe_after_success` only after their own write and
ignore its result.  This module never reads canonical state and never feeds a
Sensorium surface: it persists a deliberately small, opaque study-local ledger.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import shutil
import sqlite3
import tempfile
from functools import lru_cache
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA = "sensorium.prospective_evidence.v0"
STUDY_NAME = "prospective-evidence-capture-v0"
MAX_ROWS, MAX_BYTES, DAYS = 5000, 5 * 1024 * 1024, 14
SOURCE_CLASSES = frozenset(
    {"manual", "hermes_session", "artifact", "feedback", "machine", "memory", "kanban", "unknown"}
)
ATTENTION_CLASSES = frozenset(
    {
        "relational",
        "embodied",
        "creative",
        "mnemonic_identity",
        "operational",
        "external",
        "unknown",
    }
)
STAGES = frozenset(
    {
        "source_observed",
        "candidate_updated",
        "presented",
        "opened",
        "chosen",
        "settled",
        "correction",
        "contradiction",
        "retraction",
        "attention_snapshot",
    }
)
CHANGE_STATES = frozenset({"new", "update", "correction", "retraction", "no_change", "unknown"})
SETTLEMENTS = frozenset({"chosen", "settled", "held", "dropped", "unknown"})
VERDICTS = frozenset({"SUFFICIENT_PRIVATE_EVIDENCE", "INSUFFICIENT_PRIVATE_EVIDENCE"})
# Raw ids are accepted only to derive opaque HMAC references, never persisted.
ALLOWED_KEYS = frozenset(
    {
        "source_receipt",
        "source_id",
        "candidate_id",
        "event_id",
        "case_id",
        "source_class",
        "change_state",
        "settlement",
        "source_owner_transition",
        "explicit_material_change_receipt",
        "explicit_no_effect_evidence",
        "shared_upstream_ref",
        "source_family_ref",
        "relation_evidence",
        "attention_classes",
        "same_window_external_protected",
        "contradiction_evidence",
        "retraction_evidence",
    }
)
FORBIDDEN_KEYS = frozenset(
    {
        "raw_text",
        "text",
        "summary",
        "body",
        "excerpt",
        "name",
        "url",
        "path",
        "credential",
        "token",
        "snowflake",
        "session_id",
        "message_id",
        "thread_id",
        "precise_time",
        "error",
        "status",
        "control",
        "config",
        "frozen",
        "outcome",
        "materiality",
        "no_effect",
        "dependence",
        "cross_domain",
        "attention_mode",
    }
)

# These maps are deliberately closed.  They classify structured owner fields,
# never a candidate title, summary, correlation key, or any other prose.
CORRECTION_STAGE_BY_SIGNAL_KIND = {
    "explicit_correction": "correction",
    "correction": "correction",
    "retraction": "retraction",
    "user_correction": "correction",
}
CANDIDATE_KIND_TO_ATTENTION_CLASS = {
    "relational_salience": "relational",
    "embodiment_insight": "embodied",
    "creative_pull": "creative",
    "mnemonic": "mnemonic_identity",
    "memory": "mnemonic_identity",
    "identity": "mnemonic_identity",
    "identity_continuity": "mnemonic_identity",
    "design_insight": "operational",
    "durable_importance": "operational",
    "explicit_correction": "operational",
    "correction": "operational",
    "retraction": "operational",
    "user_correction": "operational",
    "external_evidence": "external",
    "external_reference": "external",
    "external_observation": "external",
}
PROTECTED_NONEXTERNAL_ATTENTION_CLASSES = frozenset(
    {"relational", "embodied", "creative", "mnemonic_identity", "operational"}
)
CANDIDATE_KIND_TO_DOMAIN = {
    "subconscious_advisory": "operational",
    "task_result": "operational",
    "design_insight": "operational",
    "durable_importance": "operational",
    "explicit_correction": "operational",
    "correction": "operational",
    "retraction": "operational",
    "user_correction": "operational",
    "external_evidence": "external",
    "external_reference": "external",
    "external_observation": "external",
    "creative_pull": "creative",
    "relational_salience": "relational",
    "embodiment_insight": "embodied",
    "mnemonic": "mnemonic_identity",
    "memory": "mnemonic_identity",
    "identity": "mnemonic_identity",
    "identity_continuity": "mnemonic_identity",
}
SUCCESS_FEEDBACK_OUTCOMES = frozenset(
    {"operator_approved", "completed", "response_received", "acknowledged"}
)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except ValueError:
        return None


def _now(value: str | None = None) -> datetime:
    return _parse_time(value) or datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".prospective-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def _closed(value: Any, allowed: frozenset[str], default: str = "unknown") -> str:
    return value if isinstance(value, str) and value in allowed else default


def source_class(value: Any) -> str:
    """Exact structured owner mapping only; no text/status/promotion inference."""
    return _closed(value, SOURCE_CLASSES)


def attention_classes(value: Any) -> list[str]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        return ["unknown"]
    classes = sorted(set(value).intersection(ATTENTION_CLASSES))
    return classes or ["unknown"]


def correction_stage_for_signal_kind(value: Any) -> str | None:
    """Return a correction stage only for an exact structured signal kind."""
    return CORRECTION_STAGE_BY_SIGNAL_KIND.get(value) if isinstance(value, str) else None


def attention_class_for_candidate_kind(value: Any) -> str:
    """Classify one exact candidate kind; unsupported values stay unknown."""
    return (
        CANDIDATE_KIND_TO_ATTENTION_CLASS.get(value, "unknown")
        if isinstance(value, str)
        else "unknown"
    )


def attention_snapshot_evidence(selected: list[dict], transaction_candidates: list[dict]) -> dict:
    """Build bounded class-only evidence for one successful Conscious open.

    The candidate rows are supplied by the aperture owner from its already-read
    transaction snapshot.  Advisory rows may reference source candidates in that
    same snapshot; this is an exact id join, not a search over text or state.
    """
    by_id = {
        str(row.get("id")): row
        for row in transaction_candidates
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    rows: list[dict] = []
    for candidate in selected:
        if not isinstance(candidate, dict):
            continue
        rows.append(candidate)
        for source_id in candidate.get("source_candidate_ids") or []:
            if isinstance(source_id, str) and source_id in by_id:
                rows.append(by_id[source_id])
    classes = sorted({attention_class_for_candidate_kind(row.get("kind")) for row in rows}) or [
        "unknown"
    ]
    known = set(classes)
    mixed = "external" in known and bool(
        known.intersection(PROTECTED_NONEXTERNAL_ATTENTION_CLASSES)
    )
    return {"attention_classes": classes, "same_window_external_protected": mixed}


def source_ingest_evidence(signal: dict, candidates: list[dict], *, inhibited: bool) -> dict:
    """Closed projection of accepted structured receipts; no raw join survives."""
    result = {
        "source_receipt": signal.get("id", ""),
        "source_class": signal.get("source", "unknown"),
        "change_state": "no_change" if inhibited else "new",
    }
    if isinstance(signal.get("transition"), str) and signal["transition"]:
        result["source_owner_transition"] = True
    if signal.get("liveness") is True:
        result["explicit_no_effect_evidence"] = True
    caused = signal.get("caused_by")
    origin_id = caused.get("origin_candidate_id") if isinstance(caused, dict) else None
    origin = next(
        (row for row in candidates if isinstance(row, dict) and row.get("id") == origin_id), None
    )
    domain = CANDIDATE_KIND_TO_DOMAIN.get(origin.get("kind")) if origin else None
    action_or_worker = isinstance(caused, dict) and any(
        isinstance(caused.get(key), str) and caused[key]
        for key in ("action_id", "worker_request_id")
    )
    if (
        signal.get("source") == "feedback"
        and isinstance(origin_id, str)
        and origin_id
        and signal.get("feedback_scope") == "system_action"
        and action_or_worker
        and domain
    ):
        result["shared_upstream_ref"] = True
        if signal.get("outcome") in SUCCESS_FEEDBACK_OUTCOMES and domain != "operational":
            result["relation_evidence"] = True
    return result


class ProspectiveEvidenceCapture:
    def __init__(self, profile_root: str | Path, config: dict | None = None):
        self.profile_root = Path(profile_root)
        self.config = dict(config or {})
        self.study_root = self.profile_root / "studies" / STUDY_NAME
        # This latch is intentionally outside ordinary study-payload purge.
        self.latch_path = self.profile_root / ".prospective-evidence-capture-v0.consumed.json"
        self.control_path = self.study_root / "control.json"
        self.db_path = self.study_root / "ledger.sqlite3"
        self.key_path = self.study_root / "revocation.key"
        self.generator_path = self.study_root / "generator-input.json"
        self.gold_path = self.study_root / "sealed-gold-labels.json"
        self.manifest_path = self.study_root / "evaluation-manifest.json"
        self.tombstone_path = self.study_root / "tombstones.json"

    @staticmethod
    def _project_latch(value: Any) -> dict | None:
        if (
            not isinstance(value, dict)
            or set(value) != {"schema", "window", "state"}
            or value.get("schema") != SCHEMA
            or value.get("state") not in {"active", "closed"}
        ):
            return None
        window = value.get("window")
        return (
            dict(value)
            if isinstance(window, str)
            and len(window) == 64
            and all(ch in "0123456789abcdef" for ch in window)
            else None
        )

    @staticmethod
    def _project_control(value: Any) -> dict | None:
        if not isinstance(value, dict) or value.get("schema") != SCHEMA:
            return None
        active = {"schema", "state", "start_at", "expires_at"}
        closed = {
            "schema",
            "state",
            "closed_at",
            "frozen_expires_at",
            "tombstones_expires_at",
            "verdict",
        }
        if value.get("state") == "active" and set(value) == active:
            start, expiry = _parse_time(value.get("start_at")), _parse_time(value.get("expires_at"))
            if start and expiry and expiry == start + timedelta(days=DAYS):
                return {key: value[key] for key in active}
        if (
            value.get("state") == "closed"
            and set(value) == closed
            and isinstance(value.get("verdict"), str)
            and value["verdict"] in VERDICTS
        ):
            closed_at, frozen_at, tomb_at = (
                _parse_time(value.get("closed_at")),
                _parse_time(value.get("frozen_expires_at")),
                _parse_time(value.get("tombstones_expires_at")),
            )
            # Closed control is retention authority, not merely a timestamp
            # ordering hint.  Require the exact canonical representation and
            # contractual +30/+90 UTC deadlines before trusting it anywhere.
            if (
                closed_at
                and frozen_at
                and tomb_at
                and all(
                    _iso(parsed) == value[field]
                    for parsed, field in (
                        (closed_at, "closed_at"),
                        (frozen_at, "frozen_expires_at"),
                        (tomb_at, "tombstones_expires_at"),
                    )
                )
                and frozen_at == closed_at + timedelta(days=30)
                and tomb_at == closed_at + timedelta(days=90)
            ):
                return {key: value[key] for key in closed}
        return None

    def _control(self) -> dict | None:
        try:
            return self._project_control(json.loads(self.control_path.read_text()))
        except (OSError, json.JSONDecodeError):
            return None

    def _latch(self) -> dict | None:
        try:
            return self._project_latch(json.loads(self.latch_path.read_text()))
        except (OSError, json.JSONDecodeError):
            return None

    def _window(self) -> tuple[datetime, datetime] | None:
        start, expiry = (
            _parse_time(self.config.get("start_at")),
            _parse_time(self.config.get("expires_at")),
        )
        if (
            self.config.get("enabled") is True
            and start
            and expiry
            and expiry == start + timedelta(days=DAYS)
        ):
            return start, expiry
        return None

    @staticmethod
    def _digest(start: datetime, expiry: datetime) -> str:
        return hashlib.sha256((SCHEMA + _iso(start) + _iso(expiry)).encode()).hexdigest()

    def activate(self) -> bool:
        """Consume one configured window. A failed setup still remains consumed."""
        window = self._window()
        if not window or self._latch() is not None:
            return False
        self.profile_root.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.latch_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError:
            return False
        start, expiry = window
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    {"schema": SCHEMA, "window": self._digest(start, expiry), "state": "active"},
                    handle,
                    sort_keys=True,
                )
                handle.flush()
                os.fsync(handle.fileno())
            self.study_root.mkdir(parents=True, exist_ok=True)
            key_fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(key_fd, "w", encoding="utf-8") as handle:
                handle.write(secrets.token_hex(32))
                handle.flush()
                os.fsync(handle.fileno())
            _atomic_json(
                self.control_path,
                {
                    "schema": SCHEMA,
                    "state": "active",
                    "start_at": _iso(start),
                    "expires_at": _iso(expiry),
                },
            )
            self._connect().close()
            return True
        except Exception:
            return False

    def _enabled_window(self, current: datetime) -> bool:
        window, control, latch = self._window(), self._control(), self._latch()
        return bool(
            window
            and latch
            and control
            and latch.get("state") == "active"
            and latch.get("window") == self._digest(*window)
            and control
            == {
                "schema": SCHEMA,
                "state": "active",
                "start_at": _iso(window[0]),
                "expires_at": _iso(window[1]),
            }
            and window[0] <= current < window[1]
            and self.key_path.exists()
            and self.db_path.exists()
        )

    def _key(self) -> bytes | None:
        try:
            key = bytes.fromhex(self.key_path.read_text().strip())
            return key if len(key) == 32 else None
        except (OSError, ValueError):
            return None

    @staticmethod
    def _opaque(key: bytes, raw: Any, prefix: str) -> str:
        digest = hmac.new(key, str(raw or "").encode(), hashlib.sha256).hexdigest()[:24]
        return f"{prefix}_{digest}"

    def _connect(self) -> sqlite3.Connection:
        self.study_root.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS observations (sequence INTEGER PRIMARY KEY, case_ref TEXT NOT NULL, source_ref TEXT NOT NULL, payload TEXT NOT NULL, bytes INTEGER NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS source_cases (source_ref TEXT PRIMARY KEY, case_ref TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS entity_cases (entity_ref TEXT PRIMARY KEY, case_ref TEXT NOT NULL)"
        )
        connection.execute("CREATE TABLE IF NOT EXISTS blocked (source_ref TEXT PRIMARY KEY)")
        return connection

    @staticmethod
    def _valid_evidence(evidence: Any) -> bool:
        if (
            not isinstance(evidence, dict)
            or not set(evidence).issubset(ALLOWED_KEYS)
            or set(evidence).intersection(FORBIDDEN_KEYS)
        ):
            return False
        for key, value in evidence.items():
            if key == "attention_classes":
                if (
                    not isinstance(value, list)
                    or not value
                    or len(value) > len(ATTENTION_CLASSES)
                    or not all(
                        isinstance(item, str) and item in ATTENTION_CLASSES for item in value
                    )
                ):
                    return False
            elif isinstance(value, (dict, list, tuple, set, bytes)):
                return False
            elif isinstance(value, str):
                # Opaque input identifiers may be short, but URLs/paths/control-like payloads are not evidence.
                if (
                    not value
                    or len(value) > 256
                    or "://" in value
                    or "/" in value
                    or "\\" in value
                    or "\n" in value
                ):
                    return False
            elif not isinstance(value, (bool, type(None))):
                return False
        return True

    def _row(
        self,
        stage: str,
        evidence: dict,
        current: datetime,
        key: bytes,
        sequence: int,
        case_ref: str,
        source_ref: str,
    ) -> dict | None:
        if stage not in STAGES or not self._valid_evidence(evidence) or not self._window():
            return None
        classes = attention_classes(evidence.get("attention_classes", ["unknown"]))
        # yes/no/unknown is defined solely by structured owner evidence.
        material = (
            "yes"
            if evidence.get("source_owner_transition") is True
            or evidence.get("explicit_material_change_receipt") is True
            else "unknown"
        )
        no_effect = "no" if evidence.get("explicit_no_effect_evidence") is True else "unknown"
        # An inhibited/promotion result alone is deliberately never no-effect.
        dependence = (
            "yes"
            if evidence.get("shared_upstream_ref") is True
            or evidence.get("source_family_ref") is True
            else "unknown"
        )
        cross_domain = "yes" if evidence.get("relation_evidence") is True else "unknown"
        mixed = (
            "yes"
            if evidence.get("same_window_external_protected") is True
            and "external" in classes
            and len(set(classes) - {"unknown"}) >= 2
            else "unknown"
        )
        contradiction = (
            "yes"
            if stage in {"correction", "contradiction"}
            and evidence.get("contradiction_evidence") is True
            else (
                "yes"
                if stage == "retraction" and evidence.get("retraction_evidence") is True
                else "unknown"
            )
        )
        start = self._window()[0]
        return {
            "schema": SCHEMA,
            "sequence": sequence,
            "study_day": int((current - start).total_seconds() // 86400) + 1,
            "time_bucket": current.hour // 6,
            "stage": stage,
            "case_ref": case_ref,
            "source_ref": source_ref,
            "source_class": source_class(evidence.get("source_class")),
            "change_state": _closed(evidence.get("change_state"), CHANGE_STATES),
            "material": material,
            "no_effect": no_effect,
            "dependence": dependence,
            "cross_domain": cross_domain,
            "attention_classes": classes,
            "mixed_attention": mixed,
            "contradiction": contradiction,
            "settlement": _closed(evidence.get("settlement"), SETTLEMENTS),
        }

    def observe(self, stage: str, evidence: dict, *, now: str | None = None) -> bool:
        current = _now(now)
        try:
            if not self._enabled_window(current):
                if self._window() and current >= self._window()[1] and self._control():
                    self.closeout(now=_iso(current))
                return False
            key = self._key()
            if not key or not self._valid_evidence(evidence):
                return False
            source_ref = self._opaque(
                key,
                evidence.get("source_receipt")
                or evidence.get("source_id")
                or evidence.get("candidate_id")
                or evidence.get("event_id")
                or "unknown",
                "src",
            )
            entity_ref = self._opaque(
                key,
                evidence.get("case_id")
                or evidence.get("candidate_id")
                or evidence.get("event_id")
                or evidence.get("source_receipt")
                or source_ref,
                "case",
            )
            connection = self._connect()
            try:
                connection.execute("BEGIN EXCLUSIVE")
                if connection.execute(
                    "SELECT 1 FROM blocked WHERE source_ref=?", (source_ref,)
                ).fetchone():
                    connection.rollback()
                    return False
                linked = connection.execute(
                    "SELECT case_ref FROM source_cases WHERE source_ref=?", (source_ref,)
                ).fetchone()
                entity_linked = connection.execute(
                    "SELECT case_ref FROM entity_cases WHERE entity_ref=?", (entity_ref,)
                ).fetchone()
                # A candidate/entity link is canonical once it exists.  If a
                # previously independent source is subsequently coalesced into
                # it, merge every study-local alias and observation while the
                # exclusive transaction is held.  This is deliberately not a
                # best-effort rewrite: a split denominator is invalid evidence.
                case_ref = (
                    entity_linked[0] if entity_linked else (linked[0] if linked else entity_ref)
                )
                if linked and entity_linked and linked[0] != entity_linked[0]:
                    old_ref = linked[0]
                    # `payload` is the authoritative immutable row projection
                    # read by closeout, so reparent it in the same transaction
                    # as the index column rather than leaving a split shadow.
                    for sequence, payload in connection.execute(
                        "SELECT sequence, payload FROM observations WHERE case_ref=?", (old_ref,)
                    ):
                        projected = json.loads(payload)
                        projected["case_ref"] = case_ref
                        rewritten = json.dumps(projected, sort_keys=True, separators=(",", ":"))
                        connection.execute(
                            "UPDATE observations SET case_ref=?, payload=?, bytes=? WHERE sequence=?",
                            (case_ref, rewritten, len(rewritten.encode()), sequence),
                        )
                    connection.execute(
                        "UPDATE source_cases SET case_ref=? WHERE case_ref=?", (case_ref, old_ref)
                    )
                    connection.execute(
                        "UPDATE entity_cases SET case_ref=? WHERE case_ref=?", (case_ref, old_ref)
                    )
                connection.execute(
                    "INSERT OR REPLACE INTO source_cases VALUES (?,?)", (source_ref, case_ref)
                )
                connection.execute(
                    "INSERT OR REPLACE INTO entity_cases VALUES (?,?)", (entity_ref, case_ref)
                )
                count, used = connection.execute(
                    "SELECT COUNT(*), COALESCE(SUM(bytes), 0) FROM observations"
                ).fetchone()
                # Definitive MAX(sequence)+1 must be serialized before cap checking.
                sequence = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(sequence), 0) + 1 FROM observations"
                    ).fetchone()[0]
                )
                row = self._row(stage, evidence, current, key, sequence, case_ref, source_ref)
                if row is None:
                    connection.rollback()
                    return False
                payload = json.dumps(row, sort_keys=True, separators=(",", ":"))
                size = len(payload.encode())
                if count >= MAX_ROWS or used + size > MAX_BYTES:
                    connection.rollback()
                    return False
                connection.execute(
                    "INSERT INTO observations VALUES (?,?,?,?,?)",
                    (sequence, case_ref, source_ref, payload, size),
                )
                connection.commit()
                return True
            finally:
                connection.close()
        except Exception:
            return False

    def _rows(self) -> list[dict]:
        if not self.db_path.exists():
            return []
        connection = self._connect()
        try:
            return [
                json.loads(row[0])
                for row in connection.execute("SELECT payload FROM observations ORDER BY sequence")
            ]
        finally:
            connection.close()

    @staticmethod
    def _cases(rows: list[dict]) -> list[dict]:
        cases: dict[str, dict] = {}
        for row in rows:
            case = cases.setdefault(row["case_ref"], dict(row))
            for field in (
                "material",
                "no_effect",
                "dependence",
                "cross_domain",
                "mixed_attention",
                "contradiction",
            ):
                if row.get(field) in {"yes", "no"}:
                    case[field] = row[field]
            case["attention_classes"] = sorted(
                set(case.get("attention_classes", [])) | set(row.get("attention_classes", []))
            )
        return [cases[ref] for ref in sorted(cases)]

    @staticmethod
    def _counts(cases: list[dict]) -> dict:
        by_source = {
            name: sum(case.get("source_class") == name for case in cases) for name in SOURCE_CLASSES
        }
        return {
            "total": len(cases),
            "material": sum(case.get("material") == "yes" for case in cases),
            "no_effect": sum(case.get("no_effect") == "no" for case in cases),
            "dependence": sum(case.get("dependence") == "yes" for case in cases),
            "contradiction_retraction": sum(case.get("contradiction") == "yes" for case in cases),
            "cross_domain": sum(case.get("cross_domain") == "yes" for case in cases),
            "mixed_attention": sum(case.get("mixed_attention") == "yes" for case in cases),
            "source_classes": sum(by_source[name] > 0 for name in SOURCE_CLASSES - {"unknown"}),
            "largest_source_class": max(by_source.values(), default=0),
            "unknown": by_source["unknown"],
            "by_source": by_source,
        }

    @staticmethod
    def _deficits(counts: dict) -> dict:
        minimums = {
            "total": 48,
            "material": 24,
            "no_effect": 12,
            "dependence": 6,
            "contradiction_retraction": 6,
            "cross_domain": 6,
            "mixed_attention": 6,
            "source_classes": 4,
        }
        deficits = {key: max(0, value - counts[key]) for key, value in minimums.items()}
        deficits["source_balance"] = max(0, counts["largest_source_class"] - 16)
        deficits["unknown_rate"] = max(0, counts["unknown"] - 12)
        return {key: value for key, value in deficits.items() if value}

    @classmethod
    def _select_48(cls, cases: list[dict]) -> list[dict] | None:
        """Complete deterministic feasibility search over grouped case patterns.

        Groups have one source class and one six-bit quota contribution.  The
        recursion enumerates *all* bounded group multiplicities, memoizes the
        capped quota state, and uses only admissible availability pruning.  It
        therefore cannot report insufficiency merely because a greedy prefix
        dead-ended.  There is intentionally no clock/random/search-budget exit.
        """
        fields = (
            "material",
            "no_effect",
            "dependence",
            "contradiction",
            "cross_domain",
            "mixed_attention",
        )
        limits = (24, 12, 6, 6, 6, 6)
        expected = ("yes", "no", "yes", "yes", "yes", "yes")
        normalized = [
            case
            for case in cases
            if isinstance(case, dict)
            and isinstance(case.get("case_ref"), str)
            and case.get("source_class") in SOURCE_CLASSES
        ]
        buckets: dict[tuple[str, tuple[int, ...]], list[dict]] = {}
        for case in normalized:
            pattern = tuple(int(case.get(field) == value) for field, value in zip(fields, expected))
            buckets.setdefault((case["source_class"], pattern), []).append(case)
        groups = [
            (source, pattern, sorted(items, key=lambda item: item["case_ref"]))
            for (source, pattern), items in sorted(buckets.items())
        ]
        if sum(len(items) for _, _, items in groups) < 48:
            return None
        # Suffix feature availability is an overestimate (so pruning is safe).
        suffix_total = [0] * (len(groups) + 1)
        suffix_features = [tuple(0 for _ in fields) for _ in range(len(groups) + 1)]
        suffix_known = [0] * (len(groups) + 1)
        for index in range(len(groups) - 1, -1, -1):
            source, pattern, items = groups[index]
            suffix_total[index] = suffix_total[index + 1] + len(items)
            suffix_features[index] = tuple(
                suffix_features[index + 1][slot] + pattern[slot] * len(items)
                for slot in range(len(fields))
            )
            suffix_known[index] = suffix_known[index + 1] + int(source != "unknown" and bool(items))

        @lru_cache(maxsize=None)
        def search(
            index: int,
            total: int,
            unknown: int,
            material: int,
            no_effect: int,
            dependence: int,
            contradiction: int,
            cross_domain: int,
            mixed: int,
            source_mask: int,
            source_used: int,
        ):
            values = (material, no_effect, dependence, contradiction, cross_domain, mixed)
            if total > 48 or unknown > 12 or source_used > 16:
                return None
            if total + suffix_total[index] < 48:
                return None
            if any(
                values[slot] + suffix_features[index][slot] < limits[slot]
                for slot in range(len(fields))
            ):
                return None
            if index == len(groups):
                return (
                    ()
                    if total == 48
                    and source_mask.bit_count() >= 4
                    and all(values[slot] >= limits[slot] for slot in range(len(fields)))
                    else None
                )
            source, pattern, items = groups[index]
            previous_source = groups[index - 1][0] if index else None
            used = source_used if source == previous_source else 0
            room = min(len(items), 48 - total, 16 - used)
            if source == "unknown":
                room = min(room, 12 - unknown)
            # Larger takes first makes the reconstructed opaque ref list stable;
            # every legal take is still explored before infeasibility is returned.
            bit = (
                0
                if source == "unknown"
                else 1 << sorted(SOURCE_CLASSES - {"unknown"}).index(source)
            )
            for take in range(room, -1, -1):
                next_values = tuple(
                    min(limits[slot], values[slot] + pattern[slot] * take)
                    for slot in range(len(fields))
                )
                result = search(
                    index + 1,
                    total + take,
                    unknown + (take if source == "unknown" else 0),
                    *next_values,
                    source_mask | (bit if take else 0),
                    used + take,
                )
                if result is not None:
                    return (take,) + result
            return None

        selection = search(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
        if selection is None:
            return None
        selected = [case for take, (_, _, items) in zip(selection, groups) for case in items[:take]]
        selected.sort(key=lambda case: case["case_ref"])
        return (
            selected if len(selected) == 48 and not cls._deficits(cls._counts(selected)) else None
        )

    @staticmethod
    def _generator_case(case: dict) -> dict:
        return {
            key: case[key]
            for key in (
                "schema",
                "case_ref",
                "source_ref",
                "source_class",
                "change_state",
                "attention_classes",
                "study_day",
                "time_bucket",
            )
        }

    @staticmethod
    def _gold_case(case: dict) -> dict:
        return {
            key: case[key]
            for key in (
                "case_ref",
                "material",
                "no_effect",
                "dependence",
                "cross_domain",
                "mixed_attention",
                "contradiction",
                "settlement",
            )
        }

    @staticmethod
    def _safe_case_ref(value: Any, prefix: str = "case_") -> bool:
        return (
            isinstance(value, str)
            and value.startswith(prefix)
            and len(value) == len(prefix) + 24
            and all(char in "0123456789abcdef" for char in value[len(prefix) :])
        )

    def _read_frozen(self, path: Path, kind: str) -> dict | None:
        """Project one retained artifact through its closed recursive schema."""
        try:
            value = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        if (
            not isinstance(value, dict)
            or set(value) != {"schema", "cases"}
            or value.get("schema") != SCHEMA
            or not isinstance(value.get("cases"), list)
        ):
            return None
        expected = (
            {
                "schema",
                "case_ref",
                "source_ref",
                "source_class",
                "change_state",
                "attention_classes",
                "study_day",
                "time_bucket",
            }
            if kind == "generator"
            else {
                "case_ref",
                "material",
                "no_effect",
                "dependence",
                "cross_domain",
                "mixed_attention",
                "contradiction",
                "settlement",
            }
        )
        projected = []
        for case in value["cases"]:
            if (
                not isinstance(case, dict)
                or set(case) != expected
                or not self._safe_case_ref(case.get("case_ref"))
            ):
                return None
            if kind == "generator":
                if (
                    not self._safe_case_ref(case.get("source_ref"), "src_")
                    or case.get("source_class") not in SOURCE_CLASSES
                    or case.get("change_state") not in CHANGE_STATES
                    or not isinstance(case.get("attention_classes"), list)
                    or not case["attention_classes"]
                    or not all(item in ATTENTION_CLASSES for item in case["attention_classes"])
                    or not isinstance(case.get("study_day"), int)
                    or not isinstance(case.get("time_bucket"), int)
                ):
                    return None
            elif any(
                case.get(field)
                not in {"yes", "no", "unknown", "chosen", "settled", "held", "dropped"}
                for field in expected - {"case_ref"}
            ):
                return None
            projected.append({key: case[key] for key in expected})
        return {"schema": SCHEMA, "cases": projected}

    def _read_manifest(self) -> dict | None:
        """Validate, rather than carry forward, the derived evaluation receipt."""
        try:
            value = json.loads(self.manifest_path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        expected = {"schema", "verdict", "counts", "deficits", "hashes", "selected_cases"}
        if not isinstance(value, dict) or set(value) != expected or value.get("schema") != SCHEMA:
            return None
        if value.get("verdict") not in {
            "SUFFICIENT_PRIVATE_EVIDENCE",
            "INSUFFICIENT_PRIVATE_EVIDENCE",
        } or not isinstance(value.get("selected_cases"), int):
            return None
        if (
            not isinstance(value.get("counts"), dict)
            or not isinstance(value.get("deficits"), dict)
            or not isinstance(value.get("hashes"), dict)
        ):
            return None
        if set(value["hashes"]) != {self.generator_path.name, self.gold_path.name} or not all(
            isinstance(item, str)
            and len(item) == 64
            and all(char in "0123456789abcdef" for char in item)
            for item in value["hashes"].values()
        ):
            return None
        # The manifest can only attest to the exact frozen bytes it names.
        if (
            not self.generator_path.exists()
            or not self.gold_path.exists()
            or any(
                value["hashes"][path.name] != hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (self.generator_path, self.gold_path)
            )
        ):
            return None
        return value

    def _read_tombstones(self) -> list[str] | None:
        try:
            value = json.loads(self.tombstone_path.read_text())
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError):
            return None
        return (
            value
            if isinstance(value, list)
            and all(
                isinstance(item, str)
                and len(item) == 24
                and all(char in "0123456789abcdef" for char in item)
                for item in value
            )
            else None
        )

    def _authorized_closeout(self, current: datetime) -> bool:
        window, control, latch = self._window(), self._control(), self._latch()
        return bool(
            window
            and control
            and latch
            and current >= window[1]
            and self.db_path.exists()
            and self._key()
            and latch == {"schema": SCHEMA, "window": self._digest(*window), "state": "active"}
            and control
            == {
                "schema": SCHEMA,
                "state": "active",
                "start_at": _iso(window[0]),
                "expires_at": _iso(window[1]),
            }
        )

    def closeout(self, *, now: str | None = None) -> dict:
        control, window, latch = self._control(), self._window(), self._latch()
        if control and control.get("state") == "closed":
            if window and latch == {
                "schema": SCHEMA,
                "window": self._digest(*window),
                "state": "closed",
            }:
                return {"closed": True, "idempotent": True, "verdict": control["verdict"]}
            return {"closed": False, "reason": "closeout_not_authorized"}
        current = _now(now)
        if not self._authorized_closeout(current):
            return {"closed": False, "reason": "closeout_not_authorized"}
        all_cases = self._cases(self._rows())
        selected = self._select_48(all_cases)
        counts = self._counts(selected if selected is not None else all_cases)
        deficits = self._deficits(counts)
        verdict = (
            "SUFFICIENT_PRIVATE_EVIDENCE"
            if selected is not None and not deficits
            else "INSUFFICIENT_PRIVATE_EVIDENCE"
        )
        frozen = selected or []
        _atomic_json(
            self.generator_path,
            {"schema": SCHEMA, "cases": [self._generator_case(case) for case in frozen]},
        )
        _atomic_json(
            self.gold_path, {"schema": SCHEMA, "cases": [self._gold_case(case) for case in frozen]}
        )
        # Guard against accidental future generator-label leakage.
        forbidden = {
            "material",
            "no_effect",
            "dependence",
            "cross_domain",
            "mixed_attention",
            "contradiction",
            "settlement",
        }
        assert not any(
            forbidden.intersection(case)
            for case in json.loads(self.generator_path.read_text())["cases"]
        )
        hashes = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (self.generator_path, self.gold_path)
        }
        _atomic_json(
            self.manifest_path,
            {
                "schema": SCHEMA,
                "verdict": verdict,
                "counts": counts,
                "deficits": deficits,
                "hashes": hashes,
                "selected_cases": len(frozen),
            },
        )
        shutil.rmtree(self.db_path.parent / "__never__", ignore_errors=True)
        for path in (
            self.db_path,
            Path(str(self.db_path) + "-wal"),
            Path(str(self.db_path) + "-shm"),
        ):
            path.unlink(missing_ok=True)
        _atomic_json(
            self.control_path,
            {
                "schema": SCHEMA,
                "state": "closed",
                "closed_at": _iso(current),
                "frozen_expires_at": _iso(current + timedelta(days=30)),
                "tombstones_expires_at": _iso(current + timedelta(days=90)),
                "verdict": verdict,
            },
        )
        window = self._window()
        assert window is not None
        _atomic_json(
            self.latch_path, {"schema": SCHEMA, "window": self._digest(*window), "state": "closed"}
        )
        return {
            "closed": True,
            "idempotent": False,
            "verdict": verdict,
            "counts": counts,
            "deficits": deficits,
        }

    def revoke(self, source_receipt: str, *, now: str | None = None) -> dict:
        control = self._control()
        if self.control_path.exists() and control is None:
            return {"revoked": False, "reason": "retention_control_unavailable"}
        if control and control.get("state") == "closed":
            window, latch = self._window(), self._latch()
            if not window or latch != {
                "schema": SCHEMA,
                "window": self._digest(*window),
                "state": "closed",
            }:
                return {"revoked": False, "reason": "retention_control_unavailable"}
        key = self._key()
        if not key or not isinstance(source_receipt, str) or not source_receipt:
            return {"revoked": False, "reason": "revocation_key_unavailable"}
        source_ref = self._opaque(key, source_receipt, "src")
        removed, revoked_cases = 0, set()
        if self.db_path.exists():
            connection = self._connect()
            try:
                connection.execute("BEGIN EXCLUSIVE")
                revoked_cases = {
                    row[0]
                    for row in connection.execute(
                        "SELECT DISTINCT case_ref FROM observations WHERE source_ref=?",
                        (source_ref,),
                    )
                }
                if revoked_cases:
                    placeholders = ",".join("?" for _ in revoked_cases)
                    removed += connection.execute(
                        f"DELETE FROM observations WHERE case_ref IN ({placeholders})",
                        tuple(revoked_cases),
                    ).rowcount
                    connection.execute(
                        f"DELETE FROM source_cases WHERE case_ref IN ({placeholders})",
                        tuple(revoked_cases),
                    )
                    connection.execute(
                        f"DELETE FROM entity_cases WHERE case_ref IN ({placeholders})",
                        tuple(revoked_cases),
                    )
                connection.execute("INSERT OR IGNORE INTO blocked VALUES (?)", (source_ref,))
                connection.commit()
            finally:
                connection.close()
        # Validate every retained artifact before changing one.  This prevents a
        # partial rewrite from implicitly approving a malformed sibling.
        artifacts: list[tuple[Path, dict]] = []
        invalid_frozen = False
        for path, kind in ((self.generator_path, "generator"), (self.gold_path, "gold")):
            if path.exists():
                artifact = self._read_frozen(path, kind)
                if artifact is None:
                    invalid_frozen = True
                    break
                artifacts.append((path, artifact))
        if self.manifest_path.exists() and self._read_manifest() is None:
            invalid_frozen = True
        tombstones = self._read_tombstones()
        if tombstones is None:
            invalid_frozen = True
            tombstones = []
        if not invalid_frozen:
            for path, artifact in artifacts:
                if not revoked_cases:
                    revoked_cases = {
                        case.get("case_ref")
                        for case in artifact.get("cases", [])
                        if case.get("source_ref") == source_ref
                    }
                before = len(artifact.get("cases", []))
                artifact["cases"] = [
                    case
                    for case in artifact.get("cases", [])
                    if case.get("case_ref") not in revoked_cases
                ]
                removed += before - len(artifact["cases"])
                _atomic_json(path, artifact)
        if invalid_frozen:
            # A derivative is never an authority to preserve malformed/private
            # bytes.  Delete it and its dependent manifest rather than copying.
            self.generator_path.unlink(missing_ok=True)
            self.gold_path.unlink(missing_ok=True)
        marker = hashlib.sha256(source_ref.encode()).hexdigest()[:24]
        if marker not in tombstones:
            _atomic_json(self.tombstone_path, sorted(tombstones + [marker]))
        self.manifest_path.unlink(
            missing_ok=True
        )  # derived counts/hashes are invalid, never stale.
        return {"revoked": True, "removed": removed}

    def maintenance(self, *, now: str | None = None) -> dict:
        control, current = self._control(), _now(now)
        if not control:
            return {"maintained": False}
        changed = False
        frozen_at, tomb_at = (
            _parse_time(control.get("frozen_expires_at")),
            _parse_time(control.get("tombstones_expires_at")),
        )
        if frozen_at and current >= frozen_at:
            for path in (self.generator_path, self.gold_path, self.manifest_path, self.key_path):
                if path.exists():
                    path.unlink()
                    changed = True
        if tomb_at and current >= tomb_at:
            if self.tombstone_path.exists():
                self.tombstone_path.unlink()
                changed = True
        return {"maintained": changed}


def observe_after_success(
    profile_root: str | Path, config: dict, stage: str, evidence: dict
) -> None:
    """Fire-and-forget owner seam. It intentionally has no result/exception path."""
    try:
        ProspectiveEvidenceCapture(profile_root, config).observe(stage, evidence)
    except Exception:
        pass
