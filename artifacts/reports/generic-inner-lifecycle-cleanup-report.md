# Generic Inner-Lifecycle Cleanup Report

**Branch**: cleanup/generic-inner-lifecycle  
**Date**: 2026-05-31  
**Task**: Sensorium Kanban t_69a1adfd — prepare Sensorium generic inner-lifecycle module

---

## Changed Files

| File | Change |
|---|---|
| `docs/sensorium-release-readiness.md` | New — release-readiness inventory doc |
| `scripts/sensorium_plugin_rollout.py` | New — repo-to-live plugin rollout/sync tool |
| `tests/test_rollout.py` | New — 11 tests for rollout script |
| `tests/test_subconscious.py` | Modified — 1 invariant test added (model generator not called when disabled) |
| `artifacts/reports/invariant-test-coverage-notes.md` | New — test coverage notes for release-critical invariants |

---

## Verification Commands and Outputs

```
python -m pytest tests/test_rollout.py tests/test_subconscious.py tests/test_config.py \
  tests/test_settlement_propagation.py tests/test_feedback_settlement.py \
  tests/test_kanban_native_invariants.py -q

Result: focused release-critical subset passed (exit code 0)

python -m pytest --disable-warnings --tb=short

Result: 720 passed in 18.01s (exit code 0)
```

---

## Release-Readiness Findings

See `docs/sensorium-release-readiness.md` for full inventory. Summary:

**Generic and shippable:**
- `schemas.py`, `store.py`, `gate.py`, `settlement.py`, `dispatcher.py`, `conscious.py`
- `config.py` (the `hermes_cli` import in `default_instance_name()` is a soft/optional dependency)
- `attention.py`, `actions.py`, `tools.py`, `workers.py`, `artifacts.py`, `pointers.py`, `probe_audit.py`, `salience_review.py`, `commands.py`, `plugin.py`
- Machine/network/process/kanban/hindsight sensors in `sensors.py`
- `plugin.yaml`, `pyproject.toml`, `__init__.py`, `README.md` (minor Sera references to clean)

**Sera-specific / not generic:**
- `talking_head.py` — TTS/voice pipeline, experimental, hardcoded local URLs
- `media_gifts.py` — conscious-choice gift flow, Sera-named, experimental
- `outbox.py` — deprecated outbox compatibility layer; not generic
- `sensors.py`: `tts_sidecar_pressure_sample()`, `media_capacity_sample()`, `wsl_disk_paths()` — local/WSL-specific

**Local operational (not release artifacts):**
- `live-scripts/sensorium_kanban_sensor_tick.py`
- `scripts/*.py` — dev tooling with `--instance sera` defaults
- `artifacts/reports/` — historical work reports
- `examples/seed-signal.jsonl` — Sera-specific signal example

**Cleanup backlog before public/generic release:**
1. Remove or make fully optional/configurable: `DEFAULT_CHATTERBOX_BASE` hardcoded path in `sensors.py`
2. `wsl_disk_paths()` — make conditional or document as WSL-only
3. Separate `talking_head.py`, `media_gifts.py`, `outbox.py` into optional instance extensions
4. README.md smoke-test uses `--instance sera` — update to show generic `--instance default`
5. `skills/agent-sensorium/SKILL.md` has Sera-scoped examples — add generic example
6. `examples/seed-signal.jsonl` should be relabeled as Sera example or replaced with generic seed
7. No pypi build backend in `pyproject.toml` — intentional for now; document before packaging
8. Dashboard `dist/` has no build pipeline docs — document or add `npm run build` instructions
9. `improvement.py` — verify test coverage before claiming stable

---

## Rollout Script Usage Examples

Script: `scripts/sensorium_plugin_rollout.py`

```bash
# Preview what would be synced (safe, no mutation)
python scripts/sensorium_plugin_rollout.py --dry-run --allow-dirty

# Check drift between repo and installed plugin
python scripts/sensorium_plugin_rollout.py --check
# exit 0 = no drift, exit 1 = drift found

# Full sync (requires clean git tree, backs up target first)
python scripts/sensorium_plugin_rollout.py

# Sync from dirty tree (e.g. mid-branch work)
python scripts/sensorium_plugin_rollout.py --allow-dirty

# Sync without backup (dangerous, use only if target is already backed up)
python scripts/sensorium_plugin_rollout.py --no-backup

# Remove stale files from managed target paths after the backup is made
python scripts/sensorium_plugin_rollout.py --prune

# Override HERMES_HOME
HERMES_HOME=/path/to/other/hermes python scripts/sensorium_plugin_rollout.py --dry-run
```

**Managed paths synced:**  
`agent_sensorium/`, `dashboard/`, `scripts/`, `skills/`, `plugin.yaml`, `__init__.py`, `README.md`, `pyproject.toml`  
Plus: `live-scripts/sensorium_kanban_sensor_tick.py` → `$HERMES_HOME/scripts/sensorium_kanban_sensor_tick.py`

**Safety guarantees:**
- `--dry-run` never mutates anything
- Dirty source refused by default (git --porcelain check)
- Timestamped backup created before any mutation
- `--check/--verify` detects missing, drifted, and stale extra files in managed paths
- `--prune` removes stale extra files only after the backup step
- Local cache/build residue such as `__pycache__` and `.pytest_cache` is ignored
- Never touches `~/.hermes/config.yaml`, `.env`, cron, gateway

---

## Blocked / Deferred Items

- **No live install**: The rollout script exists but has not been run against `~/.hermes/plugins`. Intentional per scope.
- **No outbox/talking_head cleanup**: Those modules are Sera-specific but non-trivial to extract; deferred to a separate branch.
- **`improvement.py` test coverage**: Listed in cleanup backlog; not addressed here (not obviously low-risk to add now).
- **WSL disk path portability**: `wsl_disk_paths()` works on WSL/Linux; making it conditional requires sensor config changes out of scope.
- **Dashboard build docs**: No changes to dashboard source; build pipeline documentation deferred.

---

SENSORIUM_GENERIC_CLEANUP_DONE
