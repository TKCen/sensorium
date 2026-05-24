# Phase D Implementation Report

## What was implemented

### Compaction handler (`agent_sensorium/tools.py`)

- `handle_sensorium_compact(instance, state_dir)` — archives expired and terminal-status items with decision receipts.
- Archives candidates with past `expires_at` or terminal status (`suppressed`, `cancelled`).
- Archives threads with past `expires_at`, respecting `pinned` flag — pinned threads are never archived.
- Writes `compact.candidate_archived` and `compact.thread_archived` decision receipts to `decisions.jsonl`.
- Uses status transition approach (not file movement) for MVP simplicity.
- Never silently deletes raw history — every archive action produces a receipt with previous status and reason.

### Command handler (`agent_sensorium/commands.py`)

- `handle_sensorium_command(raw_args, instance, state_dir)` — pure function, no Hermes runtime required.
- Supported subcommands:
  - `status` (default) — compact overview with counts, top candidates, visible threads.
  - `threads` — top visible dormant/held threads with origin and creation time.
  - `dispatch` — dry-run dispatch preview (never mutates state via command surface).
  - `compact` — run compaction and report results.
  - `help` — usage reference.
- Returns compact human-facing text, no JSON.
- Unknown subcommands return help text.

### Tick script (`scripts/sensorium_tick.py`)

- CLI args: `--instance`, `--state-dir`, `--dry-run`.
- In non-dry-run mode: runs compact → dispatch → status.
- In dry-run mode: skips compact, runs dispatch (dry-run) → status.
- Prints compact JSON result to stdout.
- No model calls, no outbound delivery.

### Bundled plugin skill (`skills/agent-sensorium/SKILL.md`)

- Documents the Sensors → Signals → Events → Candidates → Threads pipeline.
- States MVP limitations (pull-only, no proactive delivery, no model-backed Subconscious).
- Lists all tools and command surface.
- Includes Conscious review checklist: suppress, hold, save, close, create follow-up.
- Includes review boundaries (no auto-send, no external tasks without approval).

### Example artifacts (`examples/`)

- `sera-config.json` — example Sera instance config with thresholds, promote_kinds, TTL.
- `seed-signal.jsonl` — one hand-authored signal for pipeline smoke testing.
- Both are repo-local examples only — no writes to `~/.hermes`.

### Plugin registration update (`agent_sensorium/plugin.py`)

- Registers all five tools: `sensorium_status`, `sensorium_ingest_signal`, `sensorium_dispatch_once`, `sensorium_candidate_update`, `sensorium_compact`.
- Registers `/sensorium` command via `ctx.register_command`.
- Registers bundled skill if SKILL.md exists.

## Files changed

| File | Action | Purpose |
|------|--------|---------|
| `agent_sensorium/tools.py` | Modified | Added `handle_sensorium_compact`, cleaned up `_rewrite_jsonl` |
| `agent_sensorium/commands.py` | Created | `/sensorium` command handler |
| `agent_sensorium/plugin.py` | Modified | Register all tools, command, and skill |
| `scripts/sensorium_tick.py` | Created | Deterministic tick script |
| `skills/agent-sensorium/SKILL.md` | Created | Bundled plugin skill documentation |
| `examples/sera-config.json` | Created | Example Sera instance config |
| `examples/seed-signal.jsonl` | Created | Seed signal for smoke testing |
| `tests/test_compact.py` | Created | Compaction tests (13 tests) |
| `tests/test_commands.py` | Created | Command handler tests (9 tests) |
| `tests/test_tick.py` | Created | Tick script tests (4 tests) |

## Tests run and results

```
87 passed
```

All modules compile cleanly via `python -m py_compile`.

Test breakdown:
- `test_schemas.py`: 12 tests (unchanged)
- `test_store.py`: 9 tests (unchanged)
- `test_gate.py`: 12 tests (unchanged)
- `test_dispatcher.py`: 10 tests (unchanged)
- `test_tools.py`: 18 tests (unchanged)
- `test_compact.py`: 13 tests (new) — expired/active/suppressed/cancelled/pinned candidates and threads, receipts, end-to-end flow
- `test_commands.py`: 9 tests (new) — status/threads/dispatch/compact/help subcommands, default, unknown
- `test_tick.py`: 4 tests (new) — dry-run empty/with-signal, real tick dispatch, compact inclusion

Smoke verification:
- Ingest strong signal → promoted ✓
- Dispatch real → one dormant thread ✓
- Status command → shows counts and candidates ✓
- Compact with expired fixture → thread archived with receipt ✓
- Tick --dry-run → JSON output, no side effects ✓

## Deviations from implementation plan

1. **Repo-local layout**: Consistent with Phase A/B/C, all code is in `agent_sensorium/` package under the worktree, not `~/.hermes/plugins/`. The `plugin.py` registration surface documents the Hermes contract.

2. **Compact archives terminal-status candidates**: In addition to expired items, compact also archives `suppressed` and `cancelled` candidates. This makes compact useful for MVP where candidates may not have `expires_at` set. Documented in receipts with `terminal_status:` reason prefix.

3. **Command dispatch is always dry-run**: The `/sensorium dispatch` command surface always runs dry-run for safety. Real dispatch with state mutation goes through the `sensorium_dispatch_once` tool. This matches the pull-based MVP stance.

4. **Compact added as command subcommand**: Beyond the spec's `status|threads|dispatch dry-run|help`, added `compact` as a subcommand since operators should be able to run maintenance through the command surface.

5. **Tick dry-run skips compact**: In `--dry-run` mode, the tick script skips compaction (which mutates state) and only runs dispatch preview and status read.

## Remaining install/manual-smoke work

- **Live plugin install**: Sync tested code to `~/.hermes/plugins/agent-sensorium/` when ready.
- **Sera runtime config**: Copy `examples/sera-config.json` to `~/.hermes/agent-sensorium/sera/config.json`.
- **Seed signal ingest**: Run `sensorium_ingest_signal` with seed signal from `examples/seed-signal.jsonl`.
- **Hermes-side smoke**: Enable plugin, run `/sensorium status` in a live session.
- **Cron/systemd tick**: Not in MVP scope — tick script runs manually.

## Final status

AGENT_SENSORIUM_PHASE_D_DONE
