# Sensorium Memory Reflection Layer — OMC Implementation Report

**Worktree:** `/home/entity/projects/agent-sensorium-omc/memory-reflection-probes`
**Branch:** `feat/memory-reflection-probes`
**Date:** 2026-06-03
**Status:** Implemented (v0), tested, independently reviewed. Not installed/pushed.

---

## 1. What was built

A configurable, hot-loaded **Sensorium Memory Reflection Layer**. Small
Subconscious-style sessions call Hindsight `reflect`/`recall` over bounded
historical context, reduce the raw output into **compact Sensorium signals**, and
route them through the ordinary ingest path. Raw output is stored locally by ref
only; the model-visible live `sensorium` tool footprint is unchanged.

The layer is **out-of-band only**: a Python module, an out-of-band admin/dry-run
CLI, and an optional tick flag. Nothing is registered as a model-visible tool.

### Design highlights
- **Subconscious-owned, not a deterministic sensor.** Distinct from
  `hindsight_pressure_sample` (quantitative queue pressure, no reflect/recall).
- **Emitted kind `memory_reflection` is deliberately NOT in
  `DIRECT_CONSCIOUS_KINDS`** — semantic reflection enters the candidate queue for
  Subconscious review and can never bypass it to promote blind.
- **Reflection notices; it does not act.** Emits compact signals + local raw refs
  + history only. No outbound delivery, media, worker dispatch, or messages.
- **Operational claims stay `unverified: true`** on every emitted signal.

---

## 2. Changed files

### New (authored in this lane)
| File | Purpose |
|---|---|
| `agent_sensorium/memory_reflection.py` | Core: hot-load config loader/validator, injectable `HindsightMemoryClient` seam (`Http`/`Fake`), reducer, raw local-ref storage, run history + fingerprint/delta + cadence/cooldown due-detection, orchestration. |
| `scripts/sensorium_memory_reflection.py` | Out-of-band CLI: `list`, `validate`, `dry-run`, `run`. Compact JSON only; never prints raw. NOT a model-visible tool. |
| `tests/test_memory_reflection.py` | 47 tests across config hot-load, validation/clamping, reducer raw-stripping, fake-client runs, require_delta/liveness, due-detection, ingest path, dry-run, raw storage. |

### Modified (authored in this lane)
| File | My change |
|---|---|
| `agent_sensorium/probe_audit.py` | +5 lines: import + new `internal_probe_families` inventory entry marking memory reflection as Subconscious-owned, hot-loadable, `wired_live=False`, `model_visible_tool=False`. (File was clean in the seed — change is wholly mine.) |
| `scripts/sensorium_tick.py` | +~52 lines: `--memory-reflection` / `--memory-reflection-config` flags + a quiet execution step running due probes and ingesting compact signals via the existing handler. |
| `tests/test_plugin_registration.py` | +35 lines: appended `test_memory_reflection_not_in_live_tool_schema` (name-fragment + schema-substring + tool-count snapshot guard). |

### Pre-seeded WIP unavoidably included in the commit
The seed had these two files already staged with unrelated WIP that is entangled
with my additions in the same file (cannot be split without rewriting history):
- `scripts/sensorium_tick.py` — **18 pre-seeded lines** add a `--codex-usage`
  tick step (codex usage sensor). Not my work; included because my flag sits in
  the same file.
- `tests/test_plugin_registration.py` — **1 pre-seeded line** adds
  `"sensorium_sensor_config"` to the expected tool set. Not my work.

All other pre-seeded WIP (dashboard, dispatcher, gate, plugin, sensors, store,
tools, live-scripts, and unrelated test files) was **left uncommitted** and
preserved in the working tree for the lead session.

---

## 3. Tests run and results

All run with `-o 'addopts='` from the worktree root.

| Command | Result |
|---|---|
| `pytest tests/test_memory_reflection.py tests/test_plugin_registration.py` | **52 passed** |
| `pytest tests/test_probe_audit.py tests/test_config.py tests/test_tick.py` | **116 passed** |
| `pytest` (full suite) | **891 passed** |
| `ruff check` (all new/changed files) | **All checks passed** |

Key invariants under test:
- **Tool schema does not grow**: `test_memory_reflection_not_in_live_tool_schema`
  forbids tool-name fragments (`memory_reflection`/`reflect`/`recall`/`probe`),
  forbids `memory_reflection` substrings in any tool schema JSON, and pins the
  exact tool count (32). Adding any probe admin tool fails this test.
- **Reducer strips raw**: emitted signals never contain any
  `RAW_FORBIDDEN_SIGNAL_FIELDS` (transcript/memories/full_text/etc.); summaries
  are truncated; signals capped to `max_signals`.
- **Hot-load without restart**: config is re-read from disk every call; a
  rewritten config file is picked up by the next `load_config` with no caching.
- **Compact-only emission**: fake-client run writes raw to disk under
  `<state_dir>/memory_reflection/raw/` but the run record / emitted signals carry
  only summary + `raw_ref` + `raw_sha256`.
- **Does not bypass Subconscious**: ingested signal kind is asserted absent from
  `DIRECT_CONSCIOUS_KINDS`.
- **Quiet tick**: default tick stdout stays empty (cron-safe); JSON only on `--json`.

---

## 4. Confirmation: compact live tool schema was NOT expanded

- `plugin.py` `register()` was **not modified**; no new tool/command/hook added.
- No reference to memory reflection exists in `plugin.py`, `tools.py`, or
  `commands.py` (verified by grep + independent reviewer).
- The existing exact-tool-set test still passes (32 tools), and the new invariant
  test would fail if a memory-probe tool were ever added.
- Admin/config is **out-of-band only**: local `memory_reflection.json`, the
  `sensorium_memory_reflection.py` CLI, and the `--memory-reflection` tick flag.

---

## 5. How config hot-load works

- Config lives at `<state_dir>/memory_reflection.json` (or an explicit
  `--memory-reflection-config` / `config_path`).
- `load_config(...)` **re-reads and re-validates the file on every call** — there
  is no module-level cache. Each tick / CLI invocation therefore sees the current
  file. Add/remove/update a probe by editing the JSON; the next run uses it with
  **no gateway or plugin restart**.
- **Fail-closed validation**: a single invalid probe (bad mode, missing
  id/query, bad cadence, duplicate id) is disabled and recorded in
  `config.errors`; valid probes still load. Numeric fields are clamped into safe
  bounds; sensitivity/surfaces default to `private`/`["local"]`.
- **Hot-reload does not reset clocks**: cooldown/cadence are computed from the
  run-history `completed_at`, so changing config does not retroactively unlock or
  re-fire probes — new windows apply going forward (matches the contract).

Config shape (minimal):
```json
{
  "enabled": true,
  "defaults": { "timeout_s": 90, "max_summary_chars": 400, "require_delta": true },
  "probes": [
    {
      "id": "daily-continuity",
      "mode": "reflect",
      "query": "What still matters from yesterday around SERA/Sensorium?",
      "cadence": {"type": "interval", "hours": 24},
      "cooldown_hours": 20,
      "sensitivity": "private",
      "allowed_surfaces": ["local"]
    }
  ]
}
```

---

## 6. Independent review

A separate read-only `code-reviewer` (Opus) pass verified all 8 non-negotiables
as **PASS** and returned **APPROVE — no Critical/High issues**. The one Medium
finding (a single probe's store/ingest/append failure could escape `run_probe`
and abort the whole tick step) was **fixed**: the post-call body is now wrapped so
any failure is isolated into a per-probe `status:"error"` history record; sibling
probes and later tick steps proceed. Hindsight-unreachable is already caught and
recorded, never crashing the tick.

---

## 7. Manual smoke (no live writes)

- `list` / `validate` CLI: correct compact JSON; `validate` exits non-zero on
  config error.
- `tick --memory-reflection --dry-run --json`: ran due probe against the local
  Hindsight (read-only `reflect`), emitted 1 compact signal, wrote nothing
  (dry-run). The HTTP endpoint guess was confirmed reachable; no writes occurred.
- Default `tick` (no flag): stdout empty.

---

## 8. Deferred / blockers

Deferred deliberately (per plan):
- Dashboard probe-editing UI.
- Automatic live cron enablement / live install / gateway restart (lead-session
  decision).
- Relational proactive delivery / outbox action.
- Full semantic quality evaluation of reflect outputs.
- `recall_then_reflect` is implemented behind the client seam but lightly
  exercised; primary v0 path is `reflect`.

No blockers. The layer is dormant until a `memory_reflection.json` is created with
`enabled: true` and the `--memory-reflection` tick step (or CLI `run`) is invoked.

### Recommended next action for the lead session
1. Review this report and the diff.
2. Author a real `memory_reflection.json` in the live instance state dir.
3. Decide whether to wire `--memory-reflection` into the existing tick cron
   cadence (out-of-band; no plugin/tool change required).

---

MEMORY_REFLECTION_OMC_DONE
