# Agent Sensorium

Bounded inner-lifecycle substrate for Hermes agents. Captures salient signals, promotes them through a deterministic pipeline (signal → event → candidate → conscious thread capsule), and surfaces policy-gated attention for pull-based review.

## Repair posture (for the host agent reading this skill)

If you are a host agent (Sera, Sebastian's other leads) and you are reading this skill because Sensorium itself is broken or you want to change it: **do not patch the substrate inline.** Sensorium is an *attention substrate*, and altering it from the foreground compromises the very attention you are using to fix it.

- ≤3 tool calls, reversible, mostly judgment/synthesis → foreground is fine (a `status`/`ingest` smoke, a one-line config read).
- 4+ tool calls, repo-wide inspection, code patch, migration, gateway/runtime sync, ambiguous substrate repair, or anything touching `~/.hermes/plugins/agent-sensorium/` live state → dispatch a worker lane (OMC `/ultrawork` in an isolated worktree, or OMX for surgical). The host keeps the DM cockpit; the lane does the work; the host reviews the report and decides.

If a foreground patch was already started before this rule was checked: stop, save `git diff --binary`, revert, and seed the lane with the captured WIP. Do not finish the patch in the foreground.

## Ordinary use: the `sensorium` live tool

During normal operation exactly one tool is exposed: `sensorium`. It always operates on the active/default profile. You do not pass a profile name on ordinary calls.

### Actions

| Action | Purpose |
|--------|---------|
| `status` | Read current attention state: open thread count, top candidate, inbox summary, dispatcher lock status. Optionally pass `reference_id="<thread_or_candidate_id>"` to pin to one exact subject — returns an `exact_subject` block with `kind`, `id`, `title`, `status`, and (for saved residue) `kanban_settlement`. Use this when you saw a specific pointer earlier and want to recover the exact subject without scanning the rotating top-N. |
| `ingest` | Record deferred salience from the current session as a compact signal. Pass `text`, `kind`, and optionally `strength` (0–1). The tool fills correlation keys and metadata internally. |
| `open` | Open a dormant conscious thread capsule. Pass `id` of the thread (visible from `status`). Returns the capsule content if the surface/sensitivity gate allows it. |
| `update` | Apply a lifecycle keyword to an open thread. Pass `id` and `keyword` (e.g. `close`, `hold`, `archive`, `mark_reviewed`). |
| `reach_out` | Record or prepare a Conscious reach-out decision with a compact receipt. The ordinary live call is fixed to `execute=False`; it cannot deliver. |

`reach_out` belongs to the Conscious tier only. It may record or prepare a chosen decision subject to policy, target, sensitivity, and cooldown gates, but it never dispatches from the ordinary live tool. Actual delivery requires separate explicit configuration with direct delivery disabled by default and an adapter-backed actuator outside this live call. Receipts remain compact and exclude message bodies.

### When to use `ingest`

Call `sensorium(action="ingest", text="...", kind="...", strength=0.7)` when the current exchange contains:
- An explicit correction from your user or a design decision you should not lose
- Durable relational salience or a "this matters" cue
- An unresolved question worth revisiting later

Do not dump full transcripts or raw messages. Keep `text` compact.

### When to use `open`

The pre-LLM hook may inject a pointer. There are three pointer shapes:

1. **Thread pointer** — `Pointer type: thread — <thread_id>`. There IS an openable
   conscious thread. If the user says "take it up", call
   `sensorium(action="open", id="<thread_id>")` and surface the capsule content.
2. **Candidate pointer** — `Pointer type: candidate (NOT an openable thread) — <candidate_id>`.
   There is a salience candidate in the attention inbox but no thread capsule.
   If the user says "take a look" / "check the inbox", call
   `sensorium(action="open", id="<candidate_id>", surface=...)` using the
   exact id from the pointer and surface the returned compact candidate capsule
   (`object_kind: "candidate"`, `is_openable_thread: false`). Do NOT switch
   to a fresh `status` pointer after the pointer has been presented: cooldown
   selection can advance and return a different candidate/residue than the one
   the user is responding to. Do NOT improvise "thread X is waiting" when the
   pointer says candidate.
3. **Saved-residue pointer** — `Pointer type: saved_residue (Kanban SAVE/PROMOTE_CONSCIOUS; NOT an openable thread) — <candidate_id>`.
   This was previously saved to Kanban but never became a thread. Conscious
   access is preserved via an honest doorway that links to the saved intake
   task id. If the user wants a recap, call
   `sensorium(action="open", id="<candidate_id>", surface=...)` using the
   exact id from the pointer and report the compact candidate capsule plus the
   kanban intake/review task ids. Do NOT use `status` as the primary lookup for
   a presented saved-residue pointer, because cooldown selection can advance
   and show a different saved residue. Do NOT improvise "thread X is waiting
   for you" — say honestly that this was saved, not opened.

   Operators can dial the saved-residue pathway down via two opt-in pointer
   config keys (both default `None` = unlimited):
   - `pointer.saved_residue_max_age_days` — drop residues whose
     `kanban_settlement.settled_at` is older than N days. Use this to keep
     "archive-confetti" out of the live turn.
   - `pointer.saved_residue_max_items` — keep only the top-N after the
     freshness sort. Use this to bound rotation through archived residue
     when no active candidate exists.
   The freshest settled residue wins at equal pressure; a higher-pressure
   older residue still wins over a fresh low-pressure one.

That confident phrase "I have a thread waiting for you" is valid only when
Sensorium has produced a thread pointer. For candidate and saved-residue
pointers, the surface-facing copy already says so; mirror that honestly when
speaking to the user. If status shows no openable thread AND no surface-visible
candidate AND no saved residue, do not improvise — use uncertainty phrasing
such as "I think I have something that might matter — want me to look?" or
"I have some unsettled salience — want me to surface it?"

When your user explicitly says "take it up" (or equivalent) for a thread pointer,
call `sensorium(action="open", id="<id from pointer>")`. For candidate or saved-residue
pointers, "take it up" should map to `sensorium(action="open", id="<candidate_id>")`
when an explicit candidate id is shown — that returns the compact candidate capsule,
not a fake thread. Never call `sensorium(action="open", id="latest")` and then tell
the user "thread is opened" if the response is "Thread 'latest' not found." Instead
be honest about what the substrate returned.

The pointer is a doorway only — capsule internals are not returned until `open` is
called and the surface/sensitivity gate passes.

---

## Pipeline summary

```
Sensors → Signals → [Gate] → Events → Candidates → [Dispatcher] → Conscious Threads
                                           Conscious choice → prepare-only Actuator → local Artifact
```

1. **Sensors** emit compact signals (observations, corrections, artifacts).
2. A deterministic **Gate** promotes strong signals to **Events** based on hot-loaded strength/kind thresholds.
3. Events create **Candidates** with weighted pressure scores.
4. A **Dispatcher** promotes the top candidate into a dormant **Conscious Thread** capsule.
5. The **pre-LLM hook** injects a compact pointer when an eligible thread is available.
6. Optional **actuators** are hot-reloadable, trusted local scripts that prepare artifacts only after a conscious decision ref; they never authorize delivery.

### Memory Volunteering Protocol

Any Sensorium/Subconscious path that wants to volunteer a memory, insight, recalled fact, private salience, or offer candidate must follow:

```text
evidence-cited capsule -> transparent confidence proposal -> Conscious authorization receipt
```

Confidence is an attention-routing signal only. It may justify `ATTENTION_INBOX` or `CONSCIOUS_REVIEW`; it may not write Hindsight/Memory/LCM, create durable truth, generate confident personal offer language, create external tasks, prepare artifacts, or authorize delivery by itself. The capsule must carry resolvable evidence refs, explicit `do_not_know` gaps, sensitivity, and allowed surfaces. The proposal must expose confidence components and negative evidence. Durable memory writes, skill/doc changes, artifact presentation, and outbound delivery require an explicit Conscious authorization receipt plus any configured operator/policy gate.

See `docs/memory-volunteering-protocol.md` in the plugin docs for the formal schema, threshold defaults, source-type rules, and acceptance probes.

---

## Profiles

A *profile* is a named runtime namespace (config + state) under the Sensorium state root `~/.hermes/agent-sensorium/<profile>/`. Each profile has its own `instance.config.json`, signal/event/candidate/thread state, sensor registry, and optional actuator registry.

The `default` profile is the portable fallback. Multiple profiles (e.g. `default`, `demo`) can coexist.

**Active profile resolution order:**
1. Env var `AGENT_SENSORIUM_DEFAULT_INSTANCE` (or `SENSORIUM_INSTANCE`)
2. `active_profile.json` marker at the state root (set via `sensorium_profile set_default`)
3. Hermes config `agent_sensorium.default_instance`
4. `default`

---

## Admin/config interface: `agent-sensorium-admin`

Load this toolset only when you need setup, configuration, or diagnostics. It is not surfaced in ordinary agent sessions.

### `sensorium_profile` — profile management

| Action | Description |
|--------|-------------|
| `list` | List all profiles under the state root, marking the active/default one |
| `show` | Show resolved config + diagnostics for a profile (defaults to active) |
| `init` | Create a new profile namespace and write a default `instance.config.json` |
| `set_default` | Set the active/default profile |

### `sensorium_sensor_config` — sensor registry

Config-only tool for the per-profile sensor registry (`sensors/registry.json`). Never runs sensors or performs external action.

Sensor registries and profile config are hot-loaded by runners/tool handlers. Editing `instance.config.json` thresholds/`promote_kinds` or `sensors/registry.json` takes effect on the next call/tick; no gateway restart is needed unless you add plugin Python modules, hooks, dashboard routes, or model-visible tool schemas.

| Action | Description |
|--------|-------------|
| `list` | List registered sensors for the active profile |
| `register` | Register a new sensor definition |
| `modify` | Update a registered sensor's config |
| `pause` | Pause a sensor (stops tick ingestion) |
| `deprecate` | Mark a sensor deprecated |

### Prepare-only actuator registry

Optional actuators live under `~/.hermes/agent-sensorium/<profile>/actuators/registry.json`. They are trusted local scripts that prepare local artifact records only after conscious review. They are intentionally not exposed through the live `sensorium` tool.

Rules for agents/operators extending actuators:

- Keep `impl.command` an argv list; never use shell strings.
- Put scripts under an allowed local `script_roots` entry.
- Keep `input_contract.requires_conscious_decision=true` unless a human explicitly designs a different internal-only lane.
- Keep `output_contract.delivery_authorized=false`; delivery/outbound action belongs to a separate conscious/operator policy gate.
- Do not echo private prompt/message text into receipts, stdout metadata, or dashboard fields.

See `examples/demo-actuator-registry.json` and `examples/demo_script_actuator.py` for a generic prepare-only canary.

### Other admin tools

The admin surface also includes granular tools for direct signal/event ingestion, dispatch, thread service, subconscious advisory, outbox management, artifact records, thread actions, and probe audit. These are for setup, inspection, and controlled intervention — not for ordinary agent use.

---

## Agent quickstart: enable the demo profile

These are admin/CLI surfaces — never the live `sensorium` tool. Load `agent-sensorium-admin` before running these steps.

1. **List profiles** — `sensorium_profile(action="list")`
2. **Init demo** — `sensorium_profile(action="init", profile="demo")`
3. **Set default** — `sensorium_profile(action="set_default", profile="demo")`
4. **Show config** — `sensorium_profile(action="show", profile="demo")`
5. **Register or seed sensors/actuators** —
   ```python
   sensorium_sensor_config(action="register", name="runtime_heartbeat", defaults={"strength": 0.1, "surfaces": ["local"], "local_only": True})
   sensorium_sensor_config(action="pause", name="runtime_heartbeat")
   ```
   Or seed generic examples from the CLI; this writes both `sensors/registry.json` and `actuators/registry.json` when `--apply` is passed.
6. **Seed and tick** —
   ```bash
   python scripts/sensorium_demo_seed.py --instance demo --apply
   python scripts/sensorium_tick.py --instance demo --heartbeat --all-sensors --dry-run --json
   ```

---

## Key boundaries

- **Pull-based.** Nothing is pushed. The agent and user/admin request status; the pipeline does not deliver unsolicited messages.
- **No autonomous outbound delivery.** `reach_out` records/prepares with `execute=False`; artifacts queued in the outbox are never delivered without separate explicit configuration, a conscious receipt, and an adapter-backed dispatch path outside the ordinary live tool.
- **No external task creation without explicit approval.**
- **Subconscious advisory is disabled by default.** The cheap model lane is explicit opt-in (`--subconscious-model` flag on tick).
- **Missing config fails safe.** Local-only surfaces, private sensitivity.

---

## Shadow Tick

`scripts/sensorium_tick.py` runs deterministic lifecycle operations (compact, service, optional sensors, optional advisory, dispatch preview, status) without model calls or outbound delivery.

```bash
# Dry-run tick on a named profile
python scripts/sensorium_tick.py --instance demo --dry-run --json

# Full tick with all pressure sensors
python scripts/sensorium_tick.py --instance demo --all-sensors --json

# Emit a deterministic runtime heartbeat signal (opt-in, not part of --all-sensors)
python scripts/sensorium_tick.py --instance demo --heartbeat --json
```

Silent on stdout by default (safe for cron). Use `--json` for output. Use `--dry-run` to skip all mutations. `--heartbeat` emits a compact deterministic beacon (counts/names only, no model calls, stdlib-only).

---

## Conscious review actions

When reviewing a dormant thread, choose exactly one:

- **Suppress** — noise, not actionable
- **Hold** — interesting but not urgent; revisit later
- **Save** — convert to workflow guidance or reference note
- **Close** — resolved or no longer relevant
- **Create follow-up** — bounded, specific next action only

Do not auto-send messages, create external tasks, or act on expired/archived threads.
