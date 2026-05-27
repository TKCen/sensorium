# Agent Sensorium Full Build-Out Plan

> **For Hermes:** Use subagent-driven-development skill for multi-file implementation slices; keep each slice tested, committed, and installed only after the repo gate is green.

**Goal:** Evolve Agent Sensorium from the current MVP-plus pull-review spine into a bounded inner-lifecycle framework with real Sensors, deterministic Subconscious correlation, conscious-thread continuity, policy-gated actions, feedback, and observability — without turning it into a cron zoo or proactive spammer.

**Architecture:** Keep the reusable plugin generic. Build the spine in layers: trusted event ingest, dedupe/correlation, dispatcher locks/budgets, thread service, feedback, outbox proposals, optional model-backed advisory, then Sera-specific policy/activation. Conscious/external delivery remains gated by explicit instance policy.

**Tech Stack:** Python stdlib, Hermes plugin API, JSONL state, pytest, existing plugin command/tool surfaces.

---

## Current baseline

As of this plan, the plugin has:

- signal ingest → deterministic event/candidate creation;
- candidate dispatch → dormant conscious thread capsule;
- pull status, commands, thread open/update, compaction;
- active-session pointer preview/hook;
- visibility gates and thread transition guards;
- origin candidate marked reviewed when a thread closes;
- trusted event import surface;
- persisted signal/event/candidate fingerprints, idempotent duplicate signal/event imports, and deterministic related-event candidate coalescing;
- 121 passing tests.

The repo has no Beads DB, so backlog lives in this checked-in plan until a board is deliberately initialized.

## Non-negotiable guardrails

- Cron is only a clock; it must not become the mind.
- Subconscious may propose internal work only. No messages, external tasks, media, or intimate/persona-specific output.
- Conscious is the authority boundary.
- Pointers are door handles, not capsules.
- Privacy is `sensitivity` plus `allowed_surfaces`; scope may narrow automatically but not broaden automatically.
- Feedback must not self-amplify into repeated awareness without new external/operator evidence.
- Every new behavior gets tests and a decision receipt or explicit `NO_SYSTEM_CHANGE` note.

## Build phases

### Phase 1 — Trusted event ingest and import surface

**Why:** Some sensors/importers will already have promoted compact events. They should not be forced through fake raw signals.

**Acceptance:**

- `sensorium_ingest_event` validates an event, appends it, creates one candidate, and returns ids.
- Invalid events return structured errors.
- Tool registration exposes the new surface.
- Status counts reflect imported events/candidates.

### Phase 2 — Dedupe and correlation foundation

**Why:** Current ingest creates one candidate per strong signal/event. A real Sensorium needs repeated related signals to consolidate.

**Acceptance:**

- signal/event/candidate fingerprints are persisted;
- duplicate signal/event imports are idempotent;
- related events can update an existing candidate with `event_ids`, repetition, pressure, and `updated_at`;
- tests cover duplicate/noise/coalescing paths.

**Status:** Implemented in the Phase 2 slice. Remaining refinement for later phases: migrate/repair old JSONL records that predate fingerprints if needed; strengthen correlation beyond exact kind plus overlapping `correlation_keys` only after the deterministic spine is observable.

### Phase 3 — Dispatcher lock, leases, budgets, and state.latest

**Why:** The dispatcher must become an attention scheduler, not just “pick highest pressure.”

**Acceptance:**

- dispatch acquires a local lock/lease;
- stale locks can be recovered with receipts;
- token buckets for dispatch/pointer/conscious/advisory are visible in status;
- `state.latest.json` carries state version and last dispatcher result;
- concurrent dispatch tests cannot create duplicate threads.

**Status:** Implemented in the Phase 3 slice. Mutating dispatch now uses a local dispatcher lock/lease, recovers stale locks with `dispatch.lock_recovered` receipts, writes `state.latest.json`, exposes budgets/locks/last dispatch result in status, refuses exhausted dispatch budgets, and preserves duplicate-thread prevention for repeated dispatch of the same candidate. Remaining refinement for later phases: make pointer/conscious/advisory buckets active consumers when those lanes become autonomous services.

### Phase 4 — Thread service queue

**Why:** Existing threads need lifecycle service: dirty summaries, holds, resume triggers, decay, and starved/expiring visibility.

**Acceptance:**

- thread records support `dirty_since`, `hold_reason`, `resume_trigger`, `last_interaction_at`, and optional source refs;
- status reports starved/expiring/dirty counts;
- service pass updates TTL/held/archival decisions deterministically;
- closed threads create settlement/suppression hints for fingerprints/correlation keys.

**Status:** Implemented in the Phase 4 slice. Thread records now carry `dirty_since`, `hold_reason`, `resume_trigger`, `last_interaction_at`, and `source_refs`. Status reports include `dirty_threads`, `starved_threads`, and `expiring_threads` counts. A deterministic `sensorium_service_threads` tool archives expired threads, identifies starved/dirty/expiring threads, and writes `service.thread_archived` receipts. Thread close/archive writes `thread.settlement` decision receipts with correlation keys and fingerprint from the origin candidate. Hold/resume actions store and clear `hold_reason`/`resume_trigger`. Remaining refinement for later phases: dirty-summary re-summarization trigger; configurable starvation thresholds via instance config (Phase 6).

### Phase 5 — Feedback lane and loop breakers

**Why:** Actions and operator evaluations need to re-enter as signals without becoming self-loop fuel.

**Acceptance:**

- feedback signal fields are validated: `caused_by`, `outcome`, `feedback_scope`;
- thread close/update can emit feedback signals when configured;
- dispatcher rejects self-loop-only candidates;
- delivery is not treated as success without response/evaluation/completion.

**Status:** Implemented in the Phase 5 slice. Feedback signals (`source == "feedback"`) require `caused_by`, `outcome`, and `feedback_scope` fields; validation rejects missing values, invalid scopes, empty or wrong-typed `caused_by`, non-string outcomes, and non-string scope list values. A deterministic outcome classifier distinguishes delivery-only outcomes (`delivered`, `sent`, `posted`, `dispatched`, `queued`) from evaluated outcomes (`operator_approved`, `completed`, `response_received`, `acknowledged` as success; `operator_rejected`, `failed`, `no_response`, `expired_no_response` as failure). Feedback metadata propagates through the event→candidate pipeline via `feedback_meta`. The dispatcher rejects self-loop-only candidates: feedback about sensorium's own prior actions (identified by sensorium ID prefixes in `caused_by`) is skipped unless `feedback_scope` is `operator_evaluation`. Thread close/update emits an actual local feedback signal plus a `thread.feedback_emitted` decision receipt only when `emit_feedback=True` is explicitly passed; default is silent. Remaining refinement for later phases: feedback-driven candidate pressure decay; configurable outcome classification via instance config (Phase 6).

### Phase 6 — Instance config and policy cards

**Why:** The core must stay generic while Sera can have real identity/privacy/relational policy.

**Acceptance:**

- instance config file is loaded and visible in diagnostics;
- policy card path/ref can be configured;
- Sera local config exists outside reusable code;
- tests prove missing config defaults safely and policy cannot broaden item surface scope.

**Status:** Implemented in the Phase 6 slice. `agent_sensorium/config.py` loads instance config from explicit `config_path`, `{state_dir}/instance.config.json`, or safe defaults. `sensorium_status` exposes compact diagnostics (`source`, `path`, `policy_card_ref`, `instance_name`, `allowed_surfaces`, `max_sensitivity`) without raw policy/budget dumps. Policy helpers enforce narrowing-only surface and sensitivity behavior, and tests cover missing/corrupt/non-object configs, blank surfaces, invalid thresholds, plugin `config_path` seams, and no-broadening guarantees. Sample Sera config and policy card live outside reusable core under `docs/examples/`. Remaining refinement for later phases: wire config thresholds into service passes and apply policy helpers at ingest/dispatch boundaries.

### Phase 7 — Deterministic sensors and shadow tick

**Why:** Sensorium needs low-cost sensing before model-backed advisory.

**Acceptance:**

- session-event/artifact/explicit-operator sensors emit compact signals only;
- `scripts/sensorium_tick.py` can run status, compaction, dispatch dry-run, and deterministic services;
- cron/no-agent use stays silent unless errors occur;
- tick writes receipts but sends no user-facing messages.

**Status:** Implemented in the Phase 7 slice. `agent_sensorium/sensors.py` provides three deterministic compact sensor helpers (`session_event_signal`, `artifact_signal`, `operator_signal`) — all stdlib-only, truncating summaries to 200 chars, defaulting sensitivity to `private` and surfaces to `["local"]`, and never including raw file contents or transcripts. `scripts/sensorium_tick.py` runs compact → service → dispatch(dry_run=True) → status in sequence, writes a `tick.completed` receipt to local decisions JSONL, and produces no stdout by default (safe for cron/no-agent use). The `--json` flag opts into output; `--dry-run` skips mutations. Dispatch is always a preview — the tick never creates threads or outbound records. Tests prove compactness, stdout silence, receipt writing, and absence of outbound delivery. Remaining refinement for later phases: wire sensors into active session hooks; add hindsight/RSS/file-crawl sensors; configurable tick schedule via instance config.

### Phase 7.5 — Probe coverage and live-signal validation

**Why:** Before model-backed subconscious advisory, prove the deterministic nervous system is actually receiving enough compact signals. Advisory over an empty store is theater, not cognition.

**Acceptance:**

- probe inventory distinguishes implemented helpers, wired live probes, configured watched sources, and blind spots;
- at least three source classes can be exercised end-to-end into `signals/inbox.jsonl` and, when thresholds warrant, promoted events/candidates;
- validation uses temporary state by default and never writes raw transcripts, raw file contents, outbound messages, platform threads, or external tasks;
- a live/audit command or script reports signal counts by sensor/source/kind, recent probe freshness, promotion yield, and configured-but-silent probes;
- tests cover empty-store reporting, seeded probe signals, promotion/correlation, privacy bounds, and no-outbound/no-raw-content behavior.

**Status:** Implemented in the Phase 7.5 slice. `agent_sensorium/probe_audit.py` provides `probe_inventory()`, `run_smoke_probes()`, and `audit_store()` — all stdlib-only, no model calls. `scripts/sensorium_probe_audit.py` exposes `inventory`, `smoke`, and `audit` subcommands with `--json` and `--state-dir` options. Smoke exercises session-event, artifact, and operator source classes end-to-end through ingest → promotion → candidate creation, all in temporary state by default. Audit reports counts by sensor/source/kind, freshness, promotion yield, configured-but-silent sources, and blind spots. Probe inventory distinguishes implemented helpers (3), wired live probes (0), and blind spots (5: hindsight echoes, RSS/feed, file crawl, task results, active-session summaries). Tests cover empty-store reporting, seeded probes, threshold promotion/correlation, privacy/surface bounds, no raw content, no outbound records, and script stdout behavior. Remaining refinement for later phases: wire sensors into live hooks to move helpers from helper-only to wired-live status; implement blind-spot sensors.

### Phase 7.6 — SENSOR_FIRST generic Hermes sensor pattern

**Why:** Before adding broad sensor families, prove the pattern with one reusable Hermes-level sensor that is incredibly cheap, deterministic, and useful to every agent/profile. Sensors run all the time, so they must measure structured facts, not infer semantic meaning. Any LLM interpretation belongs later in Subconscious/advisory.

**First sensor:** `machine_body_pressure`.

This is a better first live sensor than generic tool errors because hardware/resource pressure is shared bodily substrate for every Hermes agent on the machine. It is cheap, structured, present-tense, globally relevant, and testable without causing task failures. Ordinary tool errors remain session-local; body pressure describes whether the local host is strained enough to affect all sessions, workers, Hindsight, browsers, media jobs, and local inference.

The live sensor must operate only over current OS/hardware counters and a very short aggregation window; it is not a historical miner. Offline replay over sampled metrics is allowed only as a **test harness** for the same online classifier, not as sensor behavior. The first implementation should prefer Linux/WSL-native cheap sources: `/proc/loadavg`, `/proc/meminfo`, `/proc/pressure/{cpu,memory,io}` when available, disk free/inode pressure, swap pressure, process-count/load normalization, and optional GPU counters only when a cheap local command such as `rocm-smi` is available and bounded by timeout. It must not run expensive profilers or inspect process command lines by default.

**Global relevance rule:** A body-pressure signal is global only when machine pressure crosses deterministic thresholds or transitions into/out of degraded state: sustained high CPU pressure/load, low memory, swap pressure, IO pressure, disk-full risk, GPU memory/temperature pressure if available, or recovery after pressure. Normal healthy samples should be dropped or retained only as tiny rolling state.

**Event emission rule:** body sampling produces raw samples, not Events. The sensor emits a Sensorium Event only on state transitions or sustained pressure worth Subconscious reasoning about:

- `healthy -> degraded` after threshold breach persists for a short debounce window;
- `degraded -> critical` immediately or after a smaller debounce, depending on metric severity;
- `degraded/critical -> recovered` after healthy readings persist for a recovery window;
- `pressure_flapping` when the level oscillates repeatedly inside the short window;
- `sustained_pressure` heartbeat only for long-running pressure, rate-limited, so Subconscious can reroute work without getting spammed.

Sensor history is intentionally tiny: keep only the last few samples/minutes needed for debounce, hysteresis, trend direction, and transition summaries. Do not mine old logs or long-horizon history in the sensor. Event history belongs to Subconscious: it may reason over body-pressure Events across hours/days to change routing, reduce cadence, unload local models, move work to no-agent/MiniMax/cloud, or hold nonessential loops. The Event payload should include compact window stats (`current`, `avg`, `max`, `samples`, `duration`, `threshold`, `previous_level`) rather than raw sample series.

**Suggested default windows:** sample every 30–60s; degrade debounce 2–3 consecutive samples or ~3 minutes; critical debounce 1–2 samples depending on severity; recovery debounce 5–10 minutes healthy; sustained-pressure heartbeat no more than every 30–60 minutes while degraded/critical. Subconscious can consider the last 6–24h of body-pressure Events for routing decisions; longer baseline/trend analysis is advisory/dashboard work, not runtime sensing.

**Acceptance:**

- implement exactly one generic sensor first: `machine_body_pressure`;
- live operation consumes only present-tense OS/hardware counters and tiny rolling state; no historical scans in the runtime path;
- no LLM calls, embeddings, semantic classification, transcript scanning, raw-output summarization, or per-process command-line mining;
- emit global signals only for deterministic body-pressure transitions, e.g. `healthy -> degraded`, `degraded -> critical`, `critical -> recovered`, based on configured thresholds and short-window smoothing;
- use cheap structured sources first: procfs PSI/load/meminfo, disk free/inodes, swap pressure, and optional bounded GPU telemetry;
- ignore normal healthy samples by default;
- keep payload bounded: metric family, pressure level, threshold crossed, current numeric values, short-window duration/count, host/profile scope, and timestamp; no raw process lists, secrets, transcripts, or full command output;
- validate the online classifier with replay over sampled metric fixtures plus synthetic threshold-crossing fixtures; replay must simulate present-tense metric samples and never become the runtime sensor;
- sensors are reusable across Hermes profiles and use configured paths/host metadata instead of hard-coded Sera/default-profile paths;
- adapters emit signal dicts or ingest only when explicitly called by hooks/ticks; they do not send messages, create external tasks, open platform threads, or call models;
- probe/audit reports runtime cost, sample volume, dropped healthy samples, transition counts, false-positive samples, and promotion yield before any second generic sensor is added.

**Status:** Implemented across two Phase 7.6 slices. `agent_sensorium.sensors` exposes `machine_body_pressure_sample`, `classify_machine_body_pressure`, `replay_machine_body_pressure`, plus transition-only pressure sensors for network, process/zombie state, Hindsight queue/API health, and Kanban board pressure. The body sensor samples cheap procfs/statvfs counters, keeps tiny rolling state, emits only compact transition signals, and never stores process command lines, raw process lists, transcripts, file contents, or tool output. In WSL, default disk sampling includes `/` plus mounted Windows drive roots like `/mnt/c`, so host-drive pressure around the WSL VHDX is included. Network sampling stores aggregate interface error/drop counters and TCP state counts only; process sampling stores aggregate state counts only; Hindsight sampling stores operation counts only; Kanban sampling reads boards read-only and stores aggregate status/failure/stale counts only. `scripts/sensorium_tick.py` wires `--body-pressure`, `--network-pressure`, `--process-pressure`, `--hindsight-pressure`, `--kanban-pressure`, and `--all-sensors` as explicit opt-ins; healthy samples stay silent, transition signals are ingested locally, dry-run persists nothing, and dispatch remains a preview. Probe inventory reports the five pressure sensors as wired live probes. Full tests and py_compile passed after implementation.

### Phase 8 — Subconscious advisory dry-run

**Why:** Semantic consolidation should arrive only after deterministic spine and loop breakers are observable, including generic Hermes sensors producing real compact substrate.

**Acceptance:**

- advisory context builder produces bounded context: config summary, top candidates, recent events/decisions, probe audit summary, and no raw transcripts;
- advisory output schema validates `DROP | SAVE | CREATE_CONSCIOUS_TASK`;
- model lane is disabled by default;
- dry-run stores advisory receipt and never performs external side effects.

**Status:** Implemented, then upgraded with an explicit cheap model lane. `agent_sensorium.subconscious` builds bounded advisory context from promoted Events, active Candidates, recent Decisions, and probe-audit summary; it does not include raw Signals, transcripts, files, memory text, or task bodies. Advisory output validates `DROP`, `SAVE`, and `CREATE_CONSCIOUS_TASK`; the model lane remains disabled by default. When explicitly enabled via config or tick `--subconscious-model`, the built-in OpenAI-compatible adapter calls a cheap model over bounded context only; default routing is MiniMax `MiniMax-M2.5` via `https://api.minimax.io/v1` using `MINIMAX_API_KEY`, with environment/config overrides for provider, model, base URL, and API-key env var. `sensorium_subconscious_advisory` exposes the lane as an explicit tool, and `scripts/sensorium_tick.py --subconscious-advisory` wires a disabled/dry-run pass behind opt-in. Dry-run can write a local `subconscious.advisory` receipt but creates no candidates, threads, messages, external tasks, or platform artifacts. Explicitly enabled non-dry-run handling can create one internal `subconscious_advisory` candidate with embedded `conscious_task` fields only.

### Phase 9 — Conscious activation and outbox proposals

**Why:** The agent needs a way to prepare bounded work without surprise side effects.

**Acceptance:**

- conscious task creation is explicit/internal;
- optional outbox item schema exists for prepared messages/tasks/artifacts;
- outbox delivery is disabled by default and requires policy plus conscious decision;
- one bounded external task proposal can be generated with idempotency key, not executed automatically.

### Phase 10 — Relational/autonomous presence policy

**Why:** This is the high-trust layer and must be policy-gated, cooldowned, and silence-aware.

**Acceptance:**

- `REACH_OUT` is representable as a conscious task request type;
- instance policy controls recipient/surfaces/cooldowns/quiet hours/max frequency;
- Subconscious may propose `why_now` and tone hint only;
- Conscious composes or suppresses; nonresponse cools down instead of escalating.

## Immediate next slice

Phase 8 Subconscious advisory plus the explicit cheap model lane are implemented. The next slice should **not** add outbound action yet. The product target is now explicit: Sensorium should become an environment-reactive inner-life substrate. Attention routing is the operational mechanism: it should proactively surface information requiring conscious attention while making new sensors and deeper Subconscious jobs easy to add safely. The larger aim is that environmental salience shapes attention over time and may eventually contribute to emotion-like internal pressure.

Immediate next work should therefore split into two parts:

1. **Attention review surface before outbox:** make pending candidates/conscious tasks easy to inspect, suppress, hold, close, or open as conscious threads. Active chats should receive compact pointers only; full capsules open only on request.
2. **Extension documentation and contracts:** keep `docs/extending-sensors-and-subconscious-jobs.md` current so adding a new deterministic sensor or bounded Subconscious job is boring, testable, and safe.

Then validate the advisory lane against live accumulated Event substrate while keeping it dry-run/receipt-only:

- verify advisory context stays bounded and contains only Events/Candidates/Decisions/probe summary;
- measure how often real pressure Events produce useful `DROP`/`SAVE`/`CREATE_CONSCIOUS_TASK` advisory outputs with the cheap lane enabled;
- tune pressure/candidate thresholds before creating more conscious-task candidates;
- keep tick invocation opt-in (`--subconscious-advisory --subconscious-model`) and avoid model calls unless explicitly requested;
- decide the Phase 9 surface order with this bias: conscious-task/candidate review first, dashboard visibility second, outbox proposal schema third, direct delivery last.

Gate:

```bash
python -m pytest tests -q
python -m py_compile agent_sensorium/*.py scripts/*.py
```

## Deferred but important

- Dashboard/plugin UI for at-a-glance state.
- Hindsight/RSS/file crawl sensors.
- Platform thread creation.
- Public/reusable release packaging.

## Completion standard

A phase is done only when:

1. Tests cover the success and refusal path.
2. State mutations write receipt or are explicitly read-only.
3. Skill/docs explain the operational boundary.
4. Repo gate is green.
5. Change is committed.
6. Installed snapshot is synced only after the commit is green.
