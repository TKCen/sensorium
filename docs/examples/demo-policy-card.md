# Demo Policy Card (Sample)

This is a sample policy card for the demo instance, demonstrating the boundary between
reusable Agent Sensorium core and instance-specific configuration.

## Identity

- **Instance name:** demo
- **Operator:** (configured per deployment)

## Surface policy

The demo instance may present pointers and thread capsules on:

- `local` — always allowed
- `dashboard` — allowed when thread sensitivity permits

Surfaces not listed here are denied by intersection policy.

## Sensitivity ceiling

- **Max sensitivity:** `private`
- Items marked `local_only` remain local regardless of this setting.
- Items marked `public_safe` are narrowed to `private` by this policy.

## Behavioral boundaries

- No proactive outbound messages without conscious task approval.
- No external task creation without operator confirmation.
- Feedback loops require operator evaluation scope to re-enter dispatch.
- Delivery-only outcomes are never treated as success.

## Note

This file is a fictional sample outside the reusable `agent_sensorium` package.
Real deployments maintain their own policy card with actual identity, relational,
and privacy constraints specific to their instance.
