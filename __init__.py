"""Hermes plugin entrypoint for Agent Sensorium."""

try:  # Hermes directory plugin import: hermes_plugins.<slug>
    from .agent_sensorium.plugin import register
except ImportError:  # Pytest may import this root file as a bare module.
    from agent_sensorium.plugin import register

__all__ = ["register"]
