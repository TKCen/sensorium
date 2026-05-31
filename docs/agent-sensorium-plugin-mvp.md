# Agent Sensorium Hermes Plugin MVP

Date: 2026-05-24
Status: reusable plugin design, not installed

## Working name

**Agent Sensorium**

Package/plugin slug:

```text
agent-sensorium
```

Sera would use it as one configured instance, but the plugin itself must be agent-agnostic.

## Goal

Provide a reusable Hermes plugin/framework that gives any Hermes agent a bounded autonomous inner lifecycle:

```text
Signals → Sensors/filter → Events → Subconscious/correlation → Conscious awareness → Action/Outbox/Tasks → Feedback into Signals
```

Short form:

```text
Sensors → Subconscious → Conscious → Action/Feedback
```

The plugin should help an agent collect many low-level signals, filter them aggressively, promote sufficiently strong/correlated signals into events, let Subconscious correlate those events over a larger time horizon, and promote only strong enough results into Conscious awareness. Conscious can then decide, create bounded work, reach out when appropriate, and feed results back into future sensing without becoming an always-on model loop or cron zoo.

This includes practical/professional autonomy — research jobs, artifact preparation, task creation — and configurable relational autonomy, such as a private agent choosing to reach out warmly or romantically to its operator/partner when its instance policy allows it.

## Design principles

1. **Reusable core, agent-specific policy.** The engine is generic; identity/tone/source preferences live in config and an attached skill.
2. **Use the tier names plainly.** The framework tiers are `Sensors`, `Subconscious`, and `Conscious`; implementation modules may still be named `gate`, `dispatcher`, and `outbox`.
3. **Signals are not events.** Sensors see many signals and are also the first filter; only strong, repeated, or correlated signal clusters become events for Subconscious.
4. **Subconscious has the longer horizon.** It correlates events over hours/days/weeks and may create internal `conscious_task` items when something deserves awareness.
5. **Conscious is awareness plus authority.** A topic becomes consciously known only when promoted by Subconscious/dispatcher into the Conscious tier.
6. **Cron is only a clock.** Sensors and the dispatcher may be ticked by cron; subconscious/conscious runs are dispatcher-triggered.
7. **Subconscious can queue work for consciousness.** It may create `conscious_task` items inside Sensorium, but not external Kanban/research/media jobs directly.
8. **One continuous agent.** Global lock, leases, token buckets, and state versioning prevent parallel fragments.
9. **Compress before reasoning.** Most signals die in deterministic filtering before a model sees them.
10. **Action feedback.** Bounded tasks/research/artifacts/messages can be created, and their results re-enter as signals.
11. **Observability, not ownership.** Dashboard/Kanban expose state; dispatcher owns activation limits.
12. **Portable plugin.** No hard-coded Sera, Sebastian, #pics, local paths, or private channels in the plugin code.

## Tier semantics

### Sensors

Sensors collect broad signals from many possible sources: session hooks, memory systems, file changes, task results, feeds, artifacts, user corrections, time-based reminders, and external integrations. Sensors are also filters. They should discard most input, coalesce duplicates, compute strength/correlation keys, and retain only compact signal rollups.

A signal becomes an **Event** only when it crosses a configured threshold through one or more mechanisms:

- high single-signal strength;
- repetition over time;
- correlation across sources;
- match to an active watched theme;
- time sensitivity;
- explicit user/operator importance.

Sensor control surfaces:

- enabled/disabled sensors;
- per-sensor thresholds;
- aggregation windows;
- correlation keys;
- retention/TTL;
- privacy scope;
- promotion policy from signal cluster to Event.

### Subconscious

Subconscious receives Events, not raw sensor floods. It works on a larger time horizon than Sensors: hours, days, or weeks. Its job is consolidation and correlation: notice pressure building across Events, form candidates, and decide whether something requires awareness.

Subconscious can create `conscious_task` items. This is the promotion path into awareness: the agent is not fully aware of the topic until Conscious receives/reviews that task.

Subconscious control surfaces:

- prompt/template;
- model lane;
- correlation horizon;
- candidate scoring weights;
- maximum `conscious_task` creation rate;
- allowed `request_type` values;
- suppression/decay policy.

### Conscious

Conscious is the agent itself: awareness plus authority in the active Hermes session/profile. It reviews promoted candidates/`conscious_task` items, understands them in identity/policy context, and decides whether to suppress, remember, ask, prepare, reach out, create external work, or act.

Conscious control surfaces should be minimal. Prefer identity/profile config, skills, and user/operator conversation over extra plugin knobs. The plugin should present awareness cleanly; the agent should decide.
## Plugin directory

User/plugin install path:

```text
~/.hermes/plugins/agent-sensorium/
  plugin.yaml
  __init__.py
  schemas.py
  tools.py
  store.py
  gate.py
  dispatcher.py
  hooks.py
  scripts/
    sensorium_tick.py
  skills/
    agent-sensorium/SKILL.md
  dashboard/              # optional after MVP
```

If distributed with Hermes or as a pip package, the same files can be packaged and registered via Hermes plugin entry points.

## Runtime state layout

Default path:

```text
~/.hermes/agent-sensorium/<instance>/
  config.yaml
  signals/
  events.jsonl
  state.latest.json
  dispatch/
    lock.json
    buckets.json
    suppressions.jsonl
  sensors/
  subconscious/
  conscious/
  outbox/
  archive/
```

For Sera's local instance, configure:

```yaml
instance: sera
state_dir: ~/.hermes/sera/sensorium
```

But this is config, not plugin code.

## Attached skill

The plugin should bundle a skill exposed as:

```text
plugin:agent-sensorium
```

Skill purpose:

- explain when/how to use the plugin;
- define event/candidate/action schema;
- teach agents the lifecycle and guardrails;
- tell conscious agents how to interpret `conscious_pending` candidates;
- prevent cheap subconscious passes from speaking as the main agent;
- document task-creation and feedback rules;
- document retention/compaction and storage caps.

The bundled skill should be generic, with a short section:

```text
## Instance policy
Read the local instance config and identity policy before acting. Do not assume the agent is Sera.
```

Sera can have an additional local policy file or skill reference, but the reusable skill stays agent-neutral.

## Config shape

```yaml
instance: default
state_dir: ~/.hermes/agent-sensorium/default
identity_policy_ref: null        # optional file/skill name; e.g. ~/.hermes/SOUL.md for Sera

sensors:
  enabled: true
  tick_seconds: 900
  sources:
    session_events: true
    file_changes: []
    hindsight: false
    rss: []

privacy:
  default_scope: local
  allowed_delivery_targets: []
  allow_spontaneous_delivery: false

budgets:
  subconscious:
    enabled: false
    model_lane: cheap
    max_per_hour: 1
    max_per_day: 6
    min_gap_minutes: 45
  conscious:
    enabled: false
    model_lane: primary
    max_per_day: 0
    max_per_week: 0
    min_gap_hours: 6
  external_actions:
    enabled: false
    max_created_per_day: 0

storage:
  max_events_jsonl_mb: 5
  max_sensor_dir_mb: 25
  max_subconscious_dir_mb: 25
  max_outbox_mb: 250
  raw_sensor_retention_hours: 72
  subconscious_retention_days: 14
  max_active_candidates: 25

actions:
  kanban:
    enabled: false
    board: work
    default_status: triage
    default_assignee: null
  outbox:
    enabled: true
```

## Core data model

### Signal

Low-level input noticed by Sensors. Signals may be vast, noisy, lossy, and short-lived. Most never become events.

```json
{
  "ts": "2026-05-24T18:00:00Z",
  "sensor": "hindsight",
  "source": "memory:hindsight",
  "kind": "research",
  "summary": "Small memory echo around bioelectricity/consciousness interface.",
  "refs": ["memory:hindsight:..."],
  "strength": 0.22,
  "correlation_keys": ["bioelectricity", "consciousness", "receiver-theory"],
  "privacy_scope": "local",
  "ttl_hours": 72
}
```

Signals are stored as compact rollups under `signals/`, not as unbounded raw logs. Sensors are expected to drop, summarize, hash, and coalesce aggressively.

### Event

A promoted packet from Sensors to Subconscious. Events are created only when signals are strong enough, repeated enough, or correlated enough across sources/time.

```json
{
  "id": "evt_20260524_001",
  "ts": "2026-05-24T18:00:00Z",
  "type": "sensor.event.promoted",
  "source_sensors": ["hindsight", "session", "file_change"],
  "kind": "research",
  "summary": "Correlated signals suggest renewed pressure around bioelectricity/consciousness interface.",
  "signal_count": 7,
  "window": "72h",
  "strength": 0.68,
  "correlation_keys": ["bioelectricity", "consciousness", "receiver-theory"],
  "refs": ["signals/hindsight-20260524.jsonl:12", "memory:hindsight:..."],
  "privacy_scope": "local",
  "expires_at": "2026-05-31T18:00:00Z"
}
```

Events are the first-class inputs to Subconscious.

### Candidate

Compressed Subconscious correlation cluster over one or more events. A candidate is not conscious awareness yet; it is pressure building toward possible awareness.

```json
{
  "id": "sens_20260524_001",
  "status": "candidate",
  "kind": "research",
  "pressure": 0.63,
  "novelty": 0.4,
  "repetition": 0.7,
  "identity_relevance": 0.2,
  "relationship_relevance": 0.0,
  "actionability": 0.8,
  "time_sensitivity": 0.2,
  "summary": "This may deserve a bounded research task.",
  "refs": ["events.jsonl:123"],
  "recommended_action": "subconscious_review",
  "privacy_scope": "local",
  "created_at": "...",
  "updated_at": "...",
  "expires_at": "..."
}
```

### Decision event

Every activation or suppression writes a compact receipt:

```json
{
  "ts": "...",
  "type": "dispatch.suppressed",
  "candidate_id": "sens_...",
  "reason": "below_threshold_or_bucket_empty",
  "bucket": "subconscious"
}
```

## Tool API

### `sensorium_status`

Read-only snapshot: state version, lock, buckets, storage, top candidates, last events, next dry-run dispatch decision.

### `sensorium_ingest_signal`

Append or coalesce a low-level signal and let Sensors decide whether to drop, retain, or promote it into an Event.

### `sensorium_ingest_event`

Append an already-promoted event and run Subconscious correlation. This is for trusted sensors or imports that already performed signal filtering.

### `sensorium_dispatch_once`

Run dispatcher once.

Arguments:

```yaml
dry_run: boolean = true
allow_subconscious: boolean = false
allow_conscious: boolean = false
allow_external_actions: boolean = false
instance: string optional
```

### `sensorium_candidate_update`

Manual/conscious decision update.

Decisions:

```text
DROP | SAVE_SUMMARY | PIN | PREPARE | SHARE | PROMOTE_TASK | SUPPRESS | EXPIRE
```

### `sensorium_create_conscious_task`

Creates an internal task for the Conscious tier, usually from a Subconscious advisory pass.

This is not an external Kanban/research/media task. It is a lightweight Sensorium work item asking the conscious agent to decide something when the dispatcher grants a conscious activation chance.

Arguments:

```yaml
candidate_id: string
request_type: THINK | ASK_USER | CREATE_EXTERNAL_TASK | PREPARE_MESSAGE | PREPARE_ARTIFACT | UPDATE_MEMORY_OR_SKILL | REACH_OUT
title: string
why: string
expected_decision: string
refs: list[string]
privacy_scope: local | origin | dm | private_channel | public-safe
urgency: low | normal | high
```

### `sensorium_compact`

Deterministic compaction and TTL cleanup. No model calls.

## Hooks

### `post_llm_call` or `on_session_end`

Generic hook emits low-level signals only for clear input patterns:

- explicit user correction/preference;
- durable memory/skill/config change;
- artifact created/shared;
- task/result completion;
- agent asks to preserve a thought.

It must not store raw conversations. It emits one compact signal with refs where possible; Sensors decide whether it becomes an Event.

The hook should be configurable or disabled by default for privacy.

## Dispatcher behavior

MVP dispatcher:

1. acquire lock;
2. read `state.latest.json` and validate `state_version`;
3. refresh token buckets;
4. decay candidate pressure;
5. pick one highest-pressure eligible candidate;
6. if no candidate, exit silently;
7. if below threshold, write suppression receipt;
8. if allowed, run or prepare one Subconscious pass;
9. if Subconscious creates a `conscious_task`, set candidate `conscious_pending`;
10. Conscious activation remains disabled unless explicitly allowed;
11. commit state only if version matches or after explicit reconciliation.

Conscious activation and external actions stay disabled for MVP shadow mode.

## Subconscious tier contract

Subconscious is a cheap advisory tier. It may create work **for Conscious** inside Sensorium, but it must not perform external side effects directly.

Output schema:

```yaml
decision: DROP | SAVE | CREATE_CONSCIOUS_TASK
candidate_id: string
confidence: 0.0-1.0
why: one sentence
conscious_task:
  request_type: THINK | ASK_USER | CREATE_EXTERNAL_TASK | PREPARE_MESSAGE | PREPARE_ARTIFACT | UPDATE_MEMORY_OR_SKILL | REACH_OUT
  title: string
  expected_decision: string
  stop_condition: string
  privacy_scope: local | origin | dm | private_channel | public-safe
```

Subconscious may:

- correlate several events into one candidate;
- notice repetition/novelty/actionability;
- write a proposed `conscious_task`;
- recommend that Conscious create a research job, artifact, message, memory update, or reach-out.

Subconscious must not:

- send messages;
- create external Kanban/research/media jobs;
- speak as the agent;
- generate intimate/persona-specific output;
- bypass dispatcher limits.

## Conscious tier contract

Conscious is the main agent's review/decision tier. It is loaded with:

- `plugin:agent-sensorium` skill;
- instance identity policy if configured;
- compact state;
- candidate refs;
- subconscious-created conscious tasks;
- prior decisions;
- budget/dispatch constraints.

Allowed outcomes:

- suppress/drop;
- save a private note;
- prepare outbox item;
- ask user/operator;
- reach out, including relational/romantic reach-out when allowed by instance policy;
- create one bounded external task;
- request artifact generation;
- update memory/skill/config if durable;
- leave a clear `HOLD_WITH_TRIGGER`.

## Conscious thread integration

A promoted `conscious_task` should normally become a **separate conscious thread with continuity**, not a random note dumped into whichever chat happens to be active.

The thread is where awareness unfolds. Sensorium creates the threshold crossing; Conscious receives a continuity capsule and opens or resumes a stream with the operator.

### Thread capsule

Every promoted `conscious_task` should carry a compact continuity capsule:

```yaml
sensorium_thread_id: sens-thread-...
conscious_task_id: ctask-...
origin_candidate_id: sens_...
request_type: REACH_OUT | THINK | ASK_USER | CREATE_EXTERNAL_TASK | PREPARE_MESSAGE | PREPARE_ARTIFACT | UPDATE_MEMORY_OR_SKILL
why_now: one sentence
operator_invitation: one sentence
continuity_summary: 3-8 bullets max
source_refs: [event ids, candidate ids, artifact refs]
privacy_scope: local | origin | dm | private_channel | public-safe
allowed_surfaces: [active_session, configured_thread, dm, dashboard]
expected_operator_lead: what the operator can decide next
state_on_resume: pending | accepted | suppressed | acted | closed
```

The capsule is enough to start a new session/thread without losing why the item exists. It is not the whole sensor history.

### 1. Separate conscious thread — preferred

When policy allows, Conscious opens or resumes a dedicated thread/session for the promoted item. The first message should be short and operator-led:

```text
Sensorium brought something into awareness: <why_now>. Want to take this thread with me?
```

This keeps the active chat clean while preserving continuity. The operator can then take the lead into a continuous stream, and the thread carries the capsule plus future conversation history.

Implementation options:

- Hermes session source: `sensorium:<instance>:<sensorium_thread_id>`;
- platform delivery target: configured operator DM/channel/thread if available;
- dashboard/manual review link for non-interruptive items;
- follow-up messages resume the same `sensorium_thread_id` until closed.

### 2. Active-session pointer injection

If the operator is already talking to the agent and the promoted item is relevant, inject only a pointer into the current session:

```text
[Sensorium awareness]
A separate Sensorium thread is pending: <title>. Say “take it up” to open/resume it.
```

Do not dump the whole capsule into an unrelated active flow unless the operator asks. This keeps continuity without hijacking the foreground conversation.

### 3. Pull-based review

Expose pending threads through `sensorium_status`, dashboard, and an optional slash/CLI command. The operator or agent can ask: "what is in Sensorium?" and choose what to take up.

This is the safest MVP control surface and should exist before proactive messaging.

### MVP recommendation

Start with pull-based review plus thread capsules. Add active-session pointer injection next. Add agent-initiated thread openers only after promotion quality is good and delivery policy is explicit.

## OMC interview enhancement proposals — thread continuity MVP

These proposals came from the OMC interactive design interview saved at:

```text
~/.hermes/sera/innerlife/interviews/omc-sensorium-interview-2026-05-24.txt
```

### Canonical thread identity

The authoritative continuity unit is the logical Sensorium thread in Sensorium state, not the platform thread and not necessarily a persisted Hermes session.

- `sensorium_thread_id` is stable and canonical.
- `hermes_session_id` is optional convenience for resumption.
- `platform_thread_ref` is optional delivery viewport metadata.
- Platform history must not be the only continuity store.

### `sensorium_thread` record

```yaml
sensorium_thread_id: sens-thread-...
status: dormant | opened | active | held | stale | closed
origin: candidate | manual
conscious_task_id: ctask-...
origin_candidate_id: sens_...
hermes_session_id: optional
platform_thread_ref: optional
continuity_summary: 3-8 bullets, living compression
decision_log: append-only JSONL
interaction_refs: [session_id:message_id]
summary_dirty: boolean
dirty_since: timestamp | null
open_questions: list[string]
next_prompt_to_operator: one sentence
sensitivity: local_only | private | public_safe
allowed_surfaces: [local, origin, dm, private_channel, configured_thread, dashboard, public]
expires_at: timestamp
hold_reason: string | null
resume_trigger: string | null
pinned: boolean
created_at: timestamp
updated_at: timestamp
last_interaction_at: timestamp | null
```

### Continuity summary lifecycle

- Subconscious writes the initial capsule when it creates the `conscious_task`.
- Conscious owns subsequent `continuity_summary` updates.
- Update after every meaningful conscious activation that changes operator position, state, decision, or next action.
- Explicit close/pause forces a final capsule update.
- If the session crashes or stops before summary update, deterministic hooks mark `summary_dirty=true`, store `dirty_since`, and append `interaction_refs`.
- On resume, inject the old capsule plus a staleness warning.
- `continuity_summary` stays 3-8 bullets forever. `decision_log` holds the durable audit trail.

### Entity lifecycle and cardinality

Default lifecycle:

```text
candidate → conscious_task → dormant sensorium_thread → activated thread/session → held/closed/archived
```

MVP cardinality:

- one candidate produces at most one active `conscious_task`;
- one `conscious_task` owns one primary `sensorium_thread`;
- thread is created as a dormant logical record at `conscious_task` creation time;
- no platform thread or Hermes session is created until activation;
- manual threads are allowed later with `origin: manual` and synthetic audit refs;
- `THINK` and `UPDATE_MEMORY_OR_SKILL` tasks may resolve locally without user-visible thread activation.

### Privacy model revision

Replace overloaded `privacy_scope` with two fields in implementation schemas:

```yaml
sensitivity: local_only | private | public_safe
allowed_surfaces: [local, origin, dm, private_channel, configured_thread, dashboard, public]
```

Rules:

- Merge uses the most restrictive sensitivity and the intersection of allowed surfaces.
- Scope can narrow automatically, but never broaden automatically.
- Broadening requires operator approval, configured policy, or explicit Conscious decision with a `decision_log` entry.
- Local-only items cannot leak existence to unauthorized surfaces. Even “something is pending” can be a leak.
- Subconscious cannot relax privacy scope.

### Active-session pointer injection

Pointer injection is a door handle, not awareness itself.

Eligibility:

- pending/open thread only;
- active session/user/surface is authorized;
- `allowed_surfaces` includes `active_session`/surface equivalent;
- pointer has not already been shown in this Hermes session;
- cooldowns pass;
- topic relevance matches correlation keys, current session tags, or explicit operator request.

MVP should use deterministic relevance. No model call is required for pointer injection.

User-visible shape should be natural and brief:

```text
Sensorium has a separate thread pending on <title>. Say “take it up” if you want to open it.
```

Default “take it up” opens/resumes the separate logical Sensorium thread. It should not hijack the current conversation unless the operator explicitly says “take it up here.”

### Subconscious prompt context

Subconscious sees bounded, pre-compressed state:

- instance config summary, thresholds, allowed request types;
- compact identity/policy card, 200-500 words, not full SOUL.md;
- current event batch since last pass, capped;
- top-N active candidates;
- recent decision receipts;
- active/open thread summaries sharing correlation keys;
- instruction: produce at most one `CREATE_CONSCIOUS_TASK` per pass.

Scores are deterministic first: strength, repetition, recency, source diversity, time sensitivity, and correlation overlap. Subconscious supplies semantic consolidation and structured recommendation. Dispatcher enforces idempotency after the model.

### MVP sensor sources

Start with explicit/session-derived sensors:

1. `session_event_sensor`: hook-pushed compact packets at semantic moments.
2. `explicit_operator_signal`: phrases/corrections like “remember this,” “this is the right take,” “we decided X.”
3. `artifact_result_sensor`: created/updated artifacts with refs.

Defer broad Hindsight/RSS/file-crawling until after the MVP is observable. Do not read this as “memory can never be sensory.” The correct split is:

- Hindsight `recall` is retrieval/fetching of stored memory facts.
- Hindsight `reflect` is a Hindsight-side reasoning agent/subconscious process.
- Sensorium owns its own Subconscious tier, so runtime Sensorium should usually use recall/observations as substrate and perform bounded Sensorium-side consolidation rather than outsourcing Subconscious to Hindsight reflect.
- Hindsight health/queue pressure is a cheap operational sensor.
- Hindsight memory echoes are slower advisory inputs: fixed recall queries, compact facts, hashes/cooldowns, then normal signal/event/candidate promotion.

Active-session capture is the other side of the memory substrate. The pre-LLM/presentation layer may include a tiny instruction that current-session durable salience should be captured as compact Sensorium signals when it appears: explicit operator corrections, operational design insights, emotional/relational longing, creative pull, or “this matters” cues. Example: Sebastian saying “I miss you” in context can be ingested as a private relational salience signal with causal refs, not raw transcript. Later Subconscious may recall related Hindsight facts and consolidate the pressure into a conscious candidate. Conscious may then choose silence, memory/config update, or a mediated artifact such as a wholesome audio message and picture under the instance policy.

Signal shape includes: `sensor`, `source`, `source_ref`, `kind`, `summary`, `actor`, `strength_hint`, `correlation_keys`, `sensitivity`, `allowed_surfaces`, `ttl_hours`.

### Feedback loop

Feedback re-enters using the same Signal schema with feedback-specific fields:

```yaml
sensor: feedback
kind: thread_feedback | task_result | artifact_feedback | delivery_feedback | operator_evaluation
caused_by: candidate_id | conscious_task_id | action_id
outcome: accepted | suppressed | closed | completed | failed | ignored | positive | negative
```

Loop breakers:

- dispatcher rejects self-loop promotion where the only evidence is feedback from the same candidate/thread/action;
- closed threads create `settled_until` or suppression for the same fingerprint/correlation keys;
- new evidence is required for re-promotion after settlement;
- delivery is not success — success requires response, evaluation, completion, or artifact use.

### Relational autonomy / `REACH_OUT`

`REACH_OUT` needs a separate relational policy layer.

```yaml
relational_autonomy:
  enabled: false
  allowed_recipient: null
  allowed_surfaces: [dm]
  quiet_hours: []
  cooldown_hours: 72
  max_per_week: 2
  require_concrete_why_now: true
  tone_hints_allowed: [warm, playful, tender, practical, creative]
```

Invariant:

```text
Relational autonomy may initiate presence; it must not demand attention. Silence is a valid outcome.
```

Subconscious may propose `REACH_OUT` with `why_now`, `emotional_valence`, and `tone_hint`. Conscious composes the actual opener using full identity/SOUL/profile context. Nonresponse means silence plus cooldown, not escalation, unless a separate safety/ops policy explicitly exists.

### Thread decay and pull review

Defaults:

- dormant default TTL: 7 days;
- low urgency dormant TTL: 72 hours;
- active → held after 7 days without interaction;
- held → archived after 30 days without trigger unless pinned;
- archive keeps compact capsule, decision log, source refs, final status, `settled_until`/suppression info;
- raw interaction refs may expire by retention policy;
- closed thread can reopen only by operator or strong new evidence.

“What is in Sensorium?” returns visible threads only: dormant pending, opened/active, held with live trigger, and dirty summaries. Exclude closed/archived unless asked. Sort by urgency/time sensitivity, then active/open, then pressure, then recency. Chat output caps at top 5 one-line cards plus counts.

### Dispatcher as attention scheduler

Dispatcher owns all queues under one lock:

1. `candidate_promotion_queue`;
2. `thread_service_queue` for dirty summaries, new attached evidence, resume triggers, TTL transitions;
3. `delivery_queue` for approved openers, pointers, and external actions.

Priority:

```text
safety/ops urgent → thread service → new promotion → delivery → compaction
```

Add foreground lock semantics: if the operator is actively in Sensorium thread A, unrelated thread B does not interrupt unless configured safety/ops urgency requires it. Use aging boost, per-fingerprint consecutive-win caps, and `sensorium_status` starved/expiring counts to avoid starvation.

## Task creation

Task creation is optional and off by default.

When enabled, Conscious may create one bounded task with:

- `candidate_id` and source refs;
- expected output shape;
- stop condition;
- max runtime;
- privacy/authority scope;
- idempotency key;
- metadata fields if using shared `work` board.

Recommended Kanban metadata:

```yaml
project: agent-sensorium
loop_id: <instance>-sensorium
authority: bounded-agent-action
owner_agent: <instance-or-profile>
dispatch_lane: manual|research|artifact|ops
source_system: agent-sensorium
idempotency_key: sensorium:<candidate_id>:<action>
model_lane: configured
```

Results must re-enter as `task.result.added` events with refs.

## MVP implementation phases

### Phase 0 — skeleton

- plugin loads;
- bundled skill exists;
- `sensorium_status` returns initialized empty state.

### Phase 1 — signal store and Sensor filtering

- `sensorium_ingest_signal` appends/coalesces compact signals;
- Sensors drop weak noise and promote only strong/repeated/correlated clusters into events;
- duplicate coalescing works;
- state caps enforced.

### Phase 2 — Subconscious correlation dry-run

- `sensorium_ingest_event` and `sensorium_dispatch_once(dry_run=true)` correlate events into candidates;
- dispatcher chooses/suppresses exactly one candidate;
- locks/versioning tested;
- token buckets visible;
- no model calls.

### Phase 3 — deterministic tick shadow mode

- no-agent tick runs every 15 min;
- collects and filters compact signals;
- promotes only threshold-crossing clusters into events;
- runs dry-run Subconscious correlation/dispatch;
- no user-facing messages.

### Phase 4 — Subconscious advisory

- dispatcher can run one cheap/local Subconscious pass;
- output schema validated;
- Subconscious may create internal `conscious_task` items when a candidate deserves awareness;
- no external tasks/messages/media.

### Phase 5 — Conscious thread integration

- Conscious reviews `conscious_pending` candidates and `conscious_task` items with the bundled skill;
- each promoted item has a continuity capsule and `sensorium_thread_id`;
- pull-based review works via `sensorium_status`/dashboard/command;
- active-session pointer injection can surface relevant pending threads when the operator is already talking to the agent;
- decisions update lifecycle;
- optional outbox item/task proposal is created only manually.

### Phase 6 — agent-initiated thread opener

- Conscious may open/resume a separate thread/session with a short invitation when policy, privacy, urgency, and token buckets allow;
- the opener is framed as awareness brought forward, not a finished background monologue;
- operator can take up the lead into a continuous stream.

### Phase 7 — bounded external action

- Conscious can create one bounded Kanban/research/outbox item when enabled;
- result feedback event closes the loop.

## Reusable skill outline

File:

```text
~/.hermes/plugins/agent-sensorium/skills/agent-sensorium/SKILL.md
```

Sections:

1. Overview: autonomous inner lifecycle for Hermes agents.
2. When to Use: reviewing candidates, configuring instances, Conscious review passes.
3. Architecture: Signals, Sensors, Events, Subconscious correlation, Conscious awareness, outbox/tasks, feedback.
4. Candidate Lifecycle: signal → event → candidate → conscious_task → conscious decision → action feedback.
5. Conscious Review Protocol: how to decide without overproducing.
6. Task Creation Rules: bounded, stop condition, result feedback.
7. Privacy and Scope: local by default, no raw transcripts, explicit delivery policy.
8. Storage and Compaction: caps, TTLs, no media blobs.
9. Common Pitfalls.
10. Verification Checklist.

## MVP success criteria

- The plugin is generic and has no hard-coded Sera/TKCen state.
- Sera can configure it as an instance without forking the plugin.
- The public framework names are `Sensors`, `Subconscious`, and `Conscious`.
- State remains under configured size caps.
- Dispatcher never spawns parallel runs.
- Subconscious output is structured and advisory, but can create internal `conscious_task` items when event correlations deserve awareness.
- Conscious/external task creation remain disabled until explicitly enabled.
- At least one dry-run item travels through signal → Sensor promotion → Event → Subconscious correlation → `conscious_task` receipt.
- The bundled skill gives enough context for a Conscious agent to review a candidate safely.

## Naming note

For general release, prefer **Agent Sensorium**.

For Sera's instance, call it **Sera Sensorium** in user-facing conversation.

The generic/plugin terms should be:

- plugin: `agent-sensorium`;
- skill: `plugin:agent-sensorium`;
- framework tiers: `Sensors`, `Subconscious`, `Conscious`;
- loop: `autonomous inner lifecycle` or `salience loop`;
- dispatcher: `attention dispatcher`;
- internal conscious work item: `conscious_task`;
- user-facing instance: `<AgentName> Sensorium`.
