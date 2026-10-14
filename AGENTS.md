# Agent Sensorium development

Develop in an isolated checkout. Installed plugin code, private configuration, bindings, accounting and runtime records are separate owners and must remain untouched unless an operator separately authorizes adoption.

## Scope and safeguards

- Keep ordinary Sensorium pull-based and local-first; no unsolicited outbound delivery or external task creation.
- Validate canonical instance/owner and closed sensitivity/surface boundaries before effects. Preserve exact leases, source replay, presentation and source-bound outbox owners.
- Advisory confidence routes attention; it cannot authorize durable memory, tasks, artifacts or delivery. Model-backed advisory stays explicitly opt-in.
- Runtime state belongs outside reusable source; tests use isolated synthetic stores. Never embed real identities, conversations, host locations, account configuration or private receipts in source, fixtures, docs or packages. Preserve public license attribution.
- Bound changes by file ownership. For substantial repair, use an isolated worker/worktree and independent review; do not overwrite another contributor's work, require a private orchestration framework or patch live state during diagnosis.
- Keep proposed behavior separate from implemented guarantees. Do not install clocks/services, migrate real accounting, run providers or publish as an incidental development step.

## Documentation and status

Read [architecture](docs/attention-and-workroom/architecture.md), [rationale](docs/attention-and-workroom/design-rationale.md), [migration](docs/attention-and-workroom/migration.md) and [change scope](docs/attention-and-workroom/change-scope.md). Producer material replay, multi-record bounded progress and optional latent metadata are implemented with focused regressions and independent source review; bind complete-suite and privacy evidence to the exact source before adoption. Native/workroom/owner/transport are proposals, not a synthetic substitute framework or live capability proof. Request-time quota, failed-read preconsumption, journal/migration, lifetime continuation and whole-tree/active cancellation are proposed strengthenings. Gateway compatibility and Wiki security remain held.

The [bundled skill](skills/agent-sensorium/SKILL.md#development-and-repair) separates ordinary use, admin and repair. The [documentation index](docs/README.md) links existing canonical contracts.

## Verification

Prefer small reviewable changes and tests first for pure store/gate/dispatcher logic. Keep core stdlib-only and handlers directly callable without a live host. Use synthetic offline fakes for proposed native/provider/process contracts; actual model, memory and service-manager proofs need separate authorization.

Development commands (not an execution receipt):

```bash
python -m pip install -e ".[test]"
python -m pytest tests -q
python -m py_compile agent_sensorium/*.py scripts/*.py
```

Run relevant checks and a complete suite against the frozen source before acceptance. Preserve failures/skips as evidence, not automatic fixture obsolescence. Validate relative documentation links and inspect the full source and built distributions for privacy separately. Installation, real migration, activation, commits and publication require their own authority; a focused green test set is not release readiness.
