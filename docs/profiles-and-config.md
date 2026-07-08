# Profiles and Configuration

This document describes the Agent Sensorium profile model and the boundary between generic plugin code and deployment-specific configuration.

---

## Agent quickstart

These steps use admin surfaces (`agent-sensorium-admin` toolset) — not the live `sensorium` tool.

1. **List profiles** — `sensorium_profile(action="list")` → returns all known profiles, marks the active default.
2. **Init demo** — `sensorium_profile(action="init", profile="demo")` → creates `~/.hermes/agent-sensorium/demo/` with a default `instance.config.json`.
3. **Set default** — `sensorium_profile(action="set_default", profile="demo")` → writes `active_profile.json`; subsequent tool calls resolve to this profile.
4. **Show config** — `sensorium_profile(action="show", profile="demo")` → returns resolved config fields, diagnostics, and policy card reference.
5. **Register / pause a sensor** —
   ```python
   sensorium_sensor_config(action="register", name="runtime_heartbeat", defaults={"strength": 0.1, "surfaces": ["local"], "local_only": True})
   # → confirms registration in sensors/registry.json
   sensorium_sensor_config(action="pause", name="runtime_heartbeat")
   # → marks sensor paused (tick skips ingestion until resumed)
   ```
6. **Seed and tick** —
   ```bash
   python scripts/sensorium_demo_seed.py --instance demo --apply
   # → writes demo registry and optional seed signal to state dir
   python scripts/sensorium_tick.py --instance demo --heartbeat --all-sensors --dry-run --json
   # → returns tick result (no mutations); includes heartbeat beacon
   ```

---

## Profile model

A *profile* is a named runtime namespace under the Sensorium state root:

```
~/.hermes/agent-sensorium/<profile>/
```

Each profile has its own:
- `instance.config.json` — deployment-specific configuration
- `sensors/registry.json` — sensor registry
- `actuators/registry.json` — prepare-only actuator registry (optional)
- Signal, event, candidate, and thread state (`signals/`, `events/`, `candidates/`, `threads/`, `decisions/`)

The `default` profile is the portable fallback. Multiple profiles (e.g. `default`, `demo`) can coexist and are addressed by name in admin and CLI surfaces.

Internally the mechanism is called the "instance"; CLI scripts use `--instance <profile>`.

---

## Active profile resolution

When a tool call or tick script needs to know which profile to operate on, resolution proceeds in this order:

1. Env var `AGENT_SENSORIUM_DEFAULT_INSTANCE` (or `SENSORIUM_INSTANCE`)
2. `active_profile.json` marker at the state root — set via `sensorium_profile set_default`
3. Hermes config `agent_sensorium.default_instance`
4. `default`

Profile names are validated: allowed characters are `[A-Za-z0-9._-]`, non-empty, path traversal rejected.

---

## Managing profiles with `sensorium_profile`

Load the `agent-sensorium-admin` toolset to access profile management. The `sensorium_profile` tool provides:

| Action | Description |
|--------|-------------|
| `list` | List all profiles under the state root, marking the active/default one |
| `show` | Show resolved config and diagnostics for a profile (defaults to active) |
| `init` | Create a new profile namespace and write a default `instance.config.json` |
| `set_default` | Set the active/default profile (writes `active_profile.json` at the state root) |

Example: initialize a new profile named `demo`:

```bash
# via admin tool
sensorium_profile(action="init", profile="demo")

# or via CLI
python scripts/sensorium_tick.py --instance demo --dry-run
```

---

## Code/config boundary

The plugin ships generic reusable code with config seams. Deployment-specific values are read from the per-profile `instance.config.json` at runtime — nothing instance-specific is baked into the code.

### Standard config fields

```json
{
  "instance_name": "demo",
  "policy_card_ref": "docs/examples/demo-policy-card.md",
  "allowed_surfaces": ["local", "dashboard"],
  "max_sensitivity": "private",
  "thresholds": {
    "single_signal_strength": 0.75,
    "important_kind_strength": 0.6,
    "candidate_pressure": 0.65,
    "dispatch_pressure": 0.5,
    "starvation_hours": 72,
    "expiring_window_hours": 24
  },
  "promote_kinds": [
    "design_decision",
    "user_correction",
    "artifact_created",
    "unresolved_question",
    "task_result"
  ],
  "budgets": {
    "dispatch": {"capacity": 10, "window_seconds": 3600},
    "pointer": {"capacity": 12, "window_seconds": 3600}
  }
}
```

### Extended config fields (with generic defaults)

These fields have safe generic defaults and are dormant until configured:

| Field | Default | Purpose |
|-------|---------|---------|
| `default_actor` | `"background_conscious"` | Actor identifier for the deprecated background-conscious lease lane |
| `subconscious_profile` | `"serasubconscious"` | The Hermes profile name the bridge assigns intake to (must be a real dispatcher profile) |
| `tick_quiet_filename` | `"sensorium_tick_quiet.latest.json"` | Dashboard quiet-tick freshness file name |
| `tts` | see below | Local TTS/talking-head sidecar config — dormant until `sidecar_base`/`control_command` are set |

Default `tts` block:

```json
{
  "base_url": "http://127.0.0.1:8892/v1",
  "model": "chatterbox-turbo",
  "voice": "warm-voice-demo",
  "sidecar_base": null,
  "control_command": null,
  "pid_file": null
}
```

The TTS sidecar is disabled when `sidecar_base` and `control_command` are `null`. Set them to activate talking-head output for your deployment.

### Config discovery order

1. Explicit `config_path` argument (passed to tools/handlers)
2. `{state_dir}/instance.config.json` (auto-discovered from instance state directory)
3. Safe defaults: `allowed_surfaces: ["local"]`, `max_sensitivity: "private"`, no policy card

Missing config fails safe: local-only surfaces, private sensitivity, default thresholds.

---

## Policy rules

- **Surface policy** intersects the item's `allowed_surfaces` with the config `allowed_surfaces`. Config can only narrow scope, never broaden it.
- **Sensitivity policy** takes the more restrictive of item sensitivity and config `max_sensitivity`. An item marked `local_only` stays `local_only` even if config allows `public_safe`.
- **Diagnostics** (`sensorium_status`, `sensorium_profile show`, dashboard GET routes) expose compact config/status projections (source labels, path labels, `policy_card_ref`, `instance_name`, `allowed_surfaces`, `max_sensitivity`) — never raw budgets, thresholds, private policy contents, raw transcript/log text, or caller-controlled filesystem paths.

---

## Basic sensors, actuators, and the demo seed

### Generic sensor kinds (`examples/demo-sensor-registry.json`)

Four sensor kinds ship with safe defaults. All are `local_only`, surfaces `["local"]`.

| Sensor | Kind | Notes |
|--------|------|-------|
| `runtime_heartbeat` | deterministic, stdlib-only | Always emits a low-strength beacon. Counts/names only — no file contents, no absolute paths. Safe on a fresh profile (all zeros). |
| `machine_body_pressure` | pressure | Emits only on level transitions (idle → elevated → critical). |
| `machine_network_pressure` | pressure | Emits only on level transitions. |
| `machine_process_pressure` | pressure | Emits only on level transitions. |

**`runtime_heartbeat` reports:** state-dir health, signal/event/candidate/thread counts, pending dormant threads, registry sensor count and names, and last-decision age. Counts and names only — no file contents or absolute paths. Recorded at low strength; not automatically promoted into threads.

Pressure sensors are silent between transitions; the heartbeat always emits.

### Generic actuator registry (`examples/demo-actuator-registry.json`)

The demo actuator registry contains one prepare-only script actuator, `demo_prepare_text_artifact`. It illustrates the authority boundary rather than a production integration:

- registry is read from `actuators/registry.json` at run time;
- script command is an argv list, never a shell string;
- script path must be under an allowed local root;
- input must include a `conscious_decision_ref` before the script runs;
- output may prepare a local artifact record only;
- `delivery_authorized` and `outbound_delivery` stay false.

Actuator registries are optional. If no actuator registry exists, Sensorium continues to work as a sensing/review substrate.

### `scripts/sensorium_demo_seed.py`

Seeds a demo profile with sensor and actuator registries plus an optional signal. **Default behavior is a dry-run — no writes occur without `--apply`.**

| Flag | Default | Description |
|------|---------|-------------|
| `--instance` | `demo` | Profile name to seed |
| `--state-dir` | resolved from profile | Explicit state directory override |
| `--registry <path>` | `examples/demo-sensor-registry.json` | Sensor registry file to import |
| `--actuator-registry <path>` | `examples/demo-actuator-registry.json` | Actuator registry file to import |
| `--seed-signal <path>` | `examples/seed-signal.jsonl` | Optional signal JSONL to ingest when `--ingest-seed` is passed |
| `--apply` | off | Perform writes (required to make changes) |
| `--dry-run` | on | Force no writes (default without `--apply`) |
| `--json` | off | Print result as JSON |
| `--ingest-seed` | off | Also ingest the seed signal after seeding |

With no flags, the script prints what it *would* do. Pass `--apply` to execute.

### `--heartbeat` tick flag

`python scripts/sensorium_tick.py --instance demo --heartbeat --json`

Opt-in flag. Emits a compact deterministic `runtime_heartbeat` signal during the tick. Not included in `--all-sensors`. Deterministic, stdlib-only, no model calls, no outbound, privacy-preserving.

---

## Sample files

- `docs/examples/demo-instance-config.json` — sample `instance.config.json` with all current fields and generic defaults.
- `docs/examples/demo-policy-card.md` — sample policy card showing the boundary between reusable core and deployment-specific policy.
- `examples/demo-sensor-registry.json` — sample sensor registry with 4 generic sensor kinds and safe defaults.
- `examples/demo-actuator-registry.json` — sample prepare-only actuator registry with conscious-gated script execution.
- `examples/demo_script_sensor.py` / `examples/demo_script_actuator.py` — stdlib-only canaries for trusted local script contracts.
