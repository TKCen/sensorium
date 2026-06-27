# Live Salience Ingest and Turn-Review Design

Status: design note before further implementation changes  
Date: 2026-06-27

## Problem

The live `sensorium(action='ingest')` aperture is meant to preserve compact salience that remains alive outside the foreground turn. It should not duplicate work the foreground agent is already answering, patching, retaining, or otherwise resolving.

A recent failure mode exposed the boundary:

```text
foreground conversation actively handled a Sensorium design issue
+ agent still ingested the same issue into Sensorium
= duplicate foreground/shadow backlog
```

The opposite failure mode is also possible:

```text
stronger "foreground owns this" wording
+ agent becomes too conservative
= Sensorium starvation / lost durable salience
```

The improvement target is not more governance for its own sake. The goal is real goal-gradient movement: reduce duplicate salience, preserve important residue, reduce future steering, and make each future turn cheaper and more likely to do the right thing.

## Current live-seam observation

The code on disk may differ from the currently injected live gateway hook until the gateway/session reloads. Treat hook text seen in a live turn as the actual active behavior for that turn. File-level tests prove the intended hook renderer, not live propagation.

Required verification before claiming a prompt/hook change is live:

1. Inspect the file-level renderer output.
2. Run focused tests for the hook wording.
3. Start a fresh gateway/session or otherwise reload plugin hooks.
4. Confirm the next live turn contains the new hook text.

## Definitions

### Foreground-owned salience

A salient item is foreground-owned when the current turn is already doing the relevant work:

- answering it directly;
- patching code/config/docs/tests for it;
- writing durable memory or a skill for it;
- creating a task/artifact for it;
- making an explicit no-action decision for it;
- asking the user for the needed decision.

Foreground-owned salience should not be ingested into Sensorium just because it is important.

### Unresolved residue

Unresolved residue is a compact leftover that remains after the foreground turn and is not otherwise captured/actioned. Examples:

- a future watch risk that cannot be verified now;
- a pattern that should accumulate across sessions;
- an emotional/relational/design pressure that remains meaningful after the direct answer;
- a later-review need that is deliberately not a foreground task.

Only unresolved residue should use `sensorium(action='ingest')` from the live turn.

### Sensorium starvation

Starvation is when the live gate suppresses too much:

- meaningful corrections/design insights/relational salience occur;
- the foreground does not persist them elsewhere;
- no Sensorium residue is created;
- later reviews show important pressure was missed.

### Over-ingestion

Over-ingestion is when Sensorium accumulates shadow backlog:

- active-session signals repeat what the foreground already handled;
- candidates remain active after the user saw/resolved them;
- Sensorium asks for review of things already patched, documented, or answered.

## Intended live-agent decision rule

```text
1. Handle the user's live turn normally.
2. Ask whether the salient item is foreground-owned.
3. If foreground-owned and fully resolved: do not ingest.
4. If foreground-owned but a compact unresolved residue remains: ingest only that residue.
5. If not foreground-owned and the item should persist as attention rather than a task: ingest.
6. If the item needs durable procedural/factual memory instead: use skill/memory/Hindsight/docs, not Sensorium ingest.
```

The short form:

```text
Foreground work goes to foreground tools.
Unresolved residue goes to Sensorium.
Durable facts/procedures go to memory/skills/docs.
```

## Structured receipt target

The live agent currently has only a blunt `sensorium(action='ingest')` handle. Future work should add or emulate a structured decision receipt so review can distinguish good suppression from starvation.

Candidate fields:

```json
{
  "foreground_action_taken": true,
  "foreground_resolution": "full | partial | none | explicit_no_action",
  "residue": "none | watch | later_review | pattern_pressure",
  "sensorium_ingest_allowed": false,
  "durable_capture": "none | memory | skill | docs | task | artifact",
  "background_action_allowed": false,
  "reason": "compact non-sensitive reason"
}
```

This receipt does not have to be injected every turn. It can be internal to a turn-review helper or emitted only when the decision is non-obvious.

## Live hook wording requirements

The hook should bias against duplicate ingest without forbidding residue capture.

Required semantics:

- say to handle the live turn normally first;
- say not to ingest merely because the turn is important;
- say not to ingest when answering/acting/retaining/patching now;
- allow ingest for unresolved residue that the foreground will not settle;
- preserve the `compact signal, not a task` boundary;
- remain under the existing small-context budget.

Example target wording:

```text
Do not ingest merely because the turn is important if you are answering,
acting on, retaining, or patching it now. Only call sensorium(action='ingest')
for unresolved residue: something important that the foreground turn will not
settle and should persist as a compact signal, not a task.
```

## Cheap turn-review safety net

A cheap review layer can reduce starvation without full-session ingestion.

### Why not full-session ingest?

Full-session ingestion is expensive and tends to create memory/context pressure. It also violates the Sensorium design goal: the agent should consciously decide what deserves attention rather than vacuuming whole transcripts.

### Proposed review unit

Review only a small bounded slice:

- last user message;
- assistant final response;
- tool actions taken in the turn;
- whether memory/skill/docs/patch/Sensorium action happened;
- optionally 1-2 previous turns for context.

### Review question

```text
Did this turn contain durable salience that was neither handled,
retained, patched, documented, tasked, nor intentionally ignored?
```

### Review outputs

```text
no_action
sensorium_residue_candidate
memory_candidate
skill_candidate
docs_candidate
followup_review_needed
```

The reviewer should be cheap and mostly deterministic. A small/cheap model can be used only when simple receipt/keyword checks are ambiguous.

## Dashboard/API projection

The dashboard snapshot exposes a transcript-free `live_turn_metrics` object derived from `decisions.jsonl` rows of type `live_turn.ingest_decision`.

Allowed projection shape:

```json
{
  "receipt_type": "live_turn.ingest_decision",
  "receipt_count": 2,
  "ingested_count": 1,
  "skipped_count": 1,
  "foreground_owned_no_residue_count": 1,
  "captured_elsewhere_no_residue_count": 0,
  "residue_breakdown": {"none": 1, "watch": 1},
  "foreground_resolution_breakdown": {"full": 1, "partial": 1},
  "durable_capture_breakdown": {"docs": 1, "none": 1},
  "skipped_reason_breakdown": {"foreground_owned_no_residue": 1},
  "background_action_allowed_count": 0,
  "latest_ts": "2026-06-27T10:01:00Z",
  "recent": [
    {
      "ts": "2026-06-27T10:01:00Z",
      "ingested": true,
      "residue": "watch",
      "foreground_resolution": "partial",
      "durable_capture": "none",
      "skipped_reason": "none",
      "background_action_allowed": false
    }
  ]
}
```

Projection privacy contract:

- do **not** return receipt `summary`, `surface`, raw `reason`, ids, correlation keys, or signal ids;
- only closed-vocabulary fields may be counted/rendered;
- unknown scalar values collapse to `unknown` / `other`, never to the persisted string;
- the dashboard may show aggregate live-turn counts and the closed-vocabulary recent timeline, but not raw receipt text.

The pending review lane uses `live_turn.review_decision` receipts. These are produced by bounded turn-review probes and expose only closed-vocabulary posture plus input sizes:

```json
{
  "receipt_type": "live_turn.review_decision",
  "receipt_count": 2,
  "pending_review_count": 1,
  "no_action_count": 1,
  "decision_breakdown": {"sensorium_residue_candidate": 1, "no_action": 1},
  "reason_breakdown": {"salience_cue_without_capture": 1, "salience_captured_elsewhere": 1},
  "recent": [
    {
      "ts": "2026-06-27T10:01:00Z",
      "decision": "sensorium_residue_candidate",
      "reason": "salience_cue_without_capture",
      "pending_review": true,
      "has_salience_cue": true,
      "durable_capture_seen": false,
      "background_action_allowed": false
    }
  ]
}
```

A pending review receipt is not a signal, not a task, and not permission for autonomous outbound action. It is the safe intermediate surface for possible starvation: foreground can inspect the count/posture, then deliberately decide whether to open a Sensorium signal, durable memory/skill/docs capture, or no action.

## Monitoring metrics

Do not judge the gate by raw signal counts alone. Track balance:

```text
foreground_salience_cues
foreground_actions_taken
sensorium_ingests_from_live_turns
turn_review_residue_found
overlap_with_foreground_resolved_work
operator_corrections_about_over_ingestion
operator_corrections_about_missed_salience
```

Useful derived indicators:

```text
over_ingestion_rate = ingests_later_marked_duplicate / live_turn_ingests
starvation_rate = reviewer_found_missed_residue / reviewed_turns
```

Thresholds should start conservative and be tuned from observed behavior, not invented as permanent policy.

## Context-aware resource/body capsule interaction

Resource/body-state injection is related but separate. The same principle applies:

```text
Only inject resource/body-state facts relevant to the current turn's likely action.
```

Avoid telemetry dumps. Inject a short behavior hint only when it should change behavior:

```text
[Body Hint] conserve frontier budget; batch tools and ask before expensive fanout.
```

No hint is better than irrelevant hint. Irrelevant resource text consumes the very context/energy it claims to protect.

## Agent-wieldable Sensorium direction

Future Sensorium should be easier for agents to wield via simple action-oriented handles rather than requiring them to understand the full substrate.

Desired live affordances:

```text
check: what should I know right now?
notice_residue: compact unresolved residue after foreground work
settle: mark this handled/seen/resolved
recommend: return action posture, not dashboard state
```

Good output shape:

```json
{
  "attention": "possible_residue",
  "why": "foreground handled implementation but future starvation risk remains",
  "do": ["record_watch_item"],
  "say_to_user": "",
  "background_action_allowed": false
}
```

## Proposed implementation slices

### Slice 1 — live hook propagation and wording verification

- Keep the stronger gate wording small.
- Add/keep focused tests asserting the anti-duplicate semantics.
- Verify the live gateway/session injects the new text after reload.

Acceptance:

```text
file renderer shows new text
tests pass
fresh live turn shows new text
```

### Slice 2 — structured live-ingest intent

- Extend the live ingest contract or add a helper wrapper for `foreground_action_taken`, `foreground_resolution`, and `residue` intent.
- Default `background_action_allowed=false`.
- Preserve compact/safe summaries only.
- Implement reusable logic in instance-neutral code; deployment policy and schedules belong in instance config/scripts.

Acceptance:

```text
agent can mark residue without implying task/action authority
foreground-owned fully resolved items are not promoted as fresh review work
```

Implemented core shape:

- `agent_sensorium/live_turn.py` normalizes closed-vocabulary live-turn intent and builds compact `live_turn.ingest_decision` receipts.
- The live `sensorium(action='ingest')` schema accepts `foreground_action_taken`, `foreground_resolution`, `residue`, `durable_capture`, and `background_action_allowed`.
- If `foreground_action_taken=true`, `foreground_resolution=full|explicit_no_action`, and `residue=none`, the live tool records a no-ingest receipt and writes no signal.
- If residue is present, the signal carries `live_turn_intent` and residue/foreground correlation keys.

### Slice 3 — cheap turn-review prototype

- Implement a bounded review over last-turn snippets and action receipts.
- Start as manual/scripted or low-frequency; do not ingest full sessions.
- Emit review results as compact candidates only when salience was missed.
- Keep automatic cadence/wiring instance-specific until there is evidence the generic helper is useful.

Acceptance:

```text
review catches a planted missed-residue fixture
review ignores a foreground-resolved fixture
review stays within bounded token/input budget
```

Implemented core shape:

- `review_turn_for_residue(...)` in `agent_sensorium/live_turn.py` is a deterministic, write-free helper over bounded turn snippets and action booleans.
- It returns `no_action` for already-captured/documented/Sensorium-ingested salience and `sensorium_residue_candidate` for uncaptured salience cues.
- `build_turn_review_receipt(...)` turns review results into transcript-free `live_turn.review_decision` receipts.
- `scripts/sensorium_live_turn_review.py` provides a manual bounded probe and optional receipt append path; it does not read full sessions or create signals.
- It does not install a scheduler, read transcripts, or write Sensorium state unless the manual `--append-receipt` flag is used; automatic deployment choices belong in instance scripts/config.

### Slice 4 — balance dashboard/metrics

- Track over-ingestion and starvation indicators.
- Separate live pressure from historical residue.
- Make the metric useful for tuning, not a new noisy attention source.

Acceptance:

```text
dashboard/API can show recent live-turn ingest count, duplicate/settled count,
and reviewer-found missed residue without exposing raw transcripts
```

Implemented core shape:

- `/snapshot` exposes `live_turn_metrics` for ingest receipts and `live_turn_review_metrics` for pending bounded-review receipts.
- Dashboard priority metrics include both live-turn receipt balance and turn-review pending count.
- Both projections collapse unknown persisted scalars and sanitize invalid timestamps before rendering.

## Non-goals

- No full-session ingestion as the default fix.
- No autonomous outbound action.
- No broad model-backed review every turn.
- No telemetry/context dump into every live prompt.
- No replacing the working signal/event/candidate/thread pipeline.

## Open questions

- Should the turn-review run after every live turn, only sampled turns, or only when cues/risk thresholds trigger?
- Should review results write directly to Sensorium, or first to a pending receipt requiring foreground/operator acceptance?
- What is the cheapest reliable model/path for ambiguous review?
- How should live hook propagation be verified in Discord/gateway sessions without manual transcript inspection?
- What exact thresholds indicate starvation versus healthy quiet?
