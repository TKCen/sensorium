"""Deprecated Sensorium-local outbox compatibility layer.

Kanban is now the live activation/ticketing substrate. Outbox records remain as
compact compatibility receipts for existing thread capsules; new expression or
delivery work should be a Kanban-reviewed action/artifact/outbox decision with
Sensorium refs, not a hidden Sensorium-local queue.

Safety defaults:
- All direct/replyable Discord modes disabled by default
- Dry-run is the default for prepare
- No live Discord API calls unless explicitly enabled + authorized
- Idempotent: same request parameters produce the same outbox record
"""

import hashlib
import json
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


def _rewrite_jsonl(store: SensoriumStore, name: str, items: list[dict]) -> None:
    store.rewrite_jsonl(name, items)


def _denied(reason: str, detail: str, *, thread_id: str = "") -> dict:
    return {
        "success": False,
        "error": reason,
        "detail": detail,
        "thread_id": thread_id,
    }


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
) -> dict:
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

    existing_requests = store.read_jsonl("outbox")
    existing = _find_existing_outbox_request(existing_requests, idempotency_key)
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
