# Runtime Configuration Interface

Status: accepted architecture direction / first-step artifact.
Date: 2026-06-03.
Decision: APPROVE_HYBRID.

## Decision

Runtime configurability is a real public Agent Sensorium capability, not only a Sera-specific enhancement.

The public plugin should let an installed Hermes agent configure its own Sensorium instance at runtime: initialize config, inspect effective policy, add/update deterministic sensor declarations, and choose Hermes profiles or execution lanes for bounded review work. Sera-specific deployment remains separate as **Sera Sensorium Configuration**: private policy cards, prompts, channels, local paths, voice/media defaults, cron choices, Hindsight query sets, and profile names.

Short form:

```text
public plugin = generic organ + safe configuration interface
local config pack = instance identity, policy, sensors, cadence, profiles
```

## Why this belongs in the public plugin

A reusable Sensorium is not useful if every installer has to edit Python to answer basic questions:

- Which instance is this?
- Which surfaces may receive pointers?
- Which sensitivities are allowed?
- Which cheap sensors are enabled?
- Which Hermes profile reviews Subconscious or Conscious work?
- Which budgets/cooldowns keep attention quiet?

Those are runtime posture questions. They belong in schema-bounded config, not hard-coded source and not private Sera code.

## Configuration layers

1. **Core defaults**.
   - In package code.
   - Safe without a config file: `allowed_surfaces: ["local"]`, `max_sensitivity: "private"`, no direct delivery, no external work.

2. **Instance config**.
   - `instance.config.json`, discovered from explicit `config_path` or `{state_dir}/instance.config.json`.
   - Owns generic policy values: instance name, allowed surfaces, max sensitivity, budgets, pointer behavior, attention policy, sensor registry, and profile bindings.

3. **Instance policy/config pack**.
   - Outside public code.
   - For Sera this is **Sera Sensorium Configuration**: private policy cards, prompts, channel IDs, profile names, Hindsight query sets, local media/TTS paths, cron scheduling, and deployment sensors.

4. **Runtime receipts**.
   - Config mutations write compact decision receipts with reason, actor/source, before/after summary, verification condition, and rollback condition.
   - Receipts are not a second config source; they explain why config changed.

## Minimal generic schema expansion

Keep the existing `instance.config.json` shape, but reserve these generic sections:

```json
{
  "instance_name": "default",
  "allowed_surfaces": ["local"],
  "max_sensitivity": "private",
  "profiles": {
    "subconscious_review": {"profile": "subconscious", "enabled": false},
    "conscious_review": {"profile": "default", "enabled": false},
    "maintenance": {"profile": "ops", "enabled": false}
  },
  "sensors": {
    "body_pressure": {"enabled": false, "cadence_seconds": 60},
    "kanban_pressure": {"enabled": false, "cadence_seconds": 300}
  },
  "attention_policy": {
    "evidence_rules": {},
    "review_budget": {},
    "priority_weights": {}
  }
}
```

Rules:

- Unknown sections are ignored by v0 loaders unless explicitly supported.
- Config may narrow surfaces/sensitivity, never broaden an item beyond its own policy.
- Sensor entries configure deterministic sensors only. They do not inject arbitrary Python or commands.
- Profile bindings are names/roles only; execution still requires the existing Kanban/worker/conscious gates.
- No config field may enable direct outbound delivery by accident. Direct delivery remains a separate explicit policy gate.

## Agent-friendly admin interface

Do not expand the normal foreground tool surface. Ordinary sessions keep the single compact `sensorium` live aperture.

Add an out-of-band admin interface for setup and maintenance, preferably mirrored as both a CLI script and an admin tool:

```text
sensorium_config status
sensorium_config init --instance <name> [--state-dir <path>]
sensorium_config validate [--config <path>]
sensorium_config patch --path <json.pointer> --value <json> --reason <text>
sensorium_config sensor add|update <kind> --enabled true|false ...
sensorium_config profile set <role> <profile> --enabled true|false
sensorium_config template --kind minimal|hermes-agent|sera-local-example
```

Equivalent admin-tool action names:

```text
status | init | validate | patch | add_sensor | update_sensor | set_profile | template
```

Required guardrails:

- Atomic JSON writes.
- Schema validation before write.
- No raw policy-card contents in diagnostics.
- No secrets in config; credentials stay in Hermes `.env` or provider auth stores.
- Every mutating call requires `reason`, `verification_condition`, and `rollback_condition`.
- Mutating calls write a compact config decision receipt.
- Live foreground sessions do not receive this admin tool by default.

## Sensor registration posture

The public plugin may expose a registry of built-in deterministic sensor kinds and their safe config fields:

- `body_pressure`.
- `network_pressure`.
- `process_pressure`.
- `hindsight_pressure`.
- `kanban_pressure`.
- later: session outcome, artifact metadata, gateway pressure, cron/job result pressure.

A runtime `add_sensor` operation should mean “enable/configure a known sensor kind,” not “install arbitrary sensor code.” Custom sensor code belongs in an extension package or local config pack after review.

## Profile posture

Profiles are runtime bindings, not identity defaults.

The public plugin can store role-to-profile bindings such as `subconscious_review`, `conscious_review`, `maintenance`, or `worker_dispatch`. It should not ship Sera-specific profile names such as `serasubconscious`, nor assume that profile exists.

Profile use must remain behind the existing authority boundary:

```text
sensor/candidate -> Subconscious/Conscious decision -> Kanban/worker request -> dispatcher/profile
```

Config chooses eligible lanes; it does not bypass review, spawn workers directly, or make background code changes automatically.

## First bounded implementation slice

Do this before any larger refactor:

1. Document the runtime configuration contract.
2. Add schema-bounded config helpers for `profiles` and `sensors` in `agent_sensorium.config`.
3. Add tests proving safe defaults, sanitization, ignored unknowns, and atomic mutation receipts.
4. Add a CLI/admin handler for `status`, `init`, `validate`, `patch`, `sensor update`, and `profile set`.
5. Update the Hermes skill doc with the interface and the public/private split.
6. Do not change live foreground tool count.
7. Do not enable any cron, outbound delivery, or direct worker dispatch as part of setup.

Acceptance for the first slice:

- Missing config still fails safe.
- A fresh install can generate a minimal config without editing Python.
- An agent can enable a known deterministic sensor through config and validate the result.
- An agent can bind a role to a Hermes profile name without executing it.
- Diagnostics show only compact effective policy and config source.
- Tests cover config mutation and prove no live foreground surface expansion.

## Rejected alternatives

- **Sera-only config scripts**: rejected. Useful locally, but it would make the public plugin a code skeleton that still needs private surgery to run.
- **Hard-coded public defaults for Sera-like behavior**: rejected. That leaks instance identity and encourages unsafe surprise outreach/delivery.
- **Arbitrary runtime sensor code injection**: rejected. It turns setup into an execution surface and violates the cheap deterministic sensor boundary.
- **Put all admin verbs in the live `sensorium` tool**: rejected. The live aperture should stay tiny; setup/admin belongs out-of-band.
