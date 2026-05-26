"""Hermes plugin registration surface."""

from __future__ import annotations

from pathlib import Path
from typing import Any

_TOOLSET = "agent-sensorium"


def _default_instance() -> str:
    """Return configured default instance for commands/tools without explicit instance."""
    try:
        from importlib import import_module

        config_mod = import_module("hermes_cli.config")
        value = config_mod.cfg_get(
            config_mod.load_config(), "agent_sensorium", "default_instance", default="default"
        )
        if isinstance(value, str) and value.strip():
            return value.strip()
    except Exception:
        pass
    return "default"


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
    from .tools import (
        handle_sensorium_attention_pointer,
        handle_sensorium_candidate_update,
        handle_sensorium_compact,
        handle_sensorium_dispatch_once,
        handle_sensorium_ingest_event,
        handle_sensorium_ingest_signal,
        handle_sensorium_service_threads,
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
            "Select the top eligible candidate and optionally create one dormant conscious thread.",
            {
                **common,
                "dry_run": {
                    "type": "boolean",
                    "description": "If true, preview without mutating state.",
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
            dry_run=bool(args.get("dry_run", False)),
            config=args.get("config"),
        ),
        description="Select top candidate and create one dormant conscious thread.",
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

    skill_path = Path(__file__).resolve().parents[1] / "skills" / "agent-sensorium" / "SKILL.md"
    if skill_path.exists():
        ctx.register_skill(
            "agent-sensorium",
            skill_path,
            description="Review and operate Agent Sensorium candidates and conscious thread capsules.",
        )
