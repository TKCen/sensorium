"""Tests for Kanban Subconscious settlement propagation and incident coalescing.

Covers the semantics required by the live `subconscious_worker` Kanban gate:
- DROP suppresses the corresponding Sensorium candidate so the legacy dispatcher
  dry-run no longer reports `kanban_review_required` for it.
- SAVE marks the candidate reviewed while preserving full audit trail.
- PROMOTE_CONSCIOUS is idempotent and attaches a conscious task ref.
- A first/new incident is not coalesced before Subconscious sees it.
- Repeated jitter from the same already-contextualized incident coalesces
  deterministically and updates continuity state instead of creating new
  Subconscious work.
- A materially distinct structural marker is treated as a new incident.
"""

from __future__ import annotations

import json

import pytest

from agent_sensorium.dispatcher import dispatch_once
from agent_sensorium.settlement import (
    DECISION_TO_CANDIDATE_STATUS,
    VALID_SETTLEMENT_DECISIONS,
    apply_kanban_settlement,
    apply_settlement_record,
    coalesce_suppression_reason,
    event_incident_key,
    extract_kanban_intake_payload,
    infer_kanban_settlement_decision,
    plan_completed_intake_settlements,
    plan_reviewed_open_intake_settlements,
)
from agent_sensorium.store import SensoriumStore


@pytest.fixture
def state_dir(tmp_path):
    return str(tmp_path / "sensorium")


@pytest.fixture
def store(state_dir):
    s = SensoriumStore(instance="test", state_dir=state_dir)
    s.ensure_dirs()
    return s


def _make_candidate(
    *,
    cand_id="cand_dash1",
    status="candidate",
    kind="dashboard_memory_pressure",
    pressure=0.8,
    event_ids=("evt_dash1",),
    correlation_keys=("dashboard-pressure",),
    fingerprint="fp_dash1",
    summary="dashboard cgroup memory high — investigation needed",
):
    return {
        "id": cand_id,
        "status": status,
        "kind": kind,
        "pressure": pressure,
        "summary": summary,
        "event_ids": list(event_ids),
        "correlation_keys": list(correlation_keys),
        "fingerprint": fingerprint,
        "sensitivity": "private",
        "allowed_surfaces": ["local"],
        "created_at": "2026-05-28T10:00:00Z",
        "updated_at": "2026-05-28T10:00:00Z",
        "expires_at": "",
    }


class TestEventIncidentKey:
    def test_dashboard_marker_grouping(self):
        event_a = {
            "kind": "dashboard_memory_pressure",
            "summary": "Dashboard task count high again; service not running",
        }
        event_b = {
            "kind": "dashboard_memory_pressure",
            "summary": "service not running and dashboard task count high",
        }
        assert event_incident_key(event_a) == event_incident_key(event_b)

    def test_distinct_marker_is_new_incident(self):
        baseline = {
            "kind": "dashboard_memory_pressure",
            "summary": "dashboard task count high",
        }
        distinct = {
            "kind": "dashboard_memory_pressure",
            "summary": "plugin api failures detected on dashboard",
        }
        assert event_incident_key(baseline) != event_incident_key(distinct)

    def test_dashboard_cgroup_fallback(self):
        event = {
            "kind": "dashboard_memory_pressure",
            "summary": "Dashboard cgroup memory high (no actionable marker)",
        }
        assert event_incident_key(event) == "dashboard_memory_pressure:cgroup_memory_high"

    def test_non_dashboard_kind_uses_fingerprint(self):
        event = {
            "kind": "body_pressure",
            "summary": "Machine body pressure healthy_to_degraded",
            "fingerprint": "fp_body_xyz",
        }
        assert event_incident_key(event) == "body_pressure:fp_body_xyz"

    def test_non_dashboard_without_fingerprint_uses_summary_hash(self):
        event = {"kind": "process_pressure", "summary": "zombies=3"}
        key = event_incident_key(event)
        assert key.startswith("process_pressure:")


class TestCoalesceSuppressionReason:
    def test_no_state_no_coalesce(self):
        event = {"kind": "dashboard_memory_pressure", "summary": "service not running"}
        assert coalesce_suppression_reason(event, None) is None
        assert coalesce_suppression_reason(event, {}) is None

    def test_first_incident_not_coalesced(self):
        event = {"kind": "dashboard_memory_pressure", "summary": "service not running"}
        state = {"incident_context": {}}
        assert coalesce_suppression_reason(event, state) is None

    def test_repeat_with_intake_is_coalesced(self):
        event = {"kind": "dashboard_memory_pressure", "summary": "service not running"}
        key = event_incident_key(event)
        state = {"incident_context": {key: {"intake_task_id": "kanban_task_1"}}}
        reason = coalesce_suppression_reason(event, state)
        assert reason is not None
        assert reason.startswith("coalesced_repeat:")
        assert key in reason

    def test_repeat_with_review_is_coalesced(self):
        event = {"kind": "dashboard_memory_pressure", "summary": "service not running"}
        key = event_incident_key(event)
        state = {"incident_context": {key: {"review_task_id": "kanban_review_1"}}}
        assert coalesce_suppression_reason(event, state) is not None

    def test_distinct_marker_not_coalesced(self):
        first = {"kind": "dashboard_memory_pressure", "summary": "service not running"}
        second = {"kind": "dashboard_memory_pressure", "summary": "plugin api failures"}
        state = {
            "incident_context": {
                event_incident_key(first): {"intake_task_id": "kanban_task_1"},
            }
        }
        assert coalesce_suppression_reason(second, state) is None

    def test_incident_without_intake_or_review_not_coalesced(self):
        event = {"kind": "dashboard_memory_pressure", "summary": "service not running"}
        key = event_incident_key(event)
        state = {"incident_context": {key: {"first_seen_at": "2026-05-28T00:00:00Z"}}}
        assert coalesce_suppression_reason(event, state) is None


class TestApplyKanbanSettlement:
    def test_constants_consistent(self):
        assert set(DECISION_TO_CANDIDATE_STATUS) == VALID_SETTLEMENT_DECISIONS

    def test_suppressed_reviewer_wording_infers_drop(self):
        task = {
            "result": "Decision: SUPPRESSED\nreason: duplicate candidate already handled",
        }
        assert infer_kanban_settlement_decision(task) == "DROP"

    def test_invalid_decision_rejected(self, store):
        result = apply_kanban_settlement(store, decision="MAYBE", candidate_id="cand_1")
        assert result["action"] == "invalid_decision"

    def test_drop_suppresses_candidate(self, store):
        cand = _make_candidate()
        store.append_jsonl("candidates", cand)
        result = apply_kanban_settlement(
            store,
            decision="DROP",
            candidate_id="cand_dash1",
            intake_task_id="kt_intake_1",
            review_task_id="kt_review_1",
            reason="not actionable",
        )
        assert result["action"] == "settled"
        assert result["updated_candidate_ids"] == ["cand_dash1"]
        candidates = store.read_jsonl("candidates")
        assert candidates[0]["status"] == "suppressed"
        assert candidates[0]["kanban_settlement"]["decision"] == "DROP"
        assert candidates[0]["kanban_settlement"]["intake_task_id"] == "kt_intake_1"

    def test_drop_blocks_legacy_dispatcher_kanban_advisory(self, store):
        cand = _make_candidate(pressure=0.9)
        store.append_jsonl("candidates", cand)
        dry_before = dispatch_once(store, dry_run=True)
        assert dry_before["action"] == "kanban_review_required"

        apply_kanban_settlement(store, decision="DROP", candidate_id="cand_dash1")
        dry_after = dispatch_once(store, dry_run=True)
        assert dry_after["action"] == "no_candidate", (
            "DROP-settled candidate must not appear in dispatch promotion pool"
        )

    def test_save_marks_reviewed(self, store):
        cand = _make_candidate()
        store.append_jsonl("candidates", cand)
        result = apply_kanban_settlement(
            store,
            decision="SAVE",
            candidate_id="cand_dash1",
            intake_task_id="kt_intake_2",
            review_task_id="kt_review_2",
            reason="save for later context",
        )
        assert result["action"] == "settled"
        candidates = store.read_jsonl("candidates")
        assert candidates[0]["status"] == "reviewed"
        assert candidates[0]["kanban_settlement"]["decision"] == "SAVE"

        dry = dispatch_once(store, dry_run=True)
        assert dry["action"] == "no_candidate"

        decisions = store.read_jsonl("decisions")
        applied = [d for d in decisions if d.get("type") == "kanban.settlement.applied"]
        assert len(applied) == 1
        assert applied[0]["intake_task_id"] == "kt_intake_2"
        assert applied[0]["new_status"] == "reviewed"

    def test_promote_conscious_marks_reviewed_and_links_ref(self, store):
        cand = _make_candidate()
        store.append_jsonl("candidates", cand)
        conscious_ref = {
            "task_id": "kt_conscious_1",
            "thread_id": "kt_thread_1",
            "board": "sensorium",
            "promoted_at": "2026-05-28T12:00:00Z",
        }
        result = apply_kanban_settlement(
            store,
            decision="PROMOTE_CONSCIOUS",
            candidate_id="cand_dash1",
            intake_task_id="kt_intake_3",
            review_task_id="kt_review_3",
            conscious_task_ref=conscious_ref,
            reason="worth conscious attention",
        )
        assert result["action"] == "settled"
        candidates = store.read_jsonl("candidates")
        assert candidates[0]["status"] == "reviewed"
        meta = candidates[0]["kanban_settlement"]
        assert meta["decision"] == "PROMOTE_CONSCIOUS"
        assert meta["conscious_task_ref"]["task_id"] == "kt_conscious_1"
        assert meta["conscious_task_ref"]["thread_id"] == "kt_thread_1"

    def test_promote_conscious_preserves_internal_candidate_ref(self, store):
        cand = _make_candidate()
        store.append_jsonl("candidates", cand)
        conscious_ref = {
            "candidate_id": "cand_internal_conscious",
            "conscious_task_id": "ctask_internal_1",
            "kind": "internal_conscious_task_candidate",
            "promoted_at": "2026-06-07T14:15:00Z",
            "ignored_extra": "not persisted",
        }
        result = apply_kanban_settlement(
            store,
            decision="PROMOTE_CONSCIOUS",
            candidate_id="cand_dash1",
            intake_task_id="kt_intake_internal",
            review_task_id="kt_review_internal",
            conscious_task_ref=conscious_ref,
            reason="promote into bounded Conscious aperture",
        )
        assert result["action"] == "settled"
        candidates = store.read_jsonl("candidates")
        meta = candidates[0]["kanban_settlement"]
        ref = meta["conscious_task_ref"]
        assert ref["candidate_id"] == "cand_internal_conscious"
        assert ref["conscious_task_id"] == "ctask_internal_1"
        assert ref["kind"] == "internal_conscious_task_candidate"
        assert "ignored_extra" not in ref

    def test_promote_conscious_is_idempotent(self, store):
        cand = _make_candidate()
        store.append_jsonl("candidates", cand)
        conscious_ref = {"task_id": "kt_conscious_1", "board": "sensorium"}
        first = apply_kanban_settlement(
            store,
            decision="PROMOTE_CONSCIOUS",
            candidate_id="cand_dash1",
            conscious_task_ref=conscious_ref,
        )
        assert first["action"] == "settled"

        second = apply_kanban_settlement(
            store,
            decision="PROMOTE_CONSCIOUS",
            candidate_id="cand_dash1",
            conscious_task_ref=conscious_ref,
        )
        assert second["action"] == "already_settled"
        assert second["updated_candidate_ids"] == []

        candidates = store.read_jsonl("candidates")
        assert len([c for c in candidates if c["id"] == "cand_dash1"]) == 1

        decisions = store.read_jsonl("decisions")
        applied = [d for d in decisions if d.get("type") == "kanban.settlement.applied"]
        assert len(applied) == 1, "Re-applying same decision must not duplicate receipts"

        dry = dispatch_once(store, dry_run=True)
        assert dry["action"] == "no_candidate"

    def test_drop_then_promote_not_resurrected(self, store):
        cand = _make_candidate()
        store.append_jsonl("candidates", cand)
        apply_kanban_settlement(store, decision="DROP", candidate_id="cand_dash1")
        # A subsequent settlement that arrives after the candidate has already
        # been suppressed must not silently overwrite the suppression by
        # re-activating the candidate. The status stays in the terminal state.
        result = apply_kanban_settlement(
            store, decision="PROMOTE_CONSCIOUS", candidate_id="cand_dash1",
        )
        candidates = store.read_jsonl("candidates")
        assert candidates[0]["status"] == "suppressed"
        # Receipt still recorded for audit, but action is updated metadata only.
        assert result["action"] in {"settled", "already_settled"}
        dry = dispatch_once(store, dry_run=True)
        assert dry["action"] == "no_candidate"

    def test_resolve_by_event_id(self, store):
        cand = _make_candidate(cand_id="cand_e1", event_ids=("evt_kanban_1",))
        store.append_jsonl("candidates", cand)
        result = apply_kanban_settlement(
            store, decision="DROP", event_id="evt_kanban_1",
        )
        assert result["action"] == "settled"
        assert result["updated_candidate_ids"] == ["cand_e1"]

    def test_resolve_by_fingerprint(self, store):
        cand = _make_candidate(cand_id="cand_fp", fingerprint="fp_unique_1")
        store.append_jsonl("candidates", cand)
        result = apply_kanban_settlement(
            store, decision="DROP", fingerprint="fp_unique_1",
        )
        assert result["action"] == "settled"
        assert result["updated_candidate_ids"] == ["cand_fp"]

    def test_resolve_by_correlation_keys(self, store):
        cand_a = _make_candidate(cand_id="cand_a", correlation_keys=("dashboard-pressure",))
        cand_b = _make_candidate(
            cand_id="cand_b",
            correlation_keys=("body-pressure",),
            fingerprint="fp_body",
        )
        store.append_jsonl("candidates", cand_a)
        store.append_jsonl("candidates", cand_b)
        result = apply_kanban_settlement(
            store, decision="SAVE", correlation_keys=["dashboard-pressure"],
        )
        assert result["action"] == "settled"
        assert result["updated_candidate_ids"] == ["cand_a"]
        candidates = store.read_jsonl("candidates")
        statuses = {c["id"]: c["status"] for c in candidates}
        assert statuses["cand_a"] == "reviewed"
        assert statuses["cand_b"] == "candidate"

    def test_no_match_records_unresolved_receipt(self, store):
        result = apply_kanban_settlement(
            store, decision="DROP", candidate_id="cand_missing",
        )
        assert result["action"] == "no_candidate_match"
        decisions = store.read_jsonl("decisions")
        unresolved = [d for d in decisions if d.get("type") == "kanban.settlement.unresolved"]
        assert len(unresolved) == 1

    def test_apply_settlement_record_passthrough(self, store):
        cand = _make_candidate()
        store.append_jsonl("candidates", cand)
        record = {
            "decision": "drop",  # lower case should be accepted
            "candidate_id": "cand_dash1",
            "intake_task_id": "kt_intake_9",
            "review_task_id": "kt_review_9",
            "reason": "via record",
        }
        result = apply_settlement_record(store, record)
        assert result["action"] == "settled"
        candidates = store.read_jsonl("candidates")
        assert candidates[0]["status"] == "suppressed"

    def test_apply_settlement_record_invalid_input(self, store):
        assert apply_settlement_record(store, None)["action"] == "invalid_record"
        bad = apply_settlement_record(store, {"decision": "nonsense"})
        assert bad["action"] == "invalid_decision"


class TestCompletedIntakeSettlementRecovery:
    def _intake_task(self, *, status="done", comment="decision: DROP — stale noise", summary="Settled as DROP — stale noise"):
        payload = {
            "candidate_id": "cand_dash1",
            "event_ids": ["evt_dash1"],
            "fingerprint": "fp_dash1",
            "correlation_keys": ["dashboard-pressure"],
            "kind": "dashboard_memory_pressure",
            "pressure": 0.8,
            "summary": "dashboard cgroup memory high",
        }
        body = (
            "Sensorium Kanban reconciliation intake v1.\n\n"
            "Compact candidate payload:\n"
            + json.dumps(payload, indent=2, sort_keys=True)
            + "\n\nExpected settlement: Subconscious comments DROP/SAVE/PROMOTE evidence."
        )
        return {
            "id": "kt_intake_done",
            "title": "sensor:intake:dashboard_memory_pressure: dashboard cgroup memory high",
            "status": status,
            "body": body,
            "comments": [{"body": comment}] if comment else [],
            "runs": [{"summary": summary}] if summary else [],
            "events": [
                {"kind": "completed", "payload": {"summary": summary}}
            ] if summary else [],
        }

    def test_extracts_candidate_payload_from_intake_body(self):
        payload = extract_kanban_intake_payload(self._intake_task()["body"])
        assert payload["candidate_id"] == "cand_dash1"
        assert payload["event_ids"] == ["evt_dash1"]
        assert payload["fingerprint"] == "fp_dash1"

    def test_infers_decision_from_review_comment_or_completion_summary(self):
        task = self._intake_task(comment="decision: SAVE — keep compact memory", summary="")
        assert infer_kanban_settlement_decision(task) == "SAVE"
        task = self._intake_task(comment="", summary="Settled as PROMOTE — needs conscious review")
        assert infer_kanban_settlement_decision(task) == "PROMOTE_CONSCIOUS"

    def test_plans_missing_completed_intake_settlement_record(self):
        task = self._intake_task()
        plan = plan_completed_intake_settlements(
            [task],
            decisions=[],
            active_candidate_ids={"cand_dash1"},
        )
        assert plan["gaps"] == []
        assert len(plan["records"]) == 1
        record = plan["records"][0]
        assert record["decision"] == "DROP"
        assert record["candidate_id"] == "cand_dash1"
        assert record["event_id"] == "evt_dash1"
        assert record["intake_task_id"] == "kt_intake_done"
        assert record["correlation_keys"] == ["dashboard-pressure"]

    def test_existing_applied_receipt_suppresses_recovery_record(self):
        task = self._intake_task()
        decisions = [{"type": "kanban.settlement.applied", "intake_task_id": "kt_intake_done"}]
        plan = plan_completed_intake_settlements(
            [task],
            decisions=decisions,
            active_candidate_ids={"cand_dash1"},
        )
        assert plan["records"] == []
        assert plan["already_settled"] == ["kt_intake_done"]

    def test_completed_intake_drop_recovery_closes_dispatch_gap(self, store):
        store.append_jsonl("candidates", _make_candidate())
        before = dispatch_once(store, dry_run=True)
        assert before["action"] == "kanban_review_required"

        plan = plan_completed_intake_settlements(
            [self._intake_task()],
            decisions=store.read_jsonl("decisions"),
            active_candidate_ids={"cand_dash1"},
        )
        assert len(plan["records"]) == 1
        result = apply_settlement_record(store, plan["records"][0])
        assert result["action"] == "settled"

        after = dispatch_once(store, dry_run=True)
        assert after["action"] == "no_candidate"

    def test_completed_intake_without_decision_is_visible_gap(self):
        task = self._intake_task(comment="reviewed stale candidate", summary="completed")
        plan = plan_completed_intake_settlements(
            [task],
            decisions=[],
            active_candidate_ids={"cand_dash1"},
        )
        assert plan["records"] == []
        assert plan["gaps"] == [
            {
                "intake_task_id": "kt_intake_done",
                "candidate_id": "cand_dash1",
                "reason": "closed_intake_missing_decision",
            }
        ]

    def test_plans_reviewed_open_intake_settlement_record(self):
        task = self._intake_task(
            status="ready",
            comment="decision: DROP — reviewed but worker could not archive",
            summary="",
        )
        plan = plan_reviewed_open_intake_settlements(
            [task],
            decisions=[],
            active_candidate_ids={"cand_dash1"},
        )
        assert plan["gaps"] == []
        assert len(plan["records"]) == 1
        record = plan["records"][0]
        assert record["decision"] == "DROP"
        assert record["candidate_id"] == "cand_dash1"
        assert record["event_id"] == "evt_dash1"
        assert record["intake_task_id"] == "kt_intake_done"
        assert plan["cleanup_task_ids"] == ["kt_intake_done"]

    def test_reviewed_open_intake_without_decision_is_visible_gap(self):
        task = self._intake_task(status="ready", comment="looked at it", summary="")
        plan = plan_reviewed_open_intake_settlements(
            [task],
            decisions=[],
            active_candidate_ids={"cand_dash1"},
        )
        assert plan["records"] == []
        assert plan["cleanup_task_ids"] == []
        assert plan["gaps"] == [
            {
                "intake_task_id": "kt_intake_done",
                "candidate_id": "cand_dash1",
                "reason": "reviewed_open_intake_missing_decision",
            }
        ]

    def test_unreviewed_open_intake_does_not_become_gap(self):
        task = self._intake_task(status="ready", comment="", summary="")
        plan = plan_reviewed_open_intake_settlements(
            [task],
            decisions=[],
            active_candidate_ids={"cand_dash1"},
        )
        assert plan["records"] == []
        assert plan["gaps"] == []
        assert plan["cleanup_task_ids"] == []

    def test_sticky_block_comment_alone_does_not_make_open_intake_a_gap(self):
        task = self._intake_task(
            status="ready",
            comment="BLOCKED: Sensorium substrate intake: sticky-blocked so only Subconscious review may settle it.",
            summary="",
        )
        plan = plan_reviewed_open_intake_settlements(
            [task],
            decisions=[],
            active_candidate_ids={"cand_dash1"},
        )
        assert plan["records"] == []
        assert plan["gaps"] == []
        assert plan["cleanup_task_ids"] == []

    def test_reviewed_open_intake_recovery_closes_dispatch_gap(self, store):
        store.append_jsonl("candidates", _make_candidate())
        before = dispatch_once(store, dry_run=True)
        assert before["action"] == "kanban_review_required"

        plan = plan_reviewed_open_intake_settlements(
            [self._intake_task(status="ready", comment="decision: DROP", summary="")],
            decisions=store.read_jsonl("decisions"),
            active_candidate_ids={"cand_dash1"},
        )
        assert len(plan["records"]) == 1
        result = apply_settlement_record(store, plan["records"][0])
        assert result["action"] == "settled"

        after = dispatch_once(store, dry_run=True)
        assert after["action"] == "no_candidate"


class TestFirstIncidentNotSuppressedBeforeSubconscious:
    """Coalescing must not block first contextualization of new pressure."""

    def test_new_dashboard_pressure_is_first_incident(self):
        event = {
            "id": "evt_new_dash_1",
            "kind": "dashboard_memory_pressure",
            "summary": "service not running on dashboard root",
        }
        empty_state = {"incident_context": {}}
        assert coalesce_suppression_reason(event, empty_state) is None

    def test_new_body_pressure_is_first_incident(self):
        event = {
            "id": "evt_body_1",
            "kind": "body_pressure",
            "summary": "Machine body pressure healthy_to_degraded",
            "fingerprint": "fp_body_first",
        }
        assert coalesce_suppression_reason(event, {"incident_context": {}}) is None
