"""Deprecated Sensorium-local worker request compatibility layer.

Kanban is now the live worker/ticketing substrate. These prepared records remain
for old thread capsules and migration tests; new work should be represented as
Kanban child tasks/comments/artifact refs with Sensorium receipts pointing back
to the originating candidate/thread.
"""

from __future__ import annotations

import hashlib
from copy import deepcopy

from .schemas import new_id, truncate_text, utc_now_iso
from .store import SensoriumStore

VALID_WORKER_TYPES = {"manual", "hermes_subagent", "kanban_task", "script"}
VALID_WORKER_STATUSES = {"prepared", "dispatched", "completed", "failed", "cancelled"}
VALID_OUTCOMES = {"completed", "failed", "cancelled", "timeout"}
OPENABLE_THREAD_STATUSES = {"dormant", "held"}

WORKER_DEFAULTS: dict = {
    "enabled": True,
    "allowed_worker_types": ["manual", "hermes_subagent", "kanban_task", "script"],
    "direct_dispatch_enabled": False,
    "max_prompt_chars": 2000,
    "max_result_chars": 2000,
}


def _merged_worker_config(config: dict | None = None) -> dict:
    cfg = deepcopy(WORKER_DEFAULTS)
    if not config:
        return cfg
    for key, value in config.items():
        cfg[key] = value
    return cfg


def _compute_idempotency_key(
    *,
    origin_thread_id: str,
    worker_type: str,
    title: str,
    task_summary: str,
) -> str:
    parts = [origin_thread_id, worker_type, title, task_summary]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:24]


def _find_thread_by_id(threads: list[dict], thread_id: str) -> dict | None:
    for t in threads:
        if t.get("id") == thread_id:
            return t
    return None


def _find_existing_worker_request(
    requests: list[dict], idempotency_key: str,
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


class WorkerAdapter:
    """Abstract interface for worker dispatch operations."""

    def dispatch(self, *, worker_type: str, request: dict) -> dict:
        raise NotImplementedError


class FakeWorkerAdapter(WorkerAdapter):
    """Test double that records calls without side effects."""

    def __init__(self, *, fail: bool = False):
        self.calls: list[dict] = []
        self._fail = fail

    def dispatch(self, *, worker_type: str, request: dict) -> dict:
        call = {
            "method": "dispatch",
            "worker_type": worker_type,
            "request_id": request.get("id"),
        }
        self.calls.append(call)
        if self._fail:
            raise RuntimeError("Fake worker failure")
        return {
            "dispatch_ref": f"fake_dispatch_{len(self.calls)}",
            "worker_type": worker_type,
        }


def prepare_worker_request(
    store: SensoriumStore,
    *,
    thread_id: str,
    worker_type: str,
    title: str,
    task_summary: str = "",
    target: dict | None = None,
    profile: dict | None = None,
    config: dict | None = None,
) -> dict:
    cfg = _merged_worker_config(config)
    now = utc_now_iso()

    if not cfg.get("enabled"):
        return _denied("workers_disabled", "Worker system is disabled in config.", thread_id=thread_id)

    if worker_type not in VALID_WORKER_TYPES:
        return _denied(
            "invalid_worker_type",
            f"Invalid worker_type: {worker_type}. Must be one of: {sorted(VALID_WORKER_TYPES)}",
            thread_id=thread_id,
        )

    allowed_types = set(cfg.get("allowed_worker_types") or [])
    if worker_type not in allowed_types:
        return _denied(
            "worker_type_not_allowed",
            f"Worker type '{worker_type}' not in allowed_worker_types {sorted(allowed_types)}",
            thread_id=thread_id,
        )

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

    max_prompt = int(cfg.get("max_prompt_chars", 2000))
    truncated_summary = truncate_text(task_summary, max_prompt) if task_summary else ""
    truncated_title = truncate_text(title, 200) if title else ""

    idempotency_key = _compute_idempotency_key(
        origin_thread_id=thread_id,
        worker_type=worker_type,
        title=truncated_title,
        task_summary=truncated_summary,
    )

    existing_requests = store.read_jsonl("worker_requests")
    existing = _find_existing_worker_request(existing_requests, idempotency_key)
    if existing is not None:
        return {
            "success": True,
            "data": existing,
            "idempotent_hit": True,
        }

    request_id = new_id("wreq")
    request = {
        "id": request_id,
        "ts": now,
        "updated_at": now,
        "status": "prepared",
        "origin_thread_id": thread_id,
        "origin_candidate_id": thread.get("origin_candidate_id", ""),
        "request_type": "worker_dispatch",
        "worker_type": worker_type,
        "title": truncated_title,
        "task_summary": truncated_summary,
        "target": target or {},
        "profile": profile or {},
        "allowed_surfaces": list(thread.get("allowed_surfaces") or []),
        "sensitivity": thread.get("sensitivity", "private"),
        "idempotency_key": idempotency_key,
    }

    store.append_jsonl("worker_requests", request)

    receipt = {
        "ts": now,
        "type": "worker.prepared",
        "thread_id": thread_id,
        "worker_request_id": request_id,
        "worker_type": worker_type,
        "idempotency_key": idempotency_key,
    }
    store.append_jsonl("decisions", receipt)

    thread.setdefault("interaction_refs", []).append({
        "type": "worker_prepared",
        "worker_request_id": request_id,
        "ts": now,
    })
    thread.setdefault("decision_log", []).append({
        "ts": now,
        "type": "worker.prepared",
        "worker_request_id": request_id,
        "worker_type": worker_type,
    })
    thread["updated_at"] = now
    _rewrite_jsonl(store, "threads", threads)

    return {
        "success": True,
        "data": request,
        "receipt": receipt,
    }


def dispatch_worker_request(
    store: SensoriumStore,
    *,
    worker_request_id: str,
    adapter: WorkerAdapter | None = None,
    config: dict | None = None,
    execute: bool = False,
) -> dict:
    cfg = _merged_worker_config(config)
    now = utc_now_iso()

    if not execute:
        return {
            "success": False,
            "error": "execute_not_set",
            "detail": "Dispatch requires execute=True.",
            "dry_run": True,
        }

    if not cfg.get("direct_dispatch_enabled"):
        return {
            "success": False,
            "error": "direct_dispatch_disabled",
            "detail": "Direct worker dispatch is disabled by default. Set config.direct_dispatch_enabled=True.",
        }

    store.ensure_dirs()
    requests = store.read_jsonl("worker_requests")
    target_req = None
    for req in requests:
        if req.get("id") == worker_request_id:
            target_req = req
            break

    if target_req is None:
        return {"success": False, "error": "not_found", "detail": f"Worker request '{worker_request_id}' not found."}

    if target_req.get("status") != "prepared":
        return {
            "success": False,
            "error": "invalid_status",
            "detail": f"Worker request status is '{target_req.get('status')}', expected 'prepared'.",
        }

    if adapter is None:
        return {"success": False, "error": "no_adapter", "detail": "No worker adapter provided for dispatch."}

    try:
        dispatch_refs = adapter.dispatch(
            worker_type=target_req.get("worker_type", ""),
            request=target_req,
        )

        target_req["status"] = "dispatched"
        target_req["updated_at"] = now
        target_req["dispatch_refs"] = dispatch_refs

        receipt = {
            "ts": now,
            "type": "worker.dispatched",
            "thread_id": target_req.get("origin_thread_id"),
            "worker_request_id": worker_request_id,
            "worker_type": target_req.get("worker_type"),
            "dispatch_refs": dispatch_refs,
        }
        store.append_jsonl("decisions", receipt)
        _rewrite_jsonl(store, "worker_requests", requests)

        threads = store.read_jsonl("threads")
        thread = _find_thread_by_id(threads, target_req.get("origin_thread_id", ""))
        if thread:
            thread.setdefault("interaction_refs", []).append({
                "type": "worker_dispatched",
                "worker_request_id": worker_request_id,
                "ts": now,
            })
            thread.setdefault("decision_log", []).append({
                "ts": now,
                "type": "worker.dispatched",
                "worker_request_id": worker_request_id,
                "dispatch_refs": dispatch_refs,
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
        target_req["error_summary"] = truncate_text(str(e), 300)

        receipt = {
            "ts": now,
            "type": "worker.dispatch_failed",
            "thread_id": target_req.get("origin_thread_id"),
            "worker_request_id": worker_request_id,
            "error": truncate_text(str(e), 300),
        }
        store.append_jsonl("decisions", receipt)
        _rewrite_jsonl(store, "worker_requests", requests)

        return {
            "success": False,
            "error": "dispatch_failed",
            "detail": str(e),
            "receipt": receipt,
        }


def _build_worker_feedback_signal(request: dict, *, store: SensoriumStore) -> dict:
    """Build a feedback signal dict from a completed/failed worker request."""
    origin_candidate_id = request.get("origin_candidate_id", "")
    correlation_keys: list[str] = []
    if origin_candidate_id:
        for c in store.read_jsonl("candidates"):
            if c.get("id") == origin_candidate_id:
                correlation_keys = list(c.get("correlation_keys") or [])
                break

    outcome = request.get("outcome", "failed")
    result_summary = request.get("result_summary", "")
    title = request.get("title", "")

    return {
        "sensor": "sensorium.worker_result",
        "source": "feedback",
        "kind": "task_result",
        "summary": truncate_text(
            f"Worker {request.get('id', '')} {outcome}: {result_summary or title}",
            200,
        ),
        "actor": "tool",
        "strength_hint": 0.6,
        "caused_by": {
            "worker_request_id": request.get("id", ""),
            "origin_thread_id": request.get("origin_thread_id", ""),
            "origin_candidate_id": origin_candidate_id,
            "worker_type": request.get("worker_type", ""),
        },
        "outcome": outcome,
        "feedback_scope": "system_action",
        "correlation_keys": correlation_keys,
        "sensitivity": request.get("sensitivity", "private"),
        "allowed_surfaces": request.get("allowed_surfaces") or ["local"],
        "source_ref": request.get("id", ""),
    }


def _attach_worker_result_to_thread(
    store: SensoriumStore,
    *,
    origin_thread_id: str,
    worker_request_id: str,
    outcome: str,
    output_refs: list[dict],
    now: str,
) -> None:
    """Attach compact result ref to origin thread, mark summary_dirty."""
    if not origin_thread_id:
        return
    threads = store.read_jsonl("threads")
    thread = _find_thread_by_id(threads, origin_thread_id)
    if thread is None:
        return
    thread.setdefault("interaction_refs", []).append({
        "type": "worker_result",
        "worker_request_id": worker_request_id,
        "outcome": outcome,
        "output_refs": list(output_refs or []),
        "ts": now,
    })
    thread["summary_dirty"] = True
    if not thread.get("dirty_since"):
        thread["dirty_since"] = now
    thread["updated_at"] = now
    _rewrite_jsonl(store, "threads", threads)


def record_worker_result(
    store: SensoriumStore,
    *,
    worker_request_id: str,
    outcome: str,
    result_summary: str = "",
    output_refs: list[dict] | None = None,
    error_summary: str = "",
    config: dict | None = None,
) -> dict:
    """Update worker request with result. Returns feedback_signal for caller to ingest."""
    cfg = _merged_worker_config(config)
    now = utc_now_iso()

    store.ensure_dirs()
    requests = store.read_jsonl("worker_requests")
    target_req = None
    for req in requests:
        if req.get("id") == worker_request_id:
            target_req = req
            break

    if target_req is None:
        return {"success": False, "error": "not_found", "detail": f"Worker request '{worker_request_id}' not found."}

    if target_req.get("status") not in ("prepared", "dispatched"):
        return {
            "success": False,
            "error": "invalid_status",
            "detail": f"Worker request status is '{target_req.get('status')}', cannot record result.",
        }

    if outcome not in VALID_OUTCOMES:
        return {
            "success": False,
            "error": "invalid_outcome",
            "detail": f"Invalid outcome: {outcome}. Must be one of: {sorted(VALID_OUTCOMES)}",
        }

    max_result = int(cfg.get("max_result_chars", 2000))

    new_status = "completed" if outcome == "completed" else "failed"
    target_req["status"] = new_status
    target_req["updated_at"] = now
    target_req["outcome"] = outcome
    target_req["result_summary"] = truncate_text(result_summary, max_result) if result_summary else ""
    target_req["output_refs"] = output_refs or []
    if error_summary:
        target_req["error_summary"] = truncate_text(error_summary, max_result)

    receipt = {
        "ts": now,
        "type": "worker.result",
        "thread_id": target_req.get("origin_thread_id"),
        "worker_request_id": worker_request_id,
        "worker_type": target_req.get("worker_type"),
        "outcome": outcome,
        "new_status": new_status,
    }
    store.append_jsonl("decisions", receipt)
    _rewrite_jsonl(store, "worker_requests", requests)

    _attach_worker_result_to_thread(
        store,
        origin_thread_id=target_req.get("origin_thread_id", ""),
        worker_request_id=worker_request_id,
        outcome=outcome,
        output_refs=target_req.get("output_refs") or [],
        now=now,
    )

    feedback_signal = _build_worker_feedback_signal(target_req, store=store)

    return {
        "success": True,
        "data": target_req,
        "receipt": receipt,
        "feedback_signal": feedback_signal,
    }


def list_worker_requests(
    store: SensoriumStore,
    *,
    thread_id: str | None = None,
    status: str | None = None,
    limit: int = 20,
) -> list[dict]:
    store.ensure_dirs()
    requests = store.read_jsonl("worker_requests")
    if thread_id:
        requests = [r for r in requests if r.get("origin_thread_id") == thread_id]
    if status:
        requests = [r for r in requests if r.get("status") == status]
    compact = []
    for req in requests[-limit:]:
        compact.append({
            "id": req.get("id"),
            "status": req.get("status"),
            "worker_type": req.get("worker_type"),
            "title": truncate_text(req.get("title", ""), 120),
            "origin_thread_id": req.get("origin_thread_id"),
            "outcome": req.get("outcome"),
            "ts": req.get("ts"),
            "updated_at": req.get("updated_at"),
        })
    return compact
