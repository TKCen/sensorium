# Sensorium Salience Review Loop — Implementation Report

**Date:** 2026-05-29  
**Branch:** work/salience-review-loop  
**Base commit:** bcae30c (declarative attention_policy config surface)  
**Task:** t_2e85563b — Port Hermes background skill-review pattern to Sensorium salience review

---

## Summary

This lane implements the v1 salience-review loop spine for Agent Sensorium: the smallest safe callable
implementation that follows the Hermes background skill-review pattern without giving any background
reviewer unsafe authority. All five required deliverables (prompt, context builder, decision parser,
apply seam, manual harness) are implemented and tested. The bounded reviewer runner is present as an
explicitly disabled stub with documented missing safety seams.

The implementation is deliberately conservative: the manual harness provides the full round-trip
(context JSON → human/conscious decision → apply → receipt) without requiring a model call. Automation
is gated behind `SALIENCE_REVIEW_ENABLED = False` and five documented missing seams.

---

## Files Changed

| File | Action | Description |
|------|--------|-------------|
| `agent_sensorium/improvement.py` | Modified | Added `_SALIENCE_REVIEW_PROMPT`, `build_salience_review_context`, `parse_salience_review_decision`, `apply_salience_review_decision` (122 lines) |
| `agent_sensorium/salience_review.py` | Created | Disabled-by-default bounded reviewer runner stub with safety documentation |
| `scripts/sensorium_salience_review.py` | Created | Manual JSON harness (no model calls) |
| `tests/test_salience_review.py` | Created | 28 focused tests |
| `artifacts/reports/salience-review-loop-report.md` | Created | This report |

---

## New Symbols in improvement.py

```python
_SALIENCE_REVIEW_PROMPT: str
    # Module-level prompt constant listing all 6 decisions, required fields,
    # signal catalog, and explicit forbidden actions (code edit, SOUL patch,
    # send_message, recursive review, privacy broadening).

build_salience_review_context(
    store: SensoriumStore, evidence: dict, *, max_decisions: int = 20
) -> dict
    # Returns bounded JSON-serializable dict:
    #   evidence, recent_attention_decisions (pruned, last N),
    #   candidate_counts, open_candidates (≤5),
    #   policy_config_snapshot, constraints

parse_salience_review_decision(payload: dict) -> dict
    # Strict validator: normalizes decision to upper, rejects invalid enum,
    # rejects missing/empty required fields (all at once), drops extra fields.
    # Returns 8-key normalized dict.

apply_salience_review_decision(
    store: SensoriumStore,
    candidate_id: str,
    decision_payload: dict,
    *,
    decided_by: str = "salience-review",
) -> dict
    # Parses payload, overrides decided_by with parameter, delegates to
    # record_attention_policy_decision. Writes exactly one
    # attention_policy_review.decision receipt.
```

---

## Tests Run and Results

```
# Targeted suite
python -m pytest tests/test_improvement.py tests/test_salience_review.py -q -o 'addopts='
  → 39 passed  (11 pre-existing + 28 new)

# Full suite
python -m pytest -q -o 'addopts='
  → 653 passed  (baseline was 625 before this lane)
  → 0 failed, 0 errors, 0 warnings
```

**New test coverage (tests/test_salience_review.py):**
- Prompt string: contains all 6 decisions; contains all 4 required field names; references SOUL and code guardrails
- Context builder: JSON-serializable; correct keys; constraints.valid_decisions matches VALID_ATTENTION_DECISIONS; forbidden list present; max_decisions respected; pruned fields only (no transcript/raw_log bleed)
- Decision parser: accepts valid payload; normalizes case; rejects invalid decision; rejects empty required fields; reports all missing fields at once; ignores extra fields; defaults decided_by
- Apply seam: writes exactly one receipt; transitions candidate to reviewed/held; receipt has future_tendency_delta and rollback_condition; respects decided_by parameter; rejects invalid payload before any write
- Runner stub: SALIENCE_REVIEW_ENABLED is False; returns disabled-status dict; _MISSING_SEAMS is non-empty; no dangerous imports in salience_review.py

---

## Manual Smoke Details

The manual harness `scripts/sensorium_salience_review.py` was validated by the implementation agent
using an in-process equivalent of:

```
# Build context for top open candidate
python scripts/sensorium_salience_review.py --instance test --json-only-context
# → prints JSON with evidence, recent_attention_decisions, constraints, etc.
# → {"status": "no_candidate", ...} when no open candidate exists

# Apply a decision file
python scripts/sensorium_salience_review.py --instance test --decision-file /tmp/decision.json
# → writes attention_policy_review.decision receipt
# → candidate transitions to "reviewed" (NO_CHANGE) or "held" (HOLD)
# → result printed as {"success": true, "data": {...}}
```

The context-apply round trip was verified via direct function calls in the test suite
(`test_apply_salience_review_decision_writes_exactly_one_receipt`, etc.).

---

## What Remains for Full Automatic Hermes-Style Background Runner

The following 5 seams are documented in `_MISSING_SEAMS` (agent_sensorium/salience_review.py) and
must all be completed before `SALIENCE_REVIEW_ENABLED` can be set to True:

1. **Hermes AIAgent fork or subprocess integration** — A bounded Hermes session (fork of AIAgent or
   hermes-cli subprocess) must expose only `record_attention_policy_decision` and
   `manage_attention_policy_config` in its toolset. No terminal, no browser, no outbox.

2. **Thread-local or subprocess-level tool whitelist enforcement** — Prompt-only restriction is
   insufficient. The whitelist must be enforced at the Hermes tool-dispatch layer (analogous to
   Hermes's `set_thread_tool_whitelist` ContextVar), not only in the system prompt.

3. **Daemon-exit flush safety** — Hermes uses `daemon=True` threads; policy writes should use
   `daemon=False` or an explicit flush/confirm step so writes are not silently lost on rapid exit.

4. **Counter serialization for stateless gateway deployments** — If Sensorium runs as a stateless
   gateway agent (fresh instance per message), salience review counters must be serialized to the
   store and hydrated on cold start, or reviews will never fire.

5. **Safety tests** — Tests proving: (a) tool whitelist denies terminal/browser/send_message;
   (b) no recursive review is spawned; (c) foreground/user conversation state is not mutated;
   (d) daemon-exit leaves no partial write on disk.

---

## Safety / Authority Review

The implementation satisfies all authority guardrails from the task spec:

| Guardrail | Status |
|-----------|--------|
| Background reviewer cannot edit Python source | ✅ Enforced — no file-write tool; all writes go through typed record/manage functions |
| Background reviewer cannot patch SOUL | ✅ Enforced — explicitly forbidden in prompt and constraints dict; no SOUL write path exists |
| Background reviewer cannot send outbound messages | ✅ Enforced — no outbox/send tool; constraints["forbidden"] includes send_message |
| Background reviewer cannot restart services | ✅ Not applicable in v1; runner stub is disabled |
| No automatic privacy/surface broadening | ✅ manage_attention_policy_config does not touch allowed_surfaces; parse_salience_review_decision drops extra fields |
| Declarative mutations only | ✅ Only `record_attention_policy_decision` and `manage_attention_policy_config` are named as allowed_mutations |
| Review automation disabled by default | ✅ `SALIENCE_REVIEW_ENABLED = False`; runner stub raises NotImplementedError if flag is bypassed |
| No modification of primary checkout | ✅ All work in /home/entity/projects/agent-sensorium-omc/salience-review-loop worktree |

---

## Commit SHA

`ae8e137`

---

SENSORIUM_SALIENCE_REVIEW_LOOP_DONE
