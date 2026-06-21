# Dashboard and Review Surfaces

Agent Sensorium has three different review surfaces. They deliberately do different jobs:

1. **Live tool (`sensorium`)** — the small foreground aperture available to the ordinary agent session: `status`, `ingest`, `open`, `update`.
2. **Admin toolset (`agent-sensorium-admin`)** — setup, diagnostics, policy/config management, and controlled intervention.
3. **Dashboard plugin (`dashboard/`)** — read-only operator observability over compact state projections.

The dashboard is not a control plane. It must not mutate Sensorium state, dispatch work, send messages, broaden privacy surfaces, or add actions to the live tool enum.

---

## Route contract

The dashboard FastAPI router exposes only GET/HEAD-safe routes. The expected route matrix is:

| Route | Method | Projection |
|-------|--------|------------|
| `/attention` | `GET` | Current pull-based attention inbox: visible active candidates and dormant/held threads |
| `/snapshot` | `GET` | Whole-profile overview: counts, attention footprint, health, config summary, freshness, traces, artifacts, actions, outbox, metrics |
| `/graph` | `GET` | Compact candidate/receipt graph links for settlement and lineage review |
| `/metrics` | `GET` | Efficiency and pressure metrics loaded from the metrics sidecar |
| `/registry` | `GET` | Compact sensor/inner-life block and edge registry projection |
| `/probe-audit` | `GET` | Compact run-state/probe audit projection |
| `/dampeners` | `GET` | Inner-life dampener sidecar evidence |
| `/blockers` | `GET` | Inner-life blocker sidecar evidence |
| `/explanation` | `GET` | Deterministic pressure explanation for one candidate/review subject |

No POST/PUT/PATCH/DELETE dashboard routes should exist. Mutations belong in the live/admin tools and their receipt-writing code paths.

---

## Privacy projection contract

Dashboard responses are output boundaries. Treat every persisted row and sidecar file as potentially hostile, especially when it is legacy, corrupt, manually edited, or written by a previous version.

Dashboard output must not echo raw:

- transcripts, chat logs, model prompts, or long prose bodies;
- secret-shaped values (`sk-`, API keys, passwords, OAuth/bearer tokens, private keys);
- raw transcript/log sentinel strings or `do_not_leak`-style corruption markers;
- arbitrary file paths or state-directory names when they come from config or test-controlled roots;
- arbitrary metric labels, config keys/values, platform refs, media refs, source refs, candidate/thread/action/outbox IDs, graph refs, or trace refs unless they are known closed-vocabulary values.

Unsafe values should be replaced with deterministic opaque labels such as:

```text
candidate#<16 hex>
thread#<16 hex>
metric#<16 hex>
source_path#<16 hex>
```

Closed-vocabulary values may remain readable when the vocabulary is enforced or checked at projection time. Examples: `held`, `dormant`, `candidate`, `prepared`, `local`, `private`, and known route/status enums.

Numeric counts, booleans, timestamps, and bounded pressure values may remain as values when they do not reveal user content.

---

## Current dashboard families

### Attention inbox (`/attention`)

`/attention` projects candidate and thread summaries for pull-based review. It is the operator-friendly mirror of the conscious aperture, not a mutation endpoint.

Sanitize candidate/thread fields including:

- IDs and origin refs;
- kinds/request types;
- titles and summaries;
- source refs;
- sensitivity and allowed surfaces;
- hold reasons and resume triggers.

### Snapshot (`/snapshot`)

`/snapshot` is the broadest surface and therefore the easiest place to regress. It includes profile health and freshness, config summary, counts, recent signals/events/candidates/decisions, perception traces, thread/action/outbox/artifact projections, lifecycle warnings, graph-adjacent trace links, and embedded metrics.

Everything in `/snapshot` should be compact, bounded, and safe for operator inspection. In particular, `state_dir`, freshness paths, tick filenames, config `outbox`, legacy dispatch reasons, lifecycle warning IDs/details, artifact refs, and trace lineage refs must pass through safe projection.

### Graph and explanation (`/graph`, `/explanation`)

Graph and explanation routes expose why a candidate surfaced and how settlement receipts relate to candidates. They must use receipt/evidence labels rather than raw candidate IDs, idempotency keys, reason strings, or legacy subject refs.

`/graph` and `/registry` should remain `compact_only` projections.

### Metrics (`/metrics`, `/snapshot.metrics`)

Metrics are loaded from sidecar files (`latest.json`, `timeseries.jsonl`). They are still an output boundary: hostile labels and nested strings in metrics JSON must be recursively sanitized before returning from `/metrics` or embedding inside `/snapshot`.

Preserve numeric metric values and counts; hash-label unsafe string keys/values.

### Registry/probe/dampener/blocker sidecars

Registry, probe audit, dampener, and blocker files are useful for debugging inner-life behavior, but they may contain legacy caller-controlled strings. Project only compact labels, statuses, counts, and bounded safe reason labels.

---

## Regression checklist for dashboard changes

Before merging dashboard or projection changes, run a total-surface privacy smoke that seeds every returned family with hostile values and checks recursive `leak_paths == {}`.

The smoke should cover at least:

- `/snapshot` candidates, events, signals, decisions, perception traces, threads, actions, outbox, artifacts, artifact groups, lifecycle warnings, config, freshness, health, and metrics;
- `/attention` candidate/thread fields;
- `/metrics` latest/series JSON;
- `/graph` candidate/receipt nodes and edges;
- `/registry`, `/probe-audit`, `/dampeners`, `/blockers`;
- invalid `instance` values, including path traversal and newline variants;
- route matrix: only expected GET routes, no mutation methods;
- static authority checks: live tool enum remains `status|ingest|open|update`, no graph/vector DB dependency, no dashboard write route.

Useful local commands:

```bash
ruff check agent_sensorium dashboard tests
node --check dashboard/dist/index.v7.js
uv run --extra test pytest tests/test_dashboard_plugin.py tests/test_dashboard_snapshot.py tests/test_dashboard_perception_trace.py tests/test_explanations.py tests/test_receipts.py -q -o 'addopts='
uv run --extra test pytest -q -o 'addopts='
git diff --check upstream/main...HEAD
```

For live deployment, keep the deployment/canary gate separate from code review. Do not restart the gateway or sync the live plugin merely because dashboard tests pass.
