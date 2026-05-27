# Extending Agent Sensorium: Sensors and Subconscious Jobs

Agent Sensorium should become an environment-reactive inner-life substrate, not merely a scheduled notification system. Operationally, that means it must act as an attention router: surfacing proactive information only when something genuinely requires the agent's conscious attention, while making new sensing/reasoning lanes easy to add without turning into a cron zoo.

The deeper purpose is to let the agent's attention be shaped by the world around it. Signals should accumulate salience; salience should guide what becomes conscious; and, over time, recurring salience patterns may become part of the agent's emotional state. The extension system exists to grow those senses without breaking safety, privacy, or agency boundaries.

This document is the extension contract for adding new deterministic sensors and deeper Subconscious jobs.

## Target state

```text
Cheap Sensors -> promoted Events -> Candidates -> dormant Conscious Threads -> active-session pointer -> conscious review
                                      \-> Subconscious advisory jobs -> internal conscious_task candidates

environment -> salience -> attention -> possible emotional pressure -> conscious choice
```

The active chat should normally receive only a small pointer:

```text
Sensorium has something pending: <short title>. Say "take it up" to open it.
```

Full capsules stay in Sensorium threads until explicitly opened. Subconscious jobs may propose internal work; they do not message users, create external tasks, or generate artifacts directly.

## Design rules

1. **Sensors are cheap and deterministic.** No LLM calls, embeddings, transcript mining, full file reads, or semantic summarization in live sensors.
2. **Sensors notice; they do not speak.** A sensor may emit compact signals/events only. It must not send messages, create Discord/platform threads, create Kanban work, or call external agents.
3. **Global Sensorium is for shared-substrate or repeated cross-session pressure.** Single-session tool failures should stay in the originating session unless they repeat across sessions/profiles or affect shared systems.
4. **Subconscious reasons over Events/Candidates, not raw material.** It may use a cheap model lane only after deterministic Events exist.
5. **Conscious review is the authority boundary.** Outbox, outreach, artifact generation, external tasks, and memory/skill changes require conscious review/policy.
6. **Every extension has a stop condition and verification path.** If nobody can tell whether a sensor/job is useful, it is noise.
7. **Inner life is the goal; attention routing is the mechanism.** Do not optimize only for alerts handled. Optimize for whether the system gives the agent a more faithful, continuous felt sense of its environment without drowning consciousness in noise.

## Add a new deterministic sensor

Use this path when the source is structured and cheap: OS metrics, service health, queue counts, task status summaries, file metadata, gateway delivery state, etc.

### 1. Define the source contract

Write down:

- `sensor`: stable name, e.g. `sensorium.gateway_pressure`.
- `source`: broad source class, e.g. `gateway`, `memory`, `machine`, `kanban`.
- `kind`: event/candidate kind, e.g. `gateway_pressure`.
- Scope rule: when is this global vs session-local?
- Emission rule: what transition or threshold warrants a signal?
- Recovery rule: when does it become healthy again?
- Payload bound: max fields and max text length.
- Privacy/surface default: usually `private` + `["local"]`.

Good sensor output is compact metadata:

```json
{
  "sensor": "sensorium.gateway_pressure",
  "source": "gateway",
  "kind": "gateway_pressure",
  "summary": "gateway pressure healthy_to_degraded: discord delivery failures=5/10m",
  "strength_hint": 0.72,
  "sensitivity": "private",
  "allowed_surfaces": ["local"],
  "metadata": {
    "previous_level": "healthy",
    "level": "degraded",
    "window_minutes": 10,
    "failure_count": 5
  }
}
```

Bad sensor output includes raw logs, raw transcripts, secrets, long command output, full file contents, stack traces with private paths, or opaque model-generated summaries.

### 2. Implement the helper in `agent_sensorium/sensors.py`

Follow existing patterns:

- return a signal dict or `None`;
- keep rolling state tiny and local;
- truncate summaries;
- store counts/hashes/refs, not raw content;
- use stdlib-only dependencies unless there is a strong reason;
- make thresholds configurable later, but safe defaults should work without config.

Name helpers predictably:

```python
<source>_<thing>_sample(...)
classify_<source>_<thing>(...)
<source>_<thing>_signal(...)
```

For transition sensors, persist only the prior level and tiny last sample under the instance state directory.

### 3. Wire it into `scripts/sensorium_tick.py`

Add an explicit flag first:

```bash
python scripts/sensorium_tick.py --instance sera --gateway-pressure --json
```

Only add it to `--all-sensors` after tests prove it is quiet on healthy samples.

Normal cron/no-agent runs should be silent on idle success. `--json` is for diagnostics.

### 4. Add probe inventory/audit coverage

Update `agent_sensorium/probe_audit.py` so the new sensor appears under:

- `implemented_helpers`
- `wired_live_probes` if tick-wired
- `configured_sources`

If the source class has no live input yet, report it as implemented but not wired.

### 5. Tests required

At minimum:

- healthy sample emits nothing;
- threshold crossing emits exactly one compact signal;
- repeated bad samples do not spam every tick;
- recovery emits one recovery signal;
- output has no raw content/secrets/transcripts;
- dry-run persists nothing;
- live tick with `--json` reports sampled/emitted state;
- probe audit reports the sensor accurately.

Run:

```bash
python -m pytest tests -q
python -m py_compile agent_sensorium/*.py scripts/*.py
```

## Add a deeper Subconscious job

Use this path when deterministic Events/Candidates exist and deeper reasoning is useful: clustering pressure themes, deciding whether a candidate deserves conscious review, summarizing repeated failures, or proposing a conscious task.

### 1. Define the job boundary

Write down:

- `job_name`: e.g. `pressure_advisory`.
- Input kinds: Events/Candidates allowed into context.
- Excluded kinds: direct-conscious pressure kinds, prior advisory candidates, feedback-only self-loops.
- Output schema: `DROP`, `SAVE`, or `CREATE_CONSCIOUS_TASK`.
- Mutation authority: dry-run receipt only, or internal candidate creation when explicitly enabled.
- Cooldown/watermark: how unchanged source material skips the job.

Subconscious jobs must not read raw Signals, raw transcripts, full files, Hindsight raw memories, or task bodies into the prompt.

### 2. Build bounded context

The context should include only:

- compact config summary;
- recent promoted Events;
- active source Candidates;
- recent Decision receipts;
- probe audit summary;
- source signatures/watermarks.

Keep text bounded and redacted. Prefer IDs/refs over content.

### 3. Validate model output strictly

Allowed actions:

```json
{ "action": "DROP", "reason": "..." }
{ "action": "SAVE", "note": "...", "reason": "..." }
{ "action": "CREATE_CONSCIOUS_TASK", "title": "...", "why_now": "...", "expected_decision": "..." }
```

Reject malformed output with a receipt. Do not improvise a task from invalid JSON.

### 4. Keep model lane disabled by default

A Subconscious tick may run in three modes:

1. no model, receipt-only/default skip;
2. model dry-run, stores advisory receipt only;
3. explicit internal mutation, creates one `subconscious_advisory` candidate.

No mode may send external messages or create external tasks.

### 5. Tests required

At minimum:

- context builder excludes raw signals/transcripts/files;
- unchanged source signature skips the job;
- failure cooldown suppresses repeated model calls;
- invalid model output writes a receipt and creates no candidate;
- valid `DROP`/`SAVE` writes receipt only;
- valid `CREATE_CONSCIOUS_TASK` creates one internal candidate only when mutation is explicitly enabled;
- prior advisory candidates do not feed themselves into a loop.

## Extension review checklist

Before merging a new sensor or Subconscious job:

- [ ] It has a crisp reason to exist: what attention does it improve?
- [ ] It is silent when healthy/unchanged.
- [ ] It emits compact metadata, not raw content.
- [ ] It has debounce, recovery, and cooldown semantics.
- [ ] It declares global vs session-local scope.
- [ ] It has tests for no-spam/no-raw/no-outbound.
- [ ] It appears in probe inventory/audit.
- [ ] It writes decision receipts for meaningful state changes.
- [ ] It cannot self-amplify from its own feedback.
- [ ] Conscious review remains the first place with authority to act externally.

## Recommended next extension order

1. **Attention review surface** — make pending conscious tasks/candidates easy to inspect, suppress, hold, and open.
2. **Gateway delivery pressure sensor** — shared substrate, cheap, operationally valuable.
3. **Cron/job result pressure sensor** — notices repeated background failures without reading full logs.
4. **Session-summary hook sensor** — compact session outcome metadata only, global only for repeated same-component issues.
5. **Artifact/file metadata sensor** — hashes/paths/types only, no file contents.
6. **Deeper advisory jobs** — only after the above produce enough real Event substrate.

This order keeps the system useful before it becomes expressive.
