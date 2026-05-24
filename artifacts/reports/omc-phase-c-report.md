# Phase C Implementation Report

## What was implemented

### Dispatcher module (`agent_sensorium/dispatcher.py`)

- `select_candidate(candidates, config)` — selects highest-pressure eligible candidate above configurable threshold (default 0.5).
- `candidate_to_thread(candidate, config)` — converts a candidate into a dormant sensorium_thread capsule with:
  - `sth_` prefixed ID
  - `status: dormant`, `origin: candidate`
  - Embedded `conscious_task` with `ctask_` ID, `request_type: THINK`, title, why, expected_decision
  - `origin_candidate_id` linking back to source
  - `continuity_summary` (2-4 bullets derived from candidate)
  - `decision_log`, `interaction_refs`, `open_questions` (empty lists)
  - `summary_dirty: false`
  - `next_prompt_to_operator` text
  - Inherited `sensitivity` and `allowed_surfaces`
  - `created_at`, `updated_at`, `expires_at` (TTL-based)
- `dispatch_once(store, dry_run, config)` — orchestrates selection and optional thread creation:
  - Returns `no_candidate` if nothing eligible
  - Returns `already_exists` with existing thread ID if candidate already dispatched (idempotent)
  - In dry_run mode: returns `would_promote` with thread preview, does not mutate state
  - In real mode: appends thread to JSONL, writes decision receipt, returns `promoted`

### Tool handler additions (`agent_sensorium/tools.py`)

- `handle_sensorium_dispatch_once(instance, state_dir, dry_run, config)` — tool surface for dispatcher.
- `handle_sensorium_candidate_update(candidate_id, action, reason, instance, state_dir)` — manual candidate status transitions:
  - Valid actions: `suppress`, `hold`, `cancel`, `mark_reviewed`
  - Appends decision receipt to decisions.jsonl (never deletes raw history)
  - Rewrites candidates.jsonl with updated status
  - Returns old/new status and receipt
- Updated `handle_sensorium_status` — now reports:
  - `dormant_threads` and `held_threads` counts
  - `top_threads` list (up to 5 visible dormant/held threads)

### Tests

- `tests/test_dispatcher.py` — 11 tests covering:
  - `select_candidate`: no candidates, below threshold, highest pressure, ignores non-candidate status
  - `candidate_to_thread`: full shape verification
  - `dispatch_once`: no candidates, dry-run non-mutation, real dispatch, idempotency, end-to-end smoke
- `tests/test_tools.py` additions — 8 new tests covering:
  - dispatch dry-run and real via tool handler
  - status showing dormant thread after dispatch
  - candidate suppress, hold, invalid action, nonexistent candidate
  - suppressed candidate excluded from dispatch

## Files changed

| File | Action | Purpose |
|------|--------|---------|
| `agent_sensorium/dispatcher.py` | Created | Candidate selection + dormant thread creation |
| `agent_sensorium/tools.py` | Modified | Added dispatch_once, candidate_update handlers; updated status |
| `tests/test_dispatcher.py` | Created | Dispatcher unit + integration tests |
| `tests/test_tools.py` | Modified | Added dispatch and candidate update tool tests |

## Tests run and results

```
61 passed
```

All modules compile cleanly via `python -m py_compile`.

Test breakdown:
- `test_schemas.py`: 12 tests (unchanged from Phase A/B)
- `test_store.py`: 9 tests (unchanged from Phase A/B)
- `test_gate.py`: 12 tests (unchanged from Phase A/B)
- `test_tools.py`: 18 tests (10 original + 8 new)
- `test_dispatcher.py`: 10 tests (new)

End-to-end smoke verified: ingest strong signal → dispatch dry_run (no mutation) → dispatch real (one thread) → dispatch again (idempotent, same thread returned).

## Deviations from implementation plan

1. **No TTL compaction in Phase C**: The plan's Task 9 (compaction/TTL cleanup) and `sensorium_compact` tool were deferred. Phase C gate criteria require dispatcher promotion and manual update — compaction is additive and can be a small follow-up without affecting the pull-review spine.

2. **No `/sensorium` command surface**: Task 8 (pull command) is a Hermes runtime convenience. The tool handlers provide equivalent functionality and are testable without runtime. Deferred to installation phase.

3. **No `scripts/sensorium_tick.py`**: Task 10 (shadow tick) deferred — the dispatch_once handler serves the same role for testing. The tick script is a thin CLI wrapper appropriate for the install phase.

4. **No Sera seed config**: Task 11 (instance config + seed signal) targets `~/.hermes/agent-sensorium/sera/` which is out of scope per Phase C instructions. The end-to-end smoke test in `test_dispatcher.py` proves the same pipeline.

5. **Candidate update uses JSONL rewrite**: Since candidates are stored in append-only JSONL, status updates require rewriting the file. This is acceptable for MVP scale. A future phase could use indexed storage.

## Remaining Phase D+ work

- **Compaction/TTL cleanup** (`sensorium_compact` tool): archive expired candidates/threads, cap visible state.
- **Pull command** (`/sensorium`): CLI/slash command for human-facing status/threads/dispatch.
- **Shadow tick script**: `scripts/sensorium_tick.py` for cron-free manual smoke.
- **Sera instance config**: Production seed config at `~/.hermes/agent-sensorium/sera/config.json`.
- **Bundled SKILL.md**: Conscious review protocol documentation for Hermes skill surface.
- **Live plugin install**: Sync tested code to `~/.hermes/plugins/agent-sensorium/`.
- **Active-session pointer injection**: Post-MVP first extension.
- **Model-backed Subconscious pass**: Post-MVP.
- **Proactive delivery / REACH_OUT**: Post-MVP.
- **Platform thread creation**: Post-MVP.
- **Hindsight/RSS/file-crawl sensors**: Post-MVP.

## Final status

AGENT_SENSORIUM_PHASE_C_DONE
