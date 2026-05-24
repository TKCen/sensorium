# Sensorium Next — Ultrawork Report

**Branch:** omc/sensorium-next-20260524-231445
**Date:** 2026-05-24
**Commit:** fd3236c

## Chosen Slice

**Richer status with closed/archived counts and latest decision receipt.**

Why: After reviewing the two recent commits (dd4040f pointers, 3a6bf8e thread lifecycle), the status output only showed dormant/held thread counts and active candidate counts. An operator had no visibility into how many threads were closed/archived or what the most recent decision was, forcing manual JSONL inspection. This is the smallest bounded improvement that makes the system easier to operate without adding new mechanisms or violating MVP guardrails.

## Files Changed

| File | Change |
|------|--------|
| `agent_sensorium/tools.py` | Added `closed_threads`, `archived_threads`, `archived_candidates` to status counts; added `latest_decision` field from decisions JSONL |
| `agent_sensorium/commands.py` | Updated `_fmt_status` to display terminal counts (e.g. `1c 0a`) and latest decision receipt |
| `tests/test_tools.py` | 4 new tests: empty terminal counts, closed thread counted, latest decision present, no decision when empty |
| `tests/test_commands.py` | 2 new tests: formatted terminal counts in output, empty status with zero terminal counts |

## Tests

```
python -m pytest tests -q -v → 103 passed (6 new, 97 existing)
python -m py_compile agent_sensorium/*.py scripts/*.py → OK
```

## Review Findings on Recent Commits

Code review of dd4040f + 3a6bf8e identified the following issues in existing code (not introduced by this slice):

### HIGH

1. **`_find_thread` bypasses visibility for explicit IDs** — `tools.py:194-202`. When given a specific thread_id (not "latest"), `_find_thread` searches all threads including closed/archived. This allows `thread_open` to serve capsules for terminal threads and `thread_update` to resurrect archived threads.

2. **No state-transition guards in `thread_update`** — `tools.py:253-305`. Any action is accepted on any status: close-on-closed succeeds silently, resume-on-archived resurrects a terminal thread, archive-on-archived is a no-op with a receipt.

3. **`pre_llm_call` hook drops `state_dir`** — `plugin.py:271-278`. The hook lambda doesn't forward `state_dir`, so it always uses the production path. Every other tool registration forwards `state_dir`.

### MEDIUM

4. **`_compact_thread_capsule` includes `continuity_summary`** — By design (capsule open is the full reveal after the pointer door-handle), but the privacy boundary between pointer and capsule should be explicitly documented since `continuity_summary` is operational memory.

5. **Tool-path `attention_pointer` doesn't record cooldown receipt** — `tools.py:308-317`. The tool is preview-only (no mutation), diverging from the hook path which records receipts. Should be documented or renamed.

6. **Type annotations** — `commands.py` format functions accept `state_dir: str | None` but declare `str`.

## HIGH Fixes — Commit 1205f6f

All 3 HIGH findings resolved in a single commit.

### HIGH-1: Thread open visibility gate

`handle_sensorium_thread_open` now checks `target.get("status") not in _VISIBLE_STATUSES` before returning a capsule. Closed and archived threads are refused even when addressed by explicit ID.

### HIGH-2: State-transition guards

Added `_ALLOWED_THREAD_TRANSITIONS` table:
- `dormant` → `{close, hold, archive, mark_reviewed, pin, unpin}`
- `held` → `{close, resume, archive, mark_reviewed, pin, unpin}`
- Terminal statuses (`closed`, `archived`) → reject all actions with error, no receipt written.
- Pin/unpin refuse no-ops (already pinned / not pinned).

### HIGH-3: Hook state_dir forwarding

`pre_llm_call` hook lambda now passes `state_dir=kw.get("state_dir")` to `handle_pointer_pre_llm`.

### Files Changed

| File | Change |
|------|--------|
| `agent_sensorium/tools.py` | Added `_VISIBLE_STATUSES`, `_ALLOWED_THREAD_TRANSITIONS`; visibility gate in `thread_open`; transition guards in `thread_update` |
| `agent_sensorium/plugin.py` | Added `state_dir=kw.get("state_dir")` to hook lambda |
| `tests/test_thread_lifecycle.py` | 9 new tests: open closed/archived refused, resume closed/archived refused with no receipt, repeated close/hold refused with no receipt, allowed transitions work, pin-already-pinned refused |
| `tests/test_plugin_registration.py` | 1 new test: hook forwards state_dir and records cooldown receipt |

### Tests

```
python -m pytest tests -q -v → 112 passed (10 new, 102 existing)
python -m py_compile agent_sensorium/*.py scripts/*.py → OK
```

### Remaining MEDIUM Items (not addressed)

4. `_compact_thread_capsule` includes `continuity_summary` — intentional design; needs documentation.
5. Tool-path `attention_pointer` is preview-only (no cooldown receipt) — needs naming/doc clarification.
6. Type annotation mismatch in `commands.py` format functions — cosmetic.

## Status

**DONE**
