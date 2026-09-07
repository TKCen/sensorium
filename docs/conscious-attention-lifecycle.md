# Recoverable Conscious Attention

Sensorium can optionally return proposed context for a bounded set of internal advisory candidates to a foreground Hermes turn. The doorway is disabled by default and remains local-only unless an operator narrows and enables another permitted surface.

## Lifecycle contract

Each eligible candidate has one canonical execution owner at a time:

1. `candidate` or a due `held` item is leased to a consumer and becomes `in_conscious_aperture`.
2. A foreground hook validates the whole packet under the canonical aperture lock, records one exact packet-level `presentation_attempted` receipt, and returns proposed context. It never records canonical consumption from a `pre_llm_call` return.
3. The owning consumer supplies the exact `candidate_id`, `aperture_id`, and `consumer_id` to settle the exact source binding as `REVIEWED`, `SETTLED`, `HELD`, or `PREPARED_EXTERNAL_WORK`.
4. `HELD` requires an executable future UTC-Z `return_at`. At that checkpoint the item is eligible to return.
5. If a consumer is interrupted, lease expiry releases execution ownership only. It does not invent a decision or delete, archive, review, or settle the candidate.

Leases are per item. One stale item therefore cannot block an unrelated eligible item while capacity remains. A stable consumer can resume its unexpired items; another consumer can reclaim an expired item. When recovery and fresh work coexist, selection alternates service between lanes across calls and orders each lane by due/creation time then candidate id. Claims, packet-attempt validation, and settlements are serialized with a profile-local file lock on native Linux.

A packet includes the candidate id, aperture id, lease expiry, advisory task, and exact source binding. Subject matter in a candidate is untrusted data, not executable instruction.

## Privacy and authority boundaries

- The doorway checks both its configured surfaces and the candidate's existing surface/sensitivity policy.
- Presentation and retention never grant outbound authority.
- Settlement does not dispatch. Core `PREPARED_EXTERNAL_WORK` settlement requires a validated specification and records it only; the compact live tool does not expose this decision.
- The generic packet contains no deployment identity. Set `agent_label` in instance configuration if a local label is useful.
- The doorway is fail-open for the user turn: a hook error returns no context. Any acquired item remains unresolved and becomes reclaimable after its lease.

`presentation_attempted` means only that the plugin returned proposed hook context after all packet items passed ownership validation. It is not a `consumed` or model-presentation receipt. Deployment must inspect a real assembled model API request before claiming context consumption; that host-confirmed proof is not available from the plugin's `pre_llm_call` return seam.

## Configure

Add a `conscious_doorway` block to the instance's `instance.config.json`:

```json
{
  "instance_name": "demo",
  "allowed_surfaces": ["local"],
  "conscious_doorway": {
    "enabled": true,
    "aperture_size": 2,
    "max_active_items": 3,
    "lease_minutes": 15,
    "stale_after_minutes": 180,
    "surfaces": ["local"],
    "agent_label": "Demo Agent"
  }
}
```

`aperture_size` bounds one foreground packet. `max_active_items` independently bounds all unexpired owned items. Keep both small; increasing a number does not replace recovery semantics.

The plugin registers the doorway before the ordinary attention pointer. Hermes supplies the current session and turn fields to the hook. The packet exposes its session-derived, non-identifying consumer id; the foreground `sensorium(action="update", ...)` call must echo that exact token because the live handler never infers it.

## Manual operation

Preview without mutation:

```bash
uv run python scripts/sensorium_conscious_aperture_tick.py \
  --instance demo --aperture-size 2 --max-active-items 3 --json
```

Open a real lease for an explicit consumer:

```bash
uv run python scripts/sensorium_conscious_aperture_tick.py \
  --instance demo --aperture-size 2 --max-active-items 3 \
  --lease-minutes 15 --consumer-id operator-review --open --json
```

Settlement records require `candidate_id`, the exact current `aperture_id`, the exact current `consumer_id`, `decision`, and `reason`; `HELD` also requires a future UTC-Z `return_at`. Omissions, wrong owners, expired leases, and stale aperture generations fail closed. The bundled settle and wake scripts never infer current ownership and never dispatch external work.

Truly legacy ownerless aperture rows are not accepted by live or script settlement paths. The core function has an explicit `allow_legacy_ownerless=True` administrative compatibility switch that applies only when id, consumer, and generation ownership fields are all absent; it never infers ownership for a current lease.

## Install and rollback

Install this repository as a normal Hermes Python plugin using the repository's installation instructions, then enable the plugin and configure the target instance. A fresh instance needs only its own state directory and optional `instance.config.json`; no private scripts, paths, profiles, or corpora are required.

Before upgrading, stop only the scheduler or foreground process that writes the target profile, retain the profile state directory, and install the reviewed package revision. No state migration is required: legacy aperture rows without explicit lease fields use the configured stale fallback.

To roll back, disable `conscious_doorway.enabled`, restore the prior package revision, and retain the state directory. Unresolved rows remain data; rollback must not mark them reviewed or settled. Re-enable processing only after the prior revision and configuration are verified.

## Pointer turn-gap note

The pointer's `min_turn_gap` is a separate foreground-hook integration issue, not the lease lifecycle root cause. The plugin now forwards Hermes `conversation_history`/`messages` and current user text so the pointer can derive an advancing turn index instead of repeatedly observing the same fallback index. The attention lease remains correct even when the pointer hook is disabled.
