# Design rationale and evidence boundaries

The [architecture](architecture.md) separates implemented attention changes from a proposed native extension. [Change scope](change-scope.md) records proof status; [migration](migration.md) defines preservation rules.

## Equality comes from the producer

Correlation is a thematic relationship, not equality. Complete source-owned item/revision joins prevent a paraphrase, changed wrapper or fresh timestamp from reawakening the same material. Conflicting and partial joins refuse equality; missing revision stays unversioned. New material can coalesce within the same item without merging unrelated items. Exact replay must not decay pressure, append domain records or emit prospective observations.

Multiple complete records per bounded index pass improve progress without replacing cursor/fingerprint/lock authority. A partial tail waits for completion. Neither newest-N snapshots nor time expiry preserve old unresolved material.

## Latent context is not a new foreground job

An explicitly selected review may benefit from held/dormant context, but projection must not launch a review or write lifecycle state. Explicit hold provenance differs from recorded status; operator evaluation differs from preference; ledger touch differs from source observation time. Unknown remains unknown. Output caps are real, but full input reads are unbounded and the projection reports incomplete semantics. No automatic native consumer is included.

## Approval is more than path substitution

Runtime installation/generation, interpreter binary/configuration, compiled ABI, real import/distribution origins and route/configuration authority matter, not just a path or module version. Native ownership includes profile/tier/instance/root metadata and the entire compression lineage, not title matching. Artifact and episode-workspace ownership are distinct. A self-digest cannot authorize itself, and a new route approval cannot erase debit or adopt an old session. Drift must HOLD rather than refresh bindings or select a fallback.

No synthetic-only generic owner framework is added merely to suggest that every local implementation was ported. The proposal identifies behavior and missing capability contracts; a later extraction needs separate scope and evidence.

## Availability is not admission or obligation

Proposed default import/install is inert: no configuration discovery, native/provider load, state creation or registry mutation. That is a design requirement, not a claim about privately coupled source modules. Initialization establishes accounting OFF; enablement still does not grant a turn. A finite current choice requires fresh grant, source approval, quota and durable debit/replay reservation. No clock or capacity sensor creates the choice. STOP, no artifact and keep-private are successful choices, not failures to fulfill a task.

Finite limits should be configured and validated, not copied from private tuning as universal operating policy. Accounting authority may be attested by an approved external caller; no new global registry or mandatory control plane is required.

## Quota is neither pressure nor fallback

Quota admits the chosen native route only. It is not burn urgency, automatic awakening or permission to query unrelated providers. All present real windows must be valid and fresh; missing secondary can be legitimate. No projected windows or reset remapping. Request-time quota checks are proposed additions to source expiry hooks. Failure after durable reservation remains charged even if later quota blocks dispatch.

## Read-only needs dispatch and provider proof

A combined owner/config/authentication digest detects drift; it does not prove remote no-write semantics. Granted eager/deferred definitions must agree with actual dispatch, not just a tool-list screenshot. Automatic retain/sync/prefetch/delegation and compression-write hooks must be suppressed. Failed reads consuming their allowance before provider dispatch is a proposed tightening: source counters advance only after successful return. Private client/host attributes require version-specific proof, not invented SDK APIs.

## Accounting and process failure must stay conservative

Full precharge before launch prevents crashes from creating free repeat capacity. Source lock/replay/precharge algorithms inform the design, but multi-file markers and counters need a proposed durable journal to close crash windows. Missing accounting cannot become empty accounting. Migration preserves original dates, charged failures and replay/lifetime episode slots. Proven overlapping imports are counted once; distinct spend is neither collapsed with `max` nor guessed away. Unknown history or attribution HOLDs.

A standalone episode admission cap does not prove that a standing worker taking a different entrypoint enforces the same lifetime continuation cap. The proposed owner must preserve it across days and callers. Parent exit, successful communication or group-leader reap does not prove whole-tree retirement. Queued STOP is a no-new-inference choice; active cancellation needs exact-claim authority and verified containment. Neither guarantee is established by source inspection alone.

## Retain established protections

Keep canonical instance/live-state-root guards, closed sensitivity/surface apertures, exact leases/presentation/settlement, anti-paraphrase suppression, source-bound outbox ownership and honest unknown runtime state. Dashboard GET projections remain compact, body-hidden and URL-validated; they are not another control plane. Memory-volunteering confidence remains evidence-cited and not write/delivery authority. Do not revive removed capacity-burn projections or overwrite newer public repairs with alternate full-file overlays.

Focused synthetic attention proof does not establish full-suite correctness, native SDK portability, remote memory behavior, migration authority, process containment, privacy of distributions or live adoption. Gateway compatibility and Wiki security remain held. Each claim must advance only when its owning implementation and corresponding evidence exist.
