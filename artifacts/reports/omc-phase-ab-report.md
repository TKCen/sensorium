# Phase A/B Implementation Report

## What was implemented

### Phase A — Plugin skeleton / status

- **Plugin manifest** (`plugin.yaml`): standalone plugin metadata (name, version, platforms).
- **Package skeleton** (`agent_sensorium/__init__.py`): version constant; package root.
- **Plugin registration surface** (`agent_sensorium/plugin.py`): `register(ctx)` function that registers tools and optional skill via Hermes plugin API contract. Callable documentation — no live Hermes runtime required.
- **Schema helpers** (`agent_sensorium/schemas.py`):
  - `utc_now_iso()` — ISO 8601 UTC timestamp.
  - `new_id(prefix)` — unique prefixed IDs (sig_, evt_, cand_).
  - `normalize_signal(raw)` — fills defaults (id, ts, sensitivity, strength_hint, ttl, etc.).
  - `validate_signal(signal)` — raises `ValueError` on missing required fields or invalid sensitivity.
  - `validate_event(event)` — raises `ValueError` on missing required event fields.
  - `merge_sensitivity(values)` — picks most restrictive sensitivity.
  - `intersect_allowed_surfaces(items)` — intersection of surface lists.
- **JSONL store** (`agent_sensorium/store.py`):
  - `SensoriumStore(instance, state_dir)` with configurable or default state directory.
  - `ensure_dirs()` — creates state directory tree (signals/, archive/).
  - `append_jsonl(name, obj)` / `read_jsonl(name, limit)` — append-only JSONL with corrupted-line skip.
  - `write_state(obj)` / `read_state()` — JSON state snapshot.
  - `paths` property for diagnostics.
  - State name mapping: signals → signals/inbox.jsonl, events → events.jsonl, candidates → candidates.jsonl, threads → threads.jsonl, decisions → decisions.jsonl.
- **`sensorium_status` tool handler** (`agent_sensorium/tools.py`):
  - Returns initialized empty state with counts and empty top_candidates.
  - After ingestion, returns accurate signal/event/candidate/thread counts and top-5 active candidates sorted by pressure.

### Phase B — Signal/event/candidate spine

- **Deterministic gate** (`agent_sensorium/gate.py`):
  - `signal_fingerprint(signal)` — stable SHA-256 fingerprint for dedup.
  - `should_promote_signal(signal, config)` — deterministic promotion rules:
    - Promote if `strength_hint >= single_signal_strength` threshold (default 0.75).
    - Promote if kind is in `promote_kinds` list and strength >= `important_kind_strength` (default 0.6).
    - Otherwise suppress with reason.
  - `promote_signal_to_event(signal, config)` — creates Event dict inheriting correlation keys, sensitivity, allowed surfaces.
  - `event_to_candidate(event, config)` — creates Candidate with weighted pressure score.
  - `candidate_fingerprint(candidate)` — stable fingerprint for candidate dedup.
  - `DEFAULT_CONFIG` with thresholds and promote_kinds.
- **`sensorium_ingest_signal` tool handler** (`agent_sensorium/tools.py`):
  - Validates, normalizes, persists signal.
  - Runs deterministic gate; if promoted, creates Event and Candidate.
  - Returns structured JSON with signal_id, promoted flag, reason, and optional event_id/candidate_id.
- **Status shows top candidates**: after ingestion, `sensorium_status` returns accurate counts and top candidates.

## Files changed

| File | Action | Purpose |
|------|--------|---------|
| `plugin.yaml` | Created | Plugin manifest |
| `agent_sensorium/__init__.py` | Created | Package root |
| `agent_sensorium/schemas.py` | Created | ID/timestamp/validation helpers |
| `agent_sensorium/store.py` | Created | JSONL-backed state store |
| `agent_sensorium/gate.py` | Created | Deterministic promotion gate |
| `agent_sensorium/tools.py` | Created | Tool handlers (status, ingest) |
| `agent_sensorium/plugin.py` | Created | Hermes plugin registration surface |
| `tests/__init__.py` | Created | Test package |
| `tests/test_schemas.py` | Created | Schema helper tests |
| `tests/test_store.py` | Created | Store tests |
| `tests/test_gate.py` | Created | Gate tests |
| `tests/test_tools.py` | Created | Tool handler tests |

## Tests run and results

```
43 passed
```

All modules compile cleanly via `python -m py_compile`.

Test breakdown:
- `test_schemas.py`: 12 tests — IDs, timestamps, normalization, validation, sensitivity merge, surface intersection.
- `test_store.py`: 9 tests — directory creation, JSONL round-trip, limit, corrupted line handling, state read/write, paths property.
- `test_gate.py`: 12 tests — promotion thresholds, kind-based promotion, custom config, event/candidate shape, fingerprint stability.
- `test_tools.py`: 10 tests — empty status, status after ingest, valid/weak/invalid signal ingestion, multi-signal accumulation, tool return shape.

## Deviations from implementation plan

1. **Repo-local layout instead of ~/.hermes path**: The implementation plan targets `~/.hermes/plugins/agent-sensorium/`. Per task instructions, all code lives in the worktree as `agent_sensorium/` Python package + `tests/`. The `plugin.py` registration surface documents the Hermes contract without requiring live installation.

2. **No SKILL.md bundled**: The bundled skill file (Task 2 in the plan) is primarily useful for live Hermes runtime and Conscious review — it can be added when installing the plugin to the Hermes path. Not required for Phase A/B gate criteria.

3. **Phase B scope subset**: The plan's Tasks 6-11 (dispatcher, commands, compaction, tick script, seed config) are Phase C+ work. Phase B gate requires only: signal ingest, deterministic promotion, candidate creation, duplicate handling foundations (fingerprints), and status showing candidates. All met.

4. **Tool return format**: Handlers return JSON strings matching the documented `{success, instance, data, error}` shape. They accept keyword arguments directly rather than going through a Hermes context wrapper, making them testable without runtime.

## Remaining Phase C+ work

- **Task 6 — Dispatcher**: `select_candidate`, `candidate_to_thread`, `dispatch_once` with dry-run mode.
- **Task 7 — Additional tools**: `sensorium_dispatch_once`, `sensorium_candidate_update`, `sensorium_compact`.
- **Task 8 — Pull command**: `/sensorium` command with status/threads/dispatch/help subcommands.
- **Task 9 — Compaction/TTL**: Archive stale candidates/threads, cap visible state.
- **Task 10 — Shadow tick script**: `scripts/sensorium_tick.py` for manual smoke runs.
- **Task 11 — Sera seed config**: Instance config + seed signal for end-to-end smoke.
- **Bundled SKILL.md**: Conscious review protocol documentation.
- **Dormant thread lifecycle**: Thread creation, status transitions, continuity capsules.
- **Duplicate signal coalescing**: Beyond fingerprint foundation — active dedup in ingest path.
- **Subconscious advisory pass**: Model-backed correlation (explicitly post-MVP).
- **Active-session pointer injection**: Post-MVP.
- **Relational autonomy / REACH_OUT**: Post-MVP.
- **Platform thread creation**: Post-MVP.
- **Proactive delivery**: Post-MVP.

## Final status

AGENT_SENSORIUM_PHASE_AB_DONE
