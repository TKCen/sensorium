"""Disposable bounded current-attention index over canonical Sensorium JSONLs.

JSONLs remain authoritative. The index keeps exact historical disposition keys off
the hot path and materializes only current admission state. It is restartable and
can be deleted and rebuilt.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from typing import Callable

STREAMS = ("signals", "events", "decisions", "candidates")
DEFAULT_SCAN_BYTES = 4 * 1024 * 1024
MAX_RECORD_BYTES = 1024 * 1024
BOUNDARY_BYTES = 64
SQLITE_TIMEOUT_SECONDS = 0.2
SCHEMA_VERSION = 3
MAX_DEPENDENT_REFRESHES_PER_PASS = 64



@dataclass(frozen=True)
class AdmissionIndexResult:
    complete: bool
    state: str
    snapshot: dict[str, list[dict]] | None
    progress: dict[str, dict]
    bytes_consumed: int
    reason: str | None = None
    records_consumed: int = 0
    query_rows: int = 0
    materialized_rows: int = 0
    sqlite_vm_steps: int = 0
    dependent_refreshes: int = 0
    value: object | None = None


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _safe_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _bounded_strings(value: object, *, limit: int | None = None) -> list[str]:
    """Project complete record-local identifiers; the record byte cap is the bound."""
    if not isinstance(value, list):
        return []
    selected = value if limit is None else value[:limit]
    return [item for item in selected if isinstance(item, str)]


def _binding_projection(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    scalar_keys = (
        "schema_version", "policy_version", "source_candidate_id",
        "source_candidate_fingerprint", "item_key", "revision_key",
        "admission_key", "identity_mode", "evidence_class", "envelope_key",
    )
    projected: dict[str, object] = {
        key: item if isinstance(item := value.get(key), (str, int, float, bool)) else None
        for key in scalar_keys if key in value
    }
    for key in ("member_keys", "selected_member_keys"):
        if key in value:
            items = value.get(key)
            if not isinstance(items, list):
                return None
            projected[key] = _bounded_strings(items)
    return projected


def _pressure(value: object) -> float:
    try:
        return float(value) if isinstance(value, (str, int, float)) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _candidate_projection(row: dict, fingerprint: Callable[[dict], str]) -> dict:
    meta = _safe_dict(row.get("advisory_meta"))
    binding = _binding_projection(row.get("admission_binding"))
    if binding is None:
        binding = _binding_projection(meta.get("admission_binding"))
    return {
        "id": row.get("id"),
        "status": row.get("status", "candidate"),
        "kind": row.get("kind"),
        "pressure": _pressure(row.get("pressure")),
        "created_at": row.get("created_at") if isinstance(row.get("created_at"), str) else "",
        "updated_at": row.get("updated_at") if isinstance(row.get("updated_at"), str) else "",
        "event_ids": _bounded_strings(row.get("event_ids")),
        "correlation_keys": _bounded_strings(row.get("correlation_keys")),
        "summary": str(row.get("summary") or "")[:240],
        "fingerprint": row.get("fingerprint"),
        "source_candidate_ids": _bounded_strings(row.get("source_candidate_ids")),
        "source_candidate_fingerprint": row.get("source_candidate_fingerprint"),
        "admission_binding": binding,
        "advisory_meta": {"action": meta.get("action"), "admission_binding": binding},
        "_admission_candidate_fingerprint": fingerprint(row),
    }


def _decision_projection(row: dict) -> dict:
    return {
        "id": row.get("id"), "ts": row.get("ts"), "type": row.get("type"),
        "dry_run": row.get("dry_run"), "action": row.get("action"),
        "output_action": row.get("output_action"), "candidate_id": row.get("candidate_id"),
        "reason_code": row.get("reason_code"),
        "admission_binding": _binding_projection(row.get("admission_binding")),
    }


def _event_projection(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "source_signal_ids": _bounded_strings(row.get("source_signal_ids")),
        "kind": row.get("kind"), "summary": str(row.get("summary") or "")[:240],
        "correlation_keys": _bounded_strings(row.get("correlation_keys")),
    }


def _signal_projection(
    row: dict, instance: str,
    claim_parser: Callable[[str, dict], tuple[dict | None, str | None]],
) -> dict:
    claim, error = claim_parser(instance, row)
    return {
        "id": row.get("id"), "sensor": row.get("sensor"), "source": row.get("source"),
        "_admission_claim": claim, "_admission_error": error,
    }


class AdmissionIndex:
    """Incremental source reader plus materialized current eligibility owner."""

    def __init__(self, store):
        self.store = store
        self.path = store.root / "indexes" / "admission-v2.sqlite3"

    def _path_safe(self, *, create: bool) -> None:
        root = self.store.root
        if root.is_symlink():
            raise OSError(errno.ELOOP, "symlinked instance root")
        index_dir = self.path.parent
        if index_dir.exists() and index_dir.is_symlink():
            raise OSError(errno.ELOOP, "symlinked admission index directory")
        if self.path.exists() and self.path.is_symlink():
            raise OSError(errno.ELOOP, "symlinked admission index")
        if create:
            index_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(index_dir, 0o700)

    def _connect(self, *, create: bool, readonly: bool = False) -> sqlite3.Connection:
        self._path_safe(create=create)
        if readonly:
            conn = sqlite3.connect(
                f"file:{self.path}?mode=ro", uri=True, timeout=SQLITE_TIMEOUT_SECONDS,
            )
        else:
            conn = sqlite3.connect(self.path, timeout=SQLITE_TIMEOUT_SECONDS)
            os.chmod(self.path, 0o600)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={int(SQLITE_TIMEOUT_SECONDS * 1000)}")
        return conn

    @staticmethod
    def _schema(conn: sqlite3.Connection) -> None:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        cursor_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_cursor'"
        ).fetchone() is not None
        legacy_zero = version == 0 and cursor_exists and "next_ordinal" not in {
            row[1] for row in conn.execute("PRAGMA table_info(source_cursor)")
        }
        if version not in {0, SCHEMA_VERSION} or legacy_zero:
            # The index is disposable. Rebuild instead of running an unbounded
            # data migration over stale derived state.
            conn.executescript("""
            DROP TABLE IF EXISTS dependency_refresh;
            DROP TABLE IF EXISTS event_signal;
            DROP TABLE IF EXISTS candidate_event;
            DROP TABLE IF EXISTS admission_summary;
            DROP TABLE IF EXISTS candidate_state;
            DROP TABLE IF EXISTS candidate_member;
            DROP TABLE IF EXISTS disposition_evidence;
            DROP TABLE IF EXISTS candidate_projection;
            DROP TABLE IF EXISTS event_join;
            DROP TABLE IF EXISTS signal_claim;
            DROP TABLE IF EXISTS source_cursor;
            """)
        conn.executescript("""
        PRAGMA journal_mode=DELETE;
        PRAGMA synchronous=FULL;
        CREATE TABLE IF NOT EXISTS source_cursor (
          stream TEXT PRIMARY KEY CHECK(stream IN ('signals','events','candidates','decisions')),
          dev INTEGER NOT NULL, ino INTEGER NOT NULL, offset INTEGER NOT NULL,
          eof_size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
          boundary_sha256 TEXT NOT NULL, generation INTEGER NOT NULL,
          complete INTEGER NOT NULL CHECK(complete IN (0,1)),
          next_ordinal INTEGER NOT NULL, record_count INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS signal_claim (
          signal_id TEXT NOT NULL, generation INTEGER NOT NULL,
          source_ordinal INTEGER NOT NULL, projection_json TEXT NOT NULL,
          PRIMARY KEY(signal_id, generation)
        );
        CREATE TABLE IF NOT EXISTS event_join (
          event_id TEXT NOT NULL, generation INTEGER NOT NULL,
          source_ordinal INTEGER NOT NULL, projection_json TEXT NOT NULL,
          PRIMARY KEY(event_id, generation)
        );
        CREATE TABLE IF NOT EXISTS candidate_projection (
          candidate_id TEXT NOT NULL, generation INTEGER NOT NULL,
          source_ordinal INTEGER NOT NULL, row_sha256 TEXT NOT NULL,
          status TEXT, kind TEXT, pressure REAL NOT NULL, created_at TEXT NOT NULL,
          prompt_projection_json TEXT NOT NULL,
          PRIMARY KEY(candidate_id, generation)
        );
        CREATE TABLE IF NOT EXISTS disposition_evidence (
          evidence_id TEXT NOT NULL, generation INTEGER NOT NULL,
          source_stream TEXT NOT NULL, source_ordinal INTEGER NOT NULL,
          item_key TEXT, admission_key TEXT, member_key TEXT,
          source_candidate_id TEXT, source_candidate_fingerprint TEXT,
          applied INTEGER NOT NULL, terminal_item_closure INTEGER NOT NULL,
          projection_json TEXT NOT NULL,
          PRIMARY KEY(evidence_id, member_key)
        );
        CREATE INDEX IF NOT EXISTS ix_disposition_item ON disposition_evidence(item_key);
        CREATE INDEX IF NOT EXISTS ix_disposition_admission ON disposition_evidence(admission_key);
        CREATE INDEX IF NOT EXISTS ix_disposition_member ON disposition_evidence(member_key);
        CREATE INDEX IF NOT EXISTS ix_disposition_source ON disposition_evidence(source_candidate_id);
        CREATE INDEX IF NOT EXISTS ix_disposition_item_ordinal
          ON disposition_evidence(item_key, source_ordinal DESC);
        CREATE INDEX IF NOT EXISTS ix_disposition_member_ordinal
          ON disposition_evidence(member_key, source_ordinal DESC);
        CREATE INDEX IF NOT EXISTS ix_signal_generation_ordinal
          ON signal_claim(generation, source_ordinal);
        CREATE INDEX IF NOT EXISTS ix_event_generation_ordinal
          ON event_join(generation, source_ordinal);
        CREATE INDEX IF NOT EXISTS ix_candidate_generation_ordinal
          ON candidate_projection(generation, source_ordinal);
        CREATE TABLE IF NOT EXISTS candidate_member (
          candidate_id TEXT NOT NULL, member_key TEXT NOT NULL,
          PRIMARY KEY(candidate_id, member_key)
        );
        CREATE INDEX IF NOT EXISTS ix_candidate_member_key
          ON candidate_member(member_key, candidate_id);
        CREATE TABLE IF NOT EXISTS candidate_state (
          candidate_id TEXT PRIMARY KEY, pressure REAL NOT NULL, created_at TEXT NOT NULL,
          binding_json TEXT, source_decisions_json TEXT NOT NULL,
          attribution_gap_count INTEGER NOT NULL, suppression_reason TEXT, item_key TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_candidate_ready
          ON candidate_state(suppression_reason, pressure DESC, created_at, candidate_id);
        CREATE INDEX IF NOT EXISTS ix_candidate_state_item
          ON candidate_state(item_key, candidate_id);
        CREATE TABLE IF NOT EXISTS candidate_event (
          candidate_id TEXT NOT NULL, event_id TEXT NOT NULL,
          PRIMARY KEY(candidate_id, event_id)
        );
        CREATE INDEX IF NOT EXISTS ix_candidate_event_event
          ON candidate_event(event_id, candidate_id);
        CREATE TABLE IF NOT EXISTS event_signal (
          event_id TEXT NOT NULL, signal_id TEXT NOT NULL,
          PRIMARY KEY(event_id, signal_id)
        );
        CREATE INDEX IF NOT EXISTS ix_event_signal_signal
          ON event_signal(signal_id, event_id);
        CREATE TABLE IF NOT EXISTS dependency_refresh (
          kind TEXT NOT NULL CHECK(kind IN ('signal','event','item','member')),
          dependency_id TEXT NOT NULL, after_candidate_id TEXT NOT NULL DEFAULT '',
          PRIMARY KEY(kind, dependency_id)
        );
        CREATE TABLE IF NOT EXISTS admission_summary (
          key TEXT PRIMARY KEY, value INTEGER NOT NULL
        );
        PRAGMA user_version=3;
        """)

    def _source_stat(self, stream: str) -> os.stat_result | None:
        try:
            return self.store.paths[stream].stat(follow_symlinks=False)
        except FileNotFoundError:
            return None

    @staticmethod
    def _boundary(fd: int, offset: int) -> tuple[str, int]:
        count = min(offset, BOUNDARY_BYTES)
        data = os.pread(fd, count, offset - count) if count else b""
        return hashlib.sha256(data).hexdigest(), count

    def _open_source(self, stream: str) -> int | None:
        try:
            return os.open(self.store.paths[stream], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return None

    @staticmethod
    def _summary_delta(conn: sqlite3.Connection, key: str | None, delta: int) -> None:
        if key is None:
            return
        conn.execute(
            "INSERT INTO admission_summary(key,value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=value+excluded.value",
            (key, delta),
        )
        conn.execute("DELETE FROM admission_summary WHERE key=? AND value=0", (key,))

    @classmethod
    def _remove_state(cls, conn: sqlite3.Connection, candidate_id: str) -> None:
        old = conn.execute(
            "SELECT suppression_reason FROM candidate_state WHERE candidate_id=?", (candidate_id,),
        ).fetchone()
        if old is not None:
            cls._summary_delta(conn, old["suppression_reason"] or "eligible", -1)
        conn.execute("DELETE FROM candidate_state WHERE candidate_id=?", (candidate_id,))
        conn.execute("DELETE FROM candidate_member WHERE candidate_id=?", (candidate_id,))

    @staticmethod
    def _current_generation(conn: sqlite3.Connection, stream: str) -> int:
        row = conn.execute("SELECT generation FROM source_cursor WHERE stream=?", (stream,)).fetchone()
        return int(row[0]) if row else 1

    @classmethod
    def _delete_stream(cls, conn: sqlite3.Connection, stream: str) -> None:
        if stream == "signals":
            conn.execute("DELETE FROM signal_claim")
            conn.execute("DELETE FROM source_cursor WHERE stream='candidates'")
        elif stream == "events":
            conn.execute("DELETE FROM event_join")
            conn.execute("DELETE FROM event_signal")
            conn.execute("DELETE FROM source_cursor WHERE stream='candidates'")
        elif stream == "candidates":
            conn.execute("DELETE FROM candidate_projection")
            conn.execute("DELETE FROM disposition_evidence WHERE source_stream='candidates'")
            conn.execute("DELETE FROM candidate_member")
            conn.execute("DELETE FROM candidate_event")
            conn.execute("DELETE FROM candidate_state")
            conn.execute("DELETE FROM admission_summary")
            conn.execute("DELETE FROM dependency_refresh")
        else:
            conn.execute("DELETE FROM disposition_evidence WHERE source_stream='decisions'")
            # Rebuild candidate state after a rewritten decision authority stream.
            conn.execute("DELETE FROM source_cursor WHERE stream='candidates'")

    def _joined_snapshot(self, conn: sqlite3.Connection, candidate: dict) -> dict[str, list[dict]]:
        event_generation = self._current_generation(conn, "events")
        signal_generation = self._current_generation(conn, "signals")
        candidate_id = str(candidate.get("id") or "")
        event_rows = conn.execute(
            "SELECT e.projection_json,e.source_ordinal FROM candidate_event ce "
            "CROSS JOIN event_join e ON e.event_id=ce.event_id "
            "WHERE ce.candidate_id=? AND e.generation=?",
            (candidate_id, event_generation),
        ).fetchall()
        events = [json.loads(row[0]) for row in sorted(event_rows, key=lambda item: item[1])]
        # Membership order and duplicate references are non-canonical. Joining
        # through normalized relationships and sorting the bounded joined set by
        # signal receipt ordinal preserves authoritative latest-revision order
        # without inviting SQLite to scan a whole generation for ORDER BY.
        signal_rows = conn.execute(
            "SELECT DISTINCT s.projection_json,s.source_ordinal "
            "FROM candidate_event ce "
            "CROSS JOIN event_join e ON e.event_id=ce.event_id AND e.generation=? "
            "CROSS JOIN event_signal es ON es.event_id=e.event_id "
            "CROSS JOIN signal_claim s ON s.signal_id=es.signal_id AND s.generation=? "
            "WHERE ce.candidate_id=?",
            (event_generation, signal_generation, candidate_id),
        ).fetchall()
        signals = [json.loads(row[0]) for row in sorted(signal_rows, key=lambda item: item[1])]
        return {"signals": signals, "events": events, "candidates": [candidate], "decisions": []}

    @staticmethod
    def _is_applying_decision(projection: dict) -> bool:
        if projection.get("dry_run") is not False:
            return False
        pair = (projection.get("output_action"), projection.get("action"))
        return pair in {("DROP", "drop"), ("SAVE", "save")} or (
            projection.get("output_action") == "CREATE_CONSCIOUS_TASK"
            and projection.get("action") in {
                "created_conscious_task_candidate", "updated_conscious_task_candidate",
            }
        )

    def _insert_dispositions(
        self, conn: sqlite3.Connection, source: str, generation: int,
        ordinal: int, projection: dict, binding: dict,
    ) -> set[str]:
        applied = source == "candidates" or self._is_applying_decision(projection)
        if not applied:
            return set()
        members = binding.get("selected_member_keys")
        member_rows = members if isinstance(members, list) and members else [None]
        evidence_id = hashlib.sha256(f"{source}:{generation}:{ordinal}".encode()).hexdigest()
        terminal = source == "candidates" and projection.get("status") in {
            "suppressed", "cancelled", "archived", "prepared_external_work",
        }
        for member in member_rows:
            conn.execute(
                "INSERT OR REPLACE INTO disposition_evidence VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    evidence_id, generation, source, ordinal, binding.get("item_key"),
                    binding.get("admission_key"), member, binding.get("source_candidate_id"),
                    binding.get("source_candidate_fingerprint"), int(applied), int(terminal),
                    _json(projection),
                ),
            )
        item_key = binding.get("item_key")
        if isinstance(item_key, str):
            conn.execute(
                "INSERT OR REPLACE INTO dependency_refresh VALUES ('item',?,'')",
                (item_key,),
            )
        if members:
            conn.executemany(
                "INSERT OR REPLACE INTO dependency_refresh VALUES ('member',?,'')",
                ((member,) for member in set(members)),
            )
        return set()

    def _resolve_v1_evidence(
        self, conn: sqlite3.Connection, candidate_id: str, binding: dict,
    ) -> set[str]:
        members = binding.get("member_keys")
        if not isinstance(members, list) or not members:
            return set()
        rows = conn.execute(
            "SELECT * FROM disposition_evidence WHERE source_candidate_id=? "
            "AND member_key IS NULL AND json_extract(projection_json,'$.admission_binding.policy_version')="
            "'source-admission-v1'",
            (candidate_id,),
        ).fetchall()
        affected: set[str] = set()
        for row in rows:
            conn.execute(
                "DELETE FROM disposition_evidence WHERE evidence_id=? AND member_key IS NULL",
                (row["evidence_id"],),
            )
            for member in members:
                conn.execute(
                    "INSERT OR REPLACE INTO disposition_evidence VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        row["evidence_id"], row["generation"], row["source_stream"],
                        row["source_ordinal"], row["item_key"], row["admission_key"], member,
                        row["source_candidate_id"], row["source_candidate_fingerprint"],
                        row["applied"], row["terminal_item_closure"], row["projection_json"],
                    ),
                )
            conn.executemany(
                "INSERT OR REPLACE INTO dependency_refresh VALUES ('member',?,'')",
                ((member,) for member in set(members)),
            )
        return affected

    def _prior_rows(self, conn: sqlite3.Connection, binding: dict) -> tuple[list[dict], str | None, int]:
        rows: list[sqlite3.Row]
        reason: str | None = None
        gaps = 0
        members = binding.get("member_keys")
        if isinstance(members, list):
            if not members:
                return [], "prior_member_disposition", 0
            marks = ",".join("?" for _ in members)
            rows = conn.execute(
                f"SELECT * FROM disposition_evidence WHERE member_key IN ({marks}) "
                "ORDER BY source_ordinal DESC LIMIT 20",
                tuple(members),
            ).fetchall()
            disposed = {
                row["member_key"] for row in conn.execute(
                    f"SELECT DISTINCT member_key FROM disposition_evidence "
                    f"WHERE applied=1 AND member_key IN ({marks})",
                    tuple(members),
                )
            }
            selected = sorted(set(members) - disposed)
            binding["selected_member_keys"] = selected
            from .admission import _opaque
            binding["admission_key"] = _opaque("memory-admission", selected)
            reason = "prior_member_disposition" if not selected else None
        else:
            rows = conn.execute(
                "SELECT * FROM disposition_evidence WHERE item_key=? "
                "ORDER BY source_ordinal DESC LIMIT 20",
                (binding.get("item_key"),),
            ).fetchall()
            exact = conn.execute(
                "SELECT 1 FROM disposition_evidence WHERE applied=1 AND admission_key=? LIMIT 1",
                (binding.get("admission_key"),),
            ).fetchone()
            terminal = conn.execute(
                "SELECT 1 FROM disposition_evidence WHERE terminal_item_closure=1 "
                "AND item_key=? LIMIT 1", (binding.get("item_key"),),
            ).fetchone()
            legacy = None
            if binding.get("identity_mode") == "legacy":
                legacy = conn.execute(
                    "SELECT 1 FROM disposition_evidence WHERE source_stream='candidates' "
                    "AND source_candidate_id=? AND (source_candidate_fingerprint IS NULL "
                    "OR source_candidate_fingerprint='""' OR source_candidate_fingerprint=?) LIMIT 1",
                    (
                        binding.get("source_candidate_id"),
                        binding.get("source_candidate_fingerprint"),
                    ),
                ).fetchone()
            if exact:
                reason = "prior_disposition"
            elif terminal:
                reason = "explicit_item_closure"
            elif legacy:
                reason = "legacy_exact_source_disposition"
        projections: list[dict] = []
        for row in reversed(rows):
            projection = json.loads(row["projection_json"])
            projection.pop("admission_binding", None)
            projection["admission_key"] = row["admission_key"]
            if row["member_key"]:
                projection["overlap_member_keys"] = [row["member_key"]]
            projections.append(projection)
        return projections[-20:], reason, gaps

    def _refresh_candidate(
        self, conn: sqlite3.Connection, candidate_id: str, binding_builder,
        *, generation: int | None = None,
    ) -> None:
        generation = generation or self._current_generation(conn, "candidates")
        row = conn.execute(
            "SELECT * FROM candidate_projection WHERE candidate_id=? AND generation=?",
            (candidate_id, generation),
        ).fetchone()
        self._remove_state(conn, candidate_id)
        if row is None:
            return
        candidate = json.loads(row["prompt_projection_json"])
        if candidate.get("status", "candidate") != "candidate":
            return
        kind = str(candidate.get("kind") or "")
        if kind in {
            "subconscious_advisory", "body_pressure", "network_pressure", "process_pressure",
            "hindsight_pressure", "kanban_pressure",
        }:
            return
        snap = self._joined_snapshot(conn, candidate)
        binding, error = binding_builder(self.store, candidate, snap)
        if error or binding is None:
            suppression = error or "invalid_source_claim"
            source_decisions: list[dict] = []
            gaps = 0
        else:
            native_memory = any(
                signal.get("sensor") == "sensorium.memory_reflection"
                and signal.get("source") == "memory" for signal in snap["signals"]
            )
            if binding.get("identity_mode") != "source" and native_memory:
                suppression = "unsupported_memory_identity"
                source_decisions = []
                gaps = 0
            else:
                if isinstance(binding.get("member_keys"), list):
                    conn.executemany(
                        "INSERT OR IGNORE INTO candidate_member VALUES (?,?)",
                        ((candidate_id, member) for member in binding["member_keys"]),
                    )
                source_decisions, suppression, gaps = self._prior_rows(conn, binding)
        conn.execute(
            "INSERT INTO candidate_state VALUES (?,?,?,?,?,?,?,?)",
            (
                candidate_id, row["pressure"], row["created_at"],
                _json(binding) if binding is not None else None,
                _json(source_decisions), gaps, suppression,
                binding.get("item_key") if isinstance(binding, dict) else None,
            ),
        )
        self._summary_delta(conn, suppression or "eligible", 1)

    def _insert_projection(
        self, conn: sqlite3.Connection, stream: str, generation: int,
        ordinal: int, row: dict, claim_parser, fingerprint, binding_builder,
    ) -> None:
        if stream == "signals":
            projection = _signal_projection(row, self.store.instance, claim_parser)
            signal_id = str(row.get("id") or f"ordinal:{ordinal}")
            previous = conn.execute(
                "SELECT projection_json FROM signal_claim WHERE signal_id=? AND generation=?",
                (signal_id, generation),
            ).fetchone()
            conn.execute(
                "INSERT OR REPLACE INTO signal_claim VALUES (?,?,?,?)",
                (signal_id, generation, ordinal, _json(projection)),
            )
            has_dependents = conn.execute(
                "SELECT 1 FROM event_signal WHERE signal_id=? LIMIT 1", (signal_id,),
            ).fetchone() is not None
            if (
                (previous is None and has_dependents)
                or (previous is not None and previous[0] != _json(projection))
            ):
                conn.execute(
                    "INSERT OR REPLACE INTO dependency_refresh VALUES ('signal',?,'')",
                    (signal_id,),
                )
            return
        if stream == "events":
            projection = _event_projection(row)
            event_id = str(row.get("id") or f"ordinal:{ordinal}")
            previous = conn.execute(
                "SELECT projection_json FROM event_join WHERE event_id=? AND generation=?",
                (event_id, generation),
            ).fetchone()
            conn.execute(
                "INSERT OR REPLACE INTO event_join VALUES (?,?,?,?)",
                (event_id, generation, ordinal, _json(projection)),
            )
            conn.execute("DELETE FROM event_signal WHERE event_id=?", (event_id,))
            conn.executemany(
                "INSERT OR IGNORE INTO event_signal VALUES (?,?)",
                ((event_id, signal_id) for signal_id in set(projection["source_signal_ids"])),
            )
            has_dependents = conn.execute(
                "SELECT 1 FROM candidate_event WHERE event_id=? LIMIT 1", (event_id,),
            ).fetchone() is not None
            if (
                (previous is None and has_dependents)
                or (previous is not None and previous[0] != _json(projection))
            ):
                conn.execute(
                    "INSERT OR REPLACE INTO dependency_refresh VALUES ('event',?,'')",
                    (event_id,),
                )
            return
        if stream == "candidates":
            projection = _candidate_projection(row, fingerprint)
            candidate_id = str(row.get("id") or f"ordinal:{ordinal}")
            conn.execute(
                "INSERT OR REPLACE INTO candidate_projection VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    candidate_id, generation, ordinal,
                    hashlib.sha256(_json(row).encode()).hexdigest(),
                    projection.get("status"), projection.get("kind"),
                    projection.get("pressure", 0.0), projection.get("created_at", ""),
                    _json(projection),
                ),
            )
            conn.execute(
                "DELETE FROM candidate_event WHERE candidate_id=?", (candidate_id,),
            )
            conn.executemany(
                "INSERT OR IGNORE INTO candidate_event VALUES (?,?)",
                ((candidate_id, event_id) for event_id in set(projection["event_ids"])),
            )
            affected: set[str] = set()
            binding = projection.get("admission_binding")
            if projection.get("kind") == "subconscious_advisory":
                if isinstance(binding, dict):
                    affected = self._insert_dispositions(
                        conn, "candidates", generation, ordinal, projection, binding,
                    )
                elif projection.get("source_candidate_ids"):
                    legacy_id = projection["source_candidate_ids"][0]
                    evidence_id = hashlib.sha256(
                        f"candidates:{generation}:{ordinal}:legacy".encode(),
                    ).hexdigest()
                    conn.execute(
                        "INSERT OR REPLACE INTO disposition_evidence VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            evidence_id, generation, "candidates", ordinal, None, None, None,
                            legacy_id, projection.get("source_candidate_fingerprint"), 1, 0,
                            _json(projection),
                        ),
                    )
                    affected.add(legacy_id)
            self._refresh_candidate(
                conn, candidate_id, binding_builder, generation=generation,
            )
            if projection.get("kind") != "subconscious_advisory":
                base, error = binding_builder(self.store, projection, self._joined_snapshot(conn, projection))
                if not error and base:
                    affected.update(self._resolve_v1_evidence(conn, candidate_id, base))
            for affected_id in affected:
                self._refresh_candidate(
                    conn, affected_id, binding_builder, generation=generation,
                )
            return
        projection = _decision_projection(row)
        binding = projection.get("admission_binding")
        if isinstance(binding, dict):
            affected = self._insert_dispositions(
                conn, "decisions", generation, ordinal, projection, binding,
            )
            for candidate_id in affected:
                self._refresh_candidate(conn, candidate_id, binding_builder)

    def _drain_dependency_refresh(self, conn: sqlite3.Connection, binding_builder) -> tuple[int, bool]:
        """Refresh reverse dependents in a fixed-size, restartable work slice."""
        refreshed = 0
        while refreshed < MAX_DEPENDENT_REFRESHES_PER_PASS:
            dependency = conn.execute(
                "SELECT kind,dependency_id,after_candidate_id FROM dependency_refresh "
                "ORDER BY kind,dependency_id LIMIT 1"
            ).fetchone()
            if dependency is None:
                return refreshed, False
            remaining = MAX_DEPENDENT_REFRESHES_PER_PASS - refreshed
            if dependency["kind"] == "signal":
                rows = conn.execute(
                    "SELECT event_id FROM event_signal "
                    "WHERE signal_id=? AND event_id>? ORDER BY event_id LIMIT ?",
                    (
                        dependency["dependency_id"],
                        dependency["after_candidate_id"],
                        remaining + 1,
                    ),
                ).fetchall()
                selected = rows[:remaining]
                conn.executemany(
                    "INSERT OR REPLACE INTO dependency_refresh VALUES ('event',?,'')",
                    ((row["event_id"],) for row in selected),
                )
                refreshed += len(selected)
                if len(rows) <= remaining:
                    conn.execute(
                        "DELETE FROM dependency_refresh WHERE kind='signal' AND dependency_id=?",
                        (dependency["dependency_id"],),
                    )
                else:
                    conn.execute(
                        "UPDATE dependency_refresh SET after_candidate_id=? "
                        "WHERE kind='signal' AND dependency_id=?",
                        (selected[-1]["event_id"], dependency["dependency_id"]),
                    )
                    return refreshed, True
                continue
            if dependency["kind"] == "item":
                sql = (
                    "SELECT candidate_id FROM candidate_state "
                    "WHERE item_key=? AND candidate_id>? ORDER BY candidate_id LIMIT ?"
                )
            elif dependency["kind"] == "member":
                sql = (
                    "SELECT candidate_id FROM candidate_member "
                    "WHERE member_key=? AND candidate_id>? ORDER BY candidate_id LIMIT ?"
                )
            else:
                sql = (
                    "SELECT candidate_id FROM candidate_event "
                    "WHERE event_id=? AND candidate_id>? ORDER BY candidate_id LIMIT ?"
                )
            rows = conn.execute(
                sql,
                (
                    dependency["dependency_id"],
                    dependency["after_candidate_id"],
                    remaining + 1,
                ),
            ).fetchall()
            selected = rows[:remaining]
            for row in selected:
                self._refresh_candidate(conn, row["candidate_id"], binding_builder)
                refreshed += 1
            if len(rows) <= remaining:
                conn.execute(
                    "DELETE FROM dependency_refresh WHERE kind=? AND dependency_id=?",
                    (dependency["kind"], dependency["dependency_id"]),
                )
            else:
                conn.execute(
                    "UPDATE dependency_refresh SET after_candidate_id=? "
                    "WHERE kind=? AND dependency_id=?",
                    (selected[-1]["candidate_id"], dependency["kind"], dependency["dependency_id"]),
                )
                return refreshed, True
        pending = conn.execute("SELECT 1 FROM dependency_refresh LIMIT 1").fetchone() is not None
        return refreshed, pending

    def prepare(
        self, *, scan_bytes: int, claim_parser, fingerprint, binding_builder,
    ) -> AdmissionIndexResult:
        budget = max(1, int(scan_bytes))
        consumed = 0
        records = 0
        sqlite_vm_steps = 0
        dependent_refreshes = 0
        pending_reason: str | None = None
        rebuild = not self.path.exists()
        conn: sqlite3.Connection | None = None
        try:
            conn = self._connect(create=True)
            self._schema(conn)
            def count_vm_step() -> int:
                nonlocal sqlite_vm_steps
                sqlite_vm_steps += 1
                return 0
            conn.set_progress_handler(count_vm_step, 1)
            conn.execute("BEGIN IMMEDIATE")
            opening = {stream: self._source_stat(stream) for stream in STREAMS}
            for stream in STREAMS:
                stat = opening[stream]
                size = stat.st_size if stat else 0
                cursor = conn.execute(
                    "SELECT * FROM source_cursor WHERE stream=?", (stream,),
                ).fetchone()
                generation = int(cursor["generation"]) if cursor else 1
                reset = cursor is None
                fd = self._open_source(stream)
                try:
                    dev, ino = (stat.st_dev, stat.st_ino) if stat else (0, 0)
                    if cursor is not None:
                        created_from_empty = (
                            cursor["dev"] == 0 and cursor["ino"] == 0
                            and cursor["offset"] == 0 and stat is not None
                        )
                        if (
                            not created_from_empty
                            and (dev != cursor["dev"] or ino != cursor["ino"] or size < cursor["offset"])
                        ):
                            reset = True
                        elif size == cursor["offset"] and stat and stat.st_mtime_ns != cursor["mtime_ns"]:
                            reset = True
                        elif fd is not None:
                            boundary, charged = self._boundary(fd, int(cursor["offset"]))
                            consumed += charged
                            budget -= charged
                            if boundary != cursor["boundary_sha256"]:
                                reset = True
                    if reset:
                        rebuild = True
                        generation += int(cursor is not None)
                        self._delete_stream(conn, stream)
                        offset = ordinal = record_count = 0
                    else:
                        assert cursor is not None
                        offset = int(cursor["offset"])
                        ordinal = int(cursor["next_ordinal"])
                        record_count = int(cursor["record_count"])
                    complete = offset == size
                    if not complete and budget > BOUNDARY_BYTES and fd is not None:
                        available = size - offset
                        request = min(available, budget - BOUNDARY_BYTES, MAX_RECORD_BYTES + 1)
                        chunk = os.pread(fd, request, offset)
                        consumed += len(chunk)
                        budget -= len(chunk)
                        final_newline = chunk.rfind(b"\n")
                        if final_newline < 0:
                            if available > MAX_RECORD_BYTES:
                                raise ValueError(f"{stream}:record_too_large")
                            if len(chunk) == available:
                                raise ValueError(f"{stream}:incomplete_record")
                            # The remaining aggregate budget may have been spent
                            # on earlier streams. Keep the complete-record cursor
                            # unchanged so the next pass can resume safely.
                            pending_reason = f"{stream}:record_waiting_for_larger_remaining_budget"
                        else:
                            parsed = chunk[:final_newline + 1]
                            for raw in parsed.splitlines():
                                if not raw.strip():
                                    continue
                                value = json.loads(raw)
                                if not isinstance(value, dict):
                                    raise ValueError(f"{stream}:non_object_record")
                                self._insert_projection(
                                    conn, stream, generation, ordinal, value,
                                    claim_parser, fingerprint, binding_builder,
                                )
                                ordinal += 1
                                record_count += 1
                                records += 1
                            offset += len(parsed)
                            complete = offset == size
                    boundary = hashlib.sha256(b"").hexdigest()
                    charge = min(offset, BOUNDARY_BYTES)
                    if fd is not None and budget >= charge:
                        boundary, charged = self._boundary(fd, offset)
                        consumed += charged
                        budget -= charged
                    conn.execute(
                        "INSERT OR REPLACE INTO source_cursor VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            stream, dev, ino, offset, size, stat.st_mtime_ns if stat else 0,
                            boundary, generation, int(complete), ordinal, record_count,
                        ),
                    )
                finally:
                    if fd is not None:
                        os.close(fd)
            dependent_refreshes, refresh_pending = self._drain_dependency_refresh(
                conn, binding_builder,
            )
            if refresh_pending:
                pending_reason = "dependent_refresh_pending"
            closing = {stream: self._source_stat(stream) for stream in STREAMS}
            stable = all(
                (a is None and b is None) or (
                    a is not None and b is not None
                    and (a.st_dev, a.st_ino, a.st_size, a.st_mtime_ns)
                    == (b.st_dev, b.st_ino, b.st_size, b.st_mtime_ns)
                ) for a, b in zip(opening.values(), closing.values())
            )
            conn.commit()
            progress = self._progress(conn)
            complete = not refresh_pending and stable and set(progress) == set(STREAMS) and all(
                value["complete"] for value in progress.values()
            )
            conn.set_progress_handler(None, 0)
            conn.close()
            return AdmissionIndexResult(
                complete, "ready" if complete else ("rebuilding" if rebuild else "catching_up"),
                None, progress, consumed,
                (pending_reason if stable else "source_snapshot_changed"),
                records_consumed=records,
                sqlite_vm_steps=sqlite_vm_steps,
                dependent_refreshes=dependent_refreshes,
            )
        except (OSError, sqlite3.Error, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            try:
                if conn is not None:
                    conn.rollback()
                    conn.close()
            except Exception:
                pass
            if isinstance(exc, sqlite3.DatabaseError) and any(
                marker in str(exc).lower()
                for marker in ("not a database", "malformed", "file is encrypted")
            ):
                try:
                    self._path_safe(create=False)
                    if self.path.is_file() and not self.path.is_symlink():
                        self.path.unlink()
                except OSError:
                    pass
            return AdmissionIndexResult(
                False, "invalid", None, {}, consumed, str(exc), records,
                sqlite_vm_steps=sqlite_vm_steps,
                dependent_refreshes=dependent_refreshes,
            )

    @staticmethod
    def _progress(conn: sqlite3.Connection) -> dict[str, dict]:
        return {
            row["stream"]: {
                "offset": row["offset"], "eof_size": row["eof_size"],
                "complete": bool(row["complete"]), "generation": row["generation"],
                "record_count": row["record_count"], "next_ordinal": row["next_ordinal"],
            }
            for row in conn.execute("SELECT * FROM source_cursor ORDER BY stream")
        }

    def _ready_connection(self) -> tuple[sqlite3.Connection | None, AdmissionIndexResult | None]:
        try:
            conn = self._connect(create=False, readonly=True)
            progress = self._progress(conn)
            if set(progress) != set(STREAMS) or not all(v["complete"] for v in progress.values()):
                conn.close()
                return None, AdmissionIndexResult(
                    False, "catching_up", None, progress, 0, "admission_index_catching_up",
                )
            if conn.execute("SELECT 1 FROM dependency_refresh LIMIT 1").fetchone() is not None:
                conn.close()
                return None, AdmissionIndexResult(
                    False, "catching_up", None, progress, 0, "dependent_refresh_pending",
                )
            for row in conn.execute("SELECT * FROM source_cursor"):
                stat = self._source_stat(row["stream"])
                current = (
                    stat.st_dev if stat else 0, stat.st_ino if stat else 0,
                    stat.st_size if stat else 0, stat.st_mtime_ns if stat else 0,
                )
                if current != (row["dev"], row["ino"], row["eof_size"], row["mtime_ns"]):
                    conn.close()
                    return None, AdmissionIndexResult(
                        False, "catching_up", None, progress, 0, "admission_index_catching_up",
                    )
                if stat is not None:
                    fd = self._open_source(row["stream"])
                    if fd is None:
                        conn.close()
                        return None, AdmissionIndexResult(
                            False, "invalid", None, progress, 0, "source_snapshot_changed",
                        )
                    try:
                        boundary, _ = self._boundary(fd, int(row["offset"]))
                    finally:
                        os.close(fd)
                    if boundary != row["boundary_sha256"]:
                        conn.close()
                        return None, AdmissionIndexResult(
                            False, "invalid", None, progress, 0, "source_boundary_changed",
                        )
            return conn, None
        except (OSError, sqlite3.Error) as exc:
            return None, AdmissionIndexResult(False, "invalid", None, {}, 0, str(exc))

    def read_plan(self, *, candidate_limit: int) -> AdmissionIndexResult:
        conn, error = self._ready_connection()
        if error is not None:
            return error
        assert conn is not None
        limit = max(1, int(candidate_limit))
        sqlite_vm_steps = 0
        def count_vm_step() -> int:
            nonlocal sqlite_vm_steps
            sqlite_vm_steps += 1
            return 0
        conn.set_progress_handler(count_vm_step, 1)
        try:
            summary = {row[0]: int(row[1]) for row in conn.execute(
                "SELECT key,value FROM admission_summary ORDER BY key",
            )}
            rows = conn.execute(
                "SELECT candidate_id,binding_json,source_decisions_json,attribution_gap_count "
                "FROM candidate_state WHERE suppression_reason IS NULL "
                "ORDER BY pressure DESC, created_at, candidate_id LIMIT ?", (limit,),
            ).fetchall()
            projected = [
                {
                    "candidate_id": row["candidate_id"],
                    "binding": json.loads(row["binding_json"]),
                    "source_decisions": json.loads(row["source_decisions_json"]),
                    "attribution_gap_count": row["attribution_gap_count"],
                }
                for row in rows
            ]
            progress = self._progress(conn)
            conn.set_progress_handler(None, 0)
            conn.close()
            value = {
                "eligible_count": summary.pop("eligible", 0),
                "suppressed_counts": summary,
                "projected": projected,
                "source_record_counts": {
                    stream: int(state.get("record_count", 0))
                    for stream, state in progress.items()
                },
            }
            return AdmissionIndexResult(
                True, "ready", None, progress, 0, records_consumed=0,
                query_rows=len(summary) + len(rows), materialized_rows=len(rows),
                sqlite_vm_steps=sqlite_vm_steps, value=value,
            )
        except (sqlite3.Error, json.JSONDecodeError) as exc:
            conn.set_progress_handler(None, 0)
            conn.close()
            return AdmissionIndexResult(
                False, "invalid", None, {}, 0, str(exc),
                sqlite_vm_steps=sqlite_vm_steps,
            )

    def read_binding(self, candidate_id: str) -> AdmissionIndexResult:
        conn, error = self._ready_connection()
        if error is not None:
            return error
        assert conn is not None
        row = conn.execute(
            "SELECT binding_json,suppression_reason FROM candidate_state WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()
        progress = self._progress(conn)
        conn.close()
        value = None if row is None or row["binding_json"] is None else json.loads(row["binding_json"])
        return AdmissionIndexResult(
            True, "ready", None, progress, 0, query_rows=int(row is not None),
            materialized_rows=int(value is not None), value=value,
            reason="source_candidate_unavailable" if value is None else None,
        )

    def read_dispositions(self, binding: dict) -> AdmissionIndexResult:
        conn, error = self._ready_connection()
        if error is not None:
            return error
        assert conn is not None
        current = dict(binding)
        rows, reason, gaps = self._prior_rows(conn, current)
        progress = self._progress(conn)
        conn.close()
        return AdmissionIndexResult(
            True, "ready", None, progress, 0,
            query_rows=len(rows), materialized_rows=len(rows),
            value={"rows": rows, "reason": reason, "attribution_gap_count": gaps},
        )

    def read_context(self, candidate_id: str) -> AdmissionIndexResult:
        conn, error = self._ready_connection()
        if error is not None:
            return error
        assert conn is not None
        generation = self._current_generation(conn, "candidates")
        row = conn.execute(
            "SELECT p.prompt_projection_json,s.source_decisions_json "
            "FROM candidate_projection p JOIN candidate_state s USING(candidate_id) "
            "WHERE p.candidate_id=? AND p.generation=?",
            (candidate_id, generation),
        ).fetchone()
        if row is None:
            progress = self._progress(conn)
            conn.close()
            return AdmissionIndexResult(
                True, "ready", None, progress, 0, "source_candidate_unavailable", value=None,
            )
        candidate = json.loads(row[0])
        events = []
        event_generation = self._current_generation(conn, "events")
        for event_id in candidate.get("event_ids") or []:
            event = conn.execute(
                "SELECT projection_json FROM event_join WHERE event_id=? AND generation=?",
                (event_id, event_generation),
            ).fetchone()
            if event is not None:
                events.append(json.loads(event[0]))
        progress = self._progress(conn)
        conn.close()
        value = {
            "candidate": candidate, "events": events,
            "source_decisions": json.loads(row[1]),
        }
        return AdmissionIndexResult(
            True, "ready", None, progress, 0, query_rows=1 + len(events),
            materialized_rows=1 + len(events), value=value,
        )

    def read_snapshot(self) -> AdmissionIndexResult:
        """Compatibility API; intentionally unavailable on a ready large index."""
        conn, error = self._ready_connection()
        if error is not None:
            return error
        assert conn is not None
        progress = self._progress(conn)
        conn.close()
        return AdmissionIndexResult(
            False, "ready", None, progress, 0, "bounded_index_requires_targeted_read",
        )


def bounded_cacheless_snapshot(
    store, *, scan_bytes: int, claim_parser, fingerprint,
) -> AdmissionIndexResult:
    """Read a small exact snapshot without creating cache state."""
    remaining = max(1, int(scan_bytes))
    consumed = records = 0
    snapshot: dict[str, list[dict]] = {stream: [] for stream in STREAMS}
    opening = {}
    try:
        for stream in STREAMS:
            path = store.paths[stream]
            try:
                stat = path.stat(follow_symlinks=False)
            except FileNotFoundError:
                stat = None
            opening[stream] = stat
            size = stat.st_size if stat else 0
            if size > remaining:
                return AdmissionIndexResult(
                    False, "catching_up", None, {}, consumed,
                    "admission_index_catching_up", records,
                )
            if not stat:
                continue
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                raw = os.pread(fd, size, 0)
            finally:
                os.close(fd)
            consumed += len(raw)
            remaining -= len(raw)
            if raw and not raw.endswith(b"\n"):
                return AdmissionIndexResult(
                    False, "invalid", None, {}, consumed, f"{stream}:incomplete_record", records,
                )
            for line in raw.splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError(f"{stream}:non_object_record")
                if stream == "signals":
                    snapshot[stream].append(_signal_projection(row, store.instance, claim_parser))
                elif stream == "events":
                    snapshot[stream].append(_event_projection(row))
                elif stream == "candidates":
                    snapshot[stream].append(_candidate_projection(row, fingerprint))
                else:
                    snapshot[stream].append(_decision_projection(row))
                records += 1
        for stream, before in opening.items():
            try:
                after = store.paths[stream].stat(follow_symlinks=False)
            except FileNotFoundError:
                after = None
            if (before is None) != (after is None) or (
                before and after and
                (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            ):
                return AdmissionIndexResult(
                    False, "invalid", None, {}, consumed, "source_snapshot_changed", records,
                )
        return AdmissionIndexResult(
            True, "ready", snapshot, {}, consumed, records_consumed=records,
            materialized_rows=records,
        )
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return AdmissionIndexResult(False, "invalid", None, {}, consumed, str(exc), records)
