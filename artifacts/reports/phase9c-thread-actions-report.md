# Phase 9C — Generic Thread Actions Report

Date: 2026-05-27
Branch: omc/phase9c-thread-actions
Base: main at Phase 9B commit 3eb3d22

## Summary

Implemented a generic prepared-action / motor-plan substrate attached to conscious threads. A thread action represents "this thread has an intended/prepared/offerable next movement" without forcing a concrete medium or expression type. Intent is a free string; the reusable core validates structure, bounds, refs, and state transitions but does not enforce an expression taxonomy. Concrete artifact/expression forms are instance-level and emerge at Conscious time.

## Changed files

| File | Change |
|------|--------|
| `agent_sensorium/actions.py` | **New.** Core actions module: prepare, attach, result, list, compact helpers, feedback signal builder |
| `agent_sensorium/store.py` | Added `thread_actions` JSONL stream to `_STATE_NAMES` |
| `agent_sensorium/tools.py` | Added 4 action tool handlers; integrated compact actions into `handle_sensorium_thread_open` capsule |
| `agent_sensorium/pointers.py` | Added action count hint to pointer invitation; no content leak |
| `tests/test_actions.py` | **New.** 54 tests covering all spec requirements |
| `docs/agent-sensorium-buildout-plan-2026-05-25.md` | Updated Phase 9 status with 9C implementation notes |
| `skills/agent-sensorium/SKILL.md` | Added thread actions boundary section, state machine, config defaults, and 4 new tool entries |
| `artifacts/reports/phase9c-thread-actions-report.md` | This report |

## Thread action schema

```json
{
  "id": "tact_...",
  "ts": "2026-05-27T...",
  "updated_at": "...",
  "status": "proposed",
  "origin_thread_id": "sth_...",
  "origin_candidate_id": "cand_...",
  "title": "bounded string",
  "intent": "free string, bounded",
  "summary": "bounded",
  "why_now": "bounded",
  "refs": { "key": "value metadata only" },
  "attachments": [{ "kind": "worker_request", "ref_id": "wreq_...", "metadata": {}, "attached_at": "..." }],
  "sensitivity": "private",
  "allowed_surfaces": ["local"],
  "resume_trigger": "",
  "idempotency_key": "sha256[:24]",
  "outcome": "",
  "result_summary": "",
  "feedback_signal_id": "",
  "closed_reason": ""
}
```

## State machine

```
proposed -> prepared -> offered -> acted    (outcome: completed)
proposed -> closed                          (outcome: failed/superseded)
proposed -> rejected                        (outcome: rejected)
proposed -> cancelled                       (outcome: cancelled)
proposed -> expired                         (outcome: expired)
```

Terminal statuses: `acted`, `closed`, `expired`, `cancelled`, `rejected`.

## New tool surfaces

| Tool | Description |
|------|-------------|
| `sensorium_action_prepare` | Prepare/propose a generic thread action from a dormant/held thread. Internal record only; no external side effect. |
| `sensorium_action_attach` | Attach a compact ref to an existing action. Validates attachment kind and bounds metadata. |
| `sensorium_action_result` | Mark action terminal; write decision receipt; emit validated feedback signal with `caused_by` containing `action_id` and `origin_thread_id`. |
| `sensorium_action_status` | Compact list/status filtered by `thread_id` and/or `status`. |

## Tests added

54 new tests in `tests/test_actions.py`:

- **TestPrepareAction** (7): success from dormant/held, idempotency, interaction refs, free-string intent, bounded intent/summary/refs, inherited sensitivity/surfaces
- **TestPrepareActionDenials** (6): missing thread, closed/archived thread, disabled actions, missing/whitespace intent
- **TestAttachActionRef** (10): all 4 attachment kinds, invalid kind, too many attachments, metadata too large, empty ref_id, terminal action, nonexistent action
- **TestRecordActionResult** (10): completed/rejected/cancelled/expired/failed/superseded outcomes, status mapping, invalid outcome, already terminal, nonexistent action, correlation keys from candidate
- **TestFeedbackSignalValidation** (1): feedback signal validates with existing schema
- **TestListThreadActions** (4): list all, filter by thread, filter by status, attachment count
- **TestThreadOpenActionIntegration** (4): includes visible actions, omits terminal, surface visibility, no key when empty
- **TestPointerActionIntegration** (3): includes action count, no count when empty, no content leak
- **TestToolHandlers** (4): prepare/attach/result/status tool wrappers
- **TestLivePressureFixture** (3): process_pressure thread action, kanban_pressure thread action, full lifecycle with feedback

## Live-pressure fixture notes

Tests exercise generic actions against threads analogous to existing sensory pressure threads (`process_pressure`, `kanban_pressure`). Intent values like `inspect_pressure` and `resolve_current_pressure` are free strings — no expression-type enum is required. The full lifecycle test covers prepare -> attach worker ref -> result -> feedback signal validation.

No live state mutation. All tests use `tmp_path` fixtures. A lead-run smoke against live `~/.hermes` state can be done by preparing an action via the tool handler with a temporary `state_dir` copy.

## Gate output

```
python -m pytest tests -v                    # 513 passed
python -m py_compile agent_sensorium/*.py scripts/*.py dashboard/plugin_api.py  # OK
git diff --check                             # clean
```

## Safety boundaries

- No live Discord sends
- No platform thread creation
- No image/audio/media generation
- No external Kanban task creation
- No edits to live ~/.hermes state
- No direct delegate_task calls from plugin code
- No push to remote
- No install to live plugin
- Intent is NOT a fixed enum; expression taxonomy is instance-level
- Attachment kinds are storage categories, not expression types
- No media blobs or raw content in attachments

## Deferred

- Concrete expression/generation/dispatch (instance-level, Conscious time)
- Live smoke command against real state (lead will run after review)
- Action compaction/TTL (can reuse existing compact pass pattern)
- Dashboard exposure of actions
- Tick integration for action expiry

## Final commit hash

9b73067 (see `git log --oneline -1` for final)
