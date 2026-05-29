"""Hermes-style bounded salience review runner — stub for future automation.

This module exists as the explicit seam for a future background reviewer that
mirrors the Hermes skill-loop pattern. It is DISABLED BY DEFAULT. The manual
harness in scripts/sensorium_salience_review.py covers v1 use.

Safety boundary (must hold before enabling):
- Reviewer may only call record_attention_policy_decision and
  manage_attention_policy_config. No Python source edits, no SOUL patches,
  no outbound messages, no gateway/plugin restarts.
- Reviewer must have recursive review disabled (intervals = 0).
- Tool whitelist must be enforced at the Hermes toolset layer, not only in
  the prompt.
- All writes must produce auditable receipts with future_tendency_delta /
  verification_condition / rollback_condition.
- Privacy/surface broadening is forbidden; any proposal must be a receipt
  for explicit conscious approval, never an automatic mutation.

Missing before automation can be enabled:
1. Hermes AIAgent fork or subprocess integration with Sensorium decision tools
   in its toolset and all other tools excluded.
2. Thread-local or subprocess-level tool whitelist enforcement (not prompt-only).
3. Daemon-exit safety: flush/confirm write before parent exits (daemon=False
   for policy writes, unlike Hermes daemon=True for skill writes).
4. Counter serialization for stateless gateway deployments.
5. Tests proving whitelist enforcement, no recursive spawn, no foreground state
   mutation, and daemon-exit safety.
"""

from __future__ import annotations

SALIENCE_REVIEW_ENABLED: bool = False
"""Master gate. Must remain False until safety boundary tests pass."""

_MISSING_SEAMS: list[str] = [
    "hermes_aiclient_fork_or_subprocess_with_sensorium_toolset",
    "thread_local_tool_whitelist_enforcement",
    "daemon_exit_flush_safety",
    "gateway_counter_serialization",
    "whitelist_enforcement_tests",
]


def run_bounded_salience_review_session(
    store,
    candidate_id: str,
    evidence: dict,
    *,
    dry_run: bool = True,
) -> dict:
    """Stub for a future Hermes-style bounded salience review session.

    Not yet implemented. Returns a disabled-status dict describing what
    is missing. Use scripts/sensorium_salience_review.py for manual review.
    """
    if not SALIENCE_REVIEW_ENABLED:
        return {
            "status": "disabled",
            "reason": "SALIENCE_REVIEW_ENABLED is False; manual harness required",
            "missing_seams": list(_MISSING_SEAMS),
            "candidate_id": candidate_id,
            "use_instead": "scripts/sensorium_salience_review.py --json-only-context",
        }
    raise NotImplementedError(
        "run_bounded_salience_review_session: set SALIENCE_REVIEW_ENABLED=True "
        "only after all _MISSING_SEAMS are resolved and safety tests pass."
    )
