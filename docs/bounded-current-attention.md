# Bounded current-attention admission

Sensorium admission treats the canonical JSONL streams as durable evidence, not as
the routine working set. The native clock incrementally projects them into a
private, disposable SQLite index under the selected instance root.

## Bounds and completeness

- Each preparation pass has one aggregate `admission_scan_bytes` budget, a
  1 MiB complete-record ceiling, durable per-stream cursors, and source
  byte/record counters. A record that cannot fit the configured pass is reported
  explicitly; it is never skipped or partially accepted.
- Until every canonical stream reaches one stable snapshot, admission reports
  `catching_up`, `rebuilding`, or `invalid`; eligibility and exact counts are not
  claimed.
- Ready selection reads an incrementally maintained current candidate projection,
  an exact eligible counter, and at most `candidate_limit` globally ordered rows.
  Receipts expose query and materialized-row work.
- Archived and settled evidence remains indexed for exact identity and replay
  checks but is absent from the ready current-attention query. Growing archived
  history therefore does not grow steady-state selection work.

## Preserved semantics

The projection preserves descending pressure, then age, then ID ordering; exact
source/member identity; newer legitimate revisions; v1/v2 memory-member overlap;
old applying dispositions; terminal item closure; and the newest bounded relevant
disposition context. Unresolved old candidates remain eligible. Deliberately held
Conscious matters retain their existing return conditions; this index introduces
no TTL, expiry, deletion, or newest-N approximation.

Planner compatibility reads remain domain-write-free for small stores that fit the
same aggregate source budget. Only the native clock owns index advancement. Cache
absence or corruption cannot change canonical state and fails closed until a
bounded rebuild completes.
