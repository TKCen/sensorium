# Agent Sensorium canonical checkout

This directory is the canonical git checkout for Agent Sensorium:

`~/.hermes/plugins/agent-sensorium`

It is intentionally both:

- the Hermes-installed plugin that the live gateway imports; and
- the primary development repository for Sensorium plugin code.

Why: the older split between `/home/entity/projects/agent-sensorium` and the installed plugin caused live-only fixes, stale rollouts, and tool-surface regressions.

Operational rule:

1. Start Sensorium plugin edits here.
2. Run tests here.
3. Commit here.
4. Restart/reset Hermes gateway/session when runtime code or tool schemas change.

Legacy mirror:

`/home/entity/projects/agent-sensorium`

That path is kept only as a mirror/compatibility checkout for old scripts and references. Do not start new work there unless Sebastian explicitly reverses this policy.
