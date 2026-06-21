# Demo Sensors and Tick Walkthrough

Fresh-install end-to-end: seed a demo profile, inspect it, run the heartbeat tick, and read the result.

---

## Safety boundaries

- **Deterministic, stdlib-only.** No model calls, no outbound network, no third-party dependencies.
- **Default is always dry-run.** The seed script makes no writes without `--apply`.
- **Privacy-preserving.** `runtime_heartbeat` reports counts and names only — no file contents, no absolute paths.
- **Low-strength signal.** Heartbeat is recorded but not automatically promoted into threads.

---

## Step 1 — Preview the seed (dry-run, no writes)

```bash
python scripts/sensorium_demo_seed.py --instance demo --json
```

Prints what the script *would* write. No files are created or modified.

---

## Step 2 — Apply the seed

```bash
python scripts/sensorium_demo_seed.py --instance demo --apply --json
```

Writes the demo sensor registry (`examples/demo-sensor-registry.json`) into the `demo` profile state directory. Registered sensor kinds:

| Sensor | Behavior |
|--------|----------|
| `runtime_heartbeat` | Always emits on tick; counts/names only |
| `machine_body_pressure` | Emits on level transitions only |
| `machine_network_pressure` | Emits on level transitions only |
| `machine_process_pressure` | Emits on level transitions only |

---

## Step 3 — Inspect the profile and sensors (admin tool)

Load `agent-sensorium-admin`, then:

> The admin toolset is for profile/sensor management. The ordinary live `sensorium` tool stays small and is only for session-time status, salience ingest, thread open, and thread update.

```python
sensorium_profile(action="show", profile="demo")
# → resolved config, policy card ref, allowed_surfaces, max_sensitivity

sensorium_sensor_config(action="list")
# → 4 registered sensors, all local_only, surfaces ["local"]
```

---

## Step 4 — Run the heartbeat tick

```bash
python scripts/sensorium_tick.py --instance demo --heartbeat --dry-run --json
```

`--heartbeat` emits a compact `runtime_heartbeat` signal. `--dry-run` means no state mutations. Output includes:

- State-dir health
- Signal / event / candidate / thread counts
- Pending dormant thread count
- Registry sensor count and names
- Last-decision age

To also run all registered pressure sensors:

```bash
python scripts/sensorium_tick.py --instance demo --heartbeat --all-sensors --dry-run --json
```

Note: `--heartbeat` is opt-in and separate from `--all-sensors`.

All sensors — built-in and trusted local script sensors declared in
`sensors/registry.json` — also run through one unified path:

```bash
python scripts/sensorium_tick.py --instance demo --list-sensors
python scripts/sensorium_tick.py --instance demo --sensor heartbeat --sensor body_pressure --dry-run --json
python scripts/sensorium_tick.py --instance demo --sensor all --dry-run --json
```

`--list-sensors` prints the runnable builtin and script sensor names and
exits. `--sensor NAME` is repeatable; `--sensor all` runs every builtin
sensor plus every enabled script sensor. The original per-sensor flags
(`--heartbeat`, `--body-pressure`, `--all-sensors`, etc.) remain compatible
aliases that resolve to the same sensor names through the same runner path.

---

## Step 5 — Read status via the live tool

In a Hermes agent session (live surface only):

```python
sensorium(action="status")
```

Returns current attention state: open thread count, top candidate, inbox summary, dispatcher lock status. No profile argument needed — operates on the active default.

---

## Common flags reference

| Flag | Script | Effect |
|------|--------|--------|
| `--instance demo` | both | Target the `demo` profile |
| `--apply` | `sensorium_demo_seed.py` | Perform writes (required) |
| `--dry-run` | `sensorium_tick.py` | Skip all state mutations |
| `--heartbeat` | `sensorium_tick.py` | Emit runtime heartbeat signal (opt-in) |
| `--all-sensors` | `sensorium_tick.py` | Run all registered sensors |
| `--json` | both | Print structured output to stdout |
