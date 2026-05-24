"""Hermes plugin registration surface.

This module would be called by Hermes plugin loader via register(ctx).
For MVP development, it documents the registration contract without
requiring a live Hermes runtime.
"""

from pathlib import Path


def register(ctx) -> None:
    """Register Agent Sensorium plugin tools and skill."""
    from .tools import handle_sensorium_ingest_signal, handle_sensorium_status

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

    skill_path = Path(__file__).parent / "skills" / "agent-sensorium" / "SKILL.md"
    if skill_path.exists():
        ctx.register_skill(
            "agent-sensorium",
            skill_path,
            description="Review and operate Agent Sensorium candidates and conscious thread capsules.",
        )
