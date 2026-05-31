# Sensorium Settlement Hygiene + Aperture Batch Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Clear the current Sera Sensorium settlement backlog and make future Subconscious review outcomes deterministically settle Sensorium candidates + Kanban intake rows, without relying on prompt compliance or worker board authority.

**Architecture:** Keep Kanban as the activation substrate, but move post-review settlement enforcement into the no-agent bridge where it has deterministic authority. `serasubconscious` may still comment and decide, but the bridge must parse review evidence, apply Sensorium candidate settlement, clean consumed intake tasks, and surface gaps when evidence is insufficient. After settlement hygiene is proven, validate the Discord attention aperture with one safe pointer/open path and no proactive outbox.

**Tech Stack:** Python stdlib, Hermes Kanban CLI, Agent Sensorium JSONL store, pytest.

---

## Live Findings from 2026-05-31 Recon

- Correct live instance is `sera`, not `default`.
  - `~/.hermes/agent-sensorium/sera`: 65 signals, 53 events, 47 candidates, 7 active candidates, 21 closed threads.
  - `~/.hermes/agent-sensorium/default` contains stale/local-only residue and should not be used for Sera health reports.
- Quiet cron is healthy:
  - job `ea3e398c4382`, `sera-sensorium-quiet-tick`, every 10m, no-agent script `sensorium_sera_tick_quiet.py`.
  - latest tick around `2026-05-31T06:43Z` succeeded.
- Current clog:
  - 5 open `sensor:intake:*` tasks on board `sensorium`, all unassigned `ready` substrate rows.
  - Latest review task `t_34c7f997` completed and commented DROP decisions on all five intakes.
  - Its run summary says: `Unable to complete intake tasks from this worker scope (kernel scope guard; board-level cleanup needed separately)`.
  - It also reports settlement CLI `no_candidate_match=5`, and no corresponding `kanban.settlement.applied`/`unresolved` receipts appeared in `sera/decisions.jsonl` for those intake IDs.
- Failure class:
  - The cheap review worker can decide/comment, but cannot reliably complete/archive intake tasks or prove candidate settlement.
  - The bridge backstop only recovers from **closed** intake tasks (`done`/`archived`), so reviewed-but-still-open intakes are invisible to the recovery path.
  - Review prompt line currently emphasizes `event_id/fingerprint/correlation_keys` and does not strongly require `candidate_id` for candidate-reconciliation intakes, increasing `no_candidate_match` risk.
- Secondary pressure:
  - TTS sidecar state is currently `healthy`, but old `tts_sidecar_pressure` candidate remains active at pressure `0.866` because review/settlement did not close it.
  - `hindsight_pressure_state.json` is `critical` (`pending_total=4978`, `failed_total=9`) but is not currently the top active candidate; keep it as a later sensor-tuning item after settlement hygiene.

---

## Batch A — Settlement Truth + Board Cleanup

### Task 1: Add failing regression for reviewed-open intake recovery

**Objective:** Prove the current gap: an intake that remains open but has a Subconscious DROP/SAVE/PROMOTE decision should still produce a settlement record and cleanup plan.

**Files:**
- Modify: `tests/test_settlement_propagation.py`
- Modify later: `agent_sensorium/settlement.py`

**Step 1: Add failing tests**

Add tests near `TestCompletedIntakeSettlementRecovery`:

```python
def test_plans_reviewed_open_intake_settlement_record(self):
    task = self._intake_task(status="ready", comment="decision: DROP — reviewed but worker could not archive")
    plan = plan_reviewed_open_intake_settlements(
        [task],
        decisions=[],
        active_candidate_ids={"cand_dash1"},
    )
    assert plan["gaps"] == []
    assert len(plan["records"]) == 1
    assert plan["records"][0]["candidate_id"] == "cand_dash1"
    assert plan["cleanup_task_ids"] == ["kt_intake_done"]


def test_reviewed_open_intake_without_decision_is_visible_gap(self):
    task = self._intake_task(status="ready", comment="looked at it", summary="")
    plan = plan_reviewed_open_intake_settlements(
        [task],
        decisions=[],
        active_candidate_ids={"cand_dash1"},
    )
    assert plan["records"] == []
    assert plan["gaps"] == [
        {
            "intake_task_id": "kt_intake_done",
            "candidate_id": "cand_dash1",
            "reason": "reviewed_open_intake_missing_decision",
        }
    ]
```

**Step 2: Run to verify failure**

Run:

```bash
pytest tests/test_settlement_propagation.py::TestCompletedIntakeSettlementRecovery -q
```

Expected: FAIL because `plan_reviewed_open_intake_settlements` does not exist yet.

### Task 2: Implement deterministic reviewed-open intake settlement planning

**Objective:** Add a pure planner that turns reviewed-but-open intakes into settlement records and cleanup actions.

**Files:**
- Modify: `agent_sensorium/settlement.py`
- Test: `tests/test_settlement_propagation.py`

**Step 1: Implement planner**

Add function:

```python
def plan_reviewed_open_intake_settlements(
    tasks: list[dict],
    *,
    decisions: list[dict],
    active_candidate_ids: set[str] | list[str] | None = None,
) -> dict:
    """Plan recovery for open intake tasks that already carry review decisions.

    This handles the live failure where serasubconscious can comment/decide but
    cannot complete/archive substrate rows due worker scope guard.
    """
    active_filter = {str(c) for c in active_candidate_ids} if active_candidate_ids is not None else None
    records: list[dict] = []
    gaps: list[dict] = []
    cleanup_task_ids: list[str] = []
    already_settled: list[str] = []

    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        intake_task_id = str(task.get("id") or "")
        title = str(task.get("title") or task.get("name") or "")
        status = str(task.get("status") or "")
        if not intake_task_id or not title.startswith("sensor:intake:"):
            continue
        if status in CLOSED_INTAKE_STATUSES:
            continue

        payload = extract_kanban_intake_payload(str(task.get("body") or ""))
        candidate_id = str(payload.get("candidate_id") or "")
        if active_filter is not None and candidate_id and candidate_id not in active_filter:
            continue
        if _settlement_receipt_exists(decisions, intake_task_id):
            already_settled.append(intake_task_id)
            cleanup_task_ids.append(intake_task_id)
            continue

        decision = infer_kanban_settlement_decision(task)
        if not decision:
            # Only flag as gap if there is actual review evidence/comment, not a fresh untouched intake.
            if _task_texts_for_decision(task):
                gaps.append({
                    "intake_task_id": intake_task_id,
                    "candidate_id": candidate_id,
                    "reason": "reviewed_open_intake_missing_decision",
                })
            continue

        event_ids = payload.get("event_ids") or []
        event_id = str(payload.get("event_id") or "")
        if not event_id and isinstance(event_ids, list) and event_ids:
            event_id = str(event_ids[0] or "")
        correlation_keys = payload.get("correlation_keys") or []
        if not isinstance(correlation_keys, list):
            correlation_keys = []
        records.append({
            "decision": decision,
            "candidate_id": candidate_id,
            "event_id": event_id,
            "fingerprint": str(payload.get("fingerprint") or ""),
            "correlation_keys": correlation_keys,
            "intake_task_id": intake_task_id,
            "review_task_id": str(task.get("review_task_id") or ""),
            "reason": truncate_text(
                "Recovered Sensorium settlement from reviewed open Kanban intake; "
                f"review evidence claimed {decision} but worker could not close the row.",
                240,
            ),
        })
        cleanup_task_ids.append(intake_task_id)

    return {
        "records": records,
        "gaps": gaps,
        "already_settled": already_settled,
        "cleanup_task_ids": cleanup_task_ids,
    }
```

Refactor common code with `plan_completed_intake_settlements` only if it stays small. Do not over-abstract.

**Step 2: Run tests**

```bash
pytest tests/test_settlement_propagation.py::TestCompletedIntakeSettlementRecovery -q
```

Expected: PASS.

### Task 3: Add bridge-side execution and cleanup

**Objective:** Make `sensorium_kanban_sensor_tick.py` apply reviewed-open settlement records and archive consumed intakes from the trusted bridge layer.

**Files:**
- Modify: `live-scripts/sensorium_kanban_sensor_tick.py`
- Copy/sync to: `~/.hermes/scripts/sensorium_kanban_sensor_tick.py` during deploy
- Test: add/extend `tests/test_kanban_native_invariants.py` or a focused bridge unit test if one exists

**Step 1: Import the new planner**

In `live-scripts/sensorium_kanban_sensor_tick.py`, import:

```python
plan_reviewed_open_intake_settlements,
```

**Step 2: Add helper to archive cleaned intakes**

Add deterministic helper near `_post_review_settle_completed_intakes`:

```python
def _cleanup_settled_open_intakes(task_ids: list[str]) -> dict[str, Any]:
    cleaned = []
    errors = []
    for task_id in task_ids:
        if not task_id:
            continue
        comment = _run([
            "hermes", "kanban", "--board", BOARD, "comment", task_id,
            "Bridge cleanup: reviewed intake was settled in Sensorium truth; archiving substrate row.",
            "--author", "sensorium-kanban-bridge",
        ], timeout=60)
        archive = _run([
            "hermes", "kanban", "--board", BOARD, "archive", task_id,
        ], timeout=60)
        if archive.returncode == 0:
            cleaned.append(task_id)
        else:
            errors.append({
                "task_id": task_id,
                "comment_rc": comment.returncode,
                "archive_rc": archive.returncode,
                "stderr": archive.stderr[-500:],
            })
    return {"cleaned": cleaned, "errors": errors}
```

Use `archive`, not `complete`, for substrate rows that were already reviewed by the Subconscious task.

**Step 3: Call reviewed-open settlement before reconciliation**

After `tasks = _list_tasks()` and before `open_intakes = _open_sensor_intakes(tasks)`, inspect full details for open intakes that have comments/runs:

```python
open_candidates = _open_sensor_intakes(tasks)
open_details = [_show_task(str(t.get("id"))) for t in open_candidates if t.get("id")]
reviewed_open_settlement = _post_review_settle_open_intakes(
    SensoriumStore(instance=INSTANCE),
    open_details,
)
if reviewed_open_settlement.get("cleanup_task_ids"):
    reviewed_open_settlement["cleanup"] = _cleanup_settled_open_intakes(
        reviewed_open_settlement["cleanup_task_ids"]
    )
    tasks = _list_tasks()
```

Return this in `result` and persist it into `state["last_reviewed_open_settlement"]`.

**Step 4: Add helper wrapper**

Wrapper should:

1. read active candidate ids via `select_active_above_threshold`;
2. call `plan_reviewed_open_intake_settlements`;
3. apply every record through `apply_settlement_record`;
4. include `records`, `applied`, `gaps`, `already_settled`, and `cleanup_task_ids` in result;
5. cleanup only records that applied or were already settled; do **not** archive gaps.

**Step 5: Run tests**

```bash
pytest tests/test_settlement_propagation.py tests/test_kanban_native_invariants.py -q
```

Expected: PASS.

### Task 4: Fix review prompt so future CLI records include candidate IDs and truthful settlement status

**Objective:** Reduce future reviewer error, while keeping deterministic bridge enforcement as the real guardrail.

**Files:**
- Modify: `live-scripts/sensorium_kanban_sensor_tick.py` (`_review_body`)
- Test: `tests/test_kanban_native_invariants.py` or new prompt invariant test

**Step 1: Patch prompt lines**

Change line about settlement record shape from:

```text
Record shape: {decision: DROP|SAVE|PROMOTE_CONSCIOUS, event_id, fingerprint, correlation_keys, intake_task_id, review_task_id, conscious_task_ref?, reason}. Use event_id/fingerprint/correlation_keys from each intake's Compact event payload.
```

to:

```text
Record shape: {decision: DROP|SAVE|PROMOTE_CONSCIOUS, candidate_id?, event_id?, fingerprint?, correlation_keys?, intake_task_id, review_task_id, conscious_task_ref?, reason}. For Compact candidate payloads, candidate_id is mandatory and should be the primary resolver. For Compact event payloads, use event_id first. Treat settlement_cli_result.no_candidate_match > 0 as unresolved, not settled.
```

**Step 2: Add invariant test**

Assert `_review_body([...])` contains:

- `candidate_id is mandatory`
- `no_candidate_match > 0 as unresolved`
- `complete/archive every intake task`

**Step 3: Run tests**

```bash
pytest tests/test_kanban_native_invariants.py -q
```

Expected: PASS.

### Task 5: Deploy/sync live and verify current backlog clears

**Objective:** Apply the accepted source changes to the live script/plugin path, then prove the current five-row clog is gone and candidates leave the active promotion pool.

**Files:**
- Source repo: `/home/entity/projects/agent-sensorium`
- Live plugin: `/home/entity/.hermes/plugins/agent-sensorium`
- Live script: `/home/entity/.hermes/scripts/sensorium_kanban_sensor_tick.py`

**Step 1: Sync source to live**

```bash
rsync -a /home/entity/projects/agent-sensorium/agent_sensorium/ /home/entity/.hermes/plugins/agent-sensorium/agent_sensorium/
cp /home/entity/projects/agent-sensorium/live-scripts/sensorium_kanban_sensor_tick.py /home/entity/.hermes/scripts/sensorium_kanban_sensor_tick.py
```

**Step 2: Run one manual tick with JSON**

```bash
/home/entity/.hermes/scripts/sensorium_kanban_sensor_tick.py --instance sera --json > /tmp/sensorium_tick_after_settlement_fix.json
```

Expected:

- `success: true`
- `reviewed_open_settlement.applied` includes current candidate IDs or reports already-settled
- `reviewed_open_settlement.cleanup.cleaned` includes the five current intake task IDs, unless a new intake legitimately appeared
- no stderr/no cron delivery noise

**Step 3: Verify live state**

```bash
python - <<'PY'
import sqlite3, json
con = sqlite3.connect('/home/entity/.hermes/kanban/boards/sensorium/kanban.db')
con.row_factory = sqlite3.Row
rows = [dict(r) for r in con.execute("select id,title,status from tasks where title like 'sensor:intake:%' and status not in ('done','archived') order by created_at desc")]
print(json.dumps(rows, indent=2))
PY
```

Expected: `[]` or only genuinely new, uncommented fresh intakes.

Then verify:

```bash
pytest tests/test_settlement_propagation.py tests/test_kanban_native_invariants.py -q
```

Expected: PASS.

### Task 6: Commit

**Objective:** Preserve the repair with traceable source state.

```bash
git add agent_sensorium/settlement.py live-scripts/sensorium_kanban_sensor_tick.py tests/test_settlement_propagation.py tests/test_kanban_native_invariants.py docs/plans/2026-05-31-sensorium-settlement-and-aperture-batch.md
git commit -m "fix: settle reviewed open sensorium intakes"
```

---

## Batch B — Sensor Recovery Semantics

### Task 7: Add recovery settlement rule for maintenance-pressure candidates

**Objective:** If a sensor currently reports healthy/recovered and the corresponding candidate was already reviewed or repeatedly dropped, it should leave active candidate status with an auditable receipt instead of remaining high-pressure forever.

**Files:**
- Inspect/modify: `agent_sensorium/sensors.py` or the specific sensor modules under `agent_sensorium/`
- Modify: `live-scripts/sensorium_kanban_sensor_tick.py` only if recovery settlement belongs at bridge layer
- Tests: `tests/test_sensors.py`, `tests/test_settlement_propagation.py`

**Acceptance:**

- Current `tts_sidecar_pressure_state.json` says `level: healthy`; old active `cand_3c66d1a08598` should be settled/reviewed/suppressed after review evidence, not stay top pressure.
- Recovery settlement writes a decision receipt with reason: current sensor level healthy + prior review/drop evidence.
- No automatic restart or outbound message.

### Task 8: Add hindsight pressure classification, not repair yet

**Objective:** Hindsight currently reports `critical` with `pending_total=4978`, `failed_total=9`. Before fixing, classify whether this is operational debt, expected backlog, or stale threshold noise.

**Files:**
- Inspect: `agent_sensorium` hindsight pressure sensor implementation
- Tests: add fixture for pending/failed totals and threshold classification

**Acceptance:**

- Produce one of: `NO_CHANGE expected backlog`, `threshold tune`, or `repair task required`.
- If repair required, create a separate bounded task. Do not let it block settlement hygiene.

### Task 9: Stack a Hindsight update after settlement truth is verified

**Objective:** Preserve the durable lesson in Hindsight after the repair is proven, so future Sensorium/self-improvement passes remember the failure class without treating today’s stale residue as a live alert.

**When to run:** After Batch A manual tick verifies that reviewed-open intakes are settled/cleaned and the active candidate pool no longer contains the already-reviewed TTS/mediated-presence residue.

**Update content shape:**

```text
Context: Sensorium settlement integrity / reviewed-open intake recovery.
Durable lesson: Kanban Subconscious review comments are not canonical settlement. The trusted bridge must apply candidate settlement and cleanup for reviewed-open intakes when workers lack board authority. Use live instance `sera`, not `default`, for Sera Sensorium status. Treat Hindsight pressure separately: classify backlog vs threshold noise before repair.
Evidence: docs/plans/2026-05-31-sensorium-settlement-and-aperture-batch.md; post-fix tick JSON path; test command output.
Tags: sensorium, self-improvement, settlement-integrity, hindsight
```

**Acceptance:**

- `hindsight_retain` stores the lesson after the repair is verified, not before.
- The retained note distinguishes historical residue from active alerts.
- A later `hindsight_recall("Sensorium settlement reviewed-open intakes")` finds the lesson.

---

## Batch C — Reporting and Aperture Proof

### Task 9: Make Sera status/reporting instance-explicit

**Objective:** Prevent future `default` vs `sera` misreads.

**Files:**
- Consider config/docs/tool wrapper, depending on where status commands are invoked.
- Skill/memory already patched in this session: live Sera instance is `sera`.

**Acceptance:**

- Any Sera Sensorium status routine reports `instance=sera`, state dir, board, and cron job.
- If `default` is queried, response labels it as non-Sera/stale unless explicitly requested.

### Task 10: Validate Discord attention aperture after settlement cleanup

**Objective:** Prove the safe review surface: visible candidate/thread pointer only, explicit open required, no raw substrate leak, no outbound delivery.

**Prerequisite:** Batch A complete and board clean.

**Steps:**

1. Create one controlled private+discord canary or use one real non-sensitive stale candidate.
2. Verify `sensorium_attention_inbox(instance='sera', surface='discord')` shows only allowed items.
3. Verify `sensorium_attention_pointer(instance='sera', surface='discord')` returns tiny pointer or no pointer according to policy.
4. Explicitly open with `sensorium_thread_open` only if a thread exists; otherwise create a conscious review task and let it close as validation-only.
5. Record receipts and close/mark-reviewed.

**Acceptance:**

- Discord surface sees no local-only TTS candidate.
- Private+discord candidate can be reviewed, held/suppressed/marked reviewed.
- No artifact, outbox, message send, worker dispatch, or proactive delivery occurs.

---

## Recommended Execution Order

1. Batch A tasks 1–6 as one focused repair PR/commit.
2. Manual live tick verification.
3. Batch B task 7 if TTS remains active after Batch A.
4. Batch B task 8 as a separate classification task; do not merge it into settlement repair.
5. Batch C aperture proof only after board + candidate settlement truth is clean.

## Non-goals for this batch

- No proactive outbox.
- No media artifact generation.
- No new recurring cron.
- No broad rewrite of Sensorium architecture.
- No model-backed Subconscious changes until deterministic settlement truth is clean.

---

## Execution Notes — 2026-05-31

- Batch A source + live bridge verified with `pytest tests/test_settlement_propagation.py tests/test_kanban_native_invariants.py -q` → 65 passed.
- Live script synced to `~/.hermes/scripts/sensorium_kanban_sensor_tick.py`; live plugin synced to `~/.hermes/plugins/agent-sensorium/agent_sensorium/`.
- Final live settlement tick proof: `/tmp/sensorium_tick_final_settlement_batch.json` showed `success=true`, `open_intake_count=0`, `active_review_count=0`, `active_conscious_count=0`, `reconcile.active_above_threshold_count=0`, and no new review task.
- TTS recovery classification: no extra recovery-settlement implementation needed in this batch. The stale active `tts_sidecar_pressure` candidate `cand_3c66d1a08598` was canonically settled with DROP by the bridge and archived by compaction; current TTS sidecar state remains `level=healthy` with no active high-pressure TTS candidate.
- Hindsight pressure classification: `threshold tune + separate bounded repair task`. Hindsight API is healthy and retain/batch-retain/consolidation/reflect queues are empty; pressure is isolated to `refresh_mental_model` pending duplicates with no shared worker slot. Follow-up task: `t_150d39bb` on board `sensorium` (blocked) for safe refresh-backlog drain and sensor operation-family classification.
- Hindsight durable lesson retained and verified via `hindsight_recall("Sensorium settlement reviewed-open intakes")`.
- Discord aperture proof: `sensorium_attention_inbox(instance='sera', surface='discord')` exposed only one allowed private+discord candidate before review, filtered local-only candidates out, and showed no visible items after `cand_a477ce3dd108` was marked reviewed. `sensorium_attention_pointer(instance='sera', surface='discord')` returned `no_pointer` / `no_visible_thread_for_surface`. No outbox dispatch, worker dispatch, proactive message, or new artifact was created.
- Final post-aperture tick proof: `/tmp/sensorium_tick_after_aperture_proof.json` showed `success=true`, `open_intake_count=0`, `active_review_count=0`, `active_conscious_count=0`, `reconcile.active_above_threshold_count=0`, and `review_created=None`.
