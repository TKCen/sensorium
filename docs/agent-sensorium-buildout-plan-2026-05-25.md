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

### Phase 7 — Deterministic sensors and shadow tick

**Why:** Sensorium needs low-cost sensing before model-backed advisory.

**Acceptance:**

- session-event/artifact/explicit-operator sensors emit compact signals only;
- `scripts/sensorium_tick.py` can run status, compaction, dispatch dry-run, and deterministic services;
- cron/no-agent use stays silent unless errors occur;
- tick writes receipts but sends no user-facing messages.

### Phase 8 — Subconscious advisory dry-run

**Why:** Semantic consolidation should arrive only after deterministic spine and loop breakers are observable.

**Acceptance:**

- advisory context builder produces bounded context: config summary, top candidates, recent events/decisions, no raw transcripts;
- advisory output schema validates `DROP | SAVE | CREATE_CONSCIOUS_TASK`;
- model lane is disabled by default;
- dry-run stores advisory receipt and never performs external side effects.

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

Implement Phase 6 next: instance config and policy cards.

Files:

- Add `agent_sensorium/config.py` for instance config loading/validation.
- Modify `agent_sensorium/tools.py` and `agent_sensorium/plugin.py` to load instance config.
- Add focused tests for config loading, missing-config defaults, and policy surface-scope narrowing.
- Update bundled skill/docs with the config boundary.

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
