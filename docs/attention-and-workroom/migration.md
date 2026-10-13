# Upgrade and proposed migration rules

Attention can be updated without activating a workroom. No real accounting migration has been run. Runtime/source/route approval, initialization, enablement, individual admission and publication are separate decisions. Read the [architecture](architecture.md), [rationale](design-rationale.md) and [status](change-scope.md) before treating this proposal as implementation.

## Preserve ordinary attention owners

Keep canonical instance/store identities and signal/event/candidate/thread/decision/artifact/outbox ownership. Do not transplant alternate whole trees, deployment fixtures or obsolete assertions over public-only repairs. Producer revisions are not manufactured for old rows. Index progress retains cursors, fingerprints, stream boundaries and old unresolved material; no reset, newest-N deletion or disguised lifetime rescan.

The attention cache is disposable, not canonical accounting. Generation 6 invalidates older ready projections because entity uniqueness, per-event revision validation and disposition attribution changed. Rebuild derives claims from canonical records, ignoring producer-supplied reserved cache fields, without rewriting source history, settlements or debit. Genuine base-era unversioned material stays settled by its exact key. An old unversioned disposition cannot establish settlement of an explicit producer revision: retained IDs and candidate representation are not historical revision proof, including after rewrite/compaction. Decisions and debit remain retained even when that explicit revision is eligible for consideration. This derived-cache rebuild is implemented and does not authorize the proposed workroom-accounting migration below.

Exact consumer/aperture generation/lease/presentation ownership remains required. Lease expiry releases execution, not semantic settlement. Future HELD checkpoints and STOP persist. Keep session-local pointers, closed privacy policy, dashboard/URL validation and honest unknown status. Optional implemented latent projection remains read-only, default-off and honestly incomplete; output caps do not bound input I/O.

## Native adoption is proposed, never implicit

Duplicate entity IDs remain ambiguous until canonical rewrite restores uniqueness; append replacement IDs are not a supported revision contract. Rebuild and dependency refresh withdraw ambiguous advisory settlement without rewriting canonical history. Missing or malformed joins of a unique candidate remain uncertain legacy where policy permits, not source equality. Fixture completion of a missing distinct event is legacy-to-source; a valid source-to-source transition instead rewrites the unique canonical event or updates candidate references to a fresh distinct event. Tools ingress (B) and visibility (C) need separate acceptance.

A future generic extension must import cold without private files/dependencies, path discovery, registry mutation, provider initialization or inference. No embedded deployment root, endpoint, memory target, grant or ledger path. Immutable externally approved context pins runtime/package-manager generation/interpreter/ABI/import origins, selected route/model/configuration, canonical DB/profile/tier/root, artifact owner, memory read owner and source authorization. Current observations must match; drift HOLDs. Code upgrades or relocation require fresh approval, never copied bindings or automatic digest refresh. Accounting authority persists through route/runtime changes.

Source authorization needs an explicitly approved versioned schema retaining original observations/times, validity, aliases and exact lengths/digests. A renamed Wiki field or retention instruction is not equivalent authority. Check current source bytes before debit, inference and every read; copying or retimestamping cannot refresh evidence. A real new observation may be separately approved without rewriting old provenance.

## Cold accounting initialization — proposed

Cold means authoritative absence of prior history for the exact owner, not a missing directory. An approved external caller may supply durable authority/virgin-history attestation. Do not require a new global registry; do require evidence surviving replaceable state-root loss. Known authority with missing, corrupt, conflicting or partial ledger/replay/episode/transport state HOLDs.

Under a stable owner lock, stage validated initialization receipt, empty journal/replay and OFF policy; fsync files/directory and atomically publish a digest-bound commit. Partial initialization is not launchable. Recovery may finish only the same authorized transaction, not allocate replacement credit. Initialization does not adopt artifacts, create native history, initialize memory, enable policy or launch a turn.

## Debit-preserving migration — proposed and separately authorized

Package installation must not inspect or export legacy private accounting. A trusted approved exporter supplies a content-free immutable snapshot with owner/schema/adapter identity and a quiescence fence over every old writer, worker and executor. Unknown ownership, process state, incomplete history or unsupported equality domain HOLDs. Keep source bytes untouched.

Preserve all nonrefunded reserved/running/failed/interrupted/complete admissions; original dates/statuses/budgets; daily and cumulative debit; consumed grant/choice/legacy replay keys across days; per-episode initial/continuation slots; clock horizon; unresolved consumed transport claims and prior migration receipts. Do not copy bodies, artifacts, bindings, credential configuration or enablement. Existing native/artifact owners separately validate lineage; approval of a new runtime cannot adopt a session.

Fence and lock both authorities in deterministic order. Validate journal/counter/replay agreement and prove canonical admission overlap before deduplication. Never sum overlapping imported spend twice or take the maximum of independent totals to discard distinct spend. Unknown attribution HOLDs. Imported same-day admissions consume today's slots; old replay and episode consumption persist. Overcap history blocks future admission without truncation, refund or automatic limit expansion.

Stage/fsync receipt, journal, replay and OFF policy, then atomically commit. Retire/fence the old authority with that exact transaction before any new writer. Identical migration is idempotent; changed snapshot after commit HOLDs. Faults leave old authority fenced and new state OFF/uncommitted or conservatively charged, never two launch authorities. Initialization and journal-based migration are substantive proposed changes, not path sanitation of the source owner.

## Admission, crash and rollover — proposed

With explicit enablement and current choice/grant, hold the owner lock through validation and reservation. Validate context, exact source bytes, expiry, continuation, clock/day/caps and permanent replay; obtain fresh selected-route quota. Durably write/fsync the full finite debit and replay reservation before session creation, assembly or launch; pass only exact lock/claim/nonce authority to the child.

Before durable reservation, refusal is uncharged. After reservation, source/quota recheck failure, launch failure, timeout, interruption and incomplete native/checkpoint results stay charged. Recover every durable reservation conservatively despite interrupted projections; never refund, retry automatically or debit the same reservation twice. UTC rollover opens a new daily window without deleting cumulative history, replay or lifetime episode slots; clock rollback HOLDs.

Validate explicit finite turn/daily/episode budgets and continuation counts, quota headroom/freshness/timeouts, read and artifact bounds. These are configurable proposal parameters, not mandatory private defaults. Require the same lifetime episode cap across every entrypoint and day; a daily cap alone does not establish it. Proposed request guards recheck grant/enablement/expiry/monotonic deadline/context/quota before every dispatch, including retries and recreated clients. Failed-read preconsumption requires its own regression proof.

## Return, STOP and disablement

Resume only exact complete native model history, checkpoint and unchanged verified private artifacts. Validate stable root and every compression ancestor/tip before transcript access; do not reseed, summarize away history, silently fork or adopt by title. Continuation needs a distinct current choice and fresh finite grant/source/quota. Incomplete/native-failed results are not made resumable by migration. No artifact is required.

START/RESUME/STOP transport, if separately implemented, consumes exact fresh packages once; RESUME has no initial prompt and failed consumed choices do not retry. Queue STOP is zero inference, not proof of active cancellation. Exact active-claim cancellation must prevent new requests and terminate/reap the entire owned domain while preserving debit/replay. Disablement prevents future admission; uninstall/rollback does not delete accounting. Unknown/live/foreign/nonzero-terminal or nonempty domains block later launch. Group-leader exit/MainPID zero is insufficient proof.

## Evidence before adoption

Use offline fake context/source/quota/DB/provider/client/registry/supervisor tests for inert import, drift, exact history, eager/deferred equality, request/retry guards, failed-read debit, authority initialization, migration overlap preservation, journal fault injection, concurrent replay, later-day continuation denial and complete-tree retirement. Exercise assembled paths, not just helpers; no real native/model/memory/service execution in default conformance.

Separately prove exact host-version runtime/ABI/import identity, canonical native DB/history/origin carry-forward, native-only no-fallback dispatch, every request/recreation hook, remote read-only memory lifecycle and process containment/parent death. Optional Linux transport needs exact unit/claim/domain retirement proof; unsupported platforms HOLD. Gateway compatibility and Wiki security remain held. Full-suite completion, frozen source/distribution privacy review, isolated install, authorized real migration, activation and publication remain independent gates.
