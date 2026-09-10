# Prospective Evidence Capture v0

This is an **isolated, passive, default-OFF** 14-day study recorder. It is not a
Sensorium decision input, dashboard surface, tool action, scheduler, source, or
outbound route. Canonical owners call the observer only after their own commit;
observer exceptions are intentionally discarded.

## Detached transactional storage

Active observations are stored in a study-local SQLite database. Each receipt is
committed under an exclusive transaction which allocates the monotonic sequence
and enforces both the 5,000-row and 5-MiB caps. Rejected/capacity observations
return false only to the discarded observer path; canonical owner results remain
unchanged.

Rows are a closed schema: opaque keyed refs, exact structured owner source class,
study-day and six-hour buckets, and bounded fields. The accepted source classes
are `manual`, `hermes_session`, `artifact`, `feedback`, `machine`, `memory`,
`kanban`, and `unknown`; unrecognized or malformed owner values remain unknown.
No prose, raw identifier, locator, exact source time, nested payload, or control
metadata is accepted.

`source_observed` is recorded after every newly accepted signal receipt, including
inhibited/quiet sources. Candidate transition, Conscious opened/settled, and
Kanban settlement have narrow post-success calls. `presented` is recorded only
after the canonical pointer receipt and `chosen` only after canonical Conscious
settlement; both remain non-material. A nonempty accepted `transition` is the
only material-yes mapping; exact `liveness: true` is material/no-effect no.
Validated feedback with a direct existing origin, action/worker ref, exact
`system_action` scope and successful distinct closed domains supplies dependence
and cross-domain. Exact correction/retraction and bounded Conscious
external-plus-protected snapshots supply only their respective labels. No prose,
promotion, timestamp, correlation, presentation, choice or settlement infers
material truth.

## Freeze and revocation

Closeout aggregates lifecycle receipts by stable case ref. It freezes distinct
`generator-input.json` (cutoff-safe features only), `sealed-gold-labels.json`
(outcomes only), and `evaluation-manifest.json` (hashes, counts, deficits, and
verdict). It requires exactly the contract thresholds: 48 cases, 24 material,
12 no-effect, six each shared dependence, contradiction/retraction,
cross-domain, mixed attention, four source classes, <=35% largest class, and
<=25% unknown. Missing criteria produce `INSUFFICIENT_PRIVATE_EVIDENCE` with
safe aggregate deficits.

Revocation uses the retained study-local HMAC key through frozen retention,
removes only the matching case and linked frozen entries, invalidates the
manifest, blocks active recapture, and writes an idempotent content-free
tombstone. The key is deleted when frozen artifacts expire after 30 days;
tombstones expire after 90 days.

## Control and nonrenewal

Activation uses an atomic, content-free consumed-window latch outside the
purgeable study payload. It accepts one exact timezone-aware 14-day window;
missing/tampered control, key, config, or latch fails closed. Ordinary purge,
config disable, restart, and removal of the study subtree cannot renew the
window. `maintenance` performs idempotent expiry cleanup and is safe on resume.

Use `scripts/sensorium_prospective_evidence.py` only with explicit candidate or
temporary roots. Commands are `status`, `activate`, `revoke --source-receipt`,
`freeze`, `maintenance`, and `purge`. No live activation is authorized by this
candidate.
