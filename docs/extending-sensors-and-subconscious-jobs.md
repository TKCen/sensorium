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
I have something for you: <short title>. Say "take it up" to open it.
```

Full capsules stay in Sensorium threads until explicitly opened. Subconscious jobs may propose internal work; they do not message users, create external tasks, or generate artifacts directly.

## Design rules

1. **Sensors are cheap and deterministic.** No LLM calls, embeddings, transcript mining, full file reads, or semantic summarization in live sensors.
2. **Sensors notice; they do not speak.** A sensor may emit compact signals/events only. It must not send messages, create Discord/platform threads, create Kanban work, or call external agents.
3. **Global Sensorium is for shared-substrate or repeated cross-session pressure.** Single-session tool failures should stay in the originating session unless they repeat across sessions/profiles or affect shared systems.
4. **Subconscious reasons over Events/Candidates, not raw material.** It may use a cheap model lane only after deterministic Events exist.
5. **Conscious review is the authority boundary.** Outbox, outreach, artifact generation, external tasks, and memory/skill changes require conscious review/policy. Relational, identity, mediated-presence, explicit-correction, and Sensorium-strategy reviews require bounded memory/context grounding before choice: use Hindsight `recall` by default, attach 3–8 compact `memory_context` facts with `source_tool`/`source_refs` plus `cited_memory_fact_refs`, or record `retrieval_skipped_reason` if retrieval is unavailable. Do not replace this with Hindsight `reflect` or a naked numeric threshold tweak.
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
python scripts/sensorium_tick.py --instance demo --gateway-pressure --json
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

## Add a hot-reloadable prepare actuator

Use this path when conscious review should prepare a local artifact (text/audio/image/video ref) through a trusted local script. Actuators are not sensors: they run only after a conscious decision and they never authorize delivery themselves.

### 1. Define the actuator contract

Write down:

- `name`: stable registry name, e.g. `prepare_text_note`.
- `kind`: currently `prepare_artifact`.
- `capability`: short closed label, e.g. `prepare_text_artifact` or `tts_voice_note`.
- `impl.command`: argv-list command. Never a shell string.
- `script_roots`: local directories the script path may live under.
- `input_contract.allowed_request_types`: e.g. `PRIVATE_EXPRESSION`, `REACH_OUT`, `PREPARE_ARTIFACT`.
- `input_contract.requires_conscious_decision`: normally `true`.
- `output_contract.artifact_kinds`: allowed artifact kinds.
- `output_contract.delivery_authorized`: always `false` for generic prepare lanes.

Example registry entry:

```json
{
  "version": 1,
  "actuators": {
    "demo_prepare_text_artifact": {
      "status": "active",
      "kind": "prepare_artifact",
      "capability": "prepare_text_artifact",
      "impl": {
        "type": "script",
        "command": ["python3", "~/.hermes/plugins/agent-sensorium/examples/demo_script_actuator.py"]
      },
      "script_roots": ["~/.hermes/plugins/agent-sensorium/examples"],
      "schedule": {"timeout_seconds": 10},
      "caps": {"max_stdout_bytes": 8192, "max_stderr_bytes": 4096},
      "input_contract": {
        "allowed_request_types": ["PRIVATE_EXPRESSION", "REACH_OUT", "PREPARE_ARTIFACT"],
        "requires_conscious_decision": true,
        "max_message_chars": 1200
      },
      "output_contract": {
        "artifact_kinds": ["text"],
        "delivery_authorized": false
      }
    }
  }
}
```

### 2. Keep the script contract boring

The script receives one compact JSON object on stdin. It emits one JSON object on stdout:

```json
{
  "artifact": {
    "kind": "text",
    "ref_path": "artifact://demo/prepared-text-note",
    "delivery_state": "prepared",
    "intended_handoff_mode": "present_thread",
    "sensitivity": "private",
    "allowed_surfaces": ["local"]
  },
  "summary": "Prepared a local artifact reference.",
  "delivery_authorized": false,
  "outbound_delivery": false
}
```

Do not print raw private prompts, secrets, tokens, command output, or stack traces. The runner bounds stdout/stderr and records sanitized error categories, but the script should still be written as if stdout is a public contract.

### 3. Hot reload without gateway schema changes

Actuator behavior lives in `~/.hermes/agent-sensorium/<profile>/actuators/registry.json` and trusted local scripts. The registry is read on every actuator run, so edits take effect on the next invocation. Adding or changing actuators does **not** require adding new Hermes tools or changing the live `sensorium` schema.

Gateway/plugin restarts are still required for new Python modules, plugin hooks, dashboard routes, or model-visible tool schemas. Keep volatile actuator behavior in registries and scripts.

### 4. Tests required

At minimum:

- registry config persists but does not run scripts;
- registry edits hot-reload between runs;
- missing `conscious_decision_ref` blocks before script execution;
- script attempts to set `delivery_authorized` or `outbound_delivery` are rejected;
- decisions/artifact receipts do not include raw private message text;
- timeout, nonzero exit, malformed JSON, stdout cap, and stderr cap are sanitized.

Run:

```bash
python -m pytest tests/test_actuators.py tests/test_script_sensor.py -q -o 'addopts='
python -m pytest -q -o 'addopts='
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
- [ ] Script actuators require conscious decision refs and never set `delivery_authorized` / `outbound_delivery` true.
- [ ] It appears in probe inventory/audit when it is a sensor or scheduled probe.
- [ ] It writes decision receipts for meaningful state changes.
- [ ] Any new dashboard/API projection is read-only, compact-only, and covered by a hostile-value privacy smoke; see `dashboard-and-review-surfaces.md`.
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
