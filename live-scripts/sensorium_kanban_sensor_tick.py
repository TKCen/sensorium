#!/usr/bin/env python3
"""Sensorium-on-Kanban no-agent sensor tick.

Dumb fixture layer:
- runs deterministic Agent Sensorium sensors without internal dispatch/advisory;
- mirrors new promoted Events into the `sensorium` Kanban board as blocked
  `sensor:intake:*` tasks;
- creates at most one ready `subconscious:review:*` task assigned to the cheap
  `serasubconscious` profile when unresolved intake exists and no review is
  already active.

Healthy/idle path prints nothing. Use --json for inspection or --force-canary
for an end-to-end canary intake.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

HOME = Path.home()
INSTANCE = "sera"
BOARD = "sensorium"
PROFILE = "serasubconscious"
PLUGIN = HOME / ".hermes" / "plugins" / "agent-sensorium"
if str(PLUGIN) not in sys.path:
    sys.path.insert(0, str(PLUGIN))
from agent_sensorium.settlement import (  # noqa: E402
    DEFAULT_DISPATCH_PRESSURE_THRESHOLD,
    CLOSED_INTAKE_STATUSES,
    apply_kanban_settlement,
    apply_settlement_record,
    candidate_intake_idempotency_key,
    coalesce_suppression_reason as _coalesced_suppression_reason,
    event_incident_key as _event_incident_key,
    extract_kanban_intake_payload,
    plan_candidate_reconciliation,
    plan_completed_intake_settlements,
    represented_candidate_ids as _represented_candidate_ids,
    select_active_above_threshold,
)
from agent_sensorium.improvement import run_improvement_collector  # noqa: E402
from agent_sensorium.store import SensoriumStore  # noqa: E402

STATE_DIR = HOME / ".hermes" / "agent-sensorium" / "kanban"
STATE_DIR.mkdir(parents=True, exist_ok=True)
STATE_PATH = STATE_DIR / "sensor_kanban_state.json"
SUBCONSCIOUS_STATE_PATH = STATE_DIR / "subconscious_state.json"
LATEST_PATH = STATE_DIR / "last_sensorium_kanban_tick.json"
EVENTS_PATH = HOME / ".hermes" / "agent-sensorium" / INSTANCE / "events.jsonl"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True), encoding="utf-8")


def _run(cmd: list[str], *, timeout: int = 90) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.setdefault("HERMES_KANBAN_BOARD", BOARD)
    return subprocess.run(cmd, text=True, capture_output=True, timeout=timeout, env=env)


def _run_checked(cmd: list[str], *, timeout: int = 90) -> subprocess.CompletedProcess[str]:
    proc = _run(cmd, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(
            f"command failed rc={proc.returncode}: {' '.join(cmd)}\n"
            f"stdout={proc.stdout[-2000:]}\nstderr={proc.stderr[-2000:]}"
        )
    return proc


def _ensure_board() -> None:
    # `boards create` is not idempotent, so use list JSON-free and create only when absent.
    proc = _run(["hermes", "kanban", "boards", "list"], timeout=60)
    if proc.returncode == 0 and BOARD in proc.stdout:
        return
    create = _run(
        [
            "hermes",
            "kanban",
            "boards",
            "create",
            BOARD,
            "--name",
            "Sensorium",
            "--description",
            "Sera Sensorium task substrate: sensor intake, Subconscious review, Conscious attention heads.",
            "--icon",
            "◌",
            "--color",
            "#8b5cf6",
            "--default-workdir",
            str(STATE_DIR / "workspaces"),
        ],
        timeout=60,
    )
    if create.returncode != 0 and "already" not in (create.stderr + create.stdout).lower():
        raise RuntimeError(create.stderr or create.stdout)


def _run_det_sensors() -> dict[str, Any]:
    cmd = [
        sys.executable,
        str(PLUGIN / "scripts" / "sensorium_tick.py"),
        "--instance",
        INSTANCE,
        "--all-sensors",
        "--json",
    ]
    proc = subprocess.run(cmd, cwd=str(PLUGIN), text=True, capture_output=True, timeout=90)
    record = {
        "cmd": cmd,
        "exit_code": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "sensorium_tick failed")
    try:
        record["result"] = json.loads(proc.stdout)
    except Exception:
        record["result_parse_error"] = True
    return record


def _load_events() -> list[dict[str, Any]]:
    if not EVENTS_PATH.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in EVENTS_PATH.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict) and obj.get("id"):
            out.append(obj)
    return out


def _list_tasks() -> list[dict[str, Any]]:
    proc = _run_checked(["hermes", "kanban", "--board", BOARD, "list", "--json"], timeout=60)
    data = json.loads(proc.stdout or "[]")
    return data if isinstance(data, list) else []


def _show_task(task_id: str) -> dict[str, Any]:
    proc = _run_checked(["hermes", "kanban", "--board", BOARD, "show", task_id, "--json"], timeout=60)
    data = json.loads(proc.stdout or "{}")
    if not isinstance(data, dict):
        return {}
    task = dict(data.get("task") or {})
    task["comments"] = data.get("comments") or []
    task["runs"] = data.get("runs") or []
    task["events"] = data.get("events") or []
    return task


def _task_title(t: dict[str, Any]) -> str:
    return str(t.get("title") or t.get("name") or "")


def _task_status(t: dict[str, Any]) -> str:
    return str(t.get("status") or "")


def _open_sensor_intakes(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    closed = {"done", "archived", "completed"}
    return [
        t
        for t in tasks
        if _task_title(t).startswith("sensor:intake:") and _task_status(t) not in closed
    ]


def _closed_sensor_intakes(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        t
        for t in tasks
        if _task_title(t).startswith("sensor:intake:")
        and _task_status(t) in CLOSED_INTAKE_STATUSES
    ]


def _post_review_settle_completed_intakes(
    store: "SensoriumStore",
    tasks: list[dict[str, Any]],
) -> dict[str, Any]:
    """Apply/flag settlements claimed by completed intake tasks.

    This is the enforcement backstop for the cheap review lane: if
    serasubconscious comments/completes an intake as DROP/SAVE/PROMOTE but fails
    to run the settlement CLI, the next bridge tick reads the completed intake,
    applies the canonical Sensorium settlement, and records the result. If the
    intake is closed without an explicit decision, the gap is surfaced in latest
    state rather than remaining invisible.
    """
    candidates = store.read_jsonl("candidates")
    active = select_active_above_threshold(
        candidates,
        threshold=DEFAULT_DISPATCH_PRESSURE_THRESHOLD,
    )
    active_ids = {str(c.get("id") or "") for c in active if c.get("id")}
    active_event_ids = {
        str(eid)
        for candidate in active
        for eid in (candidate.get("event_ids") or [])
        if eid
    }
    if not active_ids and not active_event_ids:
        return {"records": [], "applied": [], "gaps": [], "already_settled": [], "inspected": []}

    candidate_ids_to_show: list[str] = []
    for task in _closed_sensor_intakes(tasks):
        payload = extract_kanban_intake_payload(str(task.get("body") or ""))
        cand_id = str(payload.get("candidate_id") or "")
        event_id = str(payload.get("event_id") or "")
        event_ids = {str(eid) for eid in (payload.get("event_ids") or []) if eid}
        if cand_id and cand_id in active_ids:
            candidate_ids_to_show.append(str(task.get("id") or ""))
        elif event_id and event_id in active_event_ids:
            candidate_ids_to_show.append(str(task.get("id") or ""))
        elif event_ids & active_event_ids:
            candidate_ids_to_show.append(str(task.get("id") or ""))
    seen: set[str] = set()
    details: list[dict[str, Any]] = []
    for task_id in candidate_ids_to_show:
        if not task_id or task_id in seen:
            continue
        seen.add(task_id)
        detail = _show_task(task_id)
        if detail:
            details.append(detail)

    plan = plan_completed_intake_settlements(
        details,
        decisions=store.read_jsonl("decisions"),
        active_candidate_ids=active_ids,
    )
    applied: list[dict[str, Any]] = []
    for record in plan.get("records", []):
        settlement = apply_settlement_record(store, record)
        applied.append(
            {
                "intake_task_id": record.get("intake_task_id"),
                "candidate_id": record.get("candidate_id"),
                "decision": record.get("decision"),
                "settlement_action": settlement.get("action"),
                "updated_candidate_ids": settlement.get("updated_candidate_ids", []),
                "already_settled_candidate_ids": settlement.get("already_settled_candidate_ids", []),
            }
        )

    return {
        "records": plan.get("records", []),
        "applied": applied,
        "gaps": plan.get("gaps", []),
        "already_settled": plan.get("already_settled", []),
        "inspected": sorted(seen),
    }


def _active_reviews(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    active_status = {"ready", "running", "todo", "blocked"}
    return [
        t
        for t in tasks
        if _task_title(t).startswith("subconscious:review:")
        and _task_status(t) in active_status
    ]


def _active_conscious(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    active_status = {"ready", "running", "todo", "blocked"}
    return [
        t
        for t in tasks
        if _task_title(t).startswith("conscious:review:") and _task_status(t) in active_status
    ]


def _compact_event_body(event: dict[str, Any]) -> str:
    payload = {
        "event_id": event.get("id"),
        "ts": event.get("ts"),
        "kind": event.get("kind"),
        "strength": event.get("strength"),
        "summary": event.get("summary"),
        "source_signal_ids": event.get("source_signal_ids", []),
        "correlation_keys": event.get("correlation_keys", []),
        "sensitivity": event.get("sensitivity"),
        "allowed_surfaces": event.get("allowed_surfaces", []),
        "fingerprint": event.get("fingerprint"),
    }
    return (
        "Sensorium Kanban intake v1. This is substrate, not executable user work.\n\n"
        "Status semantics: this task is intentionally blocked/unassigned so the gateway "
        "dispatcher will not run it directly. The serasubconscious review task reads and "
        "settles these intakes.\n\n"
        "Compact event payload:\n"
        + json.dumps(payload, indent=2, sort_keys=True)
        + "\n\nExpected settlement: Subconscious comments DROP/SAVE/PROMOTE evidence, "
        "then archives/completes this intake or links it to a conscious review task."
    )


def _create_intake(event: dict[str, Any]) -> dict[str, Any]:
    kind = str(event.get("kind") or "event")[:80]
    eid = str(event.get("id"))
    title = f"sensor:intake:{kind}: {str(event.get('summary') or eid)[:90]}"
    key = f"sensorium:intake:{eid}"
    body = _compact_event_body(event)
    cmd = [
        "hermes",
        "kanban",
        "--board",
        BOARD,
        "create",
        title,
        "--body",
        body,
        "--initial-status",
        "blocked",
        "--idempotency-key",
        key,
        "--created-by",
        "sensorium-kanban-sensor",
        "--json",
    ]
    proc = _run_checked(cmd, timeout=90)
    try:
        data = json.loads(proc.stdout or "{}")
    except Exception:
        data = {"raw": proc.stdout}
    return {"event_id": eid, "title": title, "idempotency_key": key, "create_result": data}


def _candidate_intake_body(candidate: dict[str, Any]) -> str:
    payload = {
        "candidate_id": candidate.get("id"),
        "kind": candidate.get("kind"),
        "pressure": candidate.get("pressure"),
        "summary": candidate.get("summary"),
        "event_ids": list(candidate.get("event_ids") or []),
        "correlation_keys": list(candidate.get("correlation_keys") or []),
        "fingerprint": candidate.get("fingerprint"),
        "sensitivity": candidate.get("sensitivity"),
        "allowed_surfaces": list(candidate.get("allowed_surfaces") or []),
    }
    if isinstance(candidate.get("conscious_task"), dict):
        payload["conscious_task"] = candidate.get("conscious_task")
    if isinstance(candidate.get("improvement_meta"), dict):
        payload["improvement_meta"] = candidate.get("improvement_meta")
    return (
        "Sensorium Kanban reconciliation intake v1. This is substrate, not "
        "executable user work.\n\n"
        "Why this exists: this candidate is already active and above the dispatch "
        "pressure threshold in Sensorium, but no fresh event emitted this tick, so "
        "the event-driven bridge never mirrored it. The reconciliation pass mirrors "
        "it now so an above-threshold candidate cannot remain a quiet pending "
        "activation invisible to Kanban.\n\n"
        "Status semantics: this task is intentionally blocked/unassigned so the "
        "gateway dispatcher will not run it directly. The serasubconscious review "
        "task reads and settles these intakes.\n\n"
        "Compact candidate payload:\n"
        + json.dumps(payload, indent=2, sort_keys=True)
        + "\n\nExpected settlement: Subconscious comments DROP/SAVE/PROMOTE evidence, "
        "then settles the underlying candidate via the settlement CLI "
        "(candidate_id above) and archives/completes this intake."
    )


def _create_candidate_intake(candidate: dict[str, Any]) -> dict[str, Any]:
    cand_id = str(candidate.get("id") or "")
    kind = str(candidate.get("kind") or "candidate")[:80]
    summary = str(candidate.get("summary") or cand_id)
    title = f"sensor:intake:{kind}: {summary[:90]}"
    key = candidate_intake_idempotency_key(candidate)
    body = _candidate_intake_body(candidate)
    cmd = [
        "hermes",
        "kanban",
        "--board",
        BOARD,
        "create",
        title,
        "--body",
        body,
        "--initial-status",
        "blocked",
        "--idempotency-key",
        key,
        "--created-by",
        "sensorium-kanban-reconcile",
        "--json",
    ]
    proc = _run_checked(cmd, timeout=90)
    try:
        data = json.loads(proc.stdout or "{}")
    except Exception:
        data = {"raw": proc.stdout}
    return {
        "candidate_id": cand_id,
        "title": title,
        "idempotency_key": key,
        "create_result": data,
        "reconciled": True,
    }


def _reconcile_active_candidates(
    store: "SensoriumStore",
    *,
    open_intakes: list[dict[str, Any]],
    reconciled_candidates: dict[str, Any],
) -> dict[str, Any]:
    """Mirror or settle stale active above-threshold candidates.

    Bounded, idempotent, coalescing pass that runs after normal event
    processing. Any active candidate at/above the dispatch threshold that is not
    already represented by an open intake is either minted into a Kanban intake
    (routable kinds, e.g. ``hindsight_pressure``) or DROP-settled with a receipt
    (non-routable kinds). Returns a structured summary plus the intake task dicts
    it created so the caller can fold them into the review batch.
    """
    candidates = store.read_jsonl("candidates")
    by_id = {str(c.get("id") or ""): c for c in candidates}

    open_intake_ids = {str(t.get("id")) for t in open_intakes if t.get("id")}
    represented = _represented_candidate_ids(reconciled_candidates, open_intake_ids)

    plan = plan_candidate_reconciliation(
        candidates,
        threshold=DEFAULT_DISPATCH_PRESSURE_THRESHOLD,
        represented_candidate_ids=represented,
    )

    minted: list[dict[str, Any]] = []
    settled: list[dict[str, Any]] = []
    created_tasks: list[dict[str, Any]] = []

    for item in plan["mint"]:
        candidate = by_id.get(item["candidate_id"])
        if not candidate:
            continue
        created = _create_candidate_intake(candidate)
        created_tasks.append(created)
        task_id = (created.get("create_result") or {}).get("id")
        reconciled_candidates[item["candidate_id"]] = {
            "intake_task_id": task_id,
            "idempotency_key": item["idempotency_key"],
            "decision": "intake",
            "kind": item["kind"],
            "pressure": item.get("pressure"),
            "reconciled_at": _now_iso(),
        }
        minted.append(
            {
                "candidate_id": item["candidate_id"],
                "kind": item["kind"],
                "pressure": item.get("pressure"),
                "intake_task_id": task_id,
                "idempotency_key": item["idempotency_key"],
            }
        )

    for item in plan["settle"]:
        candidate = by_id.get(item["candidate_id"])
        if not candidate:
            continue
        settlement = apply_kanban_settlement(
            store,
            decision="DROP",
            candidate_id=item["candidate_id"],
            fingerprint=str(candidate.get("fingerprint") or ""),
            correlation_keys=list(candidate.get("correlation_keys") or []) or None,
            reason=(
                "Reconciliation: active above-threshold candidate is not "
                f"Kanban-routable ({item['reason']}); settled with receipt "
                "instead of leaving it as a quiet pending activation."
            ),
        )
        reconciled_candidates[item["candidate_id"]] = {
            "decision": "settled_drop",
            "settlement_action": settlement.get("action"),
            "reason": item["reason"],
            "reconciled_at": _now_iso(),
        }
        settled.append(
            {
                "candidate_id": item["candidate_id"],
                "kind": item["kind"],
                "reason": item["reason"],
                "settlement_action": settlement.get("action"),
                "updated_candidate_ids": settlement.get("updated_candidate_ids", []),
            }
        )

    return {
        "minted": minted,
        "settled": settled,
        "skipped": plan["skip"],
        "truncated": plan["truncated"],
        "active_above_threshold_count": plan["active_count"],
        "created_tasks": created_tasks,
    }


def _review_body(open_intakes: list[dict[str, Any]]) -> str:
    rows = [
        {
            "id": t.get("id"),
            "status": t.get("status"),
            "title": _task_title(t),
            "assignee": t.get("assignee"),
        }
        for t in open_intakes[:30]
    ]
    return (
        "You are serasubconscious, Sera's cheap Sensorium Subconscious gate.\n\n"
        "Task: review all currently open Sensorium intake tasks, decide DROP/SAVE/PROMOTE_CONSCIOUS, "
        "settle duplicates/noise, and promote at most one attention head to Conscious Sera if warranted.\n\n"
        f"Board: {BOARD}\n"
        f"State file: {SUBCONSCIOUS_STATE_PATH}\n\n"
        "Open intake tasks at creation time:\n"
        + json.dumps(rows, indent=2, sort_keys=True)
        + "\n\nRules:\n"
        "- Inspect cited intake tasks with `hermes kanban --board sensorium show <id>` if needed.\n"
        "- Also inspect the board list for newer open intakes before deciding.\n"
        "- Do not send messages, schedule crons, start gateways, or create external workers.\n"
        "- For DROP/SAVE: comment concise evidence on intake tasks, archive or complete settled intakes.\n"
        "- For PROMOTE_CONSCIOUS: create exactly one `conscious:review:` task assigned to `default`, preferably with this review as parent, include cited intake IDs and stop condition.\n"
        "- For `attention_policy_review` intakes: this is Sensorium self-improvement evidence, not ordinary noise. Unless an equivalent conscious review is already active, PROMOTE_CONSCIOUS to `default`; Conscious must decide NO_CHANGE, threshold/coalescing tweak proposal, sensor addition task, priority-map change, memory/skill patch, or HOLD, and must record future_tendency_delta, verification_condition, and rollback_condition. Subconscious must not mutate wake behavior directly.\n"
        "- CRITICAL: after deciding, propagate every consumed intake decision back into Sensorium truth by running the settlement CLI once with a JSON list of settlement records:\n"
        "  `python /home/entity/.hermes/plugins/agent-sensorium/scripts/sensorium_kanban_settle.py --instance sera --file /tmp/sensorium_settlements_<review>.json --json`\n"
        "  Record shape: {decision: DROP|SAVE|PROMOTE_CONSCIOUS, event_id, fingerprint, correlation_keys, intake_task_id, review_task_id, conscious_task_ref?, reason}. Use event_id/fingerprint/correlation_keys from each intake's Compact event payload.\n"
        "  This is mandatory so Kanban DROP/SAVE/PROMOTE removes the underlying Sensorium candidate from the active promotion pool; do not rely on Kanban comments alone.\n"
        "- After any DROP/SAVE/PROMOTE decision, comment and complete/archive every intake task you consumed so the board does not accumulate unassigned ready substrate.\n"
        "- Completion summary must include YAML-ish fields: decision, reason, intake_tasks, promoted_task, settlement_cli_result, state_updated.\n"
    )



def _create_review(open_intakes: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not open_intakes:
        return None
    ids = sorted(str(t.get("id")) for t in open_intakes if t.get("id"))
    sig = hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()[:16]
    title = f"subconscious:review:sensorium-batch:{sig}"
    body = _review_body(open_intakes)
    cmd = [
        "hermes",
        "kanban",
        "--board",
        BOARD,
        "create",
        title,
        "--body",
        body,
        "--assignee",
        PROFILE,
        "--workspace",
        f"dir:{STATE_DIR}",
        "--idempotency-key",
        f"sensorium:subconscious-review:{sig}",
        "--max-runtime",
        "20m",
        "--created-by",
        "sensorium-kanban-gate",
        "--skill",
        "autonomous-inner-lifecycle",
        "--json",
    ]
    proc = _run_checked(cmd, timeout=90)
    try:
        data = json.loads(proc.stdout or "{}")
    except Exception:
        data = {"raw": proc.stdout}
    return {"signature": sig, "create_result": data, "intake_ids": ids}


def _force_canary_event(label: str | None = None, *, conscious: bool = False) -> dict[str, Any]:
    ts = _now_iso()
    seed = f"{ts}:{label or 'canary'}:{'conscious' if conscious else 'settle'}"
    h = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
    if conscious:
        kind = "operator_attention_canary"
        summary = (
            "Operator-requested canary: validate full Sensorium-on-Kanban "
            "Subconscious promotion into a Conscious Sera review task. This is "
            "explicit test salience; promote exactly one conscious:review task, "
            "then the Conscious worker should close it as validation-only."
        )
        keys = ["sensorium-kanban", "architecture-canary", "subconscious-gate", "conscious-promotion"]
    else:
        kind = "kanban_architecture_canary"
        summary = "Canary: validate Sensorium-on-Kanban intake -> Subconscious review -> optional Conscious promotion path."
        keys = ["sensorium-kanban", "architecture-canary", "subconscious-gate"]
    return {
        "id": f"evt_kanban_canary_{h}",
        "ts": ts,
        "type": "sensor.event.promoted",
        "source_signal_ids": [f"sig_kanban_canary_{h}"],
        "kind": kind,
        "summary": summary,
        "signal_count": 1,
        "strength": 0.91,
        "correlation_keys": keys,
        "sensitivity": "private",
        "allowed_surfaces": ["local", "discord"],
        "fingerprint": h,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--instance", default=INSTANCE)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--force-canary", action="store_true")
    ap.add_argument("--force-conscious-canary", action="store_true")
    ap.add_argument("--canary-label", default="")
    args = ap.parse_args()

    if args.instance != INSTANCE:
        raise SystemExit(f"Only instance={INSTANCE!r} is configured for this wrapper")

    state = _load_json(STATE_PATH, {})
    seen = set(state.get("seen_event_ids") or [])
    result: dict[str, Any] = {"ts": _now_iso(), "board": BOARD, "instance": INSTANCE}

    try:
        _ensure_board()
        sensor_record = _run_det_sensors()
        result["sensor_tick"] = {
            "exit_code": sensor_record.get("exit_code"),
            "result": sensor_record.get("result"),
        }
        ingested_candidates_by_event_id: dict[str, dict[str, Any]] = {}
        sensor_result = sensor_record.get("result")
        if isinstance(sensor_result, dict):
            for step in sensor_result.values():
                if not isinstance(step, dict):
                    continue
                ingest = step.get("ingest")
                if not isinstance(ingest, dict):
                    continue
                event_id = str(ingest.get("event_id") or "")
                candidate_id = str(ingest.get("candidate_id") or "")
                if event_id and candidate_id:
                    ingested_candidates_by_event_id[event_id] = dict(ingest)
        events = _load_events()
        # First run should establish a watermark, not flood the new board with
        # every historical Sensorium event. Canary forcing is explicit and still
        # creates one test intake.
        if not state.get("initialized"):
            for event in events:
                if event.get("id"):
                    seen.add(str(event.get("id")))
            state["initialized"] = True
            state["initialized_at"] = _now_iso()
            state["initial_event_watermark_count"] = len(events)
        new_events = [e for e in events if str(e.get("id")) not in seen]
        if args.force_canary or args.force_conscious_canary:
            new_events.append(
                _force_canary_event(
                    args.canary_label or None,
                    conscious=bool(args.force_conscious_canary),
                )
            )
        created_intakes = []
        suppressed_events = []
        deterministic_suppressions = list(state.get("deterministic_suppressions") or [])
        incident_context = state.get("incident_context") or {}
        if not isinstance(incident_context, dict):
            incident_context = {}
        reconciled_candidates = state.get("reconciled_candidates") or {}
        if not isinstance(reconciled_candidates, dict):
            reconciled_candidates = {}
        for event in new_events:
            eid = str(event.get("id"))
            incident_key = _event_incident_key(event)
            context_state = dict(state)
            context_state["incident_context"] = incident_context
            suppress_reason = _coalesced_suppression_reason(event, context_state)
            if suppress_reason:
                suppressed = {
                    "event_id": eid,
                    "incident_key": incident_key,
                    "kind": event.get("kind"),
                    "reason": suppress_reason,
                    "ts": _now_iso(),
                    "summary": str(event.get("summary") or "")[:240],
                }
                suppressed_events.append(suppressed)
                deterministic_suppressions.append(suppressed)
                incident = incident_context.setdefault(incident_key, {})
                settlement = apply_kanban_settlement(
                    SensoriumStore(instance=INSTANCE),
                    decision="DROP",
                    event_id=eid,
                    fingerprint=str(event.get("fingerprint") or ""),
                    correlation_keys=list(event.get("correlation_keys") or []) or None,
                    intake_task_id=str(incident.get("intake_task_id") or ""),
                    review_task_id=str(incident.get("review_task_id") or ""),
                    reason=(
                        "Deterministic Kanban coalescing suppressed this repeat "
                        f"incident after prior Subconscious contextualization: {suppress_reason}"
                    ),
                )
                suppressed["candidate_settlement"] = {
                    "action": settlement.get("action"),
                    "matched_candidate_ids": settlement.get("matched_candidate_ids", []),
                    "updated_candidate_ids": settlement.get("updated_candidate_ids", []),
                    "already_settled_candidate_ids": settlement.get("already_settled_candidate_ids", []),
                }
                incident.update({
                    "last_event_id": eid,
                    "last_seen_at": _now_iso(),
                    "repeat_count": int(incident.get("repeat_count") or 0) + 1,
                    "last_summary": str(event.get("summary") or "")[:240],
                })
                seen.add(eid)
                continue
            created = _create_intake(event)
            created_intakes.append(created)
            incident_context[incident_key] = {
                "first_event_id": eid,
                "last_event_id": eid,
                "first_seen_at": _now_iso(),
                "last_seen_at": _now_iso(),
                "kind": event.get("kind"),
                "intake_task_id": (created.get("create_result") or {}).get("id"),
                "repeat_count": 0,
                "last_summary": str(event.get("summary") or "")[:240],
            }
            ingest_meta = ingested_candidates_by_event_id.get(eid) or {}
            candidate_id = str(ingest_meta.get("candidate_id") or "")
            intake_task_id = str((created.get("create_result") or {}).get("id") or "")
            if candidate_id and intake_task_id:
                # Fresh event-driven intakes are already a Kanban representation
                # of their newly-created candidate. Mark them represented before
                # the stale-candidate reconciliation pass so the same candidate
                # does not get a second candidate-intake in the same tick.
                reconciled_candidates[candidate_id] = {
                    "intake_task_id": intake_task_id,
                    "idempotency_key": created.get("idempotency_key"),
                    "decision": "intake",
                    "kind": event.get("kind"),
                    "event_id": eid,
                    "reconciled_at": _now_iso(),
                    "source": "event_intake",
                }
            seen.add(eid)
        tasks = _list_tasks()
        post_review_settlement = _post_review_settle_completed_intakes(
            SensoriumStore(instance=INSTANCE),
            tasks,
        )
        improvement_collect = run_improvement_collector(
            SensoriumStore(instance=INSTANCE),
            bridge_state=state,
            kanban_tasks=tasks,
        )
        open_intakes = _open_sensor_intakes(tasks)
        # Reconciliation pass: the event-driven path above only mirrors freshly
        # emitted events. A candidate that crossed the dispatch threshold earlier
        # (before the watermark, or with no fresh sample this tick) can sit active
        # and above-threshold while the board shows nothing -- a quiet pending
        # activation. Mirror or settle every such candidate so Kanban stays the
        # authoritative activation substrate.
        reconcile = _reconcile_active_candidates(
            SensoriumStore(instance=INSTANCE),
            open_intakes=open_intakes,
            reconciled_candidates=reconciled_candidates,
        )
        for created in reconcile.get("created_tasks", []):
            created_intakes.append(created)
        # Lean summary (drops raw CLI create payloads) for state/result surfaces.
        reconcile_summary = {
            "minted": reconcile.get("minted", []),
            "settled": reconcile.get("settled", []),
            "skipped": reconcile.get("skipped", []),
            "truncated": reconcile.get("truncated", 0),
            "active_above_threshold_count": reconcile.get("active_above_threshold_count", 0),
        }
        if reconcile.get("created_tasks"):
            # New intakes exist; refresh the board view so the review batch and
            # gating counts include them this tick instead of next.
            tasks = _list_tasks()
            open_intakes = _open_sensor_intakes(tasks)
        active_reviews = _active_reviews(tasks)
        active_conscious = _active_conscious(tasks)
        review = None
        # Gate model use: exactly one Subconscious review at a time, and avoid
        # stacking fresh Subconscious work while a Conscious attention head is open.
        if open_intakes and not active_reviews and not active_conscious:
            review = _create_review(open_intakes)
            if review:
                review_task_id = (review.get("create_result") or {}).get("id")
                review_intake_ids = set(review.get("intake_ids") or [])
                for incident in incident_context.values():
                    if incident.get("intake_task_id") in review_intake_ids:
                        incident["review_task_id"] = review_task_id
                        incident["review_created_at"] = _now_iso()
        state.update(
            {
                "updated_at": _now_iso(),
                "seen_event_ids": sorted(seen)[-5000:],
                "last_open_intake_count": len(open_intakes),
                "last_active_review_count": len(active_reviews),
                "last_active_conscious_count": len(active_conscious),
                "last_created_intake_count": len(created_intakes),
                "last_suppressed_event_count": len(suppressed_events),
                "deterministic_suppressions": deterministic_suppressions[-500:],
                "incident_context": incident_context,
                "reconciled_candidates": reconciled_candidates,
                "last_reconcile": reconcile_summary,
                "last_post_review_settlement": post_review_settlement,
                "last_improvement_collect": improvement_collect,
                "last_reconciled_intake_count": len(reconcile.get("minted", [])),
                "last_reconciled_settle_count": len(reconcile.get("settled", [])),
                "last_review_created": review,
            }
        )
        _write_json(STATE_PATH, state)
        if not SUBCONSCIOUS_STATE_PATH.exists():
            _write_json(
                SUBCONSCIOUS_STATE_PATH,
                {
                    "created_at": _now_iso(),
                    "purpose": "Cheap continuity for serasubconscious Sensorium Kanban reviews.",
                    "suppressions": [],
                    "promotions": [],
                    "last_reviewed": None,
                },
            )
        result.update(
            {
                "success": True,
                "created_intakes": created_intakes,
                "suppressed_events": suppressed_events,
                "reconcile": reconcile_summary,
                "post_review_settlement": post_review_settlement,
                "improvement_collect": improvement_collect,
                "open_intake_count": len(open_intakes),
                "active_review_count": len(active_reviews),
                "active_conscious_count": len(active_conscious),
                "review_created": review,
            }
        )
    except Exception as exc:
        result.update({"success": False, "error": str(exc)})
        _write_json(LATEST_PATH, result)
        print("sensorium_kanban_sensor_tick failed")
        print(str(exc))
        return 1

    _write_json(LATEST_PATH, result)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
