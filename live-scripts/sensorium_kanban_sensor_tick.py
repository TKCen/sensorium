#!/usr/bin/env python3
"""Sensorium-on-Kanban no-agent sensor tick.

Dumb fixture layer:
- runs deterministic Agent Sensorium sensors without internal dispatch/advisory;
- mirrors new promoted Events into the `sensorium` Kanban board as blocked
  `sensor:intake:*` tasks assigned to the subconscious-reviewer profile;
- creates at most one ready `subconscious:review:*` task assigned to the cheap
  subconscious-reviewer profile when unresolved intake exists and no review is
  already active.

Profile/instance and the reviewer profile name are resolved from the
environment with generic defaults, so this bridge ships free of any
deployment-specific instance/profile values.

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
# Profile (state namespace) and the cheap reviewer profile are resolved from the
# environment with generic defaults; no deployment-specific value is baked in.
INSTANCE = (
    os.environ.get("AGENT_SENSORIUM_DEFAULT_INSTANCE")
    or os.environ.get("SENSORIUM_INSTANCE")
    or "default"
)
BOARD = os.environ.get("SENSORIUM_KANBAN_BOARD", "sensorium")
# Default to the real `serasubconscious` Hermes profile so newly minted intake
# rows are claimable by the dispatcher. Override with SENSORIUM_SUBCONSCIOUS_PROFILE
# only when running against a deployment-specific reviewer profile.
PROFILE = os.environ.get("SENSORIUM_SUBCONSCIOUS_PROFILE", "serasubconscious")

# Resolve the package root without hardcoding a private checkout path. Prefer the
# repository copy that ships alongside this script (``<repo>/agent_sensorium``);
# fall back to the installed Hermes plugin location for the runtime cron copy.
_SCRIPT_DIR = Path(__file__).resolve().parent
PLUGIN = HOME / ".hermes" / "plugins" / "agent-sensorium"
for _candidate in (_SCRIPT_DIR.parent, PLUGIN):
    if (_candidate / "agent_sensorium").is_dir():
        PLUGIN = _candidate
        break
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
    plan_reviewed_open_intake_settlements,
    represented_candidate_ids as _represented_candidate_ids,
    select_active_above_threshold,
)
from agent_sensorium.actuator_contracts import (  # noqa: E402
    mediated_artifact_review_contract,
    memory_grounded_review_contract,
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
            "Sensorium task substrate: sensor intake, Subconscious review, Conscious attention heads.",
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
        "--codex-usage",
        "--memory-reflection",
        "--json",
    ]
    proc = subprocess.run(cmd, cwd=str(PLUGIN), text=True, capture_output=True, timeout=240)
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


def _event_id_to_candidate_id(candidates: list[dict[str, Any]]) -> dict[str, str]:
    """Map ``event_id -> candidate_id`` from the canonical candidates store.

    Active-session/manual ingests promote events into candidates *before* this
    bridge tick runs, so the fresh deterministic sensor result
    (``ingested_candidates_by_event_id``) will not carry their event->candidate
    mapping. Recovering it from ``candidate.event_ids`` lets an event intake
    mark its candidate represented before the reconciliation pass, preventing a
    second candidate-mirror intake for the same activation. On collision an
    active above-threshold candidate wins, since that is the row reconciliation
    would otherwise re-mint.
    """
    index: dict[str, str] = {}
    index_is_active: dict[str, bool] = {}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        cand_id = str(candidate.get("id") or "")
        if not cand_id:
            continue
        is_active = (
            candidate.get("status") == "candidate"
            and float(candidate.get("pressure", 0) or 0) >= DEFAULT_DISPATCH_PRESSURE_THRESHOLD
        )
        for raw_eid in candidate.get("event_ids") or []:
            eid = str(raw_eid)
            if not eid:
                continue
            if eid not in index or (is_active and not index_is_active.get(eid)):
                index[eid] = cand_id
                index_is_active[eid] = is_active
    return index


def _list_tasks(*, include_archived: bool = False) -> list[dict[str, Any]]:
    cmd = ["hermes", "kanban", "--board", BOARD, "list", "--json"]
    if include_archived:
        cmd.append("--archived")
    proc = _run_checked(cmd, timeout=60)
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

    This is the enforcement backstop for the cheap review lane: if the
    subconscious reviewer comments/completes an intake as DROP/SAVE/PROMOTE but
    fails to run the settlement CLI, the next bridge tick reads the completed intake,
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

    def _record_active_target_ids(record: dict[str, Any]) -> set[str]:
        candidate_id = str(record.get("candidate_id") or "")
        if candidate_id and candidate_id in active_ids:
            return {candidate_id}
        event_id = str(record.get("event_id") or "")
        if event_id:
            return {
                str(c.get("id") or "")
                for c in active
                if event_id in (c.get("event_ids") or [])
            }
        fingerprint = str(record.get("fingerprint") or "")
        if fingerprint:
            return {
                str(c.get("id") or "")
                for c in active
                if str(c.get("fingerprint") or "") == fingerprint
            }
        correlation_keys = {str(k) for k in (record.get("correlation_keys") or []) if k}
        if correlation_keys:
            return {
                str(c.get("id") or "")
                for c in active
                if correlation_keys & {str(k) for k in (c.get("correlation_keys") or []) if k}
            }
        return set()

    applied: list[dict[str, Any]] = []
    settled_active_ids: set[str] = set()
    for record in plan.get("records", []):
        target_ids = _record_active_target_ids(record)
        if target_ids and target_ids.issubset(settled_active_ids):
            continue
        settlement = apply_settlement_record(store, record)
        settled_active_ids.update(target_ids)
        settled_active_ids.update(
            str(cid)
            for cid in settlement.get("updated_candidate_ids", [])
            if cid
        )
        settled_active_ids.update(
            str(cid)
            for cid in settlement.get("already_settled_candidate_ids", [])
            if cid
        )
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


def _post_review_settle_open_intakes(
    store: "SensoriumStore",
    tasks: list[dict[str, Any]],
) -> dict[str, Any]:
    """Apply settlements claimed by reviewed-but-still-open intake tasks.

    The cheap review worker can leave comments/results but lack authority to
    complete/archive substrate rows. This trusted bridge layer converts explicit
    review decisions into canonical Sensorium settlement and returns only the
    intake ids that are safe for bridge cleanup.
    """
    candidates = store.read_jsonl("candidates")
    active = select_active_above_threshold(
        candidates,
        threshold=DEFAULT_DISPATCH_PRESSURE_THRESHOLD,
    )
    active_ids = {str(c.get("id") or "") for c in active if c.get("id")}
    if not active_ids:
        return {
            "records": [],
            "applied": [],
            "gaps": [],
            "already_settled": [],
            "cleanup_task_ids": [],
        }

    plan = plan_reviewed_open_intake_settlements(
        tasks,
        decisions=store.read_jsonl("decisions"),
        active_candidate_ids=active_ids,
    )
    applied: list[dict[str, Any]] = []
    cleanup_allowed: set[str] = {str(tid) for tid in plan.get("already_settled", []) if tid}
    for record in plan.get("records", []):
        settlement = apply_settlement_record(store, record)
        intake_task_id = str(record.get("intake_task_id") or "")
        action = settlement.get("action")
        if action in {"settled", "already_settled"}:
            cleanup_allowed.add(intake_task_id)
        applied.append(
            {
                "intake_task_id": record.get("intake_task_id"),
                "candidate_id": record.get("candidate_id"),
                "decision": record.get("decision"),
                "settlement_action": action,
                "updated_candidate_ids": settlement.get("updated_candidate_ids", []),
                "already_settled_candidate_ids": settlement.get("already_settled_candidate_ids", []),
            }
        )

    ordered_cleanup_ids = [
        str(tid)
        for tid in plan.get("cleanup_task_ids", [])
        if tid and str(tid) in cleanup_allowed
    ]
    return {
        "records": plan.get("records", []),
        "applied": applied,
        "gaps": plan.get("gaps", []),
        "already_settled": plan.get("already_settled", []),
        "cleanup_task_ids": ordered_cleanup_ids,
    }


def _cleanup_settled_open_intakes(task_ids: list[str]) -> dict[str, Any]:
    cleaned: list[str] = []
    errors: list[dict[str, Any]] = []
    seen: set[str] = set()
    for task_id in task_ids:
        task_id = str(task_id or "")
        if not task_id or task_id in seen:
            continue
        seen.add(task_id)
        comment = _run(
            [
                "hermes",
                "kanban",
                "--board",
                BOARD,
                "comment",
                task_id,
                "Bridge cleanup: reviewed intake was settled in Sensorium truth; archiving substrate row.",
                "--author",
                "sensorium-kanban-bridge",
            ],
            timeout=60,
        )
        archive = _run(
            ["hermes", "kanban", "--board", BOARD, "archive", task_id],
            timeout=60,
        )
        if archive.returncode == 0:
            cleaned.append(task_id)
        else:
            errors.append(
                {
                    "task_id": task_id,
                    "comment_rc": comment.returncode,
                    "archive_rc": archive.returncode,
                    "stderr": archive.stderr[-500:],
                }
            )
    return {"cleaned": cleaned, "errors": errors}


def _inactive_candidate_open_intake_cleanup(
    store: "SensoriumStore",
    tasks: list[dict[str, Any]],
) -> dict[str, Any]:
    """Select open intake rows whose underlying candidate already left active pool."""
    candidates = {
        str(c.get("id") or ""): c
        for c in store.read_jsonl("candidates")
        if c.get("id")
    }
    items: list[dict[str, Any]] = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        intake_task_id = str(task.get("id") or "")
        if not intake_task_id or not _task_title(task).startswith("sensor:intake:"):
            continue
        if _task_status(task) in CLOSED_INTAKE_STATUSES:
            continue
        payload = extract_kanban_intake_payload(str(task.get("body") or ""))
        candidate_id = str(payload.get("candidate_id") or "")
        if not candidate_id:
            continue
        candidate = candidates.get(candidate_id)
        if not candidate:
            continue
        status = str(candidate.get("status") or "candidate")
        pressure = float(candidate.get("pressure", 0) or 0)
        if status == "candidate" and pressure >= DEFAULT_DISPATCH_PRESSURE_THRESHOLD:
            continue
        items.append(
            {
                "intake_task_id": intake_task_id,
                "candidate_id": candidate_id,
                "candidate_status": status,
                "reason": "underlying_candidate_not_active",
            }
        )
    return {
        "cleanup_task_ids": [item["intake_task_id"] for item in items],
        "items": items,
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
    # Only executable Conscious rows should pause new
    # Subconscious batch creation. Blocked Conscious rows are often deliberate
    # holds, manual gates, or legacy backlog; ``todo`` rows are usually
    # dependency-gated children. Counting either here starves fresh
    # sensor:intake review indefinitely.
    active_status = {"ready", "running"}
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
    # Attach memory-grounded contract for high-stakes event types.
    memory_contract = memory_grounded_review_contract(event)
    artifact_contract = mediated_artifact_review_contract(event)
    contracts = [c for c in (memory_contract, artifact_contract) if c]
    if contracts:
        payload["conscious_review_contract"] = contracts[-1]
        payload["conscious_review_contracts"] = contracts
    contract_note_parts: list[str] = []
    if memory_contract:
        contract_note_parts.append(
            "Memory-grounded contract: before deciding, use bounded Hindsight recall/context "
            "retrieval and cite which retrieved memory facts changed or grounded the choice; "
            "if retrieval is unavailable, record retrieval_skipped_reason; do not rely on "
            "numeric thresholds alone."
        )
    if artifact_contract:
        contract_note_parts.append(
            "Mediated-artifact contract: if PROMOTE_CONSCIOUS, the internal conscious_task "
            "requires a concrete media/artifact choice or explicit HOLD/no-artifact receipt."
        )
    contract_note = ("\n\n" + " ".join(contract_note_parts)) if contract_note_parts else ""
    return (
        "Sensorium Kanban intake v1. This is substrate, not executable user work.\n\n"
        f"Status semantics: this task is intentionally blocked and assigned to {PROFILE} so the gateway "
        f"dispatcher will not run it directly. The {PROFILE} review task reads and "
        "settles these intakes.\n\n"
        "Compact event payload:\n"
        + json.dumps(payload, indent=2, sort_keys=True)
        + "\n\nExpected settlement: Subconscious comments DROP/SAVE/PROMOTE evidence, "
        "then archives/completes this intake or links it to a conscious review task."
        + contract_note
    )


def _sticky_block_and_assign_intake(task_id: str, reason: str) -> None:
    """Make a substrate intake sticky-blocked, then assign it for review.

    Kanban's dependency recompute can auto-promote initial ``blocked`` tasks
    that have no parents unless there is an explicit block event. Create the
    substrate row first, emit that sticky block event, and only then assign the
    review profile so the gateway dispatcher never sees a runnable intake.
    """
    if not task_id:
        return
    _run_checked([
        "hermes",
        "kanban",
        "--board",
        BOARD,
        "block",
        task_id,
        reason,
    ], timeout=60)
    _run_checked([
        "hermes",
        "kanban",
        "--board",
        BOARD,
        "assign",
        task_id,
        PROFILE,
    ], timeout=60)


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
    if data.get("status") not in {"done", "archived"}:
        _sticky_block_and_assign_intake(
            str(data.get("id") or ""),
            "Sensorium substrate intake: sticky-blocked so only Subconscious review may settle it.",
        )
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
    # Attach memory-grounded contract for high-stakes candidates.
    memory_contract = memory_grounded_review_contract(candidate)
    artifact_contract = mediated_artifact_review_contract(candidate)
    contracts = [c for c in (memory_contract, artifact_contract) if c]
    if contracts:
        payload["conscious_review_contract"] = contracts[-1]
        payload["conscious_review_contracts"] = contracts
    contract_note_parts: list[str] = []
    if memory_contract:
        contract_note_parts.append(
            "Memory-grounded contract: before deciding, use bounded Hindsight recall/context "
            "retrieval and cite which retrieved memory facts changed or grounded the choice; "
            "if retrieval is unavailable, record retrieval_skipped_reason; do not rely on "
            "numeric thresholds alone."
        )
    if artifact_contract:
        contract_note_parts.append(
            "Mediated-artifact contract: if PROMOTE_CONSCIOUS, the internal conscious_task "
            "requires a concrete media/artifact choice or explicit HOLD/no-artifact receipt."
        )
    contract_note = ("\n\n" + " ".join(contract_note_parts)) if contract_note_parts else ""
    return (
        "Sensorium Kanban reconciliation intake v1. This is substrate, not "
        "executable user work.\n\n"
        "Why this exists: this candidate is already active and above the dispatch "
        "pressure threshold in Sensorium, but no fresh event emitted this tick, so "
        "the event-driven bridge never mirrored it. The reconciliation pass mirrors "
        "it now so an above-threshold candidate cannot remain a quiet pending "
        "activation invisible to Kanban.\n\n"
        f"Status semantics: this task is intentionally blocked and assigned to {PROFILE} so the "
        f"gateway dispatcher will not run it directly. The {PROFILE} review "
        "task reads and settles these intakes.\n\n"
        "Compact candidate payload:\n"
        + json.dumps(payload, indent=2, sort_keys=True)
        + "\n\nExpected settlement: Subconscious comments DROP/SAVE/PROMOTE evidence, "
        "then settles the underlying candidate via the settlement CLI "
        "(candidate_id above) and archives/completes this intake."
        + contract_note
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
    if data.get("status") not in {"done", "archived"}:
        _sticky_block_and_assign_intake(
            str(data.get("id") or ""),
            "Sensorium reconciliation intake: sticky-blocked so only Subconscious review may settle it.",
        )
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
        f"You are the Sensorium Subconscious reviewer (profile: {PROFILE}), the cheap Sensorium Subconscious gate.\n\n"
        "Task: review all currently open Sensorium intake tasks, decide DROP/SAVE/PROMOTE_CONSCIOUS, "
        "settle duplicates/noise, and promote at most one attention head to Conscious if warranted.\n\n"
        f"Board: {BOARD}\n"
        f"State file: {SUBCONSCIOUS_STATE_PATH}\n\n"
        "Open intake tasks at creation time:\n"
        + json.dumps(rows, indent=2, sort_keys=True)
        + "\n\nRules:\n"
        "- Inspect cited intake tasks with `hermes kanban --board sensorium show <id>` if needed.\n"
        "- Also inspect the board list for newer open intakes before deciding.\n"
        "- Scope: review/settle Sensorium intakes and create internal conscious_task candidates when warranted.\n"
        "- For DROP/SAVE: comment concise evidence on intake tasks, archive or complete settled intakes.\n"
        "- For PROMOTE_CONSCIOUS: create one internal conscious_task candidate for the bounded Conscious aperture:\n"
        f"  `python {PLUGIN}/scripts/sensorium_create_conscious_task.py --instance {INSTANCE} --file /tmp/sensorium_conscious_task_<review>.json --json`\n"
        "  The JSON file contains rationale, event_ids/candidate_ids, optional pressure, and conscious_task {request_type,title,why,expected_decision}. Put the returned data.candidate_id and conscious_task.id in settlement conscious_task_ref as {kind: internal_conscious_task_candidate, candidate_id, conscious_task_id, promoted_at}.\n"
        "- For intakes whose compact payload includes `conscious_review_contract.type == mediated_presence_artifact_decision`: expected_decision must require prepare_thread_artifact, offer_choice, choose_silence, decline/block delivery, or HOLD with no-artifact reason.\n"
        "- For `attention_policy_review` intakes: expected_decision must require NO_CHANGE, threshold/coalescing tweak proposal, sensor addition task, priority-map change, memory/skill patch, or HOLD, plus future_tendency_delta, verification_condition, and rollback_condition.\n"
        "- CRITICAL: after deciding, propagate every consumed intake decision back into Sensorium truth by running the settlement CLI once with a JSON list of settlement records:\n"
        f"  `python {PLUGIN}/scripts/sensorium_kanban_settle.py --instance {INSTANCE} --file /tmp/sensorium_settlements_<review>.json --json`\n"
        "  Record shape: {decision: DROP|SAVE|PROMOTE_CONSCIOUS, candidate_id?, event_id?, fingerprint?, correlation_keys?, intake_task_id, review_task_id, conscious_task_ref?, reason}. For Compact candidate payloads, candidate_id is mandatory and should be the primary resolver. For Compact event payloads, use event_id first. Treat settlement_cli_result.no_candidate_match > 0 as unresolved, not settled.\n"
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
            "Subconscious promotion into an internal Conscious task. This is "
            "explicit test salience; create one internal conscious_task candidate, "
            "then the bounded aperture should settle it as validation-only."
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
    global INSTANCE, EVENTS_PATH
    ap = argparse.ArgumentParser()
    ap.add_argument("--instance", default=INSTANCE)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--force-canary", action="store_true")
    ap.add_argument("--force-conscious-canary", action="store_true")
    ap.add_argument("--canary-label", default="")
    args = ap.parse_args()

    # Allow any profile/instance: align the module-level state namespace with the
    # requested profile so all per-instance reads/writes below stay consistent.
    INSTANCE = args.instance
    EVENTS_PATH = HOME / ".hermes" / "agent-sensorium" / INSTANCE / "events.jsonl"

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
        # Canonical event->candidate lookup for events the deterministic sensor
        # result did not freshly ingest (active-session/manual ingests already
        # promoted to candidates before this tick). Used as a fallback so event
        # intakes can mark their candidate represented before reconciliation.
        event_candidate_index = _event_id_to_candidate_id(
            SensoriumStore(instance=INSTANCE).read_jsonl("candidates")
        )
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
            if not candidate_id:
                # Active-session/manual ingest: the fresh sensor result lacks the
                # mapping, so resolve it from the canonical candidates store.
                candidate_id = event_candidate_index.get(eid, "")
            intake_task_id = str((created.get("create_result") or {}).get("id") or "")
            if candidate_id and intake_task_id:
                # An event intake is already a Kanban representation of its
                # candidate (freshly created this tick, or pre-existing from an
                # active-session/manual ingest). Mark it represented before the
                # stale-candidate reconciliation pass so the same candidate does
                # not get a second candidate-intake in the same tick.
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
        all_tasks = _list_tasks(include_archived=True)
        post_review_settlement = _post_review_settle_completed_intakes(
            SensoriumStore(instance=INSTANCE),
            all_tasks,
        )
        open_intake_details: list[dict[str, Any]] = []
        for intake in _open_sensor_intakes(tasks):
            task_id = str(intake.get("id") or "")
            if not task_id:
                continue
            detail = _show_task(task_id)
            if detail:
                open_intake_details.append(detail)
        reviewed_open_settlement = _post_review_settle_open_intakes(
            SensoriumStore(instance=INSTANCE),
            open_intake_details,
        )
        inactive_open_cleanup = _inactive_candidate_open_intake_cleanup(
            SensoriumStore(instance=INSTANCE),
            open_intake_details,
        )
        reviewed_open_cleanup = {"cleaned": [], "errors": []}
        cleanup_task_ids = list(reviewed_open_settlement.get("cleanup_task_ids") or [])
        cleanup_task_ids.extend(inactive_open_cleanup.get("cleanup_task_ids") or [])
        if cleanup_task_ids:
            reviewed_open_cleanup = _cleanup_settled_open_intakes(cleanup_task_ids)
            tasks = _list_tasks()
        reviewed_open_settlement["cleanup"] = reviewed_open_cleanup
        reviewed_open_settlement["inactive_candidate_cleanup"] = inactive_open_cleanup
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
                "last_reviewed_open_settlement": reviewed_open_settlement,
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
                    "purpose": f"Cheap continuity for {PROFILE} Sensorium Kanban reviews.",
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
                "reviewed_open_settlement": reviewed_open_settlement,
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
