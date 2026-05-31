# Invariant Test Coverage Notes

Generated during cleanup/generic-inner-lifecycle — rollout test authoring + invariant review pass.

---

## Reviewed Test Files

### tests/test_feedback_settlement.py

Covers Phase 9D feedback settlement and self-loop suppression:
- `is_settled_feedback_signal`: full unit coverage of completed/failed/timeout outcomes, sensorium vs external sensors, keyword-in-summary unsettled cases (regression, reopen, blocker, escalation), missing/external `caused_by`, worker_result variants.
- `should_promote_feedback`: settled not promoted, failed promoted, config override, non-feedback source promoted.
- `TestShouldPromoteSignalFeedback`: integration between gate and feedback classification.
- `TestFeedbackSettlementE2E`: end-to-end JSONL signal/candidate behavior for ordinary completion (no new candidate) and failed action (new candidate created).
- `TestIsSettledFeedbackSignalE2E` / thread update variants.

**Gaps found**: None. Coverage is complete for the identified invariants.

---

### tests/test_settlement_propagation.py

Covers Kanban subconscious settlement propagation and incident coalescing:
- `TestEventIncidentKey`: dashboard marker grouping, distinct markers, cgroup fallback, non-dashboard fingerprint/summary-hash paths.
- `TestCoalesceSuppressionReason`: no-state, first incident, repeat with intake, repeat with review, distinct marker not coalesced, incident without intake/review.
- `TestApplyKanbanSettlement`: DROP suppresses + blocks legacy dispatcher, SAVE marks reviewed, PROMOTE_CONSCIOUS marks reviewed + links ref, PROMOTE_CONSCIOUS idempotency, DROP-then-PROMOTE not resurrected.
- `TestCompletedIntakeSettlement` / `TestReviewedOpenIntakeSettlement`: closed-intake-missing-decision gap, reviewed-open with decision recovery (end-to-end dispatch gap closed), unreviewed open not flagged, `apply_settlement_record`.
- `TestFirstIncidentNotSuppressedBeforeSubconscious`: new incident not coalesced.

**Gaps found**: None. The reviewed-open intake settlement path has explicit end-to-end coverage via `test_reviewed_open_intake_recovery_closes_dispatch_gap`.

---

### tests/test_gate.py

Covers signal promotion threshold logic:
- `TestShouldPromoteSignal`: weak/strong signal, important kinds (design_decision, body_pressure, tts_sidecar_pressure), below-kind-threshold, unknown kind needing full strength, custom config.
- `TestCandidateFingerprint` / `TestSignalFingerprint` / `TestEventToCandidate` / `TestPromoteSignalToEvent`.

**Gaps found**: None. Privacy/surface fail-closed is handled in test_config.py (surface intersection, config cannot broaden).

---

### tests/test_config.py (surface/privacy tests)

Covers instance config loading and policy enforcement:
- `TestConfigResolution`: path resolution priority, state_dir fallback.
- `TestConfigLoading`: safe defaults, corrupt config fallback, blank surfaces ignored (fail-closed to `["local"]`), blank-only surfaces fall back to safe default, invalid sensitivity ignored (fail-closed to `"private"`), invalid surfaces type ignored, empty surfaces list ignored.
- `TestSurfacePolicy`: intersection semantics, config cannot broaden surfaces, disjoint returns empty, local-only stays local with broad config.
- `TestSensitivityPolicy`: config narrows, config cannot broaden, same unchanged, local_only preserved.
- `TestAttentionPolicyMutationSurface`: patch evidence rule, required fields validated, unknown rule rejected, surface broadening via attention policy rejected.

**Gaps found**: None. Surface and sensitivity fail-closed invariants are thoroughly tested.

---

### tests/test_subconscious.py (no-external-side-effects)

Covers Subconscious advisory dry-run and activation gates:
- Context builder bounded, raw signals excluded.
- Direct quantified pressure excluded from Subconscious context.
- Advisory output schema validation (unknown action rejected, CREATE_CONSCIOUS_TASK fields required, REACH_OUT rejected).
- `dry_run=True` stores receipt but does not create candidate or thread.
- `enabled=False` (default) blocks all mutation.
- Enabled non-dry-run creates internal candidate only (no thread, no external push).
- `generate_advisory_output` uses cheap openai-compatible model via injected transport.
- MiniMax think-wrapper stripped before JSON parse.
- Model prompt routes quantified thresholds outside Subconscious.
- Enabled model lane with `model_generate` injected.
- Tool handler dry-run path.

**Gap found and addressed**: No test verified that the advisory model generator is never invoked when `enabled=False`. Added:
- `test_disabled_path_never_invokes_model_transport`: passes a sentinel `model_generate`, calls `run_subconscious_advisory(enabled=False)`, asserts the sentinel is not called and no candidates are written.

---

### tests/test_kanban_native_invariants.py

Covers Kanban-native activation regression invariants:
- `TestDispatchActivationGate`: default mutating dispatch creates no dormant thread, default dry-run returns `kanban_review_required`, status surface masks raw promoted legacy action.
- `TestConsciousClaimGate`: claim disabled by default, handler returns Kanban-pointing error message, legacy opt-in still works.
- `TestKanbanBridgePrimitives`: incident key stable per fingerprint, first event not suppressed, repeat event coalesced, repeat event settled via `apply_kanban_settlement`, settlement idempotency for repeated DROP.
- `TestStaleCandidateReconciliation`: idempotency key stable, select active above threshold, routable hindsight_pressure routes to intake, feedback self-loop routes to settle_drop, planner mints intake for stale above-threshold, duplicate open intake not double-minted, reconciliation recovery closes dispatch gap.

**Gaps found**: None. Phantom pain (feedback self-loop → settle_drop), duplicate intake coalescing, and drop idempotency are all covered.

---

## Summary

| File | Status | New tests added |
|---|---|---|
| test_feedback_settlement.py | Complete | 0 |
| test_settlement_propagation.py | Complete | 0 |
| test_gate.py | Complete | 0 |
| test_config.py | Complete | 0 |
| test_subconscious.py | Gap found and filled | 1 |
| test_kanban_native_invariants.py | Complete | 0 |

**Total new tests: 12**
- 11 in tests/test_rollout.py (Part A)
- 1 in tests/test_subconscious.py (Part B)
