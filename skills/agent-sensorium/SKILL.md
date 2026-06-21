# Agent Sensorium

Bounded inner-lifecycle substrate for Hermes agents. Captures salient signals, promotes them through a deterministic pipeline (signal → event → candidate → conscious thread capsule), and surfaces policy-gated attention for pull-based review.

## Ordinary use: the `sensorium` live tool

During normal operation exactly one tool is exposed: `sensorium`. It always operates on the active/default profile. You do not pass a profile name on ordinary calls.

### Actions

| Action | Purpose |
|--------|---------|
| `status` | Read current attention state: open thread count, top candidate, inbox summary, dispatcher lock status |
| `ingest` | Record deferred salience from the current session as a compact signal. Pass `text`, `kind`, and optionally `strength` (0–1). The tool fills correlation keys and metadata internally. |
| `open` | Open a dormant conscious thread capsule. Pass `id` of the thread (visible from `status`). Returns the capsule content if the surface/sensitivity gate allows it. |
| `update` | Apply a lifecycle keyword to an open thread. Pass `id` and `keyword` (e.g. `close`, `hold`, `archive`, `mark_reviewed`). |

### When to use `ingest`

Call `sensorium(action="ingest", text="...", kind="...", strength=0.7)` when the current exchange contains:
- An explicit correction from your user or a design decision you should not lose
- Durable relational salience or a "this matters" cue
- An unresolved question worth revisiting later

Do not dump full transcripts or raw messages. Keep `text` compact.

### When to use `open`

The pre-LLM hook may inject a pointer like:

```
I have something for you: <short title>. Say "take it up" to open it.
```

That confident phrase is valid only when Sensorium has already produced a visible/openable pointer for the current surface. Do not improvise it before checking `sensorium(action="status")` or receiving a hook-provided pointer. If status shows no openable thread but you feel residual salience, use uncertainty phrasing instead, such as “I think I have something that might matter — want me to look?” or “I have some unsettled salience — want me to surface it?”

When your user says "take it up" (or equivalent), call `sensorium(action="open", id="<id from pointer>")`.

The pointer is a doorway only — capsule internals are not returned until `open` is called and the surface/sensitivity gate passes.

---

## Pipeline summary

```
Sensors → Signals → [Gate] → Events → Candidates → [Dispatcher] → Conscious Threads
```

1. **Sensors** emit compact signals (observations, corrections, artifacts).
2. A deterministic **Gate** promotes strong signals to **Events** based on strength/kind thresholds.
3. Events create **Candidates** with weighted pressure scores.
4. A **Dispatcher** promotes the top candidate into a dormant **Conscious Thread** capsule.
5. The **pre-LLM hook** injects a compact pointer when an eligible thread is available.

---

## Profiles

A *profile* is a named runtime namespace (config + state) under the Sensorium state root `~/.hermes/agent-sensorium/<profile>/`. Each profile has its own `instance.config.json`, signal/event/candidate/thread state, and sensor registry.

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

| Action | Description |
|--------|-------------|
| `list` | List registered sensors for the active profile |
| `register` | Register a new sensor definition |
| `modify` | Update a registered sensor's config |
| `pause` | Pause a sensor (stops tick ingestion) |
| `deprecate` | Mark a sensor deprecated |

### Other admin tools

The admin surface also includes granular tools for direct signal/event ingestion, dispatch, thread service, subconscious advisory, outbox management, artifact records, thread actions, and probe audit. These are for setup, inspection, and controlled intervention — not for ordinary agent use.

---

## Agent quickstart: enable the demo profile

These are admin/CLI surfaces — never the live `sensorium` tool. Load `agent-sensorium-admin` before running these steps.

1. **List profiles** — `sensorium_profile(action="list")`
2. **Init demo** — `sensorium_profile(action="init", profile="demo")`
3. **Set default** — `sensorium_profile(action="set_default", profile="demo")`
4. **Show config** — `sensorium_profile(action="show", profile="demo")`
5. **Register a sensor / pause it** —
   ```python
   sensorium_sensor_config(action="register", name="runtime_heartbeat", defaults={"strength": 0.1, "surfaces": ["local"], "local_only": True})
   sensorium_sensor_config(action="pause", name="runtime_heartbeat")
   ```
6. **Seed and tick** —
   ```bash
   python scripts/sensorium_demo_seed.py --instance demo --apply
   python scripts/sensorium_tick.py --instance demo --heartbeat --all-sensors --dry-run --json
   ```

---

## Key boundaries

- **Pull-based.** Nothing is pushed. The agent and user/admin request status; the pipeline does not deliver unsolicited messages.
- **No autonomous outbound delivery.** Artifacts queued in the outbox are never delivered without a conscious receipt and explicit user/admin-configured dispatch rules.
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
