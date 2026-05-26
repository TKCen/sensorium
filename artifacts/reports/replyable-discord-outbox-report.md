# Replyable Discord Outbox — Implementation Report

## Summary

Implemented the first safe slice of Sensorium-owned replyable Discord/context outbox support. Authority flows from the Sensorium thread, not from external platforms. Discord threads and channels are viewports, not source of truth.

## Implemented Files

| File | Role |
|---|---|
| `agent_sensorium/outbox.py` | Core outbox module: data model, `prepare_outbox_request()`, `dispatch_outbox_request()`, policy gates, idempotency, `DiscordAdapter`/`FakeDiscordAdapter` |
| `agent_sensorium/store.py` | Added `"outbox"` JSONL state name |
| `agent_sensorium/tools.py` | Added `handle_sensorium_outbox_prepare` and `handle_sensorium_outbox_dispatch` tool handlers |
| `agent_sensorium/plugin.py` | Registered `sensorium_outbox_prepare` and `sensorium_outbox_dispatch` tools with schema |
| `agent_sensorium/commands.py` | Added `/sensorium outbox` subcommand for listing recent outbox requests |
| `tests/test_outbox.py` | 13 focused tests covering all required scenarios |
| `tests/test_plugin_registration.py` | Updated expected tool set to include new outbox tools |

## Tools Exposed

- **`sensorium_outbox_prepare`** — Prepare an outbox request for a Sensorium thread. Internal state only by default; no live Discord side effects unless dispatch is called with execute=True and policy allows it.
- **`sensorium_outbox_dispatch`** — Dispatch a prepared outbox request. No-op unless execute=True. No live adapter wired in this slice.

## Safety Boundaries Preserved

- No cron added or enabled
- No live Discord messages sent in tests
- No live Discord threads created in tests
- Subconscious cannot send messages or open platform threads
- Normal dispatch does not automatically call Discord APIs
- No modification to `~/.hermes/config.yaml`, `~/.hermes/.env`, live plugins, gateway, or profile state
- No Discord credentials required for the test suite
- Safe default is local/internal only
- Direct Discord modes (`discord_channel_thread`, `discord_dm_bound_session`) disabled by default via `direct_modes_enabled: False`
- `allowed_delivery_modes` defaults to `["peripheral_reference", "context_pointer"]` only
- Discord adapter defaults to `enabled: False`
- Dry-run is the default for prepare (tool schema defaults `dry_run=True`)
- Dispatch requires explicit `execute=True`
- Config cannot broaden surfaces beyond `thread.allowed_surfaces` (intersection enforced)

## Config Defaults

```python
{
    "enabled": True,
    "default_delivery_mode": "peripheral_reference",
    "direct_modes_enabled": False,
    "allowed_delivery_modes": ["peripheral_reference", "context_pointer"],
    "discord": {
        "enabled": False,
        "token_env": "DISCORD_TOKEN",
        "default_auto_archive_duration": 1440,
    },
}
```

## Tests Run + Results

```
372 passed (full suite)
13 new outbox tests:
  - prepare peripheral_reference succeeds + writes JSONL + receipt
  - prepare context_pointer succeeds
  - same prepare call is idempotent (no duplicate rows)
  - surface not allowed by thread -> denied + denial receipt
  - direct discord_channel_thread denied by default (not in allowed modes)
  - direct discord_channel_thread denied when direct_modes_enabled=False
  - direct discord_channel_thread allowed when config explicitly enables
  - archived thread cannot produce outbox request
  - closed thread cannot produce outbox request
  - dry-run does not write request or receipt
  - fake adapter success records platform_refs + dispatch receipt
  - fake adapter failure records failed status (not dispatched)
  - dispatch without execute=True is no-op
  - prepare updates thread interaction_refs
```

All files compile: `py_compile` passes for all `agent_sensorium/*.py` and `scripts/*.py`.

## Discord REST Implementation

**Deferred to gateway-action queue pattern.** This slice implements:
- `DiscordAdapter` abstract interface with `create_thread()` and `send_message()` methods
- `FakeDiscordAdapter` test double (records calls, supports failure simulation)
- Full policy gates that would guard a real adapter

A real `DiscordRestAdapter` using Discord API v10 can be added as a follow-up by implementing the `DiscordAdapter` interface and passing it to `dispatch_outbox_request()`. All policy, idempotency, and receipt infrastructure is in place.

## Remaining Follow-ups

1. **Real Discord REST adapter** — Implement `DiscordRestAdapter` using stdlib `urllib` or optional `requests`, reading token from `DISCORD_TOKEN` env at runtime. All guardrails (execute gate, config gate, token check) are designed for this.
2. **Gateway consumption** — Hermes Discord gateway could consume outbox records with `status=prepared` and dispatch them, updating `platform_refs` on success.
3. **Outbox status command** — `/sensorium outbox <id>` for detailed single-request view, `/sensorium outbox dispatch <id>` for manual dispatch.
4. **Outbox compaction** — Archive old dispatched/failed/cancelled outbox records (similar to existing `handle_sensorium_compact`).
5. **Subconscious integration** — Subconscious advisory could write `peripheral_reference` outbox requests as first-class references rather than direct delivery, per design intent.

## Final Commit

```
8e52a83 feat: add Sensorium-owned replyable outbox with policy gates and idempotency
```

SENSORIUM_REPLYABLE_OUTBOX_DONE
