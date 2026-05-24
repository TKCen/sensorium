"""Hermes plugin registration surface.

This module would be called by Hermes plugin loader via register(ctx).
For MVP development, it documents the registration contract without
requiring a live Hermes runtime.
"""

from pathlib import Path


def register(ctx) -> None:
    """Register Agent Sensorium plugin tools, command, and skill."""
    from .commands import handle_sensorium_command
    from .tools import (
        handle_sensorium_candidate_update,
        handle_sensorium_compact,
        handle_sensorium_dispatch_once,
        handle_sensorium_ingest_signal,
        handle_sensorium_status,
    )

    ctx.register_tool(
        "sensorium_status",
        handle_sensorium_status,
        description="Read-only snapshot of Agent Sensorium state: counts, top candidates.",
    )
    ctx.register_tool(
        "sensorium_ingest_signal",
        handle_sensorium_ingest_signal,
        description="Ingest a low-level signal and promote if threshold is met.",
    )
    ctx.register_tool(
        "sensorium_dispatch_once",
        handle_sensorium_dispatch_once,
        description="Select top candidate and create one dormant conscious thread.",
    )
    ctx.register_tool(
        "sensorium_candidate_update",
        handle_sensorium_candidate_update,
        description="Update candidate status: suppress, hold, cancel, or mark_reviewed.",
    )
    ctx.register_tool(
        "sensorium_compact",
        handle_sensorium_compact,
        description="Archive expired candidates and threads with decision receipts.",
    )

    ctx.register_command(
        "sensorium",
        handle_sensorium_command,
        description="Pull-based sensorium status and review.",
    )

    skill_path = Path(__file__).parent / "skills" / "agent-sensorium" / "SKILL.md"
    if skill_path.exists():
        ctx.register_skill(
            "agent-sensorium",
            skill_path,
            description="Review and operate Agent Sensorium candidates and conscious thread capsules.",
        )
