# Sensorium Kanban-only activation migration — 2026-05-28

## Decision

Hermes Kanban is the only activation and ticketing substrate for Sensorium salience.

Sensorium keeps these responsibilities:

- ingest signals/events;
- score/correlate candidates;
- expose status, attention inbox, pointers, and thread capsules for existing state;
- record decisions, receipts, settlements, and compact feedback refs.

Kanban owns these responsibilities:

- sensor intake tickets (`sensor:intake:*`);
- cheap Subconscious review tickets (`subconscious:review:*`);
- conscious review/execution tickets (`conscious:review:*`);
- worker/action/outbox follow-up as task comments, child tasks, artifacts, or compact refs.

## API inventory

### Keep

- `sensorium_status`, `sensorium_attention_inbox`, `sensorium_attention_pointer`, `sensorium_thread_open`, `sensorium_thread_update`, `sensorium_candidate_update`, `sensorium_compact`, and `sensorium_service_threads`.
- `sensorium_ingest_signal` and `sensorium_ingest_event` for deterministic sensors/manual validation.
- `sensorium_subconscious_advisory` only as disabled-by-default internal advisory; live promotion/settlement should come from Kanban review tasks.

### Compatibility-gated / deprecated

- `sensorium_dispatch_once` no longer returns `would_promote` or creates threads by default. It reports `kanban_review_required` in dry-run and `legacy_dispatch_disabled` for mutating calls unless `config.legacy_thread_dispatch_enabled=true` is passed explicitly.
- `candidate_to_thread` remains only to build a legacy preview/migration capsule.
- `sensorium_conscious_claim` and `sensorium_conscious_complete` are disabled by default through `CONSCIOUS_DEFAULTS.enabled=false`; use `config.enabled=true` only for old-state migration/tests.
- `sensorium_worker_*`, `sensorium_outbox_*`, and `sensorium_action_*` remain as prepared-record/receipt helpers for existing thread capsules, but live work should be represented on Kanban tasks when possible.

### Remove later

After existing `threads.jsonl`, `worker_requests.jsonl`, `outbox_requests.jsonl`, and `thread_actions.jsonl` state is archived or migrated to Kanban refs, remove direct registration of the old dispatcher/conscious lease tools from the live plugin surface.

## Live path

```text
deterministic sensor tick
→ Sensorium event/candidate
→ sensorium_kanban_sensor_tick.py mirrors first/new events to sensor:intake tasks
→ serasubconscious review settles DROP/SAVE/PROMOTE_CONSCIOUS
→ settlement CLI updates Sensorium candidate truth
→ optional conscious:review Kanban task assigned to default
```

Repeat samples from the same unresolved incident are coalesced by the Kanban bridge and receive deterministic DROP settlement against the matching Sensorium candidate so board-clean cannot leave an active hidden `would_promote` candidate.

## Regression invariant

A clean Sensorium Kanban board must not coexist with a mutating Sensorium dispatcher lane. The legacy dispatcher may expose read-only advisory (`kanban_review_required`) but must not create dormant threads unless an explicit legacy config opt-in is present in a migration/test path.
