# Agent Sensorium

Bounded autonomous inner lifecycle for Hermes agents: compact signals, filtered events, candidates, dormant conscious thread capsules, and pull-based review.

## MVP stance

Build the sensorium spine first:

- local plugin skeleton;
- local JSONL state;
- signal ingest;
- deterministic signal/event/candidate promotion;
- dormant thread capsules;
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
- `docs/agent-sensorium-mvp-implementation-plan.md` — build plan and acceptance gates.

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
