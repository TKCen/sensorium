"""Disposable bounded current-state materialization for Desktop presentation."""
from __future__ import annotations

import errno
import hashlib
import json
import os
import sqlite3
import stat as stat_module
from dataclasses import dataclass

STREAMS = ("candidates", "outbox")
SCHEMA_VERSION = 1
DEFAULT_SCAN_BYTES = 1024 * 1024
DEFAULT_SCAN_RECORDS = 2000
MAX_RECORD_BYTES = 1024 * 1024
SQLITE_TIMEOUT_SECONDS = 2.0
_CURRENT_CANDIDATE_STATUSES = {
    "candidate", "held", "in_conscious_aperture", "blocked", "error",
}
_CURRENT_OUTBOX_STATUSES = {"prepared", "failed"}
_KNOWN_CANDIDATE_STATUSES = {
    "archived", "candidate", "held", "in_conscious_aperture", "reviewed",
    "suppressed", "cancelled", "prepared_external_work", "blocked", "error",
}
_KNOWN_OUTBOX_STATUSES = {
    "prepared", "failed", "dispatched", "delivered", "cancelled", "expired",
    "rejected", "settled", "partially_presented_foreground",
}


@dataclass(frozen=True)
class DesktopProjectionIndexResult:
    complete: bool
    state: str
    rows: dict[str, list[dict]] | None
    progress: dict[str, dict]
    bytes_consumed: int = 0
    records_consumed: int = 0
    sqlite_vm_steps: int = 0
    reason: str | None = None
    mtimes_ns: tuple[int, ...] = ()


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _content_length(row: dict) -> int | None:
    preview = row.get("message_preview")
    digest = str(row.get("content_hash") or "").lower()
    if not isinstance(preview, str) or not preview or len(digest) not in {16, 64}:
        return None
    actual = hashlib.sha256(preview.encode()).hexdigest()
    if digest != (actual if len(digest) == 64 else actual[:16]):
        return None
    for key in ("content_length", "message_chars"):
        if key in row:
            try:
                if int(row[key]) != len(preview):
                    return None
            except (TypeError, ValueError):
                return None
    return len(preview)


def _candidate_projection(row: dict) -> dict:
    aperture = row.get("conscious_aperture")
    safe_aperture = None
    if isinstance(aperture, dict):
        safe_aperture = {
            key: aperture.get(key)
            for key in ("id", "state", "opened_at", "lease_expires_at", "settled_at")
            if key in aperture
        }
    return {
        "id": row.get("id"),
        "status": row.get("status"),
        "kind": row.get("kind"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "opened_at": row.get("opened_at"),
        "conscious_task": {} if isinstance(row.get("conscious_task"), dict) else None,
        "conscious_aperture": safe_aperture,
    }


def _outbox_projection(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "status": row.get("status"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "origin_thread_id": row.get("origin_thread_id"),
        "origin_candidate_id": row.get("origin_candidate_id"),
        "surface": row.get("surface"),
        "delivery_mode": row.get("delivery_mode"),
        "target": row.get("target"),
        "allowed_surfaces": row.get("allowed_surfaces"),
        "content_hash": row.get("content_hash"),
        "delivery_state": row.get("delivery_state"),
        "_verified_content_length": _content_length(row),
    }


class DesktopProjectionIndex:
    """Incrementally materialize only current Desktop-owned candidate/outbox rows."""

    def __init__(self, store):
        self.store = store
        self.path = store.root / "inner_life" / "desktop_projection.sqlite3"

    def _path_safe(self, *, create: bool) -> None:
        root = self.store.root
        if root.is_symlink():
            raise OSError(errno.ELOOP, "symlinked instance root")
        parent = self.path.parent
        if parent.exists() and parent.is_symlink():
            raise OSError(errno.ELOOP, "symlinked projection directory")
        if self.path.exists() and self.path.is_symlink():
            raise OSError(errno.ELOOP, "symlinked projection index")
        if create:
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(parent, 0o700)

    def _connect(self, *, create: bool, readonly: bool = False) -> sqlite3.Connection:
        self._path_safe(create=create)
        if readonly:
            conn = sqlite3.connect(
                f"file:{self.path}?mode=ro", uri=True, timeout=SQLITE_TIMEOUT_SECONDS,
            )
            conn.execute("PRAGMA query_only=ON")
        else:
            conn = sqlite3.connect(self.path, timeout=SQLITE_TIMEOUT_SECONDS)
            os.chmod(self.path, 0o600)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={int(SQLITE_TIMEOUT_SECONDS * 1000)}")
        return conn

    @staticmethod
    def _schema(conn: sqlite3.Connection) -> None:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version not in {0, SCHEMA_VERSION}:
            raise sqlite3.DatabaseError("unsupported desktop projection schema")
        conn.executescript("""
        PRAGMA journal_mode=DELETE;
        PRAGMA synchronous=FULL;
        CREATE TABLE IF NOT EXISTS source_cursor (
          stream TEXT PRIMARY KEY CHECK(stream IN ('candidates','outbox')),
          dev INTEGER NOT NULL, ino INTEGER NOT NULL, offset INTEGER NOT NULL,
          eof_size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
          generation INTEGER NOT NULL, complete INTEGER NOT NULL CHECK(complete IN (0,1)),
          next_ordinal INTEGER NOT NULL, record_count INTEGER NOT NULL,
          partial BLOB NOT NULL
        );
        CREATE TABLE IF NOT EXISTS current_row (
          stream TEXT NOT NULL, generation INTEGER NOT NULL, row_key TEXT NOT NULL,
          source_ordinal INTEGER NOT NULL, row_json TEXT NOT NULL,
          PRIMARY KEY(stream,generation,row_key)
        );
        CREATE INDEX IF NOT EXISTS ix_desktop_current
          ON current_row(stream,generation,source_ordinal);
        PRAGMA user_version=1;
        """)

    def _source_stat(self, stream: str) -> os.stat_result | None:
        try:
            value = self.store.paths[stream].stat(follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat_module.S_ISREG(value.st_mode):
            raise OSError(errno.EINVAL, f"{stream}:source_not_regular")
        return value

    def _open_source(self, stream: str) -> int | None:
        try:
            fd = os.open(self.store.paths[stream], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return None
        if not stat_module.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise OSError(errno.EINVAL, f"{stream}:source_not_regular")
        return fd

    @staticmethod
    def _progress(conn: sqlite3.Connection) -> dict[str, dict]:
        return {
            row["stream"]: {
                "offset": row["offset"], "eof_size": row["eof_size"],
                "complete": bool(row["complete"]), "generation": row["generation"],
                "record_count": row["record_count"], "next_ordinal": row["next_ordinal"],
                "partial_bytes": len(row["partial"]),
            }
            for row in conn.execute("SELECT * FROM source_cursor ORDER BY stream")
        }

    @staticmethod
    def _apply_row(conn: sqlite3.Connection, stream: str, generation: int, ordinal: int, row: dict) -> None:
        status = row.get("status")
        allowed = _KNOWN_CANDIDATE_STATUSES if stream == "candidates" else _KNOWN_OUTBOX_STATUSES
        current = _CURRENT_CANDIDATE_STATUSES if stream == "candidates" else _CURRENT_OUTBOX_STATUSES
        if not isinstance(status, str) or status not in allowed:
            raise ValueError(f"{stream}:unknown_status")
        identifier = row.get("id")
        row_key = f"id:{identifier}" if isinstance(identifier, str) and identifier else f"ordinal:{ordinal}"
        if status not in current:
            conn.execute(
                "DELETE FROM current_row WHERE stream=? AND generation=? AND row_key=?",
                (stream, generation, row_key),
            )
            return
        projection = _candidate_projection(row) if stream == "candidates" else _outbox_projection(row)
        conn.execute(
            "INSERT OR REPLACE INTO current_row VALUES (?,?,?,?,?)",
            (stream, generation, row_key, ordinal, _json(projection)),
        )

    def prepare(
        self, *, scan_bytes: int = DEFAULT_SCAN_BYTES,
        scan_records: int = DEFAULT_SCAN_RECORDS,
    ) -> DesktopProjectionIndexResult:
        byte_budget = max(1, int(scan_bytes))
        record_budget = max(1, int(scan_records))
        consumed = records = vm_steps = 0
        conn: sqlite3.Connection | None = None
        rebuilding = not self.path.exists()
        try:
            conn = self._connect(create=True)
            self._schema(conn)
            def count_vm_step() -> int:
                nonlocal vm_steps
                vm_steps += 1
                return 0
            conn.set_progress_handler(count_vm_step, 1)
            conn.execute("BEGIN IMMEDIATE")
            opening = {stream: self._source_stat(stream) for stream in STREAMS}
            for stream in STREAMS:
                source_stat = opening[stream]
                size = int(source_stat.st_size) if source_stat else 0
                cursor = conn.execute(
                    "SELECT * FROM source_cursor WHERE stream=?", (stream,),
                ).fetchone()
                generation = int(cursor["generation"]) if cursor else 1
                reset = cursor is None
                if cursor is not None:
                    identity = (int(source_stat.st_dev), int(source_stat.st_ino)) if source_stat else (0, 0)
                    prior_identity = (int(cursor["dev"]), int(cursor["ino"]))
                    created_from_empty = prior_identity == (0, 0) and int(cursor["offset"]) == 0 and source_stat is not None
                    if (not created_from_empty and identity != prior_identity) or size < int(cursor["offset"]):
                        reset = True
                    elif size == int(cursor["eof_size"]) and source_stat and int(source_stat.st_mtime_ns) != int(cursor["mtime_ns"]):
                        reset = True
                if reset:
                    rebuilding = True
                    generation += int(cursor is not None)
                    offset = ordinal = record_count = 0
                    partial = b""
                else:
                    assert cursor is not None
                    offset = int(cursor["offset"])
                    ordinal = int(cursor["next_ordinal"])
                    record_count = int(cursor["record_count"])
                    partial = bytes(cursor["partial"])
                fd = self._open_source(stream)
                try:
                    remaining_bytes = byte_budget - consumed
                    remaining_records = record_budget - records
                    if offset < size and remaining_bytes > 0 and remaining_records > 0 and fd is not None:
                        chunk = os.pread(fd, min(size - offset, remaining_bytes), offset)
                        consumed += len(chunk)
                        combined = partial + chunk
                        start = offset - len(partial)
                        cursor_in_combined = 0
                        parsed_count = 0
                        while parsed_count < remaining_records:
                            newline = combined.find(b"\n", cursor_in_combined)
                            if newline < 0:
                                break
                            raw = combined[cursor_in_combined:newline]
                            if len(raw) > MAX_RECORD_BYTES:
                                raise ValueError(f"{stream}:record_too_large")
                            cursor_in_combined = newline + 1
                            if not raw.strip():
                                continue
                            value = json.loads(raw)
                            if not isinstance(value, dict):
                                raise ValueError(f"{stream}:non_object_record")
                            self._apply_row(conn, stream, generation, ordinal, value)
                            ordinal += 1
                            record_count += 1
                            records += 1
                            parsed_count += 1
                        if parsed_count >= remaining_records and cursor_in_combined < len(combined):
                            offset = start + cursor_in_combined
                            partial = b""
                        else:
                            partial = combined[cursor_in_combined:]
                            offset += len(chunk)
                        if len(partial) > MAX_RECORD_BYTES or (
                            len(partial) == MAX_RECORD_BYTES and offset < size
                        ):
                            raise ValueError(f"{stream}:record_too_large")
                    if offset == size and partial:
                        raise ValueError(f"{stream}:incomplete_record")
                    complete = offset == size and not partial
                    dev, ino = (
                        (int(source_stat.st_dev), int(source_stat.st_ino))
                        if source_stat else (0, 0)
                    )
                    conn.execute(
                        "INSERT OR REPLACE INTO source_cursor VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (stream, dev, ino, offset, size,
                         int(source_stat.st_mtime_ns) if source_stat else 0,
                         generation, int(complete), ordinal, record_count, partial),
                    )
                finally:
                    if fd is not None:
                        os.close(fd)
            closing = {stream: self._source_stat(stream) for stream in STREAMS}
            stable = all(
                (a is None and b is None) or (
                    a is not None and b is not None and
                    (a.st_dev, a.st_ino, a.st_size, a.st_mtime_ns) ==
                    (b.st_dev, b.st_ino, b.st_size, b.st_mtime_ns)
                )
                for a, b in zip(opening.values(), closing.values())
            )
            conn.commit()
            progress = self._progress(conn)
            complete = stable and set(progress) == set(STREAMS) and all(
                item["complete"] for item in progress.values()
            )
            conn.set_progress_handler(None, 0)
            conn.close()
            return DesktopProjectionIndexResult(
                complete=complete,
                state="ready" if complete else ("rebuilding" if rebuilding else "catching_up"),
                rows=None,
                progress=progress,
                bytes_consumed=consumed,
                records_consumed=records,
                sqlite_vm_steps=vm_steps,
                reason=None if stable else "source_snapshot_changed",
            )
        except (OSError, sqlite3.Error, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            try:
                if conn is not None:
                    conn.rollback()
                    conn.close()
            except Exception:
                pass
            if isinstance(exc, sqlite3.DatabaseError):
                try:
                    self._path_safe(create=False)
                    if self.path.is_file() and not self.path.is_symlink():
                        self.path.unlink()
                except OSError:
                    pass
            return DesktopProjectionIndexResult(
                False, "invalid", None, {}, consumed, records, vm_steps, str(exc),
            )

    def read(self) -> DesktopProjectionIndexResult:
        """Read exact current rows without creating or advancing derived state."""
        conn: sqlite3.Connection | None = None
        vm_steps = 0
        try:
            conn = self._connect(create=False, readonly=True)
            if int(conn.execute("PRAGMA user_version").fetchone()[0]) != SCHEMA_VERSION:
                raise sqlite3.DatabaseError("unsupported desktop projection schema")
            progress = self._progress(conn)
            if set(progress) != set(STREAMS) or not all(item["complete"] for item in progress.values()):
                conn.close()
                return DesktopProjectionIndexResult(
                    False, "catching_up", None, progress, reason="desktop_projection_catching_up",
                )
            mtimes: list[int] = []
            for stream in STREAMS:
                source_stat = self._source_stat(stream)
                current = (
                    int(source_stat.st_dev) if source_stat else 0,
                    int(source_stat.st_ino) if source_stat else 0,
                    int(source_stat.st_size) if source_stat else 0,
                    int(source_stat.st_mtime_ns) if source_stat else 0,
                )
                stored = conn.execute(
                    "SELECT dev,ino,eof_size,mtime_ns FROM source_cursor WHERE stream=?",
                    (stream,),
                ).fetchone()
                if stored is None or current != tuple(int(value) for value in stored):
                    conn.close()
                    return DesktopProjectionIndexResult(
                        False, "catching_up", None, progress,
                        reason="desktop_projection_catching_up",
                    )
                if source_stat:
                    mtimes.append(int(source_stat.st_mtime_ns))
            def count_vm_step() -> int:
                nonlocal vm_steps
                vm_steps += 1
                return 0
            conn.set_progress_handler(count_vm_step, 1)
            rows: dict[str, list[dict]] = {}
            total = 0
            for stream in STREAMS:
                generation = progress[stream]["generation"]
                selected = conn.execute(
                    "SELECT row_json FROM current_row WHERE stream=? AND generation=? "
                    "ORDER BY source_ordinal",
                    (stream, generation),
                ).fetchall()
                rows[stream] = [json.loads(row[0]) for row in selected]
                total += len(selected)
            conn.set_progress_handler(None, 0)
            conn.close()
            return DesktopProjectionIndexResult(
                True, "ready", rows, progress, sqlite_vm_steps=vm_steps,
                records_consumed=0, bytes_consumed=0, mtimes_ns=tuple(mtimes),
            )
        except (OSError, sqlite3.Error, ValueError, json.JSONDecodeError) as exc:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
            return DesktopProjectionIndexResult(
                False, "invalid", None, {}, sqlite_vm_steps=vm_steps, reason=str(exc),
            )
