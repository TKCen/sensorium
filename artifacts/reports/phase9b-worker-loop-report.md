# Phase 9B: Conscious Delegation + Worker Request Lifecycle

## Summary

Phase 9B adds the conscious delegation boundary to Agent Sensorium. A conscious thread can now prepare bounded worker requests, optionally dispatch them through an adapter, and record results that re-enter the pipeline as feedback signals with causal refs. Preparing is not executing — dispatch requires explicit double opt-in (`execute=True` AND `config.direct_dispatch_enabled=True`).

The Subconscious advisory layer accepts a new `DELEGATE_WORK` conscious-task request type, but the advisory handler can only create internal candidates — it cannot prepare or dispatch worker requests.

## Changed Files

| File | Change |
|------|--------|
| `agent_sensorium/workers.py` | **NEW** — Core worker module: prepare, dispatch, record_result, list, adapters |
| `agent_sensorium/store.py` | Added `worker_requests` stream to `_STATE_NAMES` |
| `agent_sensorium/subconscious.py` | Added `DELEGATE_WORK` to `VALID_REQUEST_TYPES` |
| `agent_sensorium/tools.py` | Added 4 worker tool handlers + worker import |
| `agent_sensorium/plugin.py` | Registered 4 new tools: prepare, dispatch, result, status |
| `agent_sensorium/commands.py` | Added `/sensorium workers` subcommand |
| `tests/test_workers.py` | **NEW** — 34 tests covering full worker lifecycle |
| `tests/test_plugin_registration.py` | Updated expected tool set to include 4 new tools |
| `skills/agent-sensorium/SKILL.md` | Documented conscious delegation boundary, worker tools, state machine, config |
| `artifacts/reports/phase9b-worker-loop-report.md` | This report |

## Worker Lifecycle State Machine

```
prepared -> dispatched -> completed
prepared -> dispatched -> failed
prepared -> completed       (manual result without dispatch)
prepared -> failed          (dispatch adapter failure or manual)
prepared -> cancelled
```

## New Tool Surfaces

| Tool | Purpose |
|------|---------|
| `sensorium_worker_prepare` | Prepare a bounded worker request from a conscious thread (internal record, no execution) |
| `sensorium_worker_dispatch` | Dispatch a prepared worker request (no-op unless execute=True AND direct_dispatch_enabled=True AND adapter provided) |
| `sensorium_worker_result` | Record worker result, write `worker.result` receipt, emit feedback signal with `caused_by` containing `worker_request_id` and `origin_thread_id` |
| `sensorium_worker_status` | List worker requests with optional thread_id/status filters |

## Tests Added/Updated

### New: `tests/test_workers.py` (34 tests)

- **TestPrepareWorkerRequest** (5): success from dormant/held thread, idempotency, thread interaction refs, truncation
- **TestPrepareWorkerDenials** (6): missing thread, closed/archived thread, invalid/disallowed worker_type, disabled workers
- **TestDispatchWorkerRequest** (7): dry-run without execute, disabled by default with execute, fake adapter success, thread ref updates, adapter failure, no adapter, nonexistent request
- **TestRecordWorkerResult** (7): completed/failed results, from dispatched status, invalid outcome, already completed, truncation, correlation keys from origin candidate
- **TestToolHandlers** (4): prepare/dispatch/result/status tool wrappers, feedback emission
- **TestFeedbackSignalValidation** (1): feedback signal validates against existing schemas (system_action scope)
- **TestSubconsciousDelegateWork** (2): DELEGATE_WORK accepted in advisory, advisory does not prepare/dispatch
- **TestListWorkerRequests** (2): list all, filter by status

### Updated: `tests/test_plugin_registration.py`

- Added 4 new tools to expected tool set assertion

## Gate Output Summary

```
python -m pytest tests/ -v
  459 passed, 0 failed

python -m py_compile agent_sensorium/*.py scripts/*.py dashboard/plugin_api.py
  All OK

git diff --check
  No whitespace errors
```

## Safety Boundaries / Deferred Live Execution

1. **Subconscious cannot dispatch workers.** Advisory can propose `DELEGATE_WORK` candidates but cannot prepare or dispatch worker requests.
2. **Prepare != Execute.** `sensorium_worker_prepare` creates an internal JSONL record only. No external side effects.
3. **Double opt-in for dispatch.** `sensorium_worker_dispatch` requires BOTH `execute=True` AND `config.direct_dispatch_enabled=True` (default: false). Even then, an adapter must be provided.
4. **Feedback self-loop protection.** Worker results emit feedback with `feedback_scope: system_action`. The dispatcher's self-loop filter prevents system_action feedback from autonomously waking consciousness without operator evaluation evidence.
5. **Truncation enforced.** `task_summary` and `result_summary` are truncated to `max_prompt_chars`/`max_result_chars` (default 2000). Output refs are metadata-only dicts, not raw content.
6. **No live adapters wired.** Only `FakeWorkerAdapter` exists for tests. Live worker adapters (Hermes subagent, script execution, kanban task creation) are deferred and policy-gated.
7. **No live Discord/platform side effects.** No Discord sends, no platform thread creation, no edits to live config/plugins/state.

## Final Git Commit Hash

`1f1bc14`
