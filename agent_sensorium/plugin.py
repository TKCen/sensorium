"""Hermes plugin registration surface."""

from __future__ import annotations

from pathlib import Path
from typing import Any

_TOOLSET = "agent-sensorium"


def _default_instance() -> str:
    """Return configured default instance for commands/tools without explicit instance."""
    from .config import default_instance_name

    return default_instance_name("default")


def _arg_instance(args: dict[str, Any]) -> str:
    value = args.get("instance")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return _default_instance()


def _schema(name: str, description: str, properties: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties or {},
            "additionalProperties": False,
        },
    }


def register(ctx) -> None:
    """Register Agent Sensorium plugin tools, command, and bundled skill."""
    from .commands import handle_sensorium_command
    from .pointers import handle_pointer_pre_llm
    from .pre_llm_salience import handle_salience_pre_llm
    from .tools import (
        handle_sensorium_action_attach,
        handle_sensorium_action_prepare,
        handle_sensorium_action_result,
        handle_sensorium_action_status,
        handle_sensorium_artifact_status,
        handle_sensorium_artifact_store,
        handle_sensorium_attention_inbox,
        handle_sensorium_attention_pointer,
        handle_sensorium_candidate_update,
        handle_sensorium_compact,
        handle_sensorium_conscious_claim,
        handle_sensorium_conscious_complete,
        handle_sensorium_dispatch_once,
        handle_sensorium_improvement_collect,
        handle_sensorium_improvement_status,
        handle_sensorium_attention_policy_decide,
        handle_sensorium_attention_policy_manage,
        handle_sensorium_ingest_event,
        handle_sensorium_ingest_signal,
        handle_sensorium_media_gift_decide,
        handle_sensorium_outbox_dispatch,
        handle_sensorium_outbox_prepare,
        handle_sensorium_service_threads,
        handle_sensorium_status,
        handle_sensorium_subconscious_advisory,
        handle_sensorium_thread_open,
        handle_sensorium_thread_update,
        handle_sensorium_worker_dispatch,
        handle_sensorium_worker_prepare,
        handle_sensorium_worker_result,
        handle_sensorium_worker_status,
    )

    common = {
        "instance": {
            "type": "string",
            "description": "Sensorium instance name. Defaults to agent_sensorium.default_instance or 'default'.",
        },
        "state_dir": {
            "type": "string",
            "description": "Optional explicit state directory for tests/manual smoke.",
        },
    }

    ctx.register_tool(
        name="sensorium_status",
        toolset=_TOOLSET,
        schema=_schema(
            "sensorium_status",
            "Read-only snapshot of Agent Sensorium state: counts, top candidates, visible threads, and instance config diagnostics.",
            {
                **common,
                "config_path": {
                    "type": "string",
                    "description": "Optional explicit path to instance config JSON file.",
                },
            },
        ),
        handler=lambda args, **kw: handle_sensorium_status(
            instance=_arg_instance(args),
            state_dir=args.get("state_dir"),
            config_path=args.get("config_path"),
        ),
        description="Read-only snapshot of Agent Sensorium state and config diagnostics.",
    )
    ctx.register_tool(
        name="sensorium_ingest_signal",
        toolset=_TOOLSET,
        schema=_schema(
            "sensorium_ingest_signal",
            "Ingest a low-level signal and promote it if deterministic thresholds are met.",
            {
                **common,
                "signal": {
                    "type": "object",
                    "description": "Signal object with sensor, source, kind, summary, strength_hint, sensitivity, and allowed_surfaces.",
                },
            },
        ),
        handler=lambda args, **kw: handle_sensorium_ingest_signal(
            signal=args.get("signal") or {},
            instance=_arg_instance(args),
            state_dir=args.get("state_dir"),
        ),
        description="Ingest a low-level signal and promote if threshold is met.",
    )
    ctx.register_tool(
        name="sensorium_ingest_event",
        toolset=_TOOLSET,
        schema=_schema(
            "sensorium_ingest_event",
            "Ingest an already-promoted trusted event and create a candidate.",
            {
                **common,
                "event": {
                    "type": "object",
                    "description": "Trusted Event object with id, ts, type, kind, summary, strength, sensitivity, and allowed_surfaces.",
                },
                "config": {
                    "type": "object",
                    "description": "Optional candidate scoring config overrides.",
                },
            },
        ),
        handler=lambda args, **kw: handle_sensorium_ingest_event(
            event=args.get("event") or {},
            instance=_arg_instance(args),
            state_dir=args.get("state_dir"),
            config=args.get("config"),
        ),
        description="Ingest a trusted event and create a candidate.",
    )
    ctx.register_tool(
        name="sensorium_dispatch_once",
        toolset=_TOOLSET,
        schema=_schema(
            "sensorium_dispatch_once",
            "Deprecated compatibility advisory for the old Sensorium dispatcher. Default is read-only; mutating thread dispatch requires config.legacy_thread_dispatch_enabled=True and should be replaced by the Kanban bridge.",
            {
                **common,
                "dry_run": {
                    "type": "boolean",
                    "description": "If true, preview without mutating state. Defaults to true for compatibility safety.",
                },
                "config": {
                    "type": "object",
                    "description": "Optional dispatcher config overrides.",
                },
            },
        ),
        handler=lambda args, **kw: handle_sensorium_dispatch_once(
            instance=_arg_instance(args),
            state_dir=args.get("state_dir"),
            dry_run=bool(args.get("dry_run", True)),
            config=args.get("config"),
        ),
        description="Deprecated dispatcher advisory; use Sensorium-on-Kanban intake/review for activation.",
    )
    ctx.register_tool(
        name="sensorium_candidate_update",
        toolset=_TOOLSET,
        schema=_schema(
            "sensorium_candidate_update",
            "Update a candidate status: suppress, hold, cancel, or mark_reviewed.",
            {
                **common,
                "candidate_id": {
                    "type": "string",
                    "description": "Candidate ID to update.",
                },
                "action": {
                    "type": "string",
                    "enum": ["suppress", "hold", "cancel", "mark_reviewed"],
                    "description": "Manual candidate decision.",
                },
                "reason": {
                    "type": "string",
                    "description": "Short human-readable reason for the decision receipt.",
                },
            },
        ),
        handler=lambda args, **kw: handle_sensorium_candidate_update(
            candidate_id=args.get("candidate_id", ""),
            action=args.get("action", ""),
            reason=args.get("reason", ""),
            instance=_arg_instance(args),
            state_dir=args.get("state_dir"),
        ),
        description="Update candidate status with a decision receipt.",
    )
    ctx.register_tool(
        name="sensorium_attention_pointer",
        toolset=_TOOLSET,
        schema=_schema(
            "sensorium_attention_pointer",
            "Preview the small active-session Sensorium pointer for a surface without dumping thread capsules.",
            {
                **common,
                "surface": {
                    "type": "string",
                    "description": "Surface to check, e.g. local, dashboard, discord, telegram.",
                },
                "config": {
                    "type": "object",
                    "description": "Optional pointer config overrides.",
                },
            },
        ),
        handler=lambda args, **kw: handle_sensorium_attention_pointer(
            instance=_arg_instance(args),
            state_dir=args.get("state_dir"),
            surface=args.get("surface") or "local",
            config=args.get("config"),
        ),
        description="Preview a small Sensorium active-session pointer.",
    )
    ctx.register_tool(
        name="sensorium_thread_open",
        toolset=_TOOLSET,
        schema=_schema(
            "sensorium_thread_open",
            "Open a compact conscious-thread capsule when the requested surface is allowed.",
            {
                **common,
                "thread_id": {
                    "type": "string",
                    "description": "Thread ID to open, or 'latest'. Defaults to latest visible thread.",
                },
                "surface": {
                    "type": "string",
                    "description": "Surface requesting the capsule, e.g. local, dashboard, discord, telegram.",
                },
            },
        ),
        handler=lambda args, **kw: handle_sensorium_thread_open(
            instance=_arg_instance(args),
            state_dir=args.get("state_dir"),
            thread_id=args.get("thread_id") or "latest",
            surface=args.get("surface") or "local",
        ),
        description="Open a compact conscious-thread capsule.",
    )
    ctx.register_tool(
        name="sensorium_thread_update",
        toolset=_TOOLSET,
        schema=_schema(
            "sensorium_thread_update",
            "Update a conscious thread status or pin state with a decision receipt.",
            {
                **common,
                "thread_id": {
                    "type": "string",
                    "description": "Thread ID to update, or 'latest'.",
                },
                "action": {
                    "type": "string",
                    "enum": ["close", "hold", "resume", "archive", "pin", "unpin", "mark_reviewed"],
                    "description": "Thread lifecycle action.",
                },
                "reason": {
                    "type": "string",
                    "description": "Short human-readable reason for the decision receipt.",
                },
                "resume_trigger": {
                    "type": "string",
                    "description": "Optional trigger condition to resume a held thread.",
                },
                "emit_feedback": {
                    "type": "boolean",
                    "description": "If true, emit a local feedback receipt when closing/archiving a thread.",
                },
            },
        ),
        handler=lambda args, **kw: handle_sensorium_thread_update(
            instance=_arg_instance(args),
            state_dir=args.get("state_dir"),
            thread_id=args.get("thread_id") or "latest",
            action=args.get("action") or "",
            reason=args.get("reason") or "",
            resume_trigger=args.get("resume_trigger") or "",
            emit_feedback=bool(args.get("emit_feedback", False)),
        ),
        description="Update a conscious thread with a decision receipt.",
    )
    ctx.register_tool(
        name="sensorium_compact",
        toolset=_TOOLSET,
        schema=_schema(
            "sensorium_compact",
            "Archive expired candidates and threads with decision receipts.",
            common,
        ),
        handler=lambda args, **kw: handle_sensorium_compact(
            instance=_arg_instance(args),
            state_dir=args.get("state_dir"),
        ),
        description="Archive expired candidates and threads with decision receipts.",
    )
    ctx.register_tool(
        name="sensorium_service_threads",
        toolset=_TOOLSET,
        schema=_schema(
            "sensorium_service_threads",
            "Deterministic thread service pass: TTL archival, starvation/dirty/expiring reports.",
            {
                **common,
                "config": {
                    "type": "object",
                    "description": "Optional service config overrides (starvation_hours, expiring_window_hours).",
                },
                "now": {
                    "type": "string",
                    "description": "Optional ISO timestamp override for deterministic testing.",
                },
            },
        ),
        handler=lambda args, **kw: handle_sensorium_service_threads(
            instance=_arg_instance(args),
            state_dir=args.get("state_dir"),
            config=args.get("config"),
            now=args.get("now"),
        ),
        description="Thread service pass: archive expired, report starved/dirty/expiring.",
    )

    ctx.register_tool(
        name="sensorium_subconscious_advisory",
        toolset=_TOOLSET,
        schema=_schema(
            "sensorium_subconscious_advisory",
            "Run a bounded Subconscious advisory pass over Events/Candidates. Disabled by default; cheap model use requires config.model_enabled=true and never creates external side effects.",
            {
                **common,
                "dry_run": {
                    "type": "boolean",
                    "description": "If true, preview/store advisory receipt without creating an internal candidate.",
                },
                "enabled": {
                    "type": "boolean",
                    "description": "Explicitly enable the advisory lane. Defaults false.",
                },
                "advisory_output": {
                    "type": "object",
                    "description": "Optional already-produced model/advisory output with action DROP, SAVE, or CREATE_CONSCIOUS_TASK.",
                },
                "config": {
                    "type": "object",
                    "description": "Optional context/advisory config overrides.",
                },
            },
        ),
        handler=lambda args, **kw: handle_sensorium_subconscious_advisory(
            instance=_arg_instance(args),
            state_dir=args.get("state_dir"),
            dry_run=bool(args.get("dry_run", True)),
            enabled=bool(args.get("enabled", False)),
            advisory_output=args.get("advisory_output"),
            config=args.get("config"),
        ),
        description="Run bounded Subconscious advisory over local Sensorium Events.",
    )

    ctx.register_tool(
        name="sensorium_improvement_collect",
        toolset=_TOOLSET,
        schema=_schema(
            "sensorium_improvement_collect",
            "Run deterministic Sensorium self-improvement evidence collection. It may create at most one internal attention_policy_review candidate and never mutates wake behavior directly.",
            {
                **common,
                "dry_run": {
                    "type": "boolean",
                    "description": "If true, preview without creating a candidate or receipt.",
                },
                "bridge_state": {
                    "type": "object",
                    "description": "Optional Sensorium-on-Kanban bridge state snapshot for settlement-gap evidence.",
                },
                "kanban_tasks": {
                    "type": "array",
                    "description": "Optional compact Kanban task rows for completed-outcome evidence.",
                },
                "config": {
                    "type": "object",
                    "description": "Optional threshold/config overrides.",
                },
            },
        ),
        handler=lambda args, **kw: handle_sensorium_improvement_collect(
            instance=_arg_instance(args),
            state_dir=args.get("state_dir"),
            dry_run=bool(args.get("dry_run", False)),
            bridge_state=args.get("bridge_state"),
            kanban_tasks=args.get("kanban_tasks"),
            config=args.get("config"),
        ),
        description="Collect Sensorium-internal attention-policy evidence.",
    )
    ctx.register_tool(
        name="sensorium_attention_policy_decide",
        toolset=_TOOLSET,
        schema=_schema(
            "sensorium_attention_policy_decide",
            "Record a Conscious attention-policy-review decision receipt with future_tendency_delta, verification_condition, and rollback_condition. Does not apply policy mutations directly.",
            {
                **common,
                "candidate_id": {"type": "string", "description": "attention_policy_review candidate id."},
                "decision": {
                    "type": "string",
                    "enum": [
                        "NO_CHANGE",
                        "THRESHOLD_COALESCING_TWEAK_PROPOSAL",
                        "SENSOR_ADDITION_TASK",
                        "PRIORITY_MAP_CHANGE",
                        "MEMORY_SKILL_PATCH",
                        "HOLD",
                    ],
                    "description": "Bounded conscious decision.",
                },
                "reason": {"type": "string", "description": "Short reason for the decision."},
                "future_tendency_delta": {"type": "string", "description": "How future salience should change."},
                "verification_condition": {"type": "string", "description": "How a later run verifies this helped."},
                "rollback_condition": {"type": "string", "description": "When to undo/revisit the decision."},
                "decided_by": {"type": "string", "description": "Actor label; defaults to conscious."},
                "decision_ref": {"type": "string", "description": "Optional Kanban/thread/session ref."},
                "implementation_ref": {"type": "string", "description": "Optional implementation/task ref."},
            },
        ),
        handler=lambda args, **kw: handle_sensorium_attention_policy_decide(
            candidate_id=args.get("candidate_id", ""),
            decision=args.get("decision", ""),
            reason=args.get("reason", ""),
            future_tendency_delta=args.get("future_tendency_delta", ""),
            verification_condition=args.get("verification_condition", ""),
            rollback_condition=args.get("rollback_condition", ""),
            decided_by=args.get("decided_by") or "conscious",
            decision_ref=args.get("decision_ref") or "",
            implementation_ref=args.get("implementation_ref") or "",
            instance=_arg_instance(args),
            state_dir=args.get("state_dir"),
        ),
        description="Record a conscious Sensorium attention-policy decision receipt.",
    )
    ctx.register_tool(
        name="sensorium_attention_policy_manage",
        toolset=_TOOLSET,
        schema=_schema(
            "sensorium_attention_policy_manage",
            "Apply a narrow declarative Sensorium attention-policy config mutation. This is not generic file access and cannot broaden privacy surfaces or edit code.",
            {
                **common,
                "config_path": {"type": "string", "description": "Optional explicit instance config JSON path."},
                "action": {
                    "type": "string",
                    "enum": [
                        "patch_evidence_rule",
                        "add_ignored_key",
                        "patch_review_budget",
                        "patch_priority_weight",
                    ],
                    "description": "Typed policy mutation action.",
                },
                "rule": {"type": "string", "description": "Evidence rule name for rule-scoped actions."},
                "patch": {"type": "object", "description": "Policy patch for patch_evidence_rule or patch_review_budget."},
                "key": {"type": "string", "description": "Ignored correlation key or priority-weight key."},
                "value": {"type": "number", "description": "Positive numeric value for patch_priority_weight."},
                "reason": {"type": "string", "description": "Why this config mutation is authorized."},
                "future_tendency_delta": {"type": "string", "description": "How future salience should change."},
                "verification_condition": {"type": "string", "description": "How a later run verifies this helped."},
                "rollback_condition": {"type": "string", "description": "When to undo/revisit the change."},
                "actor": {"type": "string", "description": "Actor label; defaults to conscious."},
                "decision_ref": {"type": "string", "description": "Optional Kanban/thread/session ref."},
            },
        ),
        handler=lambda args, **kw: handle_sensorium_attention_policy_manage(
            action=args.get("action", ""),
            instance=_arg_instance(args),
            state_dir=args.get("state_dir"),
            config_path=args.get("config_path"),
            rule=args.get("rule") or "",
            patch=args.get("patch"),
            key=args.get("key") or "",
            value=args.get("value"),
            reason=args.get("reason") or "",
            future_tendency_delta=args.get("future_tendency_delta") or "",
            verification_condition=args.get("verification_condition") or "",
            rollback_condition=args.get("rollback_condition") or "",
            actor=args.get("actor") or "conscious",
            decision_ref=args.get("decision_ref") or "",
        ),
        description="Apply a narrow declarative Sensorium attention-policy config mutation.",
    )
    ctx.register_tool(
        name="sensorium_improvement_status",
        toolset=_TOOLSET,
        schema=_schema(
            "sensorium_improvement_status",
            "Read Sensorium self-improvement harness status and recent attention-policy-review receipts.",
            common,
        ),
        handler=lambda args, **kw: handle_sensorium_improvement_status(
            instance=_arg_instance(args),
            state_dir=args.get("state_dir"),
        ),
        description="Read self-improvement harness status.",
    )

    ctx.register_tool(
        name="sensorium_attention_inbox",
        toolset=_TOOLSET,
        schema=_schema(
            "sensorium_attention_inbox",
            "Read-only attention inbox: active candidates and visible conscious threads filtered by surface and sensitivity policy, with allowed decisions per item.",
            {
                **common,
                "surface": {
                    "type": "string",
                    "description": "Surface to filter for, e.g. local, dashboard, discord. Defaults to local.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max items to return. Defaults to 50.",
                },
                "config_path": {
                    "type": "string",
                    "description": "Optional explicit path to instance config JSON file.",
                },
            },
        ),
        handler=lambda args, **kw: handle_sensorium_attention_inbox(
            instance=_arg_instance(args),
            state_dir=args.get("state_dir"),
            surface=args.get("surface") or "local",
            limit=int(args.get("limit") or 50),
            config_path=args.get("config_path"),
        ),
        description="Read-only attention inbox with surface/sensitivity filtering.",
    )
    ctx.register_tool(
        name="sensorium_outbox_prepare",
        toolset=_TOOLSET,
        schema=_schema(
            "sensorium_outbox_prepare",
            "Deprecated compatibility: prepare a Sensorium-local outbox receipt for old thread state. Prefer Kanban-reviewed action/artifact/outbox work with Sensorium refs.",
            {
                **common,
                "thread_id": {
                    "type": "string",
                    "description": "Sensorium thread ID to create the outbox request for.",
                },
                "request_type": {
                    "type": "string",
                    "enum": ["REACH_OUT", "PRIVATE_EXPRESSION", "THINK"],
                    "description": "Type of outbox request. Defaults to THINK.",
                },
                "surface": {
                    "type": "string",
                    "description": "Target surface: discord or local. Defaults to local.",
                },
                "delivery_mode": {
                    "type": "string",
                    "enum": ["peripheral_reference", "context_pointer", "discord_channel_thread", "discord_dm_bound_session"],
                    "description": "Delivery mode. Direct Discord modes disabled by default.",
                },
                "target": {
                    "type": "object",
                    "description": "Target descriptor: channel_id, thread_id, dm_channel_id, session_ref.",
                },
                "title": {
                    "type": "string",
                    "description": "Short title for the outbox request.",
                },
                "message_preview": {
                    "type": "string",
                    "description": "Compact message preview (metadata only, not full content).",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "If true (default), return the would-be request without writing.",
                },
                "config": {
                    "type": "object",
                    "description": "Optional outbox config overrides.",
                },
            },
        ),
        handler=lambda args, **kw: handle_sensorium_outbox_prepare(
            thread_id=args.get("thread_id", ""),
            request_type=args.get("request_type", "THINK"),
            surface=args.get("surface", "local"),
            delivery_mode=args.get("delivery_mode", ""),
            target=args.get("target"),
            title=args.get("title", ""),
            message_preview=args.get("message_preview", ""),
            dry_run=bool(args.get("dry_run", True)),
            config=args.get("config"),
            instance=_arg_instance(args),
            state_dir=args.get("state_dir"),
        ),
        description="Prepare a Sensorium outbox request (internal state, no live Discord).",
    )
    ctx.register_tool(
        name="sensorium_outbox_dispatch",
        toolset=_TOOLSET,
        schema=_schema(
            "sensorium_outbox_dispatch",
            "Deprecated compatibility: dispatch a Sensorium-local outbox receipt. Live delivery should be routed through Kanban-reviewed work; direct modes remain fail-closed.",
            {
                **common,
                "outbox_id": {
                    "type": "string",
                    "description": "Outbox request ID to dispatch.",
                },
                "execute": {
                    "type": "boolean",
                    "description": "Must be true to actually dispatch. Default false (dry-run).",
                },
                "config": {
                    "type": "object",
                    "description": "Optional outbox config overrides.",
                },
            },
        ),
        handler=lambda args, **kw: handle_sensorium_outbox_dispatch(
            outbox_id=args.get("outbox_id", ""),
            execute=bool(args.get("execute", False)),
            config=args.get("config"),
            instance=_arg_instance(args),
            state_dir=args.get("state_dir"),
        ),
        description="Dispatch a prepared Sensorium outbox request.",
    )
    ctx.register_tool(
        name="sensorium_worker_prepare",
        toolset=_TOOLSET,
        schema=_schema(
            "sensorium_worker_prepare",
            "Deprecated compatibility: prepare a Sensorium-local worker receipt. Prefer Kanban child tasks/comments/artifact refs for live worker ticketing.",
            {
                **common,
                "thread_id": {
                    "type": "string",
                    "description": "Sensorium thread ID to create the worker request for.",
                },
                "worker_type": {
                    "type": "string",
                    "enum": ["manual", "hermes_subagent", "kanban_task", "script"],
                    "description": "Type of worker to prepare.",
                },
                "title": {
                    "type": "string",
                    "description": "Short title for the worker request.",
                },
                "task_summary": {
                    "type": "string",
                    "description": "Bounded task description (truncated to max_prompt_chars).",
                },
                "target": {
                    "type": "object",
                    "description": "Optional target/ref metadata.",
                },
                "profile": {
                    "type": "object",
                    "description": "Optional profile/toolsets/model hints.",
                },
                "config": {
                    "type": "object",
                    "description": "Optional worker config overrides.",
                },
            },
        ),
        handler=lambda args, **kw: handle_sensorium_worker_prepare(
            thread_id=args.get("thread_id", ""),
            worker_type=args.get("worker_type", ""),
            title=args.get("title", ""),
            task_summary=args.get("task_summary", ""),
            target=args.get("target"),
            profile=args.get("profile"),
            config=args.get("config"),
            instance=_arg_instance(args),
            state_dir=args.get("state_dir"),
        ),
        description="Deprecated compatibility worker receipt; prefer Kanban child tasks/comments/artifact refs.",
    )
    ctx.register_tool(
        name="sensorium_worker_dispatch",
        toolset=_TOOLSET,
        schema=_schema(
            "sensorium_worker_dispatch",
            "Deprecated compatibility: dispatch a Sensorium-local worker request. Live worker dispatch belongs to Kanban; direct dispatch remains opt-in/fail-closed.",
            {
                **common,
                "worker_request_id": {
                    "type": "string",
                    "description": "Worker request ID to dispatch.",
                },
                "execute": {
                    "type": "boolean",
                    "description": "Must be true to attempt dispatch. Default false (dry-run).",
                },
                "config": {
                    "type": "object",
                    "description": "Optional worker config overrides.",
                },
            },
        ),
        handler=lambda args, **kw: handle_sensorium_worker_dispatch(
            worker_request_id=args.get("worker_request_id", ""),
            execute=bool(args.get("execute", False)),
            config=args.get("config"),
            instance=_arg_instance(args),
            state_dir=args.get("state_dir"),
        ),
        description="Deprecated compatibility worker dispatch (requires execute + config opt-in).",
    )
    ctx.register_tool(
        name="sensorium_worker_result",
        toolset=_TOOLSET,
        schema=_schema(
            "sensorium_worker_result",
            "Record a worker result: update request status, write receipt, and emit feedback signal with causal refs.",
            {
                **common,
                "worker_request_id": {
                    "type": "string",
                    "description": "Worker request ID to record result for.",
                },
                "outcome": {
                    "type": "string",
                    "enum": ["completed", "failed", "cancelled", "timeout"],
                    "description": "Worker outcome.",
                },
                "result_summary": {
                    "type": "string",
                    "description": "Compact result summary (truncated to max_result_chars).",
                },
                "output_refs": {
                    "type": "array",
                    "description": "Optional list of output reference metadata dicts (not raw content).",
                },
                "error_summary": {
                    "type": "string",
                    "description": "Compact error summary if outcome is failed/timeout.",
                },
                "config": {
                    "type": "object",
                    "description": "Optional worker config overrides.",
                },
            },
        ),
        handler=lambda args, **kw: handle_sensorium_worker_result(
            worker_request_id=args.get("worker_request_id", ""),
            outcome=args.get("outcome", ""),
            result_summary=args.get("result_summary", ""),
            output_refs=args.get("output_refs"),
            error_summary=args.get("error_summary", ""),
            config=args.get("config"),
            instance=_arg_instance(args),
            state_dir=args.get("state_dir"),
        ),
        description="Record worker result with feedback signal emission.",
    )
    ctx.register_tool(
        name="sensorium_worker_status",
        toolset=_TOOLSET,
        schema=_schema(
            "sensorium_worker_status",
            "List worker requests with optional thread/status filters.",
            {
                **common,
                "thread_id": {
                    "type": "string",
                    "description": "Optional thread ID filter.",
                },
                "status": {
                    "type": "string",
                    "description": "Optional status filter (prepared, dispatched, completed, failed, cancelled).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max items to return. Defaults to 20.",
                },
            },
        ),
        handler=lambda args, **kw: handle_sensorium_worker_status(
            thread_id=args.get("thread_id"),
            status=args.get("status"),
            limit=int(args.get("limit") or 20),
            instance=_arg_instance(args),
            state_dir=args.get("state_dir"),
        ),
        description="List worker requests with optional filters.",
    )
    ctx.register_tool(
        name="sensorium_media_gift_decide",
        toolset=_TOOLSET,
        schema=_schema(
            "sensorium_media_gift_decide",
            "Apply Sera mediated-presence gift conscious-choice policy and write receipts. Subconscious may only propose; no automatic outbound delivery or scheduler spam.",
            {
                **common,
                "config_path": {
                    "type": "string",
                    "description": "Optional explicit instance config JSON path.",
                },
                "decision": {
                    "type": "string",
                    "enum": [
                        "propose",
                        "prepare_thread_artifact",
                        "offer_choice",
                        "choose_silence",
                        "decline",
                        "approve_delivery",
                        "block_delivery",
                    ],
                    "description": "Conscious-choice decision to receipt.",
                },
                "actor_tier": {
                    "type": "string",
                    "enum": ["subconscious", "conscious", "operator", "test"],
                    "description": "Who is making the choice; subconscious is propose-only.",
                },
                "source": {
                    "type": "string",
                    "enum": ["inner_salience", "sebastian_prompt", "manual_review", "worker_result", "scheduler", "test"],
                    "description": "Origin of the pressure/request.",
                },
                "why_now": {
                    "type": "string",
                    "description": "Bounded why-now rationale required for proposals, preparation, and delivery approval.",
                },
                "reason": {
                    "type": "string",
                    "description": "Optional reason for silence, decline, denial, or block.",
                },
                "thread_id": {"type": "string", "description": "Sensorium thread id."},
                "artifact_id": {"type": "string", "description": "Existing thread artifact id for delivery approval."},
                "surface": {"type": "string", "description": "Requested delivery/review surface."},
                "target_ref": {"type": "string", "description": "Compact configured target ref, e.g. discord:#pics or dm alias."},
                "config": {
                    "type": "object",
                    "description": "Optional media_gift_policy override for tests/manual smoke; cannot dispatch delivery.",
                },
            },
        ),
        handler=lambda args, **kw: handle_sensorium_media_gift_decide(
            decision=args.get("decision", ""),
            actor_tier=args.get("actor_tier") or "conscious",
            source=args.get("source") or "inner_salience",
            why_now=args.get("why_now") or "",
            reason=args.get("reason") or "",
            thread_id=args.get("thread_id") or "",
            artifact_id=args.get("artifact_id") or "",
            surface=args.get("surface") or "",
            target_ref=args.get("target_ref") or "",
            config=args.get("config"),
            config_path=args.get("config_path"),
            instance=_arg_instance(args),
            state_dir=args.get("state_dir"),
        ),
        description="Apply mediated-presence gift conscious-choice policy and write receipts.",
    )
    ctx.register_tool(
        name="sensorium_artifact_store",
        toolset=_TOOLSET,
        schema=_schema(
            "sensorium_artifact_store",
            "Store a mediated-presence artifact ref (text/audio/image/video) for conscious thread/action review. Private-by-default; does not generate media or deliver outbound messages.",
            {
                **common,
                "kind": {
                    "type": "string",
                    "enum": ["text", "audio", "image", "video"],
                    "description": "Artifact kind.",
                },
                "ref_path": {
                    "type": "string",
                    "description": "File path or external ref; raw content is not accepted.",
                },
                "provenance": {
                    "type": "object",
                    "description": "Compact provenance metadata (hashes/models/refs only; no raw prompts).",
                },
                "why_created": {
                    "type": "string",
                    "description": "Bounded reason this artifact was created.",
                },
                "intended_handoff_mode": {
                    "type": "string",
                    "enum": ["pillow_dm", "present_thread", "both_later"],
                    "description": "Later handoff intention; not delivery authorization.",
                },
                "delivery_state": {
                    "type": "string",
                    "enum": ["not_delivered", "prepared", "held_for_review", "delivery_blocked", "delivery_cancelled"],
                    "description": "Record state only; sent/delivered/queued are rejected.",
                },
                "capacity_requirements": {
                    "type": "object",
                    "description": "Compact resource requirements such as chatterbox/comfy/gpu/idle hints.",
                },
                "source_thread_id": {
                    "type": "string",
                    "description": "Optional source conscious thread id.",
                },
                "source_candidate_id": {
                    "type": "string",
                    "description": "Optional source candidate id.",
                },
                "source_action_id": {
                    "type": "string",
                    "description": "Optional source action id; attaches via artifact_ref when present.",
                },
                "feedback_hooks": {
                    "type": "object",
                    "description": "Compact feedback hook descriptors; no raw messages/prompts.",
                },
                "sensitivity": {
                    "type": "string",
                    "enum": ["local_only", "private", "public_safe"],
                    "description": "Optional sensitivity; defaults private and intersects source refs.",
                },
                "allowed_surfaces": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional allowed surfaces; defaults ['local'] and intersects source refs.",
                },
                "config": {
                    "type": "object",
                    "description": "Optional artifact config overrides.",
                },
            },
        ),
        handler=lambda args, **kw: handle_sensorium_artifact_store(
            kind=args.get("kind", ""),
            ref_path=args.get("ref_path", ""),
            provenance=args.get("provenance"),
            why_created=args.get("why_created", ""),
            intended_handoff_mode=args.get("intended_handoff_mode") or "present_thread",
            delivery_state=args.get("delivery_state") or "not_delivered",
            capacity_requirements=args.get("capacity_requirements"),
            source_thread_id=args.get("source_thread_id", ""),
            source_candidate_id=args.get("source_candidate_id", ""),
            source_action_id=args.get("source_action_id", ""),
            feedback_hooks=args.get("feedback_hooks"),
            sensitivity=args.get("sensitivity"),
            allowed_surfaces=args.get("allowed_surfaces"),
            config=args.get("config"),
            instance=_arg_instance(args),
            state_dir=args.get("state_dir"),
        ),
        description="Store a private-by-default mediated-presence artifact ref for later conscious review.",
    )
    ctx.register_tool(
        name="sensorium_artifact_status",
        toolset=_TOOLSET,
        schema=_schema(
            "sensorium_artifact_status",
            "List mediated-presence artifact records with optional thread/action/kind filters.",
            {
                **common,
                "thread_id": {
                    "type": "string",
                    "description": "Optional source thread filter.",
                },
                "action_id": {
                    "type": "string",
                    "description": "Optional source action filter.",
                },
                "kind": {
                    "type": "string",
                    "description": "Optional kind filter.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max items to return. Defaults to 20.",
                },
            },
        ),
        handler=lambda args, **kw: handle_sensorium_artifact_status(
            thread_id=args.get("thread_id"),
            action_id=args.get("action_id"),
            kind=args.get("kind"),
            limit=int(args.get("limit") or 20),
            instance=_arg_instance(args),
            state_dir=args.get("state_dir"),
        ),
        description="List Sensorium mediated-presence artifacts.",
    )
    ctx.register_tool(
        name="sensorium_action_prepare",
        toolset=_TOOLSET,
        schema=_schema(
            "sensorium_action_prepare",
            "Deprecated compatibility: prepare a Sensorium-local action receipt. Prefer Kanban task comments/children/artifact refs for live action ticketing.",
            {
                **common,
                "thread_id": {
                    "type": "string",
                    "description": "Conscious thread ID to attach the prepared action to.",
                },
                "intent": {
                    "type": "string",
                    "description": "Short intent string (free-form, bounded).",
                },
                "title": {
                    "type": "string",
                    "description": "Short title for the action.",
                },
                "summary": {
                    "type": "string",
                    "description": "Bounded action summary.",
                },
                "why_now": {
                    "type": "string",
                    "description": "Optional rationale string.",
                },
                "refs": {
                    "type": "object",
                    "description": "Optional small string-keyed refs map.",
                },
                "resume_trigger": {
                    "type": "string",
                    "description": "Optional resume trigger string.",
                },
                "config": {
                    "type": "object",
                    "description": "Optional action config overrides.",
                },
            },
        ),
        handler=lambda args, **kw: handle_sensorium_action_prepare(
            thread_id=args.get("thread_id", ""),
            intent=args.get("intent", ""),
            title=args.get("title", ""),
            summary=args.get("summary", ""),
            why_now=args.get("why_now", ""),
            refs=args.get("refs"),
            resume_trigger=args.get("resume_trigger", ""),
            config=args.get("config"),
            instance=_arg_instance(args),
            state_dir=args.get("state_dir"),
        ),
        description="Deprecated compatibility action receipt; prefer Kanban task comments/children/artifact refs.",
    )
    ctx.register_tool(
        name="sensorium_action_attach",
        toolset=_TOOLSET,
        schema=_schema(
            "sensorium_action_attach",
            "Attach a worker/outbox/artifact/external ref to a prepared thread action.",
            {
                **common,
                "action_id": {
                    "type": "string",
                    "description": "Thread action ID to attach to.",
                },
                "kind": {
                    "type": "string",
                    "enum": ["worker_request", "outbox_request", "artifact_ref", "external_ref"],
                    "description": "Attachment kind.",
                },
                "ref_id": {
                    "type": "string",
                    "description": "Non-empty reference ID.",
                },
                "metadata": {
                    "type": "object",
                    "description": "Optional bounded metadata dict.",
                },
                "config": {
                    "type": "object",
                    "description": "Optional action config overrides.",
                },
            },
        ),
        handler=lambda args, **kw: handle_sensorium_action_attach(
            action_id=args.get("action_id", ""),
            kind=args.get("kind", ""),
            ref_id=args.get("ref_id", ""),
            metadata=args.get("metadata"),
            config=args.get("config"),
            instance=_arg_instance(args),
            state_dir=args.get("state_dir"),
        ),
        description="Attach a ref (worker/outbox/artifact/external) to a thread action.",
    )
    ctx.register_tool(
        name="sensorium_action_result",
        toolset=_TOOLSET,
        schema=_schema(
            "sensorium_action_result",
            "Record outcome for a prepared thread action, emit feedback signal, and close the action.",
            {
                **common,
                "action_id": {
                    "type": "string",
                    "description": "Thread action ID to close with a result.",
                },
                "outcome": {
                    "type": "string",
                    "enum": ["completed", "failed", "cancelled", "expired", "rejected", "superseded"],
                    "description": "Action outcome.",
                },
                "result_summary": {
                    "type": "string",
                    "description": "Bounded result summary.",
                },
                "closed_reason": {
                    "type": "string",
                    "description": "Optional close reason.",
                },
                "config": {
                    "type": "object",
                    "description": "Optional action config overrides.",
                },
            },
        ),
        handler=lambda args, **kw: handle_sensorium_action_result(
            action_id=args.get("action_id", ""),
            outcome=args.get("outcome", ""),
            result_summary=args.get("result_summary", ""),
            closed_reason=args.get("closed_reason", ""),
            config=args.get("config"),
            instance=_arg_instance(args),
            state_dir=args.get("state_dir"),
        ),
        description="Record result for a thread action and emit feedback.",
    )
    ctx.register_tool(
        name="sensorium_action_status",
        toolset=_TOOLSET,
        schema=_schema(
            "sensorium_action_status",
            "List thread actions with optional thread/status filters.",
            {
                **common,
                "thread_id": {
                    "type": "string",
                    "description": "Optional thread ID filter.",
                },
                "status": {
                    "type": "string",
                    "description": "Optional action status filter.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max items to return. Defaults to 20.",
                },
            },
        ),
        handler=lambda args, **kw: handle_sensorium_action_status(
            thread_id=args.get("thread_id"),
            status=args.get("status"),
            limit=int(args.get("limit") or 20),
            instance=_arg_instance(args),
            state_dir=args.get("state_dir"),
        ),
        description="List thread actions with optional filters.",
    )
    ctx.register_tool(
        name="sensorium_conscious_claim",
        toolset=_TOOLSET,
        schema=_schema(
            "sensorium_conscious_claim",
            "Deprecated compatibility lease for old internal Sensorium threads. Disabled by default; create conscious:review Kanban tasks for live activation.",
            {
                **common,
                "actor": {
                    "type": "string",
                    "description": "Lease actor identifier. Defaults to sera_background_conscious.",
                },
                "mode": {
                    "type": "string",
                    "description": "Claim mode. Defaults to background.",
                },
                "thread_id": {
                    "type": "string",
                    "description": "Optional explicit thread to claim. If omitted, eligibility selects one.",
                },
                "config": {
                    "type": "object",
                    "description": "Optional claim config overrides (lease_seconds, allowed_request_types).",
                },
            },
        ),
        handler=lambda args, **kw: handle_sensorium_conscious_claim(
            actor=args.get("actor") or "",
            mode=args.get("mode") or "",
            thread_id=args.get("thread_id") or "",
            config=args.get("config"),
            instance=_arg_instance(args),
            state_dir=args.get("state_dir"),
        ),
        description="Deprecated compatibility lease for old internal Sensorium threads.",
    )
    ctx.register_tool(
        name="sensorium_conscious_complete",
        toolset=_TOOLSET,
        schema=_schema(
            "sensorium_conscious_complete",
            "Complete a background-conscious lease for a claimed thread. Clears active_lease and writes a decision receipt.",
            {
                **common,
                "thread_id": {
                    "type": "string",
                    "description": "Claimed thread ID.",
                },
                "lease_id": {
                    "type": "string",
                    "description": "Lease ID returned by sensorium_conscious_claim.",
                },
                "outcome": {
                    "type": "string",
                    "description": "Free-form outcome label (e.g., processed, deferred, no_op).",
                },
                "notes": {
                    "type": "string",
                    "description": "Optional short notes recorded on the receipt.",
                },
            },
        ),
        handler=lambda args, **kw: handle_sensorium_conscious_complete(
            thread_id=args.get("thread_id", ""),
            lease_id=args.get("lease_id", ""),
            outcome=args.get("outcome") or "",
            notes=args.get("notes") or "",
            instance=_arg_instance(args),
            state_dir=args.get("state_dir"),
        ),
        description="Complete a background-conscious lease on a thread.",
    )
    ctx.register_command(
        "sensorium",
        lambda raw_args: handle_sensorium_command(raw_args, instance=_default_instance()),
        description="Pull-based sensorium status and review.",
        args_hint="status|threads|pointer|open|thread|dispatch|compact|help",
    )

    ctx.register_hook(
        "pre_llm_call",
        lambda **kw: handle_pointer_pre_llm(
            instance=_default_instance(),
            platform=kw.get("platform") or "local",
            session_id=kw.get("session_id") or "",
            state_dir=kw.get("state_dir"),
        ),
    )

    ctx.register_hook(
        "pre_llm_call",
        lambda **kw: handle_salience_pre_llm(
            instance=_default_instance(),
            platform=kw.get("platform") or "local",
            session_id=kw.get("session_id") or "",
            state_dir=kw.get("state_dir"),
        ),
    )

    skill_path = Path(__file__).resolve().parents[1] / "skills" / "agent-sensorium" / "SKILL.md"
    if skill_path.exists():
        ctx.register_skill(
            "agent-sensorium",
            skill_path,
            description="Review and operate Agent Sensorium candidates and conscious thread capsules.",
        )
