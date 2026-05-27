# Agent Sensorium

Bounded autonomous inner lifecycle for Hermes agents: compact signals, filtered events, candidates, dormant conscious thread capsules, and pull-based review. The aim is not a smarter notification system; it is an environment-reactive inner-life substrate that can shape attention over time and, eventually, support emotion-like salience.

## MVP stance

Build the sensorium spine first:

- local plugin skeleton;
- local JSONL state;
- signal ingest;
- deterministic signal/event/candidate promotion;
- dormant thread capsules;
- active-session pointer doorways gated by surface/privacy/cooldown;
- pull-based status;
- tests and dry-run smoke.

Explicitly out of scope for MVP:

- proactive messages;
- Discord/platform thread creation;
- relational autonomy / `REACH_OUT`;
- model-backed Subconscious passes;
- Hindsight/RSS/file-crawl sensors;
- external task creation;
- dashboard UI.

## Docs

- `docs/agent-sensorium-plugin-mvp.md` — architecture/spec working doc.
- `docs/agent-sensorium-mvp-implementation-plan.md` — original MVP build plan and acceptance gates.
- `docs/agent-sensorium-buildout-plan-2026-05-25.md` — post-MVP full build-out backlog and phase gates.
- `docs/extending-sensors-and-subconscious-jobs.md` — extension contract for adding cheap deterministic sensors and bounded Subconscious reasoning jobs.

## Development shape

Runtime plugin target will eventually be:

```text
~/.hermes/plugins/agent-sensorium/
```

Development should happen in this repo first. Do not edit the live plugin path until tests pass and the change is intentionally installed/synced.

## Verification

Expected MVP verification once code exists:

```bash
python -m pytest tests -q
python -m py_compile agent_sensorium/*.py scripts/*.py
```

Local smoke should stay read-only/dry-run unless explicitly invoked otherwise:

```bash
python scripts/sensorium_tick.py --instance sera --dry-run
```
