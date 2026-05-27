# Phase 9A.2a — Attention Aperture Policy Unification

## Summary

Unified the visibility/policy gate across all attention surfaces. Before this change, the attention inbox correctly checked both item and instance-config `allowed_surfaces` plus sensitivity, but pointer selection (`select_attention_pointer`, `handle_pointer_pre_llm`) and thread open (`sensorium_thread_open`) only checked the item's own `allowed_surfaces`, ignoring instance config entirely. This meant a thread with `allowed_surfaces=["discord","local"]` could be opened on discord even when instance config restricted to `allowed_surfaces=["local"]`.

## Changed files

| File | Change |
|---|---|
| `agent_sensorium/config.py` | Added `visible_on_surface()` as the canonical unified visibility gate |
| `agent_sensorium/attention.py` | Removed local `visible_on_surface()` definition; imports from `config` |
| `agent_sensorium/pointers.py` | Removed `_surface_allowed()`. `select_attention_pointer` and `handle_pointer_pre_llm` now load instance config and use `visible_on_surface()` |
| `agent_sensorium/tools.py` | Removed `_thread_allowed_on_surface()`. `handle_sensorium_thread_open` and `handle_sensorium_attention_pointer` now load instance config and use `visible_on_surface()` |
| `tests/test_attention.py` | Updated import path for `visible_on_surface` |
| `tests/test_pointers.py` | Added `_write_config` helper; fixed discord tests to write instance config; added `TestPointerPolicyUnification` class (6 tests) |
| `tests/test_thread_lifecycle.py` | Added `_write_config` helper; fixed discord tests to write instance config; added `TestThreadOpenPolicyUnification` class (5 tests) |
| `skills/agent-sensorium/SKILL.md` | Updated pointer/capsule boundary docs to describe unified gate |
| `docs/agent-sensorium-buildout-plan-2026-05-25.md` | Added Phase 9A.2a status note |

## Exact invariant now enforced

`config.visible_on_surface(item, surface, instance_config)` is the single gate used by:

1. **Attention inbox** item filtering (`build_attention_inbox`)
2. **Pointer selection** (`select_attention_pointer`)
3. **pre_llm_call pointer injection** (`handle_pointer_pre_llm`)
4. **Thread open** (`handle_sensorium_thread_open`)
5. **Dashboard attention endpoint** (via `build_attention_inbox`)

The gate enforces:
- Requested surface must be in BOTH `item.allowed_surfaces` AND `instance_config.allowed_surfaces`
- Item sensitivity rank must be <= config `max_sensitivity` rank
- Missing surfaces or sensitivity data → item hidden (fail closed)
- Missing instance config → SAFE_DEFAULTS: `allowed_surfaces=["local"]`, `max_sensitivity="private"`

## Tests added/updated

**New tests (11):**

`TestPointerPolicyUnification` (6 tests):
- `test_config_excludes_surface_returns_no_pointer`
- `test_config_allows_surface_returns_pointer`
- `test_pre_llm_no_receipt_when_config_excludes_surface`
- `test_sensitivity_gate_blocks_pointer`
- `test_missing_config_defaults_to_local_only`
- `test_local_still_works_with_default_config`

`TestThreadOpenPolicyUnification` (5 tests):
- `test_config_excludes_surface_blocks_open`
- `test_config_allows_surface_permits_open`
- `test_sensitivity_blocks_open`
- `test_missing_config_defaults_local_only`
- `test_local_works_with_default_config`

**Updated tests (3):**
- `test_pre_llm_pointer_records_cooldown_receipt` — writes instance config allowing discord
- `test_pointer_preview_reports_cooldown_reason` — writes instance config allowing discord
- `test_thread_open_returns_compact_capsule_when_surface_allowed` — writes instance config allowing discord
- `test_thread_update_closes_thread_and_removes_pointer_eligibility` — writes instance config allowing discord

## Gate output summary

```
425 passed (all tests)
All py_compile checks pass
git diff --check clean
```

## Risks / deferred work

- Dashboard `/snapshot` endpoint returns raw threads/candidates without surface filtering (intentional: admin overview, not user-facing aperture). If snapshot gains per-surface filtering, it should use `visible_on_surface`.
- Outbox prepare/dispatch have their own policy gates in `outbox.py`; those were not changed. They should be audited for consistency with `visible_on_surface` when outbox surfaces expand beyond local.

## Final git commit hash

See commit on branch `omc/phase9a2-policy-unification` after this report.
