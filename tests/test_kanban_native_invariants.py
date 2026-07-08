"""Kanban-native activation regression invariants.

These tests lock in the architectural decision that Hermes Kanban is the only
activation/ticketing substrate for Sensorium. They guard against the split-brain
regression where a clean Kanban board could coexist with a hidden Sensorium
`would_promote`/dormant-thread lane.

Scope:
- Default `sensorium_dispatch_once` mutating call must not create internal
  Sensorium threads (legacy gate fails closed).
- Default `sensorium_conscious_claim` must not claim dormant threads.
- Status surface must mask the legacy `promoted`/`would_promote` action labels.
- Kanban sensor-bridge primitives (`event_incident_key`,
  `coalesce_suppression_reason`, `apply_kanban_settlement`) must route a
  `hindsight_pressure`-shaped event through intake → deterministic DROP rather
  than letting it sit as an invisible Sensorium candidate.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent_sensorium.conscious import claim_dormant_thread, complete_claim
from agent_sensorium.dispatcher import dispatch_once
from agent_sensorium.settlement import (
    DEFAULT_DISPATCH_PRESSURE_THRESHOLD,
    apply_kanban_settlement,
    candidate_intake_idempotency_key,
    candidate_route,
    coalesce_suppression_reason,
    event_incident_key,
    plan_candidate_reconciliation,
    represented_candidate_ids,
    select_active_above_threshold,
)
from agent_sensorium.store import SensoriumStore
from agent_sensorium.tools import (
    handle_sensorium_conscious_claim,
    handle_sensorium_dispatch_once,
    handle_sensorium_ingest_event,
    handle_sensorium_ingest_signal,
    handle_sensorium_status,
)


@pytest.fixture
def state_dir(tmp_path):
    return str(tmp_path / "sensorium")


@pytest.fixture
def store(state_dir):
    s = SensoriumStore(instance="test", state_dir=state_dir)
    s.ensure_dirs()
    return s


def _strong_signal(kind: str = "design_decision", summary: str = "Strong test signal") -> dict:
    return {
        "sensor": "test",
        "source": "manual",
        "kind": kind,
        "summary": summary,
        "strength_hint": 0.9,
        "correlation_keys": [f"{kind}-corr"],
    }


def _seed_candidate(state_dir: str, *, kind: str = "design_decision") -> str:
    raw = handle_sensorium_ingest_signal(
        signal=_strong_signal(kind=kind),
        instance="test",
        state_dir=state_dir,
    )
    return json.loads(raw)["data"]["candidate_id"]


def _load_live_bridge_module():
    path = Path(__file__).resolve().parents[1] / "live-scripts" / "sensorium_kanban_sensor_tick.py"
    spec = importlib.util.spec_from_file_location("sensorium_kanban_sensor_tick_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bridge_intake_script_paths() -> list[tuple[str, Path]]:
    repo_root = Path(__file__).resolve().parents[1]
    paths = [("rollout_source", repo_root / "live-scripts" / "sensorium_kanban_sensor_tick.py")]
    runtime = Path.home() / ".hermes" / "scripts" / "sensorium_kanban_sensor_tick.py"
    if runtime.exists():
        paths.append(("runtime", runtime))
    return paths



class TestDispatchActivationGate:
    """Sensorium dispatch must not become a second activation substrate."""

    def test_default_mutating_dispatch_creates_no_dormant_thread(self, store):
        """In default (Kanban-only) mode, mutation must fail closed."""
        _seed_candidate(str(store.root))
        result = dispatch_once(store, dry_run=False)
        assert result["action"] == "legacy_dispatch_disabled"
        assert result["recommended_activation"] == "kanban_bridge"
        assert store.read_jsonl("threads") == []
        # No promotion receipt either: the dispatcher must not even record a
        # silent `dispatch.promoted_to_thread` decision in Kanban mode.
        decisions = store.read_jsonl("decisions")
        assert not any(
            d.get("type") == "dispatch.promoted_to_thread" for d in decisions
        )

    def test_default_dry_run_returns_kanban_review_required(self, store):
        _seed_candidate(str(store.root))
        result = dispatch_once(store, dry_run=True)
        assert result["action"] == "kanban_review_required"
        assert result["recommended_activation"] == "kanban_bridge"
        # Dry-run must remain side-effect-free.
        assert store.read_jsonl("threads") == []
        assert store.read_jsonl("decisions") == []

    def test_status_surface_masks_raw_promoted_legacy_action(self, tmp_path):
        """Even when legacy was explicitly enabled, status surface presents the
        Kanban activation contract so dashboards/agents do not silently learn
        about a hidden Sensorium-internal promotion."""
        state_dir = str(tmp_path / "sensorium")
        _seed_candidate(state_dir)

        promoted_raw = json.loads(handle_sensorium_dispatch_once(
            instance="test",
            state_dir=state_dir,
            dry_run=False,
            config={"legacy_thread_dispatch_enabled": True},
        ))
        # The raw dispatcher response still describes what really happened
        # under explicit legacy opt-in -- that is the audit truth for tests.
        assert promoted_raw["data"]["action"] == "promoted"

        status = json.loads(
            handle_sensorium_status(instance="test", state_dir=state_dir)
        )["data"]
        legacy = status["legacy_dispatch_result"]
        assert legacy["action"] == "kanban_review_required"
        assert legacy["raw_legacy_action"] == "promoted"
        assert legacy["deprecated"] is True
        assert legacy["activation_substrate"] == "kanban"
        assert legacy["ignored_as_activation"] is True


class TestConsciousClaimGate:
    """Background-conscious claim/complete must not be a parallel scheduler."""

    def test_claim_disabled_by_default_returns_conscious_disabled(self, store):
        result = claim_dormant_thread(store)
        assert result["success"] is False
        assert result["error"] == "conscious_disabled"

    def test_handler_claim_returns_error_envelope_by_default(self, state_dir):
        raw = handle_sensorium_conscious_claim(
            instance="test", state_dir=state_dir,
        )
        result = json.loads(raw)
        assert result["success"] is False
        # `_err` surfaces the human-readable detail, which must point callers
        # at the Kanban substrate rather than at a flag they can flip silently.
        message = result.get("error") or ""
        assert "deprecated" in message
        assert "Kanban" in message

    def test_claim_only_under_explicit_legacy_opt_in_still_works(self, store):
        """We keep the legacy path runnable for migration/tests but only when
        callers explicitly pass `config.enabled=True`."""
        _seed_candidate(str(store.root))
        dispatch = dispatch_once(
            store,
            dry_run=False,
            config={"legacy_thread_dispatch_enabled": True},
        )
        assert dispatch["action"] == "promoted"

        claim = claim_dormant_thread(store, config={"enabled": True})
        assert claim["success"] is True
        thread_id = claim["data"]["thread"]["thread_id"]
        lease_id = claim["data"]["lease_id"]

        complete = complete_claim(
            store,
            thread_id=thread_id,
            lease_id=lease_id,
            outcome="completed",
        )
        assert complete["success"] is True


class TestKanbanBridgePrimitives:
    """`hindsight_pressure` must flow through Kanban intake / settlement.

    The live bridge script `live-scripts/sensorium_kanban_sensor_tick.py`
    consumes Sensorium events and either mirrors them into Kanban intake tasks
    or coalesces repeats via `apply_kanban_settlement(DROP, ...)`. We test the
    bridge primitives directly so the invariant survives any rewording of the
    wrapper.
    """

    def _hindsight_event(self, *, eid: str, summary: str, fingerprint: str) -> dict:
        # `handle_sensorium_ingest_event` recomputes `fingerprint` from a
        # canonical content hash, so callers that need to settle by fingerprint
        # must read the candidate's recomputed value. Tests below settle by
        # `event_id` instead, which the bridge already does in the live path.
        return {
            "id": eid,
            "ts": "2026-05-28T10:00:00Z",
            "type": "sensor.event.promoted",
            "kind": "hindsight_pressure",
            "summary": summary,
            "strength": 0.84,
            "source_signal_ids": [f"sig_{eid}"],
            "correlation_keys": ["hindsight-pressure"],
            "sensitivity": "private",
            "allowed_surfaces": ["local"],
            "fingerprint": fingerprint,
        }

    def test_incident_key_is_stable_per_fingerprint(self):
        ev1 = self._hindsight_event(
            eid="evt_a", summary="pending=210", fingerprint="hp_abc",
        )
        ev2 = self._hindsight_event(
            eid="evt_b", summary="pending=212", fingerprint="hp_abc",
        )
        assert event_incident_key(ev1) == event_incident_key(ev2)
        assert event_incident_key(ev1).startswith("hindsight_pressure:")

    def test_first_event_is_not_suppressed(self):
        ev = self._hindsight_event(
            eid="evt_first", summary="pending=210", fingerprint="hp_first",
        )
        # No prior incident_context -> no suppression -> bridge would mint a
        # `sensor:intake:hindsight_pressure` task.
        assert coalesce_suppression_reason(ev, state={}) is None
        assert coalesce_suppression_reason(ev, state={"incident_context": {}}) is None

    def test_repeat_event_is_coalesced_when_intake_already_exists(self):
        ev = self._hindsight_event(
            eid="evt_repeat", summary="pending=215", fingerprint="hp_repeat",
        )
        key = event_incident_key(ev)
        state = {
            "incident_context": {
                key: {
                    "intake_task_id": "kanban_task_42",
                    "first_event_id": "evt_prior",
                },
            },
        }
        reason = coalesce_suppression_reason(ev, state)
        assert reason is not None
        assert reason.startswith("coalesced_repeat:")

    def test_repeat_event_is_settled_via_apply_kanban_settlement(self, state_dir):
        """End-to-end invariant: a repeat hindsight_pressure event drops the
        active Sensorium candidate so it cannot remain `would_promote` while
        the Kanban board is clean."""
        # Promote a hindsight_pressure event through the normal ingest path so
        # there is a real candidate in the active promotion pool.
        first = self._hindsight_event(
            eid="evt_hp_1", summary="pending=300", fingerprint="hp_active",
        )
        ingest_raw = handle_sensorium_ingest_event(
            event=first, instance="test", state_dir=state_dir,
        )
        ingest = json.loads(ingest_raw)
        candidate_id = ingest["data"]["candidate_id"]

        store = SensoriumStore(instance="test", state_dir=state_dir)
        active = [
            c for c in store.read_jsonl("candidates")
            if c.get("status") == "candidate"
        ]
        assert any(c.get("id") == candidate_id for c in active)

        # Simulate the bridge deciding this is a coalesced repeat of an
        # already-contextualized incident: it propagates DROP back into
        # Sensorium so the active candidate disappears from the promotion pool.
        repeat = self._hindsight_event(
            eid="evt_hp_2", summary="pending=305", fingerprint="hp_active",
        )
        settlement = apply_kanban_settlement(
            store,
            decision="DROP",
            event_id="evt_hp_1",
            fingerprint=str(repeat.get("fingerprint")),
            correlation_keys=list(repeat.get("correlation_keys") or []),
            intake_task_id="kanban_intake_42",
            review_task_id="",
            reason="Deterministic Kanban coalescing for repeat hindsight_pressure.",
        )
        assert settlement["action"] == "settled"
        assert candidate_id in settlement["updated_candidate_ids"]

        # After settlement, the candidate is no longer eligible for dispatch:
        # this is the invariant that closes the split-brain gap.
        remaining_active = [
            c for c in store.read_jsonl("candidates")
            if c.get("status") == "candidate"
        ]
        assert not any(c.get("id") == candidate_id for c in remaining_active)
        result = dispatch_once(store, dry_run=True)
        # No other candidate is above threshold, so the dispatcher cannot find
        # the dropped one even in dry-run advisory mode.
        assert result["action"] == "no_candidate"

    def test_settlement_is_idempotent_for_repeated_drop(self, state_dir):
        first = self._hindsight_event(
            eid="evt_idem", summary="pending=300", fingerprint="hp_idem",
        )
        handle_sensorium_ingest_event(
            event=first, instance="test", state_dir=state_dir,
        )
        store = SensoriumStore(instance="test", state_dir=state_dir)
        s1 = apply_kanban_settlement(
            store,
            decision="DROP",
            event_id="evt_idem",
            fingerprint="hp_idem",
            correlation_keys=["hindsight-pressure"],
            reason="first drop",
        )
        s2 = apply_kanban_settlement(
            store,
            decision="DROP",
            event_id="evt_idem",
            fingerprint="hp_idem",
            correlation_keys=["hindsight-pressure"],
            reason="repeat drop",
        )
        assert s1["action"] == "settled"
        assert s2["action"] == "already_settled"
        # Idempotency means the candidate is not double-mutated and no new
        # settlement receipt is appended for the repeat call.
        assert s2["updated_candidate_ids"] == []


class TestStaleCandidateReconciliation:
    """Stale active above-threshold candidates must reach Kanban or settlement.

    The regression: a candidate crossed the dispatch threshold earlier (before
    the bridge's event watermark, or with no fresh sample this tick), so the
    event-driven path mirrors nothing. `dispatch_once(dry_run=True)` reports
    `kanban_review_required` while the board is empty -- a quiet pending
    activation. The reconciliation planner closes that gap deterministically:
    it mints an intake for routable kinds (`hindsight_pressure`) or settles
    non-routable kinds with a receipt, and coalesces so no duplicate intake is
    minted while one is already open.
    """

    def _hindsight_event(self, *, eid: str, summary: str, fingerprint: str) -> dict:
        return {
            "id": eid,
            "ts": "2026-05-28T10:00:00Z",
            "type": "sensor.event.promoted",
            "kind": "hindsight_pressure",
            "summary": summary,
            "strength": 0.84,
            "source_signal_ids": [f"sig_{eid}"],
            "correlation_keys": ["hindsight-pressure"],
            "sensitivity": "private",
            "allowed_surfaces": ["local"],
            "fingerprint": fingerprint,
        }

    def _feedback_self_loop_candidate(self) -> dict:
        # is_feedback_self_loop: feedback_meta with a caused_by value carrying a
        # Sensorium id prefix and a non-operator_evaluation scope.
        return {
            "id": "cand_selfloop",
            "status": "candidate",
            "kind": "thread_update",
            "pressure": 0.9,
            "summary": "internal sensorium echo",
            "event_ids": ["evt_loop"],
            "correlation_keys": ["loop"],
            "feedback_meta": {
                "caused_by": {"thread_id": "sth_internal"},
                "outcome": "completed",
                "feedback_scope": "internal",
            },
        }

    def test_idempotency_key_is_stable_and_candidate_scoped(self):
        cand = {"id": "cand_12617f0ee225"}
        key = candidate_intake_idempotency_key(cand)
        assert key == "sensorium:intake:candidate:cand_12617f0ee225"
        # Stable across calls; distinct per candidate id.
        assert key == candidate_intake_idempotency_key(dict(cand))
        assert candidate_intake_idempotency_key({"id": "cand_other"}) != key

    def test_select_active_above_threshold_filters_status_and_pressure(self):
        candidates = [
            {"id": "a", "status": "candidate", "pressure": 0.77},
            {"id": "b", "status": "candidate", "pressure": 0.10},
            {"id": "c", "status": "suppressed", "pressure": 0.99},
            {"id": "d", "status": "reviewed", "pressure": 0.99},
        ]
        active = select_active_above_threshold(candidates)
        assert [c["id"] for c in active] == ["a"]

    def test_routable_hindsight_pressure_routes_to_intake(self):
        cand = {"id": "cand_hp", "status": "candidate", "kind": "hindsight_pressure", "pressure": 0.77}
        assert candidate_route(cand) == "intake"

    def test_feedback_self_loop_routes_to_settle_drop(self):
        assert candidate_route(self._feedback_self_loop_candidate()) == "settle_drop"

    def test_planner_mints_intake_for_stale_above_threshold_hindsight_pressure(self, state_dir):
        """THE regression: an active above-threshold hindsight_pressure candidate
        with no open intake must be planned for a fresh Kanban intake."""
        event = self._hindsight_event(
            eid="evt_stale", summary="pending=210 critical", fingerprint="hp_stale",
        )
        ingest = json.loads(
            handle_sensorium_ingest_event(event=event, instance="test", state_dir=state_dir)
        )
        candidate_id = ingest["data"]["candidate_id"]

        store = SensoriumStore(instance="test", state_dir=state_dir)
        candidates = store.read_jsonl("candidates")
        # The seeded candidate is genuinely above threshold (strength 0.84).
        cand = next(c for c in candidates if c["id"] == candidate_id)
        assert cand["pressure"] >= DEFAULT_DISPATCH_PRESSURE_THRESHOLD

        # Board is clean: nothing represented yet.
        plan = plan_candidate_reconciliation(candidates, represented_candidate_ids=set())
        minted_ids = [m["candidate_id"] for m in plan["mint"]]
        assert candidate_id in minted_ids
        mint = next(m for m in plan["mint"] if m["candidate_id"] == candidate_id)
        assert mint["kind"] == "hindsight_pressure"
        assert mint["idempotency_key"] == f"sensorium:intake:candidate:{candidate_id}"
        assert plan["settle"] == []

    def test_planner_coalesces_when_candidate_already_represented(self, state_dir):
        """Once an intake is open for the candidate, the planner must not mint a
        second one -- it coalesces into a skip."""
        event = self._hindsight_event(
            eid="evt_rep", summary="pending=210", fingerprint="hp_rep",
        )
        ingest = json.loads(
            handle_sensorium_ingest_event(event=event, instance="test", state_dir=state_dir)
        )
        candidate_id = ingest["data"]["candidate_id"]
        store = SensoriumStore(instance="test", state_dir=state_dir)
        candidates = store.read_jsonl("candidates")

        plan = plan_candidate_reconciliation(
            candidates, represented_candidate_ids={candidate_id}
        )
        assert plan["mint"] == []
        assert any(s["candidate_id"] == candidate_id for s in plan["skip"])

    def test_planner_settles_non_routable_feedback_self_loop(self):
        candidates = [self._feedback_self_loop_candidate()]
        plan = plan_candidate_reconciliation(candidates, represented_candidate_ids=set())
        assert plan["mint"] == []
        assert len(plan["settle"]) == 1
        assert plan["settle"][0]["candidate_id"] == "cand_selfloop"
        assert "feedback_self_loop" in plan["settle"][0]["reason"]

    def test_planner_ignores_below_threshold_candidate(self):
        candidates = [
            {"id": "low", "status": "candidate", "kind": "hindsight_pressure", "pressure": 0.10},
        ]
        plan = plan_candidate_reconciliation(candidates, represented_candidate_ids=set())
        assert plan["mint"] == []
        assert plan["settle"] == []
        assert plan["active_count"] == 0

    def test_planner_is_bounded_and_reports_truncation(self):
        candidates = [
            {"id": f"cand_{i}", "status": "candidate", "kind": "hindsight_pressure", "pressure": 0.8}
            for i in range(30)
        ]
        plan = plan_candidate_reconciliation(
            candidates, represented_candidate_ids=set(), max_intakes=25
        )
        assert len(plan["mint"]) == 25
        # Overflow is surfaced, never silently dropped.
        assert plan["truncated"] == 5
        assert plan["active_count"] == 30

    def test_represented_candidate_ids_tracks_open_intakes_only(self):
        reconciled = {
            "cand_open": {"intake_task_id": "task_1", "decision": "intake"},
            "cand_archived": {"intake_task_id": "task_2", "decision": "intake"},
            "cand_settled": {"decision": "settled_drop"},
        }
        # Only task_1 is still open on the board.
        rep = represented_candidate_ids(reconciled, {"task_1"})
        assert rep == {"cand_open"}
        # If an intake was archived without settling its candidate, that
        # candidate is NOT represented and must be re-mirrored next tick.
        assert "cand_archived" not in rep
        # Settle-drop entries never count as represented.
        assert "cand_settled" not in rep

    def test_end_to_end_stale_candidate_minted_then_drops_out_after_settlement(self, state_dir):
        """Full invariant: stale above-threshold hindsight_pressure candidate is
        planned for intake while active; once settled it leaves the active pool
        and the planner no longer surfaces it."""
        event = self._hindsight_event(
            eid="evt_e2e", summary="pending=300 critical", fingerprint="hp_e2e",
        )
        ingest = json.loads(
            handle_sensorium_ingest_event(event=event, instance="test", state_dir=state_dir)
        )
        candidate_id = ingest["data"]["candidate_id"]
        store = SensoriumStore(instance="test", state_dir=state_dir)

        # Stage 1: active + board-clean -> planner mints an intake (not silent).
        plan = plan_candidate_reconciliation(
            store.read_jsonl("candidates"), represented_candidate_ids=set()
        )
        assert candidate_id in [m["candidate_id"] for m in plan["mint"]]

        # Stage 2: a Subconscious review settles the candidate (DROP) via the
        # bridge. It now leaves the active promotion pool.
        settlement = apply_kanban_settlement(
            store,
            decision="DROP",
            candidate_id=candidate_id,
            reason="Subconscious DROP after reconciliation intake review.",
        )
        assert settlement["action"] == "settled"

        # Stage 3: planner no longer surfaces it, and the dispatcher dry-run sees
        # no candidate -- the split-brain pending activation is closed.
        plan_after = plan_candidate_reconciliation(
            store.read_jsonl("candidates"), represented_candidate_ids=set()
        )
        assert candidate_id not in [m["candidate_id"] for m in plan_after["mint"]]
        assert candidate_id not in [s["candidate_id"] for s in plan_after["settle"]]
        assert dispatch_once(store, dry_run=True)["action"] == "no_candidate"


class TestRuntimeKanbanBridgeIntakeRows:
    """Runtime bridge intake rows must be blocked and owned by Subconscious.

    Canonical runtime path: ``~/.hermes/scripts/sensorium_kanban_sensor_tick.py``
    is invoked by the quiet no-agent cron wrapper. Rollout hazard: the repo
    ``live-scripts/sensorium_kanban_sensor_tick.py`` copy is mapped onto that
    runtime script by ``scripts/sensorium_plugin_rollout.py``. Exercise both
    paths when available so a later rollout cannot reintroduce ready/unassigned
    substrate intakes.
    """

    @staticmethod
    def _load_bridge(path: Path):
        plugin_root = Path(__file__).resolve().parents[1]
        if str(plugin_root) not in sys.path:
            sys.path.insert(0, str(plugin_root))
        spec = importlib.util.spec_from_file_location(
            f"sensorium_kanban_sensor_tick_{path.stem}_{abs(hash(path))}", path
        )
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def _flag_value(cmd: list[str], flag: str) -> str:
        idx = cmd.index(flag)
        return cmd[idx + 1]

    def _assert_substrate_create_cmd(
        self,
        cmd: list[str],
        *,
        created_by: str,
    ) -> None:
        assert cmd[:5] == ["hermes", "kanban", "--board", "sensorium", "create"]
        assert self._flag_value(cmd, "--initial-status") == "blocked"
        assert self._flag_value(cmd, "--created-by") == created_by
        assert "--assignee" not in cmd
        assert "--triage" not in cmd

    def _assert_sticky_block_then_assign(
        self, calls: list[list[str]], start: int, expected_profile: str
    ) -> None:
        assert calls[start][:5] == ["hermes", "kanban", "--board", "sensorium", "block"]
        assert calls[start + 1][:5] == ["hermes", "kanban", "--board", "sensorium", "assign"]
        # Assert the intake is assigned to the bridge's configured reviewer
        # profile, whatever it is — a generic invariant that holds for the
        # generic repo default and for any deployment-configured runtime copy.
        assert calls[start + 1][-1] == expected_profile

    @pytest.mark.parametrize("label,path", _bridge_intake_script_paths())
    def test_event_and_reconciliation_intakes_are_blocked_assigned(
        self,
        label: str,
        path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        assert path.exists(), label
        bridge = self._load_bridge(path)
        calls: list[list[str]] = []

        def fake_run_checked(cmd: list[str], *, timeout: int = 90):
            calls.append(cmd)
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout=json.dumps({"id": f"t_fake_{len(calls)}"}),
                stderr="",
            )

        monkeypatch.setattr(bridge, "_run_checked", fake_run_checked)

        event_result = bridge._create_intake(
            {
                "id": "evt_blocked_assigned",
                "kind": "hindsight_pressure",
                "summary": "must not be executable substrate",
            }
        )
        candidate_result = bridge._create_candidate_intake(
            {
                "id": "cand_blocked_assigned",
                "kind": "hindsight_pressure",
                "summary": "stale candidate must not be executable substrate",
            }
        )

        assert event_result["create_result"]["id"] == "t_fake_1"
        assert candidate_result["create_result"]["id"] == "t_fake_4"
        self._assert_substrate_create_cmd(
            calls[0], created_by="sensorium-kanban-sensor"
        )
        self._assert_sticky_block_then_assign(calls, 1, bridge.PROFILE)
        self._assert_substrate_create_cmd(
            calls[3], created_by="sensorium-kanban-reconcile"
        )
        self._assert_sticky_block_then_assign(calls, 4, bridge.PROFILE)

    @pytest.mark.parametrize("label,path", _bridge_intake_script_paths())
    def test_intake_assignee_is_a_real_hermes_profile_no_ghost(
        self,
        label: str,
        path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Regression: no `subconscious_worker` ghost assignee in any intake path.

        The bridge must default to a profile that the Hermes dispatcher
        actually knows about (currently ``serasubconscious``) so newly minted
        `sensor:intake:*` rows are immediately claimable. Override via
        ``SENSORIUM_SUBCONSCIOUS_PROFILE`` is honored, but the hardcoded
        fallback must never reintroduce the legacy ghost name.
        """
        assert path.exists(), label
        bridge = self._load_bridge(path)

        # 1. The module's default profile constant must not be the ghost.
        assert bridge.PROFILE != "subconscious_worker", (
            f"{label}: bridge.PROFILE still defaults to the ghost 'subconscious_worker'; "
            "update the script default to a real Hermes profile (e.g. 'serasubconscious')."
        )

        # 2. Drive both intake paths and assert no emitted command argument
        #    ever carries the ghost name — neither as the assignee nor as a
        #    free-standing token that could leak into the body or metadata.
        calls: list[list[str]] = []

        def fake_run_checked(cmd: list[str], *, timeout: int = 90):
            calls.append(cmd)
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout=json.dumps({"id": f"t_fake_{len(calls)}"}),
                stderr="",
            )

        monkeypatch.setattr(bridge, "_run_checked", fake_run_checked)

        bridge._create_intake(
            {
                "id": "evt_ghost_check",
                "kind": "hindsight_pressure",
                "summary": "intake must not use ghost assignee",
            }
        )
        bridge._create_candidate_intake(
            {
                "id": "cand_ghost_check",
                "kind": "hindsight_pressure",
                "summary": "candidate intake must not use ghost assignee",
            }
        )

        ghost = "subconscious_worker"
        for idx, cmd in enumerate(calls, start=1):
            assert ghost not in cmd, (
                f"{label}: call #{idx} carries ghost assignee {ghost!r} -> {cmd!r}"
            )
        # And the sticky-block-then-assign path used by both intake creators
        # must point at the bridge's configured profile, never the ghost.
        assert bridge.PROFILE != ghost

    @pytest.mark.parametrize("label,path", _bridge_intake_script_paths())
    def test_blocked_conscious_backlog_does_not_starve_subconscious_review(
        self,
        label: str,
        path: Path,
    ):
        """Blocked Conscious furniture must not suppress Subconscious intake review.

        Regression: the runtime bridge counted blocked ``conscious:review:*``
        rows as active attention heads. A few intentional holds then prevented
        creation of any ``subconscious:review:*`` batch, leaving fresh
        ``sensor:intake:*`` rows sticky-blocked forever.
        """
        assert path.exists(), label
        bridge = self._load_bridge(path)

        blocked = {
            "id": "t_blocked_conscious",
            "title": "conscious:review: parked old attention",
            "status": "blocked",
        }
        todo = {
            "id": "t_todo_conscious",
            "title": "conscious:review: dependency-gated child",
            "status": "todo",
        }
        ready = {
            "id": "t_ready_conscious",
            "title": "conscious:review: live attention",
            "status": "ready",
        }

        assert bridge._active_conscious([blocked, todo]) == []
        assert bridge._active_conscious([ready]) == [ready]


class TestLiveKanbanBridgeReviewedOpenCleanup:
    def _open_candidate_intake(self, *, status="ready", comment="decision: DROP") -> dict:
        payload = {
            "candidate_id": "cand_open_reviewed",
            "event_ids": ["evt_open_reviewed"],
            "fingerprint": "fp_open_reviewed",
            "correlation_keys": ["open-reviewed"],
            "kind": "tts_sidecar_pressure",
            "pressure": 0.86,
            "summary": "TTS sidecar timeout pressure already reviewed",
        }
        return {
            "id": "kt_open_reviewed",
            "title": "sensor:intake:tts_sidecar_pressure: timeout pressure",
            "status": status,
            "body": (
                "Sensorium Kanban reconciliation intake v1.\n\n"
                "Compact candidate payload:\n"
                + json.dumps(payload, indent=2, sort_keys=True)
            ),
            "comments": [{"body": comment}] if comment else [],
            "runs": [],
            "events": [],
        }

    def test_post_review_settle_open_intakes_applies_and_returns_cleanup_ids(self, store):
        bridge = _load_live_bridge_module()
        store.append_jsonl(
            "candidates",
            {
                "id": "cand_open_reviewed",
                "status": "candidate",
                "kind": "tts_sidecar_pressure",
                "pressure": 0.86,
                "summary": "TTS sidecar timeout pressure already reviewed",
                "event_ids": ["evt_open_reviewed"],
                "correlation_keys": ["open-reviewed"],
                "fingerprint": "fp_open_reviewed",
            },
        )

        result = bridge._post_review_settle_open_intakes(
            store,
            [self._open_candidate_intake()],
        )

        assert result["gaps"] == []
        assert result["cleanup_task_ids"] == ["kt_open_reviewed"]
        assert result["applied"] == [
            {
                "intake_task_id": "kt_open_reviewed",
                "candidate_id": "cand_open_reviewed",
                "decision": "DROP",
                "settlement_action": "settled",
                "updated_candidate_ids": ["cand_open_reviewed"],
                "already_settled_candidate_ids": [],
            }
        ]
        assert dispatch_once(store, dry_run=True)["action"] == "no_candidate"

    def test_completed_intake_recovery_applies_once_per_active_candidate(self, store, monkeypatch):
        bridge = _load_live_bridge_module()
        store.append_jsonl(
            "candidates",
            {
                "id": "cand_open_reviewed",
                "status": "candidate",
                "kind": "tts_sidecar_pressure",
                "pressure": 0.86,
                "summary": "TTS sidecar timeout pressure already reviewed",
                "event_ids": ["evt_open_reviewed"],
                "correlation_keys": ["open-reviewed"],
                "fingerprint": "fp_open_reviewed",
            },
        )
        first = self._open_candidate_intake(status="archived", comment="decision: DROP")
        second = self._open_candidate_intake(status="archived", comment="decision: PROMOTE_CONSCIOUS")
        second["id"] = "kt_open_reviewed_duplicate"
        details = {first["id"]: first, second["id"]: second}
        monkeypatch.setattr(bridge, "_show_task", lambda task_id: details[task_id])

        result = bridge._post_review_settle_completed_intakes(store, [first, second])

        assert [a["intake_task_id"] for a in result["applied"]] == ["kt_open_reviewed"]
        candidate = store.read_jsonl("candidates")[0]
        assert candidate["status"] == "suppressed"
        assert candidate["kanban_settlement"]["decision"] == "DROP"

    def test_cleanup_settled_open_intakes_comments_and_archives_each_task(self, monkeypatch):
        bridge = _load_live_bridge_module()
        calls = []

        def fake_run(cmd, *, timeout=90):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(bridge, "_run", fake_run)

        result = bridge._cleanup_settled_open_intakes(["kt_a", "kt_b"])

        assert result == {"cleaned": ["kt_a", "kt_b"], "errors": []}
        assert [cmd[4] for cmd in calls] == ["comment", "archive", "comment", "archive"]
        assert calls[0][5] == "kt_a"
        assert calls[2][5] == "kt_b"

    def test_list_tasks_include_archived_adds_cli_flag(self, monkeypatch):
        bridge = _load_live_bridge_module()
        calls = []

        def fake_run_checked(cmd, *, timeout=90):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")

        monkeypatch.setattr(bridge, "_run_checked", fake_run_checked)

        assert bridge._list_tasks(include_archived=True) == []
        assert "--archived" in calls[0]

    def test_inactive_candidate_intake_cleanup_selects_open_rows_whose_candidate_left_pool(self, store):
        bridge = _load_live_bridge_module()
        store.append_jsonl(
            "candidates",
            {
                "id": "cand_open_reviewed",
                "status": "suppressed",
                "kind": "tts_sidecar_pressure",
                "pressure": 0.86,
                "summary": "settled candidate",
            },
        )

        result = bridge._inactive_candidate_open_intake_cleanup(
            store,
            [self._open_candidate_intake(status="ready", comment="")],
        )

        assert result == {
            "cleanup_task_ids": ["kt_open_reviewed"],
            "items": [
                {
                    "intake_task_id": "kt_open_reviewed",
                    "candidate_id": "cand_open_reviewed",
                    "candidate_status": "suppressed",
                    "reason": "underlying_candidate_not_active",
                }
            ],
        }

    def test_review_body_requires_candidate_id_and_truthful_no_match_status(self):
        bridge = _load_live_bridge_module()

        body = bridge._review_body([
            {
                "id": "kt_open_reviewed",
                "status": "ready",
                "title": "sensor:intake:tts_sidecar_pressure: timeout pressure",
                "assignee": None,
            }
        ])

        assert "candidate_id is mandatory" in body
        assert "no_candidate_match > 0 as unresolved" in body
        assert "complete/archive every intake task" in body


class TestActiveSessionEventCandidateDeduplication:
    """Regression: active-session event + pre-existing active candidate.

    An active-session/manual ingest promotes an event into a candidate *before*
    the bridge tick runs, so the tick's deterministic sensor result carries no
    event->candidate mapping. The event mirror still creates one intake; the
    bug was that the bridge could not mark that candidate represented, so the
    reconciliation pass minted a *second* candidate-mirror intake for the same
    activation. The fix resolves the mapping from the canonical candidates store
    (`candidate.event_ids`). Exactly one intake must be created.
    """

    def _event(self) -> dict:
        return {
            "id": "evt_active_session",
            "ts": "2026-05-28T10:00:00Z",
            "type": "sensor.event.promoted",
            "kind": "hindsight_pressure",
            "summary": "active-session pressure already promoted to candidate",
            "strength": 0.9,
            "source_signal_ids": ["sig_active_session"],
            "correlation_keys": ["active-session"],
            "sensitivity": "private",
            "allowed_surfaces": ["local"],
            "fingerprint": "fp_active_session",
        }

    def test_event_id_to_candidate_id_recovers_active_session_mapping(self):
        bridge = _load_live_bridge_module()
        candidates = [
            # Settled candidate sharing the event id must not shadow the active
            # one: the active above-threshold candidate wins on collision.
            {"id": "cand_settled", "status": "suppressed", "pressure": 0.9, "event_ids": ["evt_x"]},
            {"id": "cand_active", "status": "candidate", "pressure": 0.9, "event_ids": ["evt_x", "evt_y"]},
            {"id": "cand_low", "status": "candidate", "pressure": 0.1, "event_ids": ["evt_z"]},
        ]
        index = bridge._event_id_to_candidate_id(candidates)
        assert index["evt_x"] == "cand_active"
        assert index["evt_y"] == "cand_active"
        assert index["evt_z"] == "cand_low"

    def test_active_session_event_does_not_double_mint_with_reconciliation(
        self, tmp_path, monkeypatch
    ):
        bridge = _load_live_bridge_module()
        event = self._event()
        eid = event["id"]
        cand_id = "cand_active_session"

        # Canonical Sensorium store already holds the active above-threshold
        # candidate an earlier active-session ingest promoted from this event.
        # It is NOT in this tick's deterministic sensor result.
        state_dir = str(tmp_path / "sensorium")
        store = SensoriumStore(instance="test", state_dir=state_dir)
        store.ensure_dirs()
        store.append_jsonl(
            "candidates",
            {
                "id": cand_id,
                "status": "candidate",
                "kind": "hindsight_pressure",
                "pressure": 0.9,
                "summary": event["summary"],
                "event_ids": [eid],
                "correlation_keys": ["active-session"],
                "fingerprint": "fp_active_session",
            },
        )

        # Bind every SensoriumStore() the bridge constructs to the tmp store and
        # redirect bridge state files into tmp.
        monkeypatch.setattr(
            bridge,
            "SensoriumStore",
            lambda instance=None: SensoriumStore(instance="test", state_dir=state_dir),
        )
        state_path = tmp_path / "sensor_kanban_state.json"
        state_path.write_text(json.dumps({"initialized": True}), encoding="utf-8")
        monkeypatch.setattr(bridge, "STATE_PATH", state_path)
        monkeypatch.setattr(bridge, "LATEST_PATH", tmp_path / "latest.json")
        monkeypatch.setattr(bridge, "SUBCONSCIOUS_STATE_PATH", tmp_path / "subconscious.json")

        # No fresh deterministic ingest this tick (active-session already did it).
        monkeypatch.setattr(bridge, "_ensure_board", lambda: None)
        monkeypatch.setattr(bridge, "_run_det_sensors", lambda: {"exit_code": 0, "result": {}})
        monkeypatch.setattr(bridge, "_load_events", lambda: [event])
        monkeypatch.setattr(bridge, "run_improvement_collector", lambda *a, **k: {})

        event_intake_task_id = "t_event_intake"
        listed_tasks = [
            {
                "id": event_intake_task_id,
                "title": f"sensor:intake:{event['kind']}: {event['summary'][:90]}",
                "status": "blocked",
                "assignee": bridge.PROFILE,
            }
        ]
        monkeypatch.setattr(
            bridge, "_list_tasks", lambda *, include_archived=False: [dict(t) for t in listed_tasks]
        )
        monkeypatch.setattr(
            bridge,
            "_show_task",
            lambda task_id: {
                "id": task_id,
                "title": listed_tasks[0]["title"],
                "status": "blocked",
                "body": bridge._compact_event_body(event),
                "comments": [],
                "runs": [],
                "events": [],
            },
        )

        create_calls: list[list[str]] = []

        def fake_run_checked(cmd, *, timeout=90):
            if "create" in cmd:
                create_calls.append(cmd)
                created_by = (
                    cmd[cmd.index("--created-by") + 1] if "--created-by" in cmd else ""
                )
                task_id = (
                    event_intake_task_id
                    if created_by == "sensorium-kanban-sensor"
                    else "t_other"
                )
                return subprocess.CompletedProcess(cmd, 0, json.dumps({"id": task_id}), "")
            return subprocess.CompletedProcess(cmd, 0, "{}", "")

        monkeypatch.setattr(bridge, "_run_checked", fake_run_checked)
        monkeypatch.setattr(
            bridge,
            "_run",
            lambda cmd, *, timeout=90: subprocess.CompletedProcess(cmd, 0, "", ""),
        )
        monkeypatch.setattr(sys, "argv", ["sensorium_kanban_sensor_tick.py"])

        assert bridge.main() == 0

        result = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
        assert result["success"] is True

        # Exactly one intake for this activation: the event mirror. No
        # reconciliation candidate-mirror intake for the same candidate.
        assert len(result["created_intakes"]) == 1
        only = result["created_intakes"][0]
        assert only.get("event_id") == eid
        assert "candidate_id" not in only  # not a reconciliation intake
        assert result["reconcile"]["minted"] == []

        # The reconciliation candidate-intake creator was never invoked.
        reconcile_creates = [
            c
            for c in create_calls
            if "--created-by" in c
            and c[c.index("--created-by") + 1] == "sensorium-kanban-reconcile"
        ]
        assert reconcile_creates == []

    def test_genuinely_unmirrored_candidate_still_reconciled(
        self, tmp_path, monkeypatch
    ):
        """Stale-candidate reconciliation must survive: an active above-threshold
        candidate with NO event this tick and NO open intake is still minted."""
        bridge = _load_live_bridge_module()
        state_dir = str(tmp_path / "sensorium")
        store = SensoriumStore(instance="test", state_dir=state_dir)
        store.ensure_dirs()
        store.append_jsonl(
            "candidates",
            {
                "id": "cand_stale",
                "status": "candidate",
                "kind": "hindsight_pressure",
                "pressure": 0.9,
                "summary": "stale above-threshold candidate, no fresh event",
                "event_ids": ["evt_stale_unmirrored"],
                "correlation_keys": ["stale"],
                "fingerprint": "fp_stale",
            },
        )
        monkeypatch.setattr(
            bridge,
            "SensoriumStore",
            lambda instance=None: SensoriumStore(instance="test", state_dir=state_dir),
        )
        state_path = tmp_path / "sensor_kanban_state.json"
        state_path.write_text(json.dumps({"initialized": True}), encoding="utf-8")
        monkeypatch.setattr(bridge, "STATE_PATH", state_path)
        monkeypatch.setattr(bridge, "LATEST_PATH", tmp_path / "latest.json")
        monkeypatch.setattr(bridge, "SUBCONSCIOUS_STATE_PATH", tmp_path / "subconscious.json")
        monkeypatch.setattr(bridge, "_ensure_board", lambda: None)
        monkeypatch.setattr(bridge, "_run_det_sensors", lambda: {"exit_code": 0, "result": {}})
        monkeypatch.setattr(bridge, "_load_events", lambda: [])  # no events this tick
        monkeypatch.setattr(bridge, "run_improvement_collector", lambda *a, **k: {})
        # Board starts empty; after reconcile mints, refresh returns the new row.
        reconcile_task_id = "t_reconcile_intake"
        board_state = {"tasks": []}

        def fake_list_tasks(*, include_archived=False):
            return [dict(t) for t in board_state["tasks"]]

        monkeypatch.setattr(bridge, "_list_tasks", fake_list_tasks)
        monkeypatch.setattr(
            bridge,
            "_show_task",
            lambda task_id: {
                "id": task_id, "title": "sensor:intake:hindsight_pressure: stale",
                "status": "blocked", "body": "", "comments": [], "runs": [], "events": [],
            },
        )

        def fake_run_checked(cmd, *, timeout=90):
            if "create" in cmd:
                created_by = (
                    cmd[cmd.index("--created-by") + 1] if "--created-by" in cmd else ""
                )
                if created_by == "sensorium-kanban-reconcile":
                    board_state["tasks"].append(
                        {
                            "id": reconcile_task_id,
                            "title": "sensor:intake:hindsight_pressure: stale",
                            "status": "blocked",
                            "assignee": bridge.PROFILE,
                        }
                    )
                    return subprocess.CompletedProcess(
                        cmd, 0, json.dumps({"id": reconcile_task_id}), ""
                    )
                return subprocess.CompletedProcess(cmd, 0, json.dumps({"id": "t_other"}), "")
            return subprocess.CompletedProcess(cmd, 0, "{}", "")

        monkeypatch.setattr(bridge, "_run_checked", fake_run_checked)
        monkeypatch.setattr(
            bridge, "_run", lambda cmd, *, timeout=90: subprocess.CompletedProcess(cmd, 0, "", "")
        )
        monkeypatch.setattr(sys, "argv", ["sensorium_kanban_sensor_tick.py"])

        assert bridge.main() == 0
        result = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
        assert result["success"] is True
        # Stale candidate with no event mirror is still reconciled into one intake.
        assert [m["candidate_id"] for m in result["reconcile"]["minted"]] == ["cand_stale"]
        assert len(result["created_intakes"]) == 1
