# Phase 9D — Feedback Settlement and Self-Loop Suppression

## Summary

Ordinary successful internal Sensorium feedback (action results, thread updates, worker results) is now stored as causal signals but suppressed at the Signal-to-Event-to-Candidate promotion gate. This prevents self-loop clutter where the Sensorium's own completion feedback re-enters as a fresh active candidate, waking Conscious again without new external evidence.

### Semantics implemented

- **Feedback signals still written** with full causal refs (`action_id`, `origin_thread_id`, `origin_candidate_id`, `outcome`, `feedback_scope`).
- **Settled feedback suppressed at promotion**: `is_settled_feedback_signal()` classifies a signal as settled when:
  - `source == "feedback"` AND
  - `sensor` is `sensorium.action_result`, `sensorium.thread_update`, or `sensorium.worker_result` AND
  - `outcome` is NOT `failed`/`timeout`/`operator_rejected` AND
  - no `promote_feedback` override flag AND
  - no regression/reopen/blocker keywords in summary AND
  - `caused_by` contains Sensorium-internal ID prefixes
- **`should_promote_feedback()`** wraps the check with config override (`promote_all_feedback: true`).
- **`should_promote_signal()`** calls feedback check before normal strength/kind thresholds, so even high-strength ordinary feedback is suppressed.
- **Failure/regression still promotes**: `failed`, `timeout`, `operator_rejected` outcomes bypass suppression. Regression/reopen/blocker keywords in summary bypass suppression. Explicit `promote_feedback` signal flag and `promote_all_feedback` config override bypass suppression.
- **Thread close settlement preserved**: closing a thread still marks the origin candidate as `reviewed` via `_mark_origin_candidate_reviewed`.
- **Privacy/surfaces preserved**: feedback signals inherit sensitivity and allowed_surfaces from source records; no broadening.

## Files changed

| File | Change |
|------|--------|
| `agent_sensorium/gate.py` | Added `_SENSORIUM_FEEDBACK_SENSORS`, `_PROMOTABLE_FEEDBACK_OUTCOMES`, `_REGRESSION_KEYWORDS` constants; `is_settled_feedback_signal()` and `should_promote_feedback()` helpers; feedback settlement check in `should_promote_signal()`; added `"tact_"` and `"wreq_"` to `_SENSORIUM_ID_PREFIXES` |
| `tests/test_feedback_settlement.py` | 33 new tests: unit tests for `is_settled_feedback_signal` (16), `should_promote_feedback` (4), `should_promote_signal` integration (6), end-to-end JSONL behavior (7 including full smoke sequence) |
| `docs/agent-sensorium-buildout-plan-2026-05-25.md` | Added Phase 9D status note |
| `artifacts/reports/phase9d-feedback-settlement-report.md` | This report |

## Tests run and results

```
python -m pytest tests -v
  547 passed

python -m py_compile agent_sensorium/*.py scripts/*.py dashboard/plugin_api.py
  All OK

git diff --check
  Clean
```

### New test coverage (tests/test_feedback_settlement.py)

- `TestIsSettledFeedbackSignal` (16 tests): ordinary completion settled; failed/timeout/operator_rejected not settled; non-feedback source; non-sensorium sensor; promote_feedback override; regression/reopen/blocker/escalation keywords; no sensorium IDs in caused_by; missing caused_by; worker_result variants
- `TestShouldPromoteFeedback` (4 tests): settled not promoted; failed promoted; config override; non-feedback passthrough
- `TestShouldPromoteSignalFeedback` (6 tests): ordinary action feedback suppressed; failed still promoted; thread close suppressed; archive not suppressed by settlement; high-strength still suppressed; normal signals unaffected
- `TestFeedbackSettlementE2E` (7 tests): action result stored but not promoted; failed action promoted; thread close stored but not promoted; causal refs present; schema validation; full smoke sequence zero clutter; surfaces not broadened

## Remaining gaps/blockers

None. All acceptance criteria met.

## Final commit hash

Implementation commit at OMC closeout before lead report-hash correction: `ef296a5` — `fix: suppress ordinary Sensorium feedback self-promotion`.

Authoritative branch head: verify with `git log --oneline -1` after any report metadata amend.

PHASE9D_DONE
