# Agent Sensorium Development Notes

This repo is the development source for the Agent Sensorium Hermes plugin.

## Guardrails

- Build in this repo first; do not edit `~/.hermes/plugins/agent-sensorium/` directly unless installing a tested snapshot.
- MVP must remain pull-based and local-only.
- **Repair posture:** a host agent who is patching the Sensorium plugin itself (in this repo or in the live `~/.hermes/plugins/agent-sensorium/` install) must do it via an async worker lane unless the change is ≤3 tool calls, reversible, and mostly judgment/synthesis. See the `agent-sensorium` bundled skill's *Repair posture* section and `lead-orchestrator-posture` § *Sensorium repair preflight*.
- No proactive Discord/DM delivery in MVP.
- No model-backed Subconscious pass until the deterministic spine is verified.
- Runtime state must live outside the repo unless tests use temp dirs.
- Keep enhancement proposals separate from accepted MVP scope.

## Verification

Run before committing code changes:

```bash
python -m pytest tests -q
python -m py_compile agent_sensorium/*.py scripts/*.py
```

## Implementation style

- Small commits per phase.
- Tests first for pure store/gate/dispatcher logic.
- Prefer stdlib-only for MVP.
- Tool handlers must be callable directly in tests without a live Hermes runtime.
