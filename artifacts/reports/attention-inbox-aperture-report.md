# Attention Inbox / Conscious Aperture — Implementation Report

## Summary

Built the Attention Inbox as a read-only conscious aperture surface for Agent Sensorium. The inbox collects active candidates and visible dormant/held conscious threads, filters by surface and sensitivity policy (fail-closed), computes allowed decisions per item, and returns compact diagnostic data. It never mutates state — no decisions, outbox records, threads, signals, or receipts are written by inbox reads.

The implementation adds:
- A core inbox builder module (`agent_sensorium/attention.py`)
- A tool handler (`sensorium_attention_inbox`) registered in the plugin schema
- A dashboard API endpoint (`GET /attention`) for read-only inbox access
- 38 focused tests covering all required scenarios

## Files Changed

| File | Change |
|------|--------|
| `agent_sensorium/attention.py` | **New** — core inbox builder with surface/sensitivity filtering |
| `agent_sensorium/tools.py` | Added `handle_sensorium_attention_inbox` handler; import for builder; kept seeded WIP fixes (state_dir passing, threshold extraction) |
| `agent_sensorium/plugin.py` | Registered `sensorium_attention_inbox` tool; kept seeded WIP (centralized default_instance_name import) |
| `agent_sensorium/config.py` | Kept seeded WIP: `default_instance_name()`, operational_pointer config, thread_ttl_hours, pointer/outbox defaults |
| `agent_sensorium/dispatcher.py` | Kept seeded WIP: operational_pointer in DEFAULT_DISPATCH_CONFIG, `_apply_operational_pointer_policy()` |
| `agent_sensorium/gate.py` | Kept seeded WIP: fixed threshold merging in `should_promote_signal` |
| `dashboard/plugin_api.py` | Added `GET /attention` endpoint; kept seeded WIP (default instance resolution, multi-instance support) |
| `tests/test_attention.py` | **New** — 38 tests for inbox builder, tool handler, surface/sensitivity filtering, read-only guarantees, and config |
| `tests/test_plugin_registration.py` | Added `sensorium_attention_inbox` to expected tool set |
| `tests/test_dispatcher.py` | Kept seeded WIP: operational pointer policy tests |
| `docs/extending-sensors-and-subconscious-jobs.md` | Kept seeded WIP: extension contract documentation |
| `docs/README.md` | Kept seeded WIP: docs index update |
| `docs/agent-sensorium-buildout-plan-2026-05-25.md` | Kept seeded WIP: buildout plan refinements |
| `scripts/sensorium_tick.py` | Kept seeded WIP: body-pressure flag wiring |
| `scripts/sensorium_probe_audit.py` | Kept seeded WIP: minor adjustments |
| `scripts/sensorium_subconscious_tick.py` | Kept seeded WIP: minor adjustments |
| `README.md` | Kept seeded WIP: minor doc update |

## What Happened to the Seeded WIP Patch

**All seeded WIP changes were preserved.** They are foundational to this slice:

- `default_instance_name()` centralizes instance resolution used by dashboard, plugin, and scripts — directly needed by the attention inbox dashboard endpoint.
- `operational_pointer` config and dispatcher policy controls which thread kinds appear on which surfaces — the inbox respects these surfaces.
- Threshold merging fix in `gate.py` prevents silent config override bugs.
- Dashboard multi-instance resolution is required for the new `/attention` endpoint.
- The dispatcher operational pointer tests in `test_dispatcher.py` cover the pointer policy that the inbox filters against.

## Tests / Verification Run

### Test Suite
- **412 tests passed** (full suite including 38 new attention tests)
- `python -m py_compile` passed for all `.py` files under `agent_sensorium/` and `scripts/`

### Smoke Test (temp state)
```
Ingested signal -> promoted=True, candidate_id=cand_2e37461c29ea
Dispatched -> action=promoted, thread_id=sth_d277a0f3d343
Inbox counts: {'total': 2, 'candidates': 1, 'threads': 1, 'filtered_out': 0}
  [thread] sth_d277a0f3d343 status=dormant decisions=['open', 'hold', 'close', 'archive', 'mark_reviewed']
  [candidate] cand_2e37461c29ea status=candidate decisions=['open', 'suppress', 'hold', 'mark_reviewed']
Dashboard inbox (should be empty): {'total': 0, 'candidates': 0, 'threads': 0, 'filtered_out': 2}
Read-only guarantee verified: no mutations from inbox reads
SMOKE TEST PASSED
```

### Test Coverage Summary
- Inbox includes active candidates and dormant/held threads with compact fields
- Surface filtering hides items not allowed on requested surface
- Sensitivity/max-sensitivity gates fail closed (public_safe hidden when max=private)
- local_only sensitivity passes on local surface
- Missing surfaces or empty surface fails closed
- Allowed decision list correct for candidate / dormant thread / held thread
- Read-only guarantee: 5 tests verify no writes to decisions, outbox, threads, signals, or candidates
- Plugin registration includes `sensorium_attention_inbox`
- Dashboard handles default instance and missing state safely
- Seeded operational-pointer/default-instance WIP covered by existing tests

## Privacy / Safety Boundary Review

- **No raw content**: Inbox items contain only truncated summaries (100/200 chars), IDs, status, and metadata. No raw signals, transcripts, file contents, secrets, or full tool output.
- **No mutations**: The inbox builder and tool handler never call `append_jsonl`, `write_state`, or any store write method. Verified by 5 explicit read-only tests and smoke test.
- **Fail-closed filtering**: Items missing from `allowed_surfaces` or exceeding `max_sensitivity` are hidden, not defaulted to visible. Empty/missing surface strings return nothing.
- **No outbound effects**: No messages, Discord API calls, platform thread creation, media generation, model calls, or outbox record creation.
- **Surface scope cannot broaden**: `visible_on_surface()` requires the surface to be in BOTH item AND config allowed surfaces. Config narrows, never broadens.
- **Existing mutation tools unchanged**: `sensorium_candidate_update`, `sensorium_thread_open/update`, and `sensorium_outbox_prepare` remain the only mutation paths.

## Remaining Follow-ups

1. **Dashboard UI rendering** — The `/attention` endpoint returns JSON; a frontend component to render the inbox is not yet built.
2. **Inbox in `/sensorium` command** — The CLI command (`commands.py`) could add an `inbox` subcommand for terminal review.
3. **Attention pointer integration** — The existing `sensorium_attention_pointer` could reference inbox counts for richer pointer text.
4. **Configurable freshness thresholds** — Currently hardcoded (1h fresh, 24h recent); could be moved to instance config.
5. **Inbox in tick script** — `sensorium_tick.py` could optionally include inbox summary in `--json` output.

## Final Commit SHA

`bd636a0` on branch `feat/sensorium-attention-inbox-aperture`

SENSORIUM_ATTENTION_INBOX_DONE
