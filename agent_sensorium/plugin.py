"""Hermes plugin registration surface."""

from __future__ import annotations

from pathlib import Path
from typing import Any

_TOOLSET = "agent-sensorium"


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
    from .tools import (
        handle_sensorium_candidate_update,
        handle_sensorium_compact,
        handle_sensorium_dispatch_once,
        handle_sensorium_ingest_signal,
        handle_sensorium_status,
    )

    common = {
        "instance": {
            "type": "string",
            "description": "Sensorium instance name. Defaults to 'default'.",
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
            "Read-only snapshot of Agent Sensorium state: counts, top candidates, and visible threads.",
            common,
        ),
        handler=lambda args, **kw: handle_sensorium_status(
            instance=args.get("instance", "default"),
            state_dir=args.get("state_dir"),
        ),
        description="Read-only snapshot of Agent Sensorium state.",
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
            instance=args.get("instance", "default"),
            state_dir=args.get("state_dir"),
        ),
        description="Ingest a low-level signal and promote if threshold is met.",
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
            instance=args.get("instance", "default"),
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
            instance=args.get("instance", "default"),
            state_dir=args.get("state_dir"),
        ),
        description="Update candidate status with a decision receipt.",
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
            instance=args.get("instance", "default"),
            state_dir=args.get("state_dir"),
        ),
        description="Archive expired candidates and threads with decision receipts.",
    )

    ctx.register_command(
        "sensorium",
        handle_sensorium_command,
        description="Pull-based sensorium status and review.",
        args_hint="status|threads|dispatch|compact|help",
    )

    skill_path = Path(__file__).resolve().parents[1] / "skills" / "agent-sensorium" / "SKILL.md"
    if skill_path.exists():
        ctx.register_skill(
            "agent-sensorium",
            skill_path,
            description="Review and operate Agent Sensorium candidates and conscious thread capsules.",
        )
