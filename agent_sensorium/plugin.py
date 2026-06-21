"""Hermes plugin registration surface."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_LIVE_TOOLSET = "agent-sensorium-live"
_ADMIN_TOOLSET = "agent-sensorium-admin"


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
        handle_sensorium_artifact_status,
        handle_sensorium_artifact_store,
        handle_sensorium_attention_inbox,
        handle_sensorium_attention_pointer,
        handle_sensorium_candidate_update,
        handle_sensorium_compact,
        handle_sensorium_improvement_collect,
        handle_sensorium_improvement_status,
        handle_sensorium_attention_policy_decide,
        handle_sensorium_attention_policy_manage,
        handle_sensorium_ingest_event,
        handle_sensorium_ingest_signal,
        handle_sensorium_media_gift_decide,
        handle_sensorium_profile,
        handle_sensorium_service_threads,
        handle_sensorium_sensor_config,
        handle_sensorium_status,
        handle_sensorium_subconscious_advisory,
        handle_sensorium_thread_open,
        handle_sensorium_thread_update,
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

    def _live_result(payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False)

    def _loads_result(raw: str) -> dict[str, Any]:
        try:
            return json.loads(raw or "{}")
        except Exception:
            return {"success": False, "error": "invalid_sensorium_result", "raw": raw}

    def _handle_live_sensorium(args: dict[str, Any], **kw) -> str:
        """Compact foreground Sensorium aperture.

        The live model surface stays intentionally tiny. Granular operations are
        still registered below, but only under the admin toolset.
        """
        action = str(args.get("action") or "status").strip().lower()
        instance = _arg_instance(args)
        state_dir = args.get("state_dir")
        surface = str(args.get("surface") or kw.get("platform") or "local").strip() or "local"
        text = str(args.get("text") or "").strip()
        target_id = str(args.get("id") or "latest").strip() or "latest"

        if action == "status":
            status = _loads_result(handle_sensorium_status(instance=instance, state_dir=state_dir))
            pointer = _loads_result(handle_sensorium_attention_pointer(
                instance=instance, state_dir=state_dir, surface=surface,
            ))
            data = status.get("data") or {}
            return _live_result({
                "success": bool(status.get("success", True)),
                "instance": instance,
                "data": {
                    "counts": data.get("counts", {}),
                    "top_candidates": data.get("top_candidates", [])[:3],
                    "top_threads": data.get("top_threads", [])[:3],
                    "pointer": (pointer.get("data") or {}),
                    "ts": data.get("ts"),
                },
                "error": status.get("error"),
            })

        if action == "ingest":
            kind = str(args.get("kind") or "operator_salience").strip() or "operator_salience"
            if not text:
                return _live_result({"success": False, "instance": instance, "error": "text_required"})
            try:
                strength = float(args.get("strength") or 0.75)
            except (TypeError, ValueError):
                strength = 0.75
            strength = max(0.0, min(1.0, strength))
            allowed_surfaces = ["local"] if surface == "local" else ["local", surface]
            signal = {
                "sensor": "active_session",
                "source": "foreground_tool",
                "kind": kind,
                "summary": text[:500],
                "strength_hint": strength,
                "sensitivity": "private",
                "allowed_surfaces": allowed_surfaces,
                "correlation_keys": ["active-session", f"surface:{surface}", kind],
            }
            return handle_sensorium_ingest_signal(signal=signal, instance=instance, state_dir=state_dir)

        if action == "open":
            return handle_sensorium_thread_open(
                thread_id=target_id, surface=surface, instance=instance, state_dir=state_dir,
            )

        if action == "update":
            keyword = str(args.get("keyword") or "mark_reviewed").strip().lower()
            return handle_sensorium_thread_update(
                thread_id=target_id,
                action=keyword,
                reason=text,
                instance=instance,
                state_dir=state_dir,
            )

        return _live_result({
            "success": False,
            "instance": instance,
            "error": "invalid_action",
            "allowed_actions": ["status", "ingest", "open", "update"],
        })

    ctx.register_tool(
        name="sensorium",
        toolset=_LIVE_TOOLSET,
        schema=_schema(
            "sensorium",
            "Compact live Sensorium aperture: status, ingest deferred salience, open a pointer/thread, or update by keyword.",
            {
                "action": {
                    "type": "string",
                    "enum": ["status", "ingest", "open", "update"],
                    "description": "status|ingest|open|update",
                },
                "text": {"type": "string", "description": "Short salience summary or update reason."},
                "kind": {"type": "string", "description": "Optional salience kind for ingest."},
                "strength": {"type": "number", "description": "Optional ingest strength 0-1."},
                "id": {"type": "string", "description": "Thread id, or latest."},
                "keyword": {
                    "type": "string",
                    "description": "Update keyword: close, hold, resume, archive, mark_reviewed, pin, or unpin.",
                },
                "surface": {"type": "string", "description": "local or discord; defaults local."},
            },
        ),
        handler=_handle_live_sensorium,
        description="Compact foreground Sensorium keyword tool.",
    )

    ctx.register_tool(
        name="sensorium_status",
        toolset=_ADMIN_TOOLSET,
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
        toolset=_ADMIN_TOOLSET,
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
        name="sensorium_sensor_config",
        toolset=_ADMIN_TOOLSET,
        schema=_schema(
            "sensorium_sensor_config",
            "Register, modify, pause, deprecate, or list deterministic Sensorium sensor kinds. Config-only; no sensor execution or task spawning.",
            {
                **common,
                "action": {
                    "type": "string",
                    "enum": ["list", "register", "modify", "pause", "deprecate"],
                    "description": "Registry action to perform.",
                },
                "name": {
                    "type": "string",
                    "description": "Sensor kind name for register/modify/pause/deprecate.",
                },
                "defaults": {
                    "type": "object",
                    "description": "Optional compact signal defaults: strength_hint, sensitivity, allowed_surfaces, correlation_keys.",
                },
                "status": {
                    "type": "string",
                    "enum": ["active", "paused", "deprecated"],
                    "description": "Explicit status for register/modify; pause/deprecate override this.",
                },
            },
        ),
        handler=lambda args, **kw: handle_sensorium_sensor_config(
            action=args.get("action", "list"),
            name=args.get("name", ""),
            defaults=args.get("defaults"),
            status=args.get("status", "active"),
            instance=_arg_instance(args),
            state_dir=args.get("state_dir"),
        ),
        description="Runtime sensor registry management without external action.",
    )
    ctx.register_tool(
        name="sensorium_profile",
        toolset=_ADMIN_TOOLSET,
        schema=_schema(
            "sensorium_profile",
            "List, show, initialize, or set the default Sensorium profile (a named runtime config/state namespace). Config-only: writes only a profile's instance.config.json or the active-profile marker; no sensor execution, ingestion, or arbitrary file writes.",
            {
                "action": {
                    "type": "string",
                    "enum": ["list", "show", "init", "set_default"],
                    "description": "Profile operation: list profiles, show one profile's resolved config, init a new profile, or set the active/default profile.",
                },
                "profile": {
                    "type": "string",
                    "description": "Profile name for show/init/set_default. Safe names only; defaults to the active profile for show.",
                },
                "overwrite": {
                    "type": "boolean",
                    "description": "For init: overwrite an existing instance.config.json with defaults. Defaults to false.",
                },
            },
        ),
        handler=lambda args, **kw: handle_sensorium_profile(
            action=args.get("action", "list"),
            profile=args.get("profile", ""),
            overwrite=bool(args.get("overwrite", False)),
        ),
        description="Manage Sensorium profiles (config/state namespaces) without external action.",
    )
    ctx.register_tool(
        name="sensorium_ingest_event",
        toolset=_ADMIN_TOOLSET,
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
        name="sensorium_candidate_update",
        toolset=_ADMIN_TOOLSET,
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
        toolset=_ADMIN_TOOLSET,
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
        toolset=_ADMIN_TOOLSET,
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
        toolset=_ADMIN_TOOLSET,
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
        toolset=_ADMIN_TOOLSET,
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
        toolset=_ADMIN_TOOLSET,
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
        toolset=_ADMIN_TOOLSET,
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
        toolset=_ADMIN_TOOLSET,
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
        toolset=_ADMIN_TOOLSET,
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
                "memory_context": {
                    "type": "array",
                    "description": "Optional 3-8 compact Hindsight-recall/context facts for memory-grounded review; each item needs fact, source_tool, and source_refs.",
                    "items": {"type": "object"},
                },
                "retrieval_skipped_reason": {"type": "string", "description": "Explicit reason bounded memory/context retrieval was skipped or unavailable."},
                "cited_memory_fact_refs": {
                    "type": "array",
                    "description": "Fact refs from memory_context that affected the choice.",
                    "items": {"type": "string"},
                },
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
            memory_context=args.get("memory_context"),
            retrieval_skipped_reason=args.get("retrieval_skipped_reason") or "",
            cited_memory_fact_refs=args.get("cited_memory_fact_refs"),
            instance=_arg_instance(args),
            state_dir=args.get("state_dir"),
        ),
        description="Record a conscious Sensorium attention-policy decision receipt.",
    )
    ctx.register_tool(
        name="sensorium_attention_policy_manage",
        toolset=_ADMIN_TOOLSET,
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
        toolset=_ADMIN_TOOLSET,
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
        toolset=_ADMIN_TOOLSET,
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
        name="sensorium_media_gift_decide",
        toolset=_ADMIN_TOOLSET,
        schema=_schema(
            "sensorium_media_gift_decide",
            "Apply mediated-presence gift conscious-choice policy and write receipts. Subconscious may only propose; no automatic outbound delivery or scheduler spam.",
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
                    "enum": ["inner_salience", "operator_prompt", "manual_review", "worker_result", "scheduler", "test"],
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
                "target_ref": {"type": "string", "description": "Compact configured target ref, e.g. discord:#general or dm alias."},
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
        toolset=_ADMIN_TOOLSET,
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
        toolset=_ADMIN_TOOLSET,
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
