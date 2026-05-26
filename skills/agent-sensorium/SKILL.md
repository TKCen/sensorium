# Agent Sensorium

Bounded autonomous inner lifecycle for Hermes agents: compact signals, filtered events, candidates, dormant conscious thread capsules, and pull-based review.

## Pipeline

```
Sensors -> Signals -> [Gate] -> Events -> Candidates -> [Dispatcher] -> Conscious Threads
```

1. **Sensors** emit raw **Signals** (observations, corrections, artifacts).
2. A deterministic **Gate** promotes strong signals to **Events** based on strength and kind thresholds.
3. Events create **Candidates** with weighted pressure scores.
4. Optional **Subconscious advisory** can build bounded context from Events/Candidates/Decisions and propose an internal conscious-task candidate. The model lane is disabled by default.
5. A **Dispatcher** promotes the top candidate into a dormant **Conscious Thread** capsule.
6. A tiny active-session pointer can mention that an eligible thread exists when the current surface is allowed and cooldown is open. The pointer is only a doorway, not the capsule.

## MVP Limitations

- **Pull-based only** — no proactive messages, no DM delivery.
- **Subconscious advisory is bounded and disabled by default** — it builds compact context and can validate/store advisory receipts; live model calls are not run by the plugin core.
- **No platform thread creation** — threads are internal state only.
- **No relational autonomy** — no REACH_OUT decisions.
- **No external task creation** — no Kanban/research/media tasks.
- **No scheduled automation** — tick runs manually or via explicit invocation.

## Trusted event imports

Use `sensorium_ingest_signal` for low-level observations that still need deterministic thresholding. Use `sensorium_ingest_event` only when an upstream sensor/importer has already done the first filtering step and produced a compact promoted event. Trusted event imports still validate required event fields and sensitivity, then create or update one candidate for dispatcher review. They do not create raw signal records.

## Dedupe and correlation

Signal, event, and candidate records persist deterministic fingerprints. Re-importing the same signal or event is idempotent: the tool returns the existing ids and does not append duplicate JSONL records. Related promoted events with the same kind and overlapping `correlation_keys` coalesce into the existing active candidate by extending `event_ids`, raising repetition/pressure deterministically, narrowing sensitivity/surface scope, and writing a local decision receipt. This is still local-only and deterministic; no model-backed semantic clustering runs in MVP.

## Dispatcher lock, budgets, and state.latest

Mutating dispatch is guarded by one local dispatcher lock/lease under the instance state directory. An active unexpired lock returns `lock_unavailable` and creates no thread; an expired lock is recovered with a `dispatch.lock_recovered` decision receipt before dispatch continues. Dispatch state is written to `state.latest.json` with `state_version`, `last_dispatch_result`, `budgets`, and lock status. Status exposes those fields so dashboards and operators can observe the attention scheduler without owning it. Token buckets are currently enforced for mutating dispatch and visible for dispatch/pointer/conscious/advisory lanes; pointer/conscious/advisory consumption remains deferred until those services exist.

## Feedback lane and loop breakers

Feedback signals (`source == "feedback"`) re-enter the pipeline with three required fields:

- **`caused_by`**: compact non-empty dict identifying the action/thread/candidate being evaluated.
- **`outcome`**: explicit result string classified deterministically:
  - *Delivery-only* (not success): `delivered`, `sent`, `posted`, `dispatched`, `queued`.
  - *Success*: `operator_approved`, `completed`, `response_received`, `acknowledged`.
  - *Failure*: `operator_rejected`, `failed`, `no_response`, `expired_no_response`.
- **`feedback_scope`**: one of `thread`, `candidate`, `delivery`, `operator_evaluation`, `system_action`.

Feedback metadata propagates through events and candidates as `feedback_meta`. The dispatcher rejects self-loop-only candidates: feedback about sensorium's own prior actions (thread/candidate/dispatch IDs) without operator evaluation evidence cannot wake consciousness by itself. Only `feedback_scope: operator_evaluation` bypasses the self-loop filter.

Thread close/update can optionally emit a local feedback signal plus a `thread.feedback_emitted` decision receipt when `emit_feedback=True` is passed. The emitted signal uses `source: feedback`, `feedback_scope: operator_evaluation`, a thread/action `caused_by` dict, and an evaluated outcome (`completed` for close/mark_reviewed, `operator_rejected` for archive). Default is silent — no feedback emission unless explicitly configured. This is local-only: no outbound messages, no external tasks.

## Thread service boundary

Threads carry lifecycle fields: `dirty_since`, `hold_reason`, `resume_trigger`, `last_interaction_at`, and `source_refs`. The `sensorium_service_threads` tool runs a deterministic service pass that:

- Archives threads past their TTL (unless pinned)
- Identifies starved threads (no interaction for >72h by default)
- Reports dirty threads (needing re-summarization)
- Reports expiring threads (within 24h of TTL)

All archival decisions write `service.thread_archived` receipts. Thread closure writes `thread.settlement` receipts containing correlation keys and fingerprint from the origin candidate, enabling downstream suppression without model-backed learning.

Hold/resume preserves `hold_reason` and `resume_trigger` fields. Every thread update stamps `last_interaction_at`.

## Subconscious advisory boundary

`sensorium_subconscious_advisory` is the Phase 8 internal advisory lane. It reads promoted Events, active Candidates, recent Decisions, and probe-audit summary into bounded context. It does **not** read raw signals, transcripts, files, task bodies, memory text, or Kanban task details.

The advisory output schema accepts only:

- `DROP` — write a local advisory receipt;
- `SAVE` — write a local advisory receipt for later conscious interpretation;
- `CREATE_CONSCIOUS_TASK` — create or preview one internal `subconscious_advisory` candidate with embedded `conscious_task` fields.

The lane is disabled by default. With no explicit advisory output, enabled runs return `model_output_required`; the plugin core does not call an LLM directly. Dry-run mode stores a local `subconscious.advisory` receipt by default when invoked outside tick `--dry-run`, but it never creates candidates, threads, outbound messages, platform threads, or external Kanban/research/media work.

## Tools

| Tool | Description |
|------|-------------|
| `sensorium_status` | Read-only state snapshot: counts, top candidates, visible threads, and instance config diagnostics |
| `sensorium_ingest_signal` | Ingest a signal and promote if threshold met |
| `sensorium_ingest_event` | Ingest an already-promoted trusted event and create a candidate |
| `sensorium_dispatch_once` | Select top candidate and create one dormant thread |
| `sensorium_candidate_update` | Suppress / hold / cancel / mark_reviewed a candidate |
| `sensorium_attention_pointer` | Preview the small active-session pointer for a surface; read-only and non-mutating |
| `sensorium_thread_open` | Open a compact conscious-thread capsule when the requested surface is allowed |
| `sensorium_thread_update` | Close / hold / resume / archive / pin / unpin a conscious thread with a receipt |
| `sensorium_service_threads` | Deterministic thread service pass: TTL archival, starvation/dirty/expiring reports |
| `sensorium_subconscious_advisory` | Bounded advisory context/output validator; disabled by default; may create only internal conscious-task candidates when explicitly enabled |
| `sensorium_compact` | Archive expired candidates and threads with receipts |

## Instance config and policy boundary

The reusable `agent_sensorium` package is generic — it contains no instance-specific identity, channel IDs, or private policy. Instance-specific configuration lives in a separate config file loaded at runtime.

### Config discovery order

1. Explicit `config_path` argument (passed to tools/handlers)
2. `{state_dir}/instance.config.json` (auto-discovered from instance state directory)
3. Safe defaults: `allowed_surfaces: ["local"]`, `max_sensitivity: "private"`, no policy card

### Config file format

```json
{
  "instance_name": "sera",
  "policy_card_ref": "docs/sera-policy-card.md",
  "allowed_surfaces": ["local", "discord"],
  "max_sensitivity": "private",
  "thresholds": { "starvation_hours": 72, "expiring_window_hours": 24 },
  "budgets": { "dispatch": { "capacity": 10, "window_seconds": 3600 } }
}
```

### Policy rules

- **Surface policy** intersects item `allowed_surfaces` with config `allowed_surfaces`. Config can only narrow scope, never broaden it.
- **Sensitivity policy** takes the more restrictive of item sensitivity and config `max_sensitivity`. An item marked `local_only` stays `local_only` even if config allows `public_safe`.
- **Missing config** fails safe: local-only surfaces, private sensitivity, default thresholds.
- **Diagnostics** (`sensorium_status`) expose compact config status (source, path, policy_card_ref, instance_name, allowed_surfaces, max_sensitivity) — never raw budgets, thresholds, or private policy contents.

### Sample configs

See `docs/examples/sera-instance-config.json` and `docs/examples/sera-policy-card.md` for a sample instance config and policy card outside the reusable package.

## Pointer vs capsule boundary

The active-session pointer is a doorway, not awareness itself. It may reveal only:

- that one eligible dormant/held Sensorium thread exists;
- the thread id and short title;
- the operator phrase for opening it, such as “take it up”.

The pointer must not include capsule internals such as continuity notes, decision logs, open questions, or private operational memory. Those are returned only by `sensorium_thread_open` after the requested surface passes the thread’s `allowed_surfaces` gate.

There are two pointer paths:

- `sensorium_attention_pointer` is a **preview tool**. It does not mutate state or write cooldown receipts.
- The `pre_llm_call` hook is the **presentation path**. When it injects a pointer into the active turn, it writes a `pointer.presented` receipt so cooldown can suppress repeats.

The injected pointer context is model-facing validation scaffolding. It can include explicit instructions such as “if the user says take it up, call `sensorium_thread_open`.” Final user-facing UX should stay smaller and more natural.

## Command

```
/sensorium [status|threads|pointer|open|thread|dispatch|compact|help]
```

- **status** (default) — compact overview with counts and top items
- **threads** — visible dormant/held threads with origin info
- **pointer [surface]** — preview the small doorway that may be injected into an active session when surface/privacy/cooldown gates allow it
- **open [thread_id|latest] [surface]** — open a compact thread capsule if the requested surface is allowed
- **thread <thread_id|latest> <close|hold|resume|archive|pin|unpin|mark_reviewed> [reason]** — update thread lifecycle/pin state with a receipt
- **dispatch** — dry-run dispatch preview (never mutates via command)
- **compact** — archive expired items
- **help** — usage reference

## Deterministic Sensors

Compact sensor helpers in `agent_sensorium.sensors` emit signal dicts suitable for `sensorium_ingest_signal`. They are stdlib-only, deterministic, and contain only metadata — never raw file contents, transcripts, or unbounded data.

| Helper | Source | Use |
|--------|--------|-----|
| `session_event_signal` | `hermes_session` | Compact session event: kind/summary/ref |
| `artifact_signal` | `artifact` | File metadata: path/size/hash/ref, no content |
| `operator_signal` | `manual` | Explicit operator note/correction |
| `machine_body_pressure_sample` + `classify_machine_body_pressure` | `machine` | Cheap body-pressure samples and transition-only signals |
| `machine_network_pressure_sample` + `classify_machine_network_pressure` | `machine` | Interface error/drop and TCP-state pressure counts, no endpoints |
| `machine_process_pressure_sample` + `classify_machine_process_pressure` | `machine` | Process-state counts for zombies/D-state, no cmdlines |
| `hindsight_pressure_sample` + `classify_hindsight_pressure` | `memory` | Hindsight API/operation queue pressure counts only |
| `kanban_pressure_sample` + `classify_kanban_pressure` | `kanban` | Kanban board aggregate pressure counts only |

Summaries are truncated to 200 chars. Sensitivity defaults to `private`, surfaces to `["local"]`; body-pressure signals are `local_only` and global-scoped because machine pressure affects all local agents.

### Pressure sensors

The wired generic sensors are explicit tick options and can also be run together with `scripts/sensorium_tick.py --all-sensors`:

- `--body-pressure`: samples `/proc/loadavg`, `/proc/meminfo`, `/proc/pressure/{cpu,memory,io}`, swap, disk, and inode pressure. In WSL, default disk sampling includes `/` plus mounted Windows drive roots such as `/mnt/c`, so it sees both ext4/VHDX-internal fullness and host-drive free-space pressure around the VHDX. It does not inspect process command lines, raw process lists, transcripts, logs, or task outputs.
- `--network-pressure`: samples `/proc/net/dev`, `/proc/net/tcp`, and `/proc/net/tcp6`; it records interface names, aggregate error/drop counters, and TCP state counts only. It does not store packet data, local/remote addresses, ports, DNS names, or connection tuples.
- `--process-pressure`: samples `/proc/[pid]/stat` state letters only, reporting aggregate process count, zombie count, and uninterruptible-sleep count. It does not store PIDs, command lines, env vars, cwd, executable paths, or raw process lists.
- `--hindsight-pressure`: samples the local Hindsight API for health and operation queue counts only (`pending`, `processing`, `failed`). It does not call Hindsight reflect/recall/retain and does not read memory text.
- `--kanban-pressure`: opens Kanban SQLite boards read-only and reports aggregate task/status/stale-running/failed/blocked counts only. It does not read task title, body, comments, result, errors, branch names, or session IDs.

Body sampling keeps tiny rolling state in `{state_dir}/body_pressure_state.json` and emits no signal for healthy samples. It emits compact signals only for deterministic transitions such as `healthy_to_degraded`, `healthy_to_critical`, `degraded_to_recovered`, or rate-limited `sustained_degraded`. Runtime sensing uses present-tense samples plus short-window debounce only; replay helpers are for tests/audits and are not runtime sensing. The other pressure sensors keep tiny `{name}_state.json` transition state and emit only when the observed pressure level changes.

## Shadow Tick

`scripts/sensorium_tick.py` runs deterministic lifecycle operations without model calls or outbound delivery:

1. **Optional pressure sensors** — `--body-pressure`, `--network-pressure`, `--process-pressure`, `--hindsight-pressure`, `--kanban-pressure`, or `--all-sensors`; sample present-tense counters and ingest only transition signals.
2. **Compact** — archive expired candidates/threads
3. **Service** — TTL archival, starvation/dirty/expiring reports
4. **Optional Subconscious advisory** — `--subconscious-advisory` builds bounded context and runs disabled/dry-run by default; `--enable-subconscious-advisory` explicitly enables advisory-output handling, but the core tick still does not perform live model calls
5. **Dispatch preview** — dry-run dispatch (never creates threads)
6. **Status** — read-only state snapshot

Silent on stdout by default (safe for cron). Use `--json` for output. Writes a `tick.completed` receipt to local decisions JSONL. Use `--dry-run` to skip mutations, including body-pressure state persistence and signal ingest.

The tick must not: send messages, create external tasks, create platform threads, call messaging APIs, or invoke models.

## Probe Coverage Boundary

Before enabling model-backed advisory, validate that real probes produce enough compact signals. Do not confuse helper functions with live sensing. A probe audit should distinguish:

- implemented helpers (`session_event_signal`, `artifact_signal`, `operator_signal`);
- wired live probes that actually call ingest;
- configured watched sources that are currently silent;
- blind spots such as Hindsight echoes, RSS/feed items, file crawls, task results, or active-session summaries when not yet wired.

Use temporary state by default for smoke validation. A good pre-advisory smoke exercises at least session-event, artifact, and explicit-operator source classes end-to-end into `signals/inbox.jsonl`, then reports counts by sensor/source/kind, freshness, and promotion yield. The smoke/audit must not store raw transcripts, raw file contents, outbound messages, platform threads, external tasks, or model output.

## Probe Audit Tool

`scripts/sensorium_probe_audit.py` validates probe coverage and exercises the signal pipeline without model calls or live state mutation.

### Subcommands

| Subcommand | Description |
|------------|-------------|
| `inventory` | List implemented helpers, wired live probes, configured sources, and blind spots |
| `smoke` | Exercise session-event, artifact, and operator probes end-to-end in temp state |
| `audit` | Report counts by sensor/source/kind, freshness, promotion yield, and silent sources |

### Usage

```bash
# Probe inventory (helpers vs wired vs blind spots)
python scripts/sensorium_probe_audit.py inventory --json

# Smoke test — exercises 3 source classes in temp state (default)
python scripts/sensorium_probe_audit.py smoke --json

# Smoke test — explicit state dir
python scripts/sensorium_probe_audit.py smoke --json --state-dir /tmp/my_probe_state

# Audit an existing store
python scripts/sensorium_probe_audit.py audit --json --state-dir /path/to/state
python scripts/sensorium_probe_audit.py audit --json --instance sera
```

### Interpreting results

- **inventory**: `wired_live_probes` should grow as sensors are connected to live hooks. `blind_spots` lists sensors not yet implemented.
- **smoke**: `all_promoted: true` means all three source classes successfully traverse signal → event → candidate. Each probe should have `signal_id`, `event_id`, and `candidate_id`.
- **audit**: `promotion_yield.yield_pct` shows what fraction of signals become events. `silent_sources` lists configured sources with zero signals. `blind_spots` lists unimplemented sensors.

### Programmatic API

```python
from agent_sensorium.probe_audit import probe_inventory, run_smoke_probes, audit_store

inv = probe_inventory()
smoke = run_smoke_probes(state_dir="/tmp/probe_test")
report = audit_store(state_dir="/tmp/probe_test", instance="probe_smoke")
```

### Safety

- Uses temporary state by default for smoke; never mutates the real default Sensorium unless `--state-dir` explicitly points there.
- Never writes raw transcripts, file contents, outbound messages, platform threads, external tasks, or model output.
- No model calls. Stdlib-only.

## Conscious Review Checklist

When reviewing a dormant thread, choose exactly one action:

- [ ] **Suppress** — noise, not actionable, discard
- [ ] **Hold** — interesting but not urgent; revisit later
- [ ] **Save** — convert to workflow guidance or reference note
- [ ] **Close** — resolved or no longer relevant
- [ ] **Create follow-up** — bounded, specific next action only

### Review boundaries

- Do NOT auto-send messages to the operator.
- Do NOT create external tasks without explicit approval.
- Do NOT escalate beyond the current review scope.
- Do NOT assume urgency from pressure scores alone.
- Do NOT act on expired or archived threads.
