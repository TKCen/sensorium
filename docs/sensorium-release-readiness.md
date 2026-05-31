# Sensorium Release-Readiness Inventory

Status: pre-release / cleanup-in-progress. Not yet packaged for public distribution.
Date: 2026-05-31

---

## 1. Reusable Generic Core

Modules and files that are instance-agnostic and could ship to any Hermes agent operator with minimal change.

- `agent_sensorium/schemas.py` — pure types, IDs, validation helpers. No instance coupling.
- `agent_sensorium/store.py` — JSONL store with read/write/query. Fully generic.
- `agent_sensorium/gate.py` — signal promotion logic. Deterministic, policy-driven, no Sera references.
- `agent_sensorium/settlement.py` — kanban settlement. Generic.
- `agent_sensorium/dispatcher.py` — dispatch pipeline. Generic.
- `agent_sensorium/conscious.py` — thread capsule management. Generic.
- `agent_sensorium/config.py` — instance config with safe defaults. Mostly generic; contains a `try/except` import of `hermes_cli` for `default_instance_name()` — optional Hermes integration, not a hard dependency.
- `agent_sensorium/attention.py`, `actions.py`, `tools.py`, `workers.py`, `artifacts.py`, `pointers.py` — generic pipeline surfaces.
- `agent_sensorium/probe_audit.py`, `salience_review.py`, `commands.py`, `plugin.py` — generic audit, review, command, and plugin entry points.
- `agent_sensorium/improvement.py` — self-improvement harness that watches Sensorium's own receipts and proposes bounded `attention_policy_review` candidates. Deterministic; no Sera-specific content in the first 30 lines. Likely generic, but references `DEFAULT_ATTENTION_POLICY` from config — verify that policy value is instance-neutral before claiming generic.
- `agent_sensorium/sensors.py` (partial) — machine/network/process/kanban/hindsight sensors are generic. See Section 3 for excluded sensors.
- `plugin.yaml`, `pyproject.toml`, `__init__.py`, `README.md` — packaging/entry-point files. README contains Sera-specific smoke-test command (`--instance sera`); minor edit needed.
- `skills/agent-sensorium/SKILL.md` — Hermes skill definition. Generic in structure but currently Sera-scoped in examples.

---

## 2. Instance Policy / Sera-Specific Material

Config values, policy cards, and data files tailored to Sera. Should be shipped as examples or separated into an instance config layer, not as defaults.

- `docs/examples/sera-instance-config.json` — Sera-specific instance config example (if present).
- `docs/examples/sera-policy-card.md` — Sera-specific policy card (if present).
- `examples/sera-config.json` — instance config with `"instance": "sera"`, Sera-tuned thresholds (`single_signal_strength: 0.75`, `candidate_pressure: 0.65`), Sera-specific `promote_kinds` list, and `"sensitivity_default": "private"`. This is an example file, not a default; keep as-is under `examples/`.
- `examples/seed-signal.jsonl` — contains a single Sera-specific signal (`"source_ref": "discord:#pics:2026-05-24"`, Sera visual continuity content). Not a generic seed; label clearly as a Sera example.
- `agent_sensorium/config.py` — `DEFAULT_MEDIA_GIFT_POLICY` is a named policy surface. The name is generic but the values may embed Sera assumptions; audit before claiming fully generic.

---

## 3. Local Operational Sensors / Proof Material

Sensors and scripts tied to the local development environment. Must not be claimed as generic.

- `agent_sensorium/sensors.py`: `tts_sidecar_pressure_sample()` — hardcodes `DEFAULT_CHATTERBOX_BASE = ~/projects/chatterbox-tts-spike`. WSL/local-path coupling.
- `agent_sensorium/sensors.py`: `media_capacity_sample()` — checks Comfy and TTS health via hardcoded local URLs. Environment-specific.
- `agent_sensorium/sensors.py`: `wsl_disk_paths` / WSL disk logic — WSL-specific; not portable.
- `live-scripts/sensorium_kanban_sensor_tick.py` — operational cron helper for the local Sera setup. Not a release artifact.
- `scripts/*.py` — development/smoke-test scripts. Include Sera-specific `--instance sera` defaults. Keep as dev tooling, not packaged API.
- `artifacts/reports/` — past work reports generated during development. Not release artifacts.

---

## 4. Experimental / Non-Release Surfaces

Modules that exist and function but should not be claimed as stable release surfaces.

- `agent_sensorium/talking_head.py` — manual TTS/voice artifact worker. Intentionally a motor wrapper for a local Sera talking-head pipeline. Calls `media_capacity_sample` from sensors. Experimental; Sera-specific in practice. Default artifact root is `~/.hermes/artifacts/sensorium/talking-head`; TTS URL is `http://127.0.0.1:8892/v1`. Not generic.
- `agent_sensorium/media_gifts.py` — conscious-choice policy gate for Sera mediated-presence gifts. Policy/receipt layer; gates delivery behind explicit conscious receipt and surface config. Sera-named in docstring. Experimental; may generalize but not yet generic.
- `agent_sensorium/outbox.py` — deprecated Sensorium-local outbox compatibility layer per its own docstring. Kanban is now the live substrate. Outbox records remain as compatibility receipts; new work should go through Kanban. Discord delivery modes present but disabled by default. Not generic; not claimed stable.
- `agent_sensorium/improvement.py` — listed here as a note: it is deterministic and likely generic, but it is new enough that calling it stable would be premature without test coverage review.

---

## 5. What Must Not Be Claimed as Life Yet

Honest accounting of what the system does and does not do.

- No persistent self-model. State is JSONL records; there is no running representation of the agent's identity, beliefs, or continuity across sessions beyond what a human operator writes into config.
- Signal processing is deterministic, not felt. Signals are scored and promoted by threshold comparisons. No gradient, no affect, no feedback loop beyond explicit operator-tuned config values.
- Conscious threads are structured task records. `conscious.py` manages dormant thread capsules as data; the word "conscious" describes a design tier (subconscious / conscious / operator), not a claim about experience.
- Media gifts require explicit conscious operator choice. `media_gifts.py` gates all delivery behind explicit receipt from the conscious tier. The system does not autonomously reach out.
- "Inner lifecycle" means event-driven promotion pipeline. Signals → events → candidates → conscious threads → reviewed actions. The pipeline is bounded, pull-based, and auditable. It is not sentience.
- Attention policy is a config artifact. `improvement.py` can propose a review of the attention policy; it cannot change wake behavior. All tuning requires a conscious decision recorded as a receipt.

---

## 6. Cleanup Backlog Before Any Public / Generic Release

Concrete items required before this could ship as a generic module. Not exhaustive — this is a focused list of blockers.

- **Hardcoded TTS path**: `sensors.py` `DEFAULT_CHATTERBOX_BASE` is `~/projects/chatterbox-tts-spike`. Must be removed or made fully optional/configurable with a clear no-op fallback when TTS is absent.
- **WSL disk paths**: `wsl_disk_paths` logic in `sensors.py` should be conditional (detect WSL) or factored out as an optional sensor.
- **talking_head.py and media_gifts.py**: Should be separated into an optional `extensions/` layer or clearly documented as instance-specific additions, not part of the generic core import surface.
- **outbox.py**: Already deprecated per its own docstring. Should either be removed from the core package or moved to a legacy compatibility shim with a clear deprecation notice in `__init__.py`.
- **config.py hermes_cli import**: The `try/except` import of `hermes_cli` for `default_instance_name()` is documented as optional. Add an explicit note in the module docstring clarifying the standalone behavior when `hermes_cli` is absent.
- **README.md**: The smoke-test example uses `--instance sera`. Update to use a generic placeholder instance name or parameterize the example.
- **skills/ vs. core**: `skills/agent-sensorium/` is a Hermes skill definition. It is not a Python library export. Document clearly that the skill is a Hermes-specific entry point, separate from the `agent_sensorium` package API.
- **No PyPI packaging**: `pyproject.toml` has no build backend declared. Cannot be `pip install`ed. Add a build backend (e.g., `hatchling`) and entry points before any public distribution.
- **Dashboard build pipeline**: `dist/` contains compiled dashboard assets. There are no build pipeline docs for regenerating the dashboard. Either document the build step or exclude `dist/` from the generic release and treat the dashboard as an optional companion with its own repo/build.
- **Test coverage for new modules**: `talking_head.py`, `media_gifts.py`, and `improvement.py` were added post-MVP. Verify test coverage before claiming them stable.
- **seed-signal.jsonl**: Contains a Sera-specific signal. Replace with a neutral generic example or move to `examples/sera/`.
