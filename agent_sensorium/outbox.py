"""Deprecated Sensorium-local outbox compatibility layer.

Kanban remains the live activation/ticketing substrate. Outbox records remain
compact compatibility receipts for existing thread capsules, with one bounded
local Conscious-consumer exception: authored local reach-outs may be prepared
here without creating a thread or dispatching work.

Safety defaults:
- All direct/replyable Discord modes disabled by default
- Dry-run is the default for prepare
- No live Discord API calls unless explicitly enabled + authorized
- Idempotent: same request parameters produce the same outbox record
"""

import hashlib
import json
import os
import sqlite3
from copy import deepcopy

from .schemas import new_id, truncate_text, utc_now_iso
from .store import SensoriumStore

VALID_REQUEST_TYPES = {"REACH_OUT", "PRIVATE_EXPRESSION", "THINK"}
VALID_SURFACES = {"discord", "local"}
VALID_DELIVERY_MODES = {
    "peripheral_reference",
    "context_pointer",
    "discord_channel_thread",
    "discord_dm_bound_session",
}
DIRECT_DELIVERY_MODES = {"discord_channel_thread", "discord_dm_bound_session"}
OPENABLE_THREAD_STATUSES = {"dormant", "held"}
OUTBOX_INDEX_CATCHUP_BYTES = 1024 * 1024
OUTBOX_INDEX_CATCHUP_RECORDS = 2000
OUTBOX_INDEX_MAX_RECORD_BYTES = 1024 * 1024
_OUTBOX_INDEX_VERSION = 2

OUTBOX_DEFAULTS: dict = {
    "enabled": True,
    "default_delivery_mode": "peripheral_reference",
    "direct_modes_enabled": False,
    "allowed_delivery_modes": ["peripheral_reference", "context_pointer"],
    "discord": {
        "enabled": False,
        "token_env": "DISCORD_TOKEN",
        "default_auto_archive_duration": 1440,
    },
}


def _merged_outbox_config(config: dict | None = None) -> dict:
    cfg = deepcopy(OUTBOX_DEFAULTS)
    if not config:
        return cfg
    for key, value in config.items():
        if isinstance(value, dict) and isinstance(cfg.get(key), dict):
            cfg[key].update(value)
        else:
            cfg[key] = value
    return cfg


def _compute_idempotency_key(
    *,
    origin_thread_id: str,
    delivery_mode: str,
    target: dict,
    content_hash: str,
) -> str:
    parts = [
        origin_thread_id,
        delivery_mode,
        json.dumps(target, sort_keys=True, separators=(",", ":")),
        content_hash,
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:24]


def source_revision_key(
    *,
    candidate_id: str,
    source_candidate_ids: list[str] | None = None,
    source_candidate_fingerprint: str = "",
) -> str:
    """Return an idempotency identity for one candidate source revision."""
    source_ids = [str(value) for value in (source_candidate_ids or []) if str(value)]
    fingerprint = str(source_candidate_fingerprint or "")
    if source_ids and fingerprint:
        material = {"source_candidate_ids": source_ids, "source_candidate_fingerprint": fingerprint}
    else:
        material = {"candidate_id": str(candidate_id or "")}
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(("source-revision|" + encoded).encode()).hexdigest()[:24]


def _find_thread_by_id(threads: list[dict], thread_id: str) -> dict | None:
    for t in threads:
        if t.get("id") == thread_id:
            return t
    return None


def _find_existing_outbox_request(
    requests: list[dict], idempotency_key: str
) -> dict | None:
    for req in requests:
        if req.get("idempotency_key") == idempotency_key:
            return req
    return None


def _indexed_outbox_request(
    store: SensoriumStore,
    idempotency_key: str,
    *,
    scan_bytes: int = OUTBOX_INDEX_CATCHUP_BYTES,
    metrics: dict[str, int] | None = None,
) -> tuple[dict | None, str | None]:
    """Return one exact row through a bounded disposable JSONL index.

    Canonical JSONL remains authoritative. Missing or stale indexes catch up
    under the profile outbox lock within one fixed byte budget and fail closed
    until ready. Malformed source and duplicate keys never authorize an append.
    """
    canonical = store.paths["outbox"]
    index_path = store.root / "inner_life" / "outbox_index.sqlite3"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    source_bytes = records_consumed = sqlite_vm_steps = 0
    try:
        with sqlite3.connect(index_path) as conn:
            def count_vm_step() -> int:
                nonlocal sqlite_vm_steps
                sqlite_vm_steps += 1
                return 0

            conn.set_progress_handler(count_vm_step, 1)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS metadata "
                "(singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL, "
                "device INTEGER NOT NULL, inode INTEGER NOT NULL, offset INTEGER NOT NULL)"
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(metadata)")}
            for name, declaration in (
                ("eof_size", "INTEGER NOT NULL DEFAULT 0"),
                ("mtime_ns", "INTEGER NOT NULL DEFAULT 0"),
                ("generation", "INTEGER NOT NULL DEFAULT 0"),
                ("partial", "BLOB NOT NULL DEFAULT X''"),
                ("error", "TEXT"),
            ):
                if name not in columns:
                    conn.execute(f"ALTER TABLE metadata ADD COLUMN {name} {declaration}")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS entries_v2 "
                "(generation INTEGER NOT NULL, idempotency_key TEXT NOT NULL, "
                "row_json TEXT NOT NULL, PRIMARY KEY(generation,idempotency_key))"
            )
            source_stat = canonical.stat() if canonical.exists() else None
            identity = (
                (int(source_stat.st_dev), int(source_stat.st_ino))
                if source_stat is not None else (0, 0)
            )
            size = int(source_stat.st_size) if source_stat is not None else 0
            mtime_ns = int(source_stat.st_mtime_ns) if source_stat is not None else 0
            meta = conn.execute(
                "SELECT version,device,inode,offset,eof_size,mtime_ns,generation,partial,error "
                "FROM metadata WHERE singleton=1"
            ).fetchone()
            reset = meta is None
            if meta is not None:
                created_from_empty = (
                    (int(meta[1]), int(meta[2])) == (0, 0)
                    and int(meta[3]) == 0 and source_stat is not None
                )
                reset = (
                    int(meta[0]) != _OUTBOX_INDEX_VERSION
                    or (not created_from_empty and (int(meta[1]), int(meta[2])) != identity)
                    or int(meta[3]) > size
                    or (
                        int(meta[4]) == size and int(meta[5]) != mtime_ns
                    )
                )
            generation = (int(meta[6]) if meta is not None else 0) + int(reset)
            if generation <= 0:
                generation = 1
            if not reset:
                assert meta is not None
            offset = 0 if reset else int(meta[3])
            partial = b"" if reset else bytes(meta[7])
            prior_error = None if reset else meta[8]

            def save(error: str | None) -> None:
                conn.execute(
                    "INSERT OR REPLACE INTO metadata"
                    "(singleton,version,device,inode,offset,eof_size,mtime_ns,generation,partial,error) "
                    "VALUES(1,?,?,?,?,?,?,?,?,?)",
                    (_OUTBOX_INDEX_VERSION, identity[0], identity[1], offset, size,
                     mtime_ns, generation, partial, error),
                )
                conn.commit()
                if metrics is not None:
                    metrics.update(
                        source_bytes=source_bytes,
                        records_consumed=records_consumed,
                        sqlite_vm_steps=sqlite_vm_steps,
                        generation=generation,
                    )

            if prior_error:
                return None, str(prior_error)
            opening = identity + (size, mtime_ns)
            if offset < size:
                fd = os.open(canonical, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                try:
                    chunk = os.pread(fd, min(size - offset, max(1, int(scan_bytes))), offset)
                finally:
                    os.close(fd)
                source_bytes += len(chunk)
                combined = partial + chunk
                record_start = offset - len(partial)
                position = records = 0
                while records < OUTBOX_INDEX_CATCHUP_RECORDS:
                    newline = combined.find(b"\n", position)
                    if newline < 0:
                        break
                    raw = combined[position:newline]
                    if len(raw) > OUTBOX_INDEX_MAX_RECORD_BYTES:
                        offset += len(chunk)
                        partial = combined[position:]
                        save("outbox_index_record_too_large")
                        return None, "outbox_index_record_too_large"
                    position = newline + 1
                    if not raw.strip():
                        continue
                    try:
                        row = json.loads(raw)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        offset += len(chunk)
                        partial = combined[position:]
                        save("outbox_index_source_corrupt")
                        return None, "outbox_index_source_corrupt"
                    if not isinstance(row, dict):
                        offset += len(chunk)
                        partial = combined[position:]
                        save("outbox_index_source_corrupt")
                        return None, "outbox_index_source_corrupt"
                    key = row.get("idempotency_key")
                    if isinstance(key, str) and key:
                        encoded = json.dumps(row, sort_keys=True, separators=(",", ":"))
                        previous = conn.execute(
                            "SELECT row_json FROM entries_v2 "
                            "WHERE generation=? AND idempotency_key=?",
                            (generation, key),
                        ).fetchone()
                        if previous is not None and previous[0] != encoded:
                            offset += len(chunk)
                            partial = combined[position:]
                            save("outbox_duplicate_idempotency_key")
                            return None, "outbox_duplicate_idempotency_key"
                        conn.execute(
                            "INSERT OR REPLACE INTO entries_v2 VALUES(?,?,?)",
                            (generation, key, encoded),
                        )
                    records += 1
                    records_consumed += 1
                if records >= OUTBOX_INDEX_CATCHUP_RECORDS and position < len(combined):
                    offset = record_start + position
                    partial = b""
                else:
                    offset += len(chunk)
                    partial = combined[position:]
                if len(partial) > OUTBOX_INDEX_MAX_RECORD_BYTES or (
                    len(partial) == OUTBOX_INDEX_MAX_RECORD_BYTES and offset < size
                ):
                    save("outbox_index_record_too_large")
                    return None, "outbox_index_record_too_large"
            if offset == size and partial:
                save("outbox_index_source_corrupt")
                return None, "outbox_index_source_corrupt"
            closing_stat = canonical.stat() if canonical.exists() else None
            closing = (
                int(closing_stat.st_dev) if closing_stat else 0,
                int(closing_stat.st_ino) if closing_stat else 0,
                int(closing_stat.st_size) if closing_stat else 0,
                int(closing_stat.st_mtime_ns) if closing_stat else 0,
            )
            save(None)
            if closing != opening or offset < size:
                return None, "outbox_index_catching_up"
            found = conn.execute(
                "SELECT row_json FROM entries_v2 "
                "WHERE generation=? AND idempotency_key=?",
                (generation, idempotency_key),
            ).fetchone()
            if metrics is not None:
                metrics["sqlite_vm_steps"] = sqlite_vm_steps
            return (json.loads(found[0]) if found is not None else None), None
    except (OSError, sqlite3.DatabaseError):
        return None, "outbox_index_unavailable"


def _rewrite_jsonl(store: SensoriumStore, name: str, items: list[dict]) -> None:
    store.rewrite_jsonl(name, items)


def _denied(reason: str, detail: str, *, thread_id: str = "") -> dict:
    return {
        "success": False,
        "error": reason,
        "detail": detail,
        "thread_id": thread_id,
    }


def prepare_local_outbox_request(
    store: SensoriumStore,
    *,
    origin_candidate_id: str,
    request_type: str,
    surface: str,
    delivery_mode: str,
    target: dict,
    title: str = "",
    message_preview: str = "",
    content_hash: str = "",
    sensitivity: str = "private",
    allowed_surfaces: list[str] | None = None,
    source_candidate_ids: list[str] | None = None,
    source_candidate_fingerprint: str = "",
    dry_run: bool = False,
    _locked: bool = False,
) -> dict:
    """Prepare a local outbox record without creating a thread.

    This is the narrow outbox path for the bounded Conscious consumer. It keeps
    authored content in the existing outbox content owner while using the
    originating candidate as provenance. It never creates threads, workers, or
    delivery requests.
    """
    if not dry_run and not _locked:
        with store.outbox_transaction():
            return prepare_local_outbox_request(
                store,
                origin_candidate_id=origin_candidate_id,
                request_type=request_type,
                surface=surface,
                delivery_mode=delivery_mode,
                target=target,
                title=title,
                message_preview=message_preview,
                content_hash=content_hash,
                sensitivity=sensitivity,
                allowed_surfaces=allowed_surfaces,
                source_candidate_ids=source_candidate_ids,
                source_candidate_fingerprint=source_candidate_fingerprint,
                dry_run=False,
                _locked=True,
            )
    candidate_id = str(origin_candidate_id or "").strip()
    if not candidate_id:
        return _denied("origin_candidate_required", "A source candidate is required.")
    if request_type not in VALID_REQUEST_TYPES:
        return _denied("invalid_request_type", f"Invalid request_type: {request_type}")
    if surface not in VALID_SURFACES:
        return _denied("invalid_surface", f"Invalid surface: {surface}")
    if surface != "local":
        return _denied("invalid_surface", "Local preparation requires surface='local'.")
    if delivery_mode not in VALID_DELIVERY_MODES:
        return _denied("invalid_delivery_mode", f"Invalid delivery_mode: {delivery_mode}")
    if delivery_mode != "context_pointer":
        return _denied("invalid_delivery_mode", "Local preparation requires delivery_mode='context_pointer'.")
    if delivery_mode in DIRECT_DELIVERY_MODES:
        return _denied("direct_modes_disabled", "The local consumer cannot prepare direct delivery modes.")

    stored_title = truncate_text(title, 200) if title else ""
    stored_message = truncate_text(message_preview, 500) if message_preview else ""
    authored_content = stored_message or stored_title
    effective_content_hash = hashlib.sha256(authored_content.encode()).hexdigest()[:16]
    if content_hash and str(content_hash).lower() != effective_content_hash:
        return _denied(
            "content_hash_mismatch",
            "The supplied content hash does not match the exact authored content.",
        )
    content_length = len(authored_content)
    revision_key = source_revision_key(
        candidate_id=candidate_id,
        source_candidate_ids=source_candidate_ids,
        source_candidate_fingerprint=source_candidate_fingerprint,
    )
    idempotency_key = _compute_idempotency_key(
        origin_thread_id=candidate_id,
        delivery_mode=delivery_mode,
        target={},
        content_hash=f"{revision_key}:{request_type}:{effective_content_hash}",
    )
    if dry_run:
        existing = _find_existing_outbox_request(store.read_jsonl("outbox"), idempotency_key)
        index_error = None
    else:
        existing, index_error = _indexed_outbox_request(store, idempotency_key)
    if index_error:
        return _denied(index_error, "The bounded outbox index is not ready for a safe append.")
    if existing is not None:
        if (
            existing.get("origin_candidate_id") != candidate_id
            or existing.get("source_revision_key") != revision_key
            or existing.get("source_candidate_ids") != list(source_candidate_ids or [])
            or existing.get("source_candidate_fingerprint")
            != str(source_candidate_fingerprint or "")
            or existing.get("request_type") != request_type
            or existing.get("message_preview") != stored_message
            or str(existing.get("content_hash") or "").lower() != effective_content_hash
            or existing.get("content_length") != content_length
        ):
            return _denied(
                "idempotency_content_mismatch",
                "The existing local request does not match the exact source and authored content.",
            )
        return {"success": True, "data": existing, "idempotent_hit": True}

    now = utc_now_iso()
    request = {
        "id": new_id("obx"),
        "created_at": now,
        "updated_at": now,
        "status": "prepared",
        "origin_thread_id": "",
        "origin_candidate_id": candidate_id,
        "request_type": request_type,
        "surface": surface,
        "delivery_mode": delivery_mode,
        "target": {},
        "title": stored_title,
        "message_preview": stored_message,
        "media_refs": [],
        "content_hash": effective_content_hash,
        "content_length": content_length,
        "source_candidate_ids": list(source_candidate_ids or []),
        "source_candidate_fingerprint": str(source_candidate_fingerprint or ""),
        "source_revision_key": revision_key,
        "idempotency_key": idempotency_key,
        "sensitivity": sensitivity,
        "allowed_surfaces": ["local"],
        "platform_refs": {},
    }
    if dry_run:
        return {"success": True, "data": request, "dry_run": True}

    store.ensure_dirs()
    store.append_jsonl("outbox", request)
    receipt = {
        "ts": now,
        "type": "outbox.prepared",
        "outbox_id": request["id"],
        "origin_candidate_id": candidate_id,
        "delivery_mode": delivery_mode,
        "surface": surface,
        "request_type": request_type,
        "content_hash": effective_content_hash,
        "content_length": content_length,
        "idempotency_key": idempotency_key,
    }
    store.append_jsonl("decisions", receipt)
    return {"success": True, "data": request, "receipt": receipt}


def prepare_outbox_request(
    store: SensoriumStore,
    *,
    thread_id: str,
    request_type: str,
    surface: str,
    delivery_mode: str,
    target: dict,
    title: str = "",
    message_preview: str = "",
    media_refs: list[str] | None = None,
    content_hash: str = "",
    origin_candidate_id: str = "",
    config: dict | None = None,
    dry_run: bool = False,
    _locked: bool = False,
) -> dict:
    if not dry_run and not _locked:
        with store.outbox_transaction():
            return prepare_outbox_request(
                store,
                thread_id=thread_id,
                request_type=request_type,
                surface=surface,
                delivery_mode=delivery_mode,
                target=target,
                title=title,
                message_preview=message_preview,
                media_refs=media_refs,
                content_hash=content_hash,
                origin_candidate_id=origin_candidate_id,
                config=config,
                dry_run=False,
                _locked=True,
            )
    cfg = _merged_outbox_config(config)
    now = utc_now_iso()

    if not cfg.get("enabled"):
        return _denied("outbox_disabled", "Outbox is disabled in config.", thread_id=thread_id)

    if request_type not in VALID_REQUEST_TYPES:
        return _denied("invalid_request_type", f"Invalid request_type: {request_type}", thread_id=thread_id)

    if surface not in VALID_SURFACES:
        return _denied("invalid_surface", f"Invalid surface: {surface}", thread_id=thread_id)

    if delivery_mode not in VALID_DELIVERY_MODES:
        return _denied("invalid_delivery_mode", f"Invalid delivery_mode: {delivery_mode}", thread_id=thread_id)

    store.ensure_dirs()
    threads = store.read_jsonl("threads")
    thread = _find_thread_by_id(threads, thread_id)

    if thread is None:
        return _denied("thread_not_found", f"Thread '{thread_id}' not found.", thread_id=thread_id)

    thread_status = thread.get("status", "")
    if thread_status not in OPENABLE_THREAD_STATUSES:
        return _denied(
            "thread_not_openable",
            f"Thread '{thread_id}' is {thread_status}, not openable.",
            thread_id=thread_id,
        )

    thread_surfaces = set(thread.get("allowed_surfaces") or [])
    if surface not in thread_surfaces:
        receipt = {
            "ts": now,
            "type": "outbox.denied",
            "thread_id": thread_id,
            "reason": "surface_not_allowed",
            "detail": f"Surface '{surface}' not in thread allowed_surfaces {sorted(thread_surfaces)}",
            "delivery_mode": delivery_mode,
            "surface": surface,
        }
        if not dry_run:
            store.append_jsonl("decisions", receipt)
        return {
            "success": False,
            "error": "surface_not_allowed",
            "detail": receipt["detail"],
            "thread_id": thread_id,
            "receipt": receipt,
        }

    allowed_modes = set(cfg.get("allowed_delivery_modes") or [])
    if delivery_mode not in allowed_modes:
        receipt = {
            "ts": now,
            "type": "outbox.denied",
            "thread_id": thread_id,
            "reason": "delivery_mode_not_allowed",
            "detail": f"Delivery mode '{delivery_mode}' not in config allowed_delivery_modes {sorted(allowed_modes)}",
            "delivery_mode": delivery_mode,
        }
        if not dry_run:
            store.append_jsonl("decisions", receipt)
        return {
            "success": False,
            "error": "delivery_mode_not_allowed",
            "detail": receipt["detail"],
            "thread_id": thread_id,
            "receipt": receipt,
        }

    if delivery_mode in DIRECT_DELIVERY_MODES and not cfg.get("direct_modes_enabled"):
        receipt = {
            "ts": now,
            "type": "outbox.denied",
            "thread_id": thread_id,
            "reason": "direct_modes_disabled",
            "detail": f"Direct delivery mode '{delivery_mode}' requires direct_modes_enabled=true.",
            "delivery_mode": delivery_mode,
        }
        if not dry_run:
            store.append_jsonl("decisions", receipt)
        return {
            "success": False,
            "error": "direct_modes_disabled",
            "detail": receipt["detail"],
            "thread_id": thread_id,
            "receipt": receipt,
        }

    effective_content_hash = content_hash or hashlib.sha256(
        (message_preview or title or "").encode()
    ).hexdigest()[:16]

    idempotency_key = _compute_idempotency_key(
        origin_thread_id=thread_id,
        delivery_mode=delivery_mode,
        target=target,
        content_hash=effective_content_hash,
    )

    if dry_run:
        existing = _find_existing_outbox_request(store.read_jsonl("outbox"), idempotency_key)
        index_error = None
    else:
        existing, index_error = _indexed_outbox_request(store, idempotency_key)
    if index_error:
        return _denied(
            index_error,
            "The bounded outbox index is not ready for a safe append.",
            thread_id=thread_id,
        )
    if existing is not None:
        return {
            "success": True,
            "data": existing,
            "idempotent_hit": True,
        }

    outbox_id = new_id("obx")
    request = {
        "id": outbox_id,
        "created_at": now,
        "updated_at": now,
        "status": "prepared",
        "origin_thread_id": thread_id,
        "origin_candidate_id": origin_candidate_id or thread.get("origin_candidate_id", ""),
        "request_type": request_type,
        "surface": surface,
        "delivery_mode": delivery_mode,
        "target": target,
        "title": truncate_text(title, 200) if title else "",
        "message_preview": truncate_text(message_preview, 500) if message_preview else "",
        "media_refs": media_refs or [],
        "content_hash": effective_content_hash,
        "idempotency_key": idempotency_key,
        "sensitivity": thread.get("sensitivity", "private"),
        "allowed_surfaces": sorted(set(thread.get("allowed_surfaces") or []) & VALID_SURFACES),
        "platform_refs": {},
    }

    if dry_run:
        return {
            "success": True,
            "data": request,
            "dry_run": True,
        }

    store.append_jsonl("outbox", request)

    receipt = {
        "ts": now,
        "type": "outbox.prepared",
        "thread_id": thread_id,
        "outbox_id": outbox_id,
        "delivery_mode": delivery_mode,
        "surface": surface,
        "request_type": request_type,
        "idempotency_key": idempotency_key,
    }
    store.append_jsonl("decisions", receipt)

    thread.setdefault("interaction_refs", []).append({
        "type": "outbox_prepared",
        "outbox_id": outbox_id,
        "ts": now,
    })
    thread.setdefault("decision_log", []).append({
        "ts": now,
        "type": "outbox.prepared",
        "outbox_id": outbox_id,
        "delivery_mode": delivery_mode,
    })
    thread["updated_at"] = now
    _rewrite_jsonl(store, "threads", threads)

    return {
        "success": True,
        "data": request,
        "receipt": receipt,
    }


# --- Discord Adapter ---


class DiscordAdapter:
    """Abstract interface for Discord API operations."""

    def create_thread(
        self,
        *,
        channel_id: str,
        name: str,
        message_content: str,
        auto_archive_duration: int = 1440,
    ) -> dict:
        raise NotImplementedError

    def send_message(self, *, channel_id: str, content: str) -> dict:
        raise NotImplementedError


class FakeDiscordAdapter(DiscordAdapter):
    """Test double that records calls without network access."""

    def __init__(self, *, fail: bool = False):
        self.calls: list[dict] = []
        self._fail = fail

    def create_thread(
        self,
        *,
        channel_id: str,
        name: str,
        message_content: str,
        auto_archive_duration: int = 1440,
    ) -> dict:
        call = {
            "method": "create_thread",
            "channel_id": channel_id,
            "name": name,
            "message_content": message_content,
            "auto_archive_duration": auto_archive_duration,
        }
        self.calls.append(call)
        if self._fail:
            raise RuntimeError("Fake Discord failure")
        return {
            "thread_id": f"fake_thread_{len(self.calls)}",
            "channel_id": channel_id,
            "message_id": f"fake_msg_{len(self.calls)}",
        }

    def send_message(self, *, channel_id: str, content: str) -> dict:
        call = {
            "method": "send_message",
            "channel_id": channel_id,
            "content": content,
        }
        self.calls.append(call)
        if self._fail:
            raise RuntimeError("Fake Discord failure")
        return {
            "message_id": f"fake_msg_{len(self.calls)}",
            "channel_id": channel_id,
        }


def dispatch_outbox_request(
    store: SensoriumStore,
    *,
    outbox_id: str,
    adapter: DiscordAdapter | None = None,
    config: dict | None = None,
    execute: bool = False,
    _locked: bool = False,
) -> dict:
    """Dispatch a prepared outbox request via the appropriate adapter."""
    cfg = _merged_outbox_config(config)
    now = utc_now_iso()

    if not execute:
        return {
            "success": False,
            "error": "execute_not_set",
            "detail": "Dispatch requires execute=True.",
        }
    if not _locked:
        with store.outbox_transaction():
            return dispatch_outbox_request(
                store,
                outbox_id=outbox_id,
                adapter=adapter,
                config=config,
                execute=True,
                _locked=True,
            )

    requests = store.read_jsonl("outbox")
    target_req = None
    for req in requests:
        if req.get("id") == outbox_id:
            target_req = req
            break

    if target_req is None:
        return {"success": False, "error": "not_found", "detail": f"Outbox request '{outbox_id}' not found."}

    if target_req.get("status") != "prepared":
        return {
            "success": False,
            "error": "invalid_status",
            "detail": f"Outbox request status is '{target_req.get('status')}', expected 'prepared'.",
        }

    surface = target_req.get("surface", "")
    delivery_mode = target_req.get("delivery_mode", "")

    if surface == "discord" and delivery_mode in DIRECT_DELIVERY_MODES:
        if not cfg.get("direct_modes_enabled"):
            return {"success": False, "error": "direct_modes_disabled", "detail": "Direct Discord modes are disabled in config."}
        if not (cfg.get("discord") or {}).get("enabled"):
            return {"success": False, "error": "discord_disabled", "detail": "Discord adapter is disabled in config."}
        if adapter is None:
            return {"success": False, "error": "no_adapter", "detail": "No Discord adapter provided for dispatch."}

    try:
        platform_refs: dict = {}
        if adapter and surface == "discord" and delivery_mode in DIRECT_DELIVERY_MODES:
            target = target_req.get("target") or {}
            if delivery_mode == "discord_channel_thread":
                platform_refs = adapter.create_thread(
                    channel_id=target.get("channel_id", ""),
                    name=target_req.get("title", "Sensorium thread"),
                    message_content=target_req.get("message_preview", ""),
                    auto_archive_duration=cfg.get("discord", {}).get("default_auto_archive_duration", 1440),
                )
            elif delivery_mode == "discord_dm_bound_session":
                platform_refs = adapter.send_message(
                    channel_id=target.get("dm_channel_id", ""),
                    content=target_req.get("message_preview", ""),
                )

        target_req["status"] = "dispatched"
        target_req["updated_at"] = now
        target_req["platform_refs"] = platform_refs

        receipt = {
            "ts": now,
            "type": "outbox.dispatched",
            "thread_id": target_req.get("origin_thread_id"),
            "outbox_id": outbox_id,
            "delivery_mode": delivery_mode,
            "surface": surface,
            "platform_refs": platform_refs,
        }
        store.append_jsonl("decisions", receipt)
        _rewrite_jsonl(store, "outbox", requests)

        threads = store.read_jsonl("threads")
        thread = _find_thread_by_id(threads, target_req.get("origin_thread_id", ""))
        if thread:
            thread.setdefault("interaction_refs", []).append({
                "type": "outbox_dispatched",
                "outbox_id": outbox_id,
                "ts": now,
            })
            thread.setdefault("decision_log", []).append({
                "ts": now,
                "type": "outbox.dispatched",
                "outbox_id": outbox_id,
                "platform_refs": platform_refs,
            })
            thread["updated_at"] = now
            _rewrite_jsonl(store, "threads", threads)

        return {
            "success": True,
            "data": target_req,
            "receipt": receipt,
        }

    except Exception as e:
        target_req["status"] = "failed"
        target_req["updated_at"] = now

        receipt = {
            "ts": now,
            "type": "outbox.failed",
            "thread_id": target_req.get("origin_thread_id"),
            "outbox_id": outbox_id,
            "error": str(e),
        }
        store.append_jsonl("decisions", receipt)
        _rewrite_jsonl(store, "outbox", requests)

        return {
            "success": False,
            "error": "dispatch_failed",
            "detail": str(e),
            "receipt": receipt,
        }
