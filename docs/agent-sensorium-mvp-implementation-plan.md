# Agent Sensorium MVP Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Build the smallest working Agent Sensorium spine as a reusable Hermes plugin: ingest compact signals, promote them to events, form candidates, create dormant conscious threads, and expose pull-based status without proactive messaging.

**Architecture:** A local-state Hermes plugin under `~/.hermes/plugins/agent-sensorium/` with pure-Python store/gate/dispatcher modules, registered tools, and optional slash command. MVP is deterministic-first and local-only: no model-backed Subconscious pass, no Discord thread creation, no proactive `REACH_OUT`, no external task creation.

**Tech Stack:** Python stdlib, Hermes plugin API (`plugin.yaml`, `register(ctx)`, `ctx.register_tool`, optional `ctx.register_command`, `ctx.register_skill`), JSONL/YAML-ish JSON state files, pytest.

---

## Product stance

Build the nervous system before the romance.

MVP includes:

- plugin skeleton;
- state directory resolution;
- `sensorium_ingest_signal`;
- deterministic signal filtering/promotion to Events;
- candidate creation from Events;
- dormant `sensorium_thread` creation for promoted `conscious_task` records;
- `sensorium_status` pull review;
- `sensorium_candidate_update` for manual decisions;
- `sensorium_compact` for TTL cleanup;
- tests and one dry-run smoke.

MVP excludes:

- live model-backed Subconscious prompt calls;
- Hindsight/RSS/file-crawl sensors;
- proactive Discord/DM delivery;
- platform thread creation;
- relational autonomy / `REACH_OUT` delivery;
- external Kanban/research/media task creation;
- active-session pointer injection;
- dashboard UI.

The enhancement proposals from the OMC interview remain design pressure, not MVP requirements.

---

## Target files

Create:

```text
~/.hermes/plugins/agent-sensorium/
  plugin.yaml
  __init__.py
  schemas.py
  store.py
  gate.py
  dispatcher.py
  tools.py
  commands.py
  scripts/
    sensorium_tick.py
  skills/
    agent-sensorium/SKILL.md
  tests/
    test_store.py
    test_gate.py
    test_dispatcher.py
    test_tools.py
```

Runtime state:

```text
~/.hermes/agent-sensorium/<instance>/
  config.json
  signals/inbox.jsonl
  events.jsonl
  candidates.jsonl
  threads.jsonl
  decisions.jsonl
  state.latest.json
  dispatch/lock.json
  archive/
```

Sera instance config target:

```text
~/.hermes/agent-sensorium/sera/config.json
```

Do **not** edit upstream Hermes source for MVP. Use user plugin surfaces only.

---

## MVP data contracts

### Signal

```json
{
  "id": "sig_...",
  "ts": "2026-05-24T21:30:00Z",
  "sensor": "explicit_operator_signal",
  "source": "manual|hermes_session|artifact",
  "source_ref": "session:... or file:...",
  "kind": "design_decision",
  "summary": "Operator corrected that Sera images should use references for continuity.",
  "actor": "operator|agent|tool",
  "strength_hint": 0.8,
  "correlation_keys": ["sera-visual-continuity", "references"],
  "sensitivity": "private",
  "allowed_surfaces": ["local", "dashboard"],
  "ttl_hours": 72
}
```

### Event

```json
{
  "id": "evt_...",
  "ts": "...",
  "type": "sensor.event.promoted",
  "source_signal_ids": ["sig_..."],
  "kind": "design_decision",
  "summary": "Reference-based Sera image continuity correction crossed promotion threshold.",
  "signal_count": 1,
  "strength": 0.82,
  "correlation_keys": ["sera-visual-continuity", "references"],
  "sensitivity": "private",
  "allowed_surfaces": ["local", "dashboard"],
  "expires_at": "..."
}
```

### Candidate

```json
{
  "id": "cand_...",
  "status": "candidate",
  "kind": "design_decision",
  "pressure": 0.72,
  "novelty": 0.4,
  "repetition": 0.2,
  "identity_relevance": 0.8,
  "relationship_relevance": 0.2,
  "actionability": 0.7,
  "time_sensitivity": 0.1,
  "summary": "This may deserve a continuity-aware Sera visual workflow reminder.",
  "event_ids": ["evt_..."],
  "correlation_keys": ["sera-visual-continuity", "references"],
  "sensitivity": "private",
  "allowed_surfaces": ["local", "dashboard"],
  "created_at": "...",
  "updated_at": "...",
  "expires_at": "..."
}
```

### Conscious task + dormant thread

For MVP, `dispatcher.py` may create a minimal `conscious_task` embedded in the thread record rather than a separate file.

```json
{
  "sensorium_thread_id": "sth_...",
  "status": "dormant",
  "origin": "candidate",
  "conscious_task": {
    "id": "ctask_...",
    "request_type": "THINK",
    "title": "Review reference-based continuity correction",
    "why": "The correction affects future Sera visual generation quality.",
    "expected_decision": "Save workflow reminder, suppress, or turn into implementation task."
  },
  "origin_candidate_id": "cand_...",
  "continuity_summary": [
    "Operator corrected that Sera images should use references for sustained continuity.",
    "Mood-cards are acceptable as non-canon exploration but not identity continuity."
  ],
  "decision_log": [],
  "interaction_refs": [],
  "summary_dirty": false,
  "open_questions": [],
  "next_prompt_to_operator": "Take up this continuity thread, suppress it, or save as workflow guidance?",
  "sensitivity": "private",
  "allowed_surfaces": ["local", "dashboard"],
  "created_at": "...",
  "updated_at": "...",
  "expires_at": "..."
}
```

---

## Phase gates

### Phase A gate — skeleton/status

Pass when:

- plugin loads;
- `sensorium_status` returns initialized empty state;
- state directory is created under configured instance path;
- no model calls;
- no outbound messages.

### Phase B gate — signal/event/candidate spine

Pass when:

- a manual signal can be ingested;
- deterministic gate promotes threshold-crossing signals to Events;
- Events create/update Candidates;
- duplicate signals coalesce or are ignored;
- status shows top candidates.

### Phase C gate — dormant thread pull review

Pass when:

- dispatcher can promote one Candidate into one dormant thread;
- `sensorium_status` shows top visible dormant threads;
- manual candidate/thread update can suppress/close/pin;
- TTL compaction archives stale items.

Stop here before any proactive delivery.

---

## Tasks

### Task 1: Create plugin skeleton

**Objective:** Create a loadable user plugin with no behavior except registration.

**Files:**

- Create: `~/.hermes/plugins/agent-sensorium/plugin.yaml`
- Create: `~/.hermes/plugins/agent-sensorium/__init__.py`

**Implementation:**

`plugin.yaml`:

```yaml
name: agent-sensorium
version: 0.1.0
kind: standalone
description: "Bounded autonomous inner lifecycle for Hermes agents: signals, events, candidates, and conscious thread capsules."
author: Sera / NousResearch Hermes local plugin
platforms:
  - linux
  - macos
  - windows
```

`__init__.py` initially:

```python
from pathlib import Path


def register(ctx) -> None:
    """Register Agent Sensorium plugin tools and skill."""
    skill_path = Path(__file__).parent / "skills" / "agent-sensorium" / "SKILL.md"
    if skill_path.exists():
        ctx.register_skill(
            "agent-sensorium",
            skill_path,
            description="Review and operate Agent Sensorium candidates and conscious thread capsules.",
        )
```

**Verify:**

Run:

```bash
hermes plugins list | grep -i sensorium || true
python -m py_compile ~/.hermes/plugins/agent-sensorium/__init__.py
```

Expected: compile passes. Plugin listing may require enabling later.

---

### Task 2: Add bundled skill

**Objective:** Provide a minimal explicit skill for future Conscious review.

**Files:**

- Create: `~/.hermes/plugins/agent-sensorium/skills/agent-sensorium/SKILL.md`

**Content requirements:**

- Explain Sensors → Events → Subconscious → Conscious.
- State MVP limitations: no proactive delivery, no relational autonomy, no external task creation.
- Include Conscious review checklist: suppress, hold, save, close, or create bounded follow-up proposal.

**Verify:**

Run:

```bash
test -f ~/.hermes/plugins/agent-sensorium/skills/agent-sensorium/SKILL.md
```

Expected: file exists.

---

### Task 3: Implement schema helpers

**Objective:** Create pure helpers for IDs, timestamps, validation, and normalization.

**Files:**

- Create: `~/.hermes/plugins/agent-sensorium/schemas.py`
- Test: `~/.hermes/plugins/agent-sensorium/tests/test_store.py` or `test_schemas.py`

**Implementation notes:**

- Use dataclasses or dict validators. Prefer simple dict validators for MVP.
- Functions:
  - `utc_now_iso() -> str`
  - `new_id(prefix: str) -> str`
  - `normalize_signal(raw: dict) -> dict`
  - `validate_signal(signal: dict) -> None`
  - `validate_event(event: dict) -> None`
  - `merge_sensitivity(values: list[str]) -> str`
  - `intersect_allowed_surfaces(items: list[list[str]]) -> list[str]`

**Tests:**

- missing required signal fields raises `ValueError`;
- invalid sensitivity raises `ValueError`;
- allowed surfaces intersection works;
- IDs have expected prefixes.

**Verify:**

```bash
cd ~/.hermes/plugins/agent-sensorium
python -m pytest tests/test_schemas.py -q
```

---

### Task 4: Implement JSONL store

**Objective:** Persist and read compact local state safely.

**Files:**

- Create: `~/.hermes/plugins/agent-sensorium/store.py`
- Test: `~/.hermes/plugins/agent-sensorium/tests/test_store.py`

**Implementation notes:**

Class: `SensoriumStore`.

Constructor:

```python
SensoriumStore(instance: str = "default", state_dir: str | None = None)
```

Methods:

- `ensure_dirs()`;
- `append_jsonl(name: str, obj: dict) -> None`;
- `read_jsonl(name: str, limit: int | None = None) -> list[dict]`;
- `write_state(obj: dict) -> None`;
- `read_state() -> dict`;
- `paths` property for diagnostics.

State names map to files:

- `signals` → `signals/inbox.jsonl`;
- `events` → `events.jsonl`;
- `candidates` → `candidates.jsonl`;
- `threads` → `threads.jsonl`;
- `decisions` → `decisions.jsonl`.

**Tests:**

- temp state dir is created;
- append/read round trip works;
- corrupted JSONL line is skipped with error marker or raises controlled error. Choose one and document it.

---

### Task 5: Implement deterministic gate

**Objective:** Promote signals into events and events into candidates without model calls.

**Files:**

- Create: `~/.hermes/plugins/agent-sensorium/gate.py`
- Test: `~/.hermes/plugins/agent-sensorium/tests/test_gate.py`

**Implementation notes:**

Functions:

- `signal_fingerprint(signal: dict) -> str`;
- `should_promote_signal(signal: dict, config: dict) -> tuple[bool, str]`;
- `promote_signal_to_event(signal: dict, config: dict) -> dict`;
- `event_to_candidate(event: dict, config: dict) -> dict`;
- `candidate_fingerprint(candidate: dict) -> str`.

Initial deterministic rules:

- promote if `strength_hint >= config.thresholds.single_signal_strength`;
- promote if `kind in config.promote_kinds` and strength exceeds lower kind threshold;
- candidate `pressure` can be simple weighted average of strength, identity relevance hint, actionability hint, and recency.

Default config:

```json
{
  "thresholds": {
    "single_signal_strength": 0.75,
    "important_kind_strength": 0.6,
    "candidate_pressure": 0.65
  },
  "promote_kinds": ["design_decision", "user_correction", "artifact_created", "unresolved_question", "task_result"]
}
```

**Tests:**

- weak signal does not promote;
- strong signal promotes to event;
- important kind with sufficient strength promotes;
- candidate inherits sensitivity and allowed surfaces;
- duplicate fingerprint is stable.

---

### Task 6: Implement dispatcher dry-run

**Objective:** Select one candidate and optionally create one dormant thread.

**Files:**

- Create: `~/.hermes/plugins/agent-sensorium/dispatcher.py`
- Test: `~/.hermes/plugins/agent-sensorium/tests/test_dispatcher.py`

**Implementation notes:**

Functions:

- `select_candidate(candidates: list[dict], config: dict) -> dict | None`;
- `candidate_to_thread(candidate: dict, config: dict) -> dict`;
- `dispatch_once(store: SensoriumStore, dry_run: bool = True) -> dict`.

MVP behavior:

- choose highest `pressure` candidate above threshold;
- if `dry_run=True`, return planned action only;
- if `dry_run=False`, append dormant thread and decision receipt;
- never create external task/message.

Decision receipt:

```json
{
  "ts": "...",
  "type": "dispatch.promoted_to_thread",
  "candidate_id": "cand_...",
  "thread_id": "sth_...",
  "dry_run": false
}
```

**Tests:**

- no candidates returns `no_candidate`;
- below threshold writes/returns suppression;
- above threshold creates exactly one dormant thread;
- repeated dispatch does not create duplicate thread for same candidate.

---

### Task 7: Implement tools

**Objective:** Register model-callable tools through the plugin API.

**Files:**

- Create: `~/.hermes/plugins/agent-sensorium/tools.py`
- Modify: `~/.hermes/plugins/agent-sensorium/__init__.py`
- Test: `~/.hermes/plugins/agent-sensorium/tests/test_tools.py`

**Tools:**

1. `sensorium_status`.
2. `sensorium_ingest_signal`.
3. `sensorium_dispatch_once`.
4. `sensorium_candidate_update`.
5. `sensorium_compact`.

`__init__.py` should call `tools.register_tools(ctx)`.

Tool return shape:

```json
{
  "success": true,
  "instance": "sera",
  "data": {...},
  "error": null
}
```

**Important:** Tool handlers must return JSON strings if registering directly through Hermes registry expectations.

**Tests:**

- handlers can be called directly without Hermes runtime;
- ingest then status shows signal/event/candidate count;
- dispatch creates dormant thread when not dry-run;
- candidate update can suppress a candidate.

---

### Task 8: Add pull command

**Objective:** Provide a human-facing pull surface before any proactive delivery.

**Files:**

- Create: `~/.hermes/plugins/agent-sensorium/commands.py`
- Modify: `~/.hermes/plugins/agent-sensorium/__init__.py`

**Command:**

Register `/sensorium` with optional args:

- no args or `status`: compact status;
- `threads`: top 5 visible dormant/active/held threads;
- `dispatch dry-run`: dry-run scheduler;
- `help`: usage.

**Verify:**

Manual, after plugin enable/reset:

```text
/sensorium status
/sensorium threads
```

Expected: compact local-only report, no external delivery.

---

### Task 9: Add compaction and TTL cleanup

**Objective:** Archive stale candidates/threads and cap visible state.

**Files:**

- Modify: `~/.hermes/plugins/agent-sensorium/store.py`
- Modify: `~/.hermes/plugins/agent-sensorium/tools.py`
- Test: `~/.hermes/plugins/agent-sensorium/tests/test_store.py`

**Rules:**

- expired candidates become archived decision receipts;
- dormant expired threads become stale/archived;
- closed/archived excluded from default status;
- raw JSONL can remain for MVP, but status filters correctly.

**Tests:**

- expired dormant thread disappears from visible status after compaction;
- archive receipt exists;
- pinned thread is not archived.

---

### Task 10: Add shadow tick script

**Objective:** Provide a deterministic no-agent tick for local/manual smoke, not a scheduled cron yet.

**Files:**

- Create: `~/.hermes/plugins/agent-sensorium/scripts/sensorium_tick.py`

**Behavior:**

- args: `--instance sera`, `--dry-run`, `--state-dir PATH`;
- loads store;
- optionally promotes pending signals;
- dispatches once;
- prints compact JSON result;
- no model calls;
- no outbound messages.

**Verify:**

```bash
python ~/.hermes/plugins/agent-sensorium/scripts/sensorium_tick.py --instance sera --dry-run
```

Expected: JSON with `success: true` and no side effects beyond local state reads.

---

### Task 11: Add Sera local config and one seed smoke signal

**Objective:** Prove the pipeline with one hand-authored signal without enabling autonomy.

**Files:**

- Create: `~/.hermes/agent-sensorium/sera/config.json`
- Create or append: `~/.hermes/agent-sensorium/sera/signals/inbox.jsonl`

Seed signal:

```json
{"sensor":"explicit_operator_signal","source":"manual","source_ref":"discord:#pics:2026-05-24","kind":"design_decision","summary":"Operator corrected that Sera identity images should use references for sustained continuity; mood-cards are non-canon exploration.","actor":"operator","strength_hint":0.9,"correlation_keys":["sera-visual-continuity","references-first"],"sensitivity":"private","allowed_surfaces":["local","dashboard"],"ttl_hours":168}
```

**Verify:**

- ingest succeeds;
- event/candidate created;
- dry-run dispatch proposes dormant thread;
- non-dry dispatch creates exactly one dormant thread;
- `sensorium_status` shows it.

---

## Acceptance checklist

MVP is accepted only when all are true:

- [ ] Plugin loads without upstream source edits.
- [ ] All plugin unit tests pass.
- [ ] Manual signal ingest works.
- [ ] Deterministic promotion creates Event and Candidate.
- [ ] Dispatch creates at most one dormant thread per candidate.
- [ ] `sensorium_status` shows top visible candidates/threads.
- [ ] No model call is required.
- [ ] No outbound message is sent.
- [ ] No platform thread is created.
- [ ] No cron job is installed.
- [ ] Enhancement proposals remain clearly marked as proposals.

---

## Commands for implementation verification

From plugin dir:

```bash
cd ~/.hermes/plugins/agent-sensorium
python -m pytest tests -q
python -m py_compile __init__.py schemas.py store.py gate.py dispatcher.py tools.py commands.py scripts/sensorium_tick.py
```

Hermes-side smoke after enabling plugin and starting a new session:

```text
/sensorium status
```

Local script smoke:

```bash
python ~/.hermes/plugins/agent-sensorium/scripts/sensorium_tick.py --instance sera --dry-run
```

---

## Review gates before expanding scope

Do not implement the following until MVP is demonstrated and reviewed:

1. active-session pointer injection;
2. model-backed Subconscious prompt;
3. `REACH_OUT` relational autonomy;
4. Discord/platform thread creation;
5. dashboard UI;
6. Hindsight/RSS/file sensors;
7. external Kanban/research/media task creation.

The next meta pass should ask only:

```text
Does the local pull-based Sensorium spine produce useful, low-noise dormant threads from real signals?
```

If no, tune sensors/thresholds before adding delivery.
If yes, add active-session pointer injection as the next narrow extension.
