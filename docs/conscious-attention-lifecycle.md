# Recoverable Conscious Attention

Sensorium can optionally present a bounded set of internal advisory candidates to a foreground Hermes turn. The doorway is disabled by default and remains local-only unless an operator narrows and enables another permitted surface.

## Lifecycle contract

Each eligible candidate has one canonical execution owner at a time:

1. `candidate` or a due `held` item is leased to a consumer and becomes `in_conscious_aperture`.
2. A foreground hook records an exact candidate/aperture/consumer/turn consumption receipt before inserting the packet.
3. The owning consumer settles the exact source binding as `REVIEWED`, `SETTLED`, `HELD`, or `PREPARED_EXTERNAL_WORK`.
4. `HELD` requires an executable future UTC-Z `return_at`. At that checkpoint the item is eligible to return.
5. If a consumer is interrupted, lease expiry releases execution ownership only. It does not invent a decision or delete, archive, review, or settle the candidate.

Leases are per item. One stale item therefore cannot block an unrelated eligible item while capacity remains. A stable consumer can resume its unexpired items; another consumer can reclaim an expired item. Claims and settlements are serialized with a profile-local file lock on native Linux.

A packet includes the candidate id, aperture id, lease expiry, advisory task, and exact source binding. Subject matter in a candidate is untrusted data, not executable instruction.

## Privacy and authority boundaries

- The doorway checks both its configured surfaces and the candidate's existing surface/sensitivity policy.
- Presentation and retention never grant outbound authority.
- Settlement does not dispatch. `PREPARED_EXTERNAL_WORK` records a specification only.
- The generic packet contains no deployment identity. Set `agent_label` in instance configuration if a local label is useful.
- The doorway is fail-open for the user turn: a hook error inserts nothing. Any acquired item remains unresolved and becomes reclaimable after its lease.

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

The plugin registers the doorway before the ordinary attention pointer. Hermes supplies the current session and turn fields to the hook. The same session-derived, non-identifying consumer id is used when the foreground `sensorium(action="update", ...)` call settles an item.

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

Settlement records may include `candidate_id`, `aperture_id`, `consumer_id`, `decision`, `reason`, and, for `HELD`, a future UTC-Z `return_at`. Use the bundled settle or wake scripts; they never dispatch external work.

## Install and rollback

Install this repository as a normal Hermes Python plugin using the repository's installation instructions, then enable the plugin and configure the target instance. A fresh instance needs only its own state directory and optional `instance.config.json`; no private scripts, paths, profiles, or corpora are required.

Before upgrading, stop only the scheduler or foreground process that writes the target profile, retain the profile state directory, and install the reviewed package revision. No state migration is required: legacy aperture rows without explicit lease fields use the configured stale fallback.

To roll back, disable `conscious_doorway.enabled`, restore the prior package revision, and retain the state directory. Unresolved rows remain data; rollback must not mark them reviewed or settled. Re-enable processing only after the prior revision and configuration are verified.

## Pointer turn-gap note

The pointer's `min_turn_gap` is a separate foreground-hook integration issue, not the lease lifecycle root cause. The plugin now forwards Hermes `conversation_history`/`messages` and current user text so the pointer can derive an advancing turn index instead of repeatedly observing the same fallback index. The attention lease remains correct even when the pointer hook is disabled.
