# Memory Volunteering Protocol

Status: accepted design contract, 2026-07-04

This protocol governs any Sensorium/Subconscious path that wants to volunteer a memory, insight, recalled fact, private salience, or offer candidate to Conscious Sera.

It exists to keep confidence useful without letting confidence become authority. A high score may earn attention; it may not create truth, write durable memory, or authorize delivery.

## Authority hierarchy

Every volunteering path must preserve this sequence:

```text
evidence-cited capsule -> transparent confidence proposal -> Conscious authorization receipt
```

1. Evidence-cited capsule: compact factual payload with resolvable provenance refs, sensitivity, allowed surfaces, and explicit gaps.
2. Transparent confidence proposal: bounded score/label plus named components and negative evidence; this is an attention recommendation only.
3. Conscious authorization receipt: explicit foreground choice before durable truth, memory writes, artifact presentation, or outbound delivery.

Subconscious may propose. Conscious chooses. Operator/user policy may still forbid execution after Conscious chooses.

## Non-negotiable boundaries

- Confidence can surface attention; it cannot commit truth.
- Confidence can request Conscious review; it cannot write Hindsight, Memory, LCM, skills, docs, or Kanban follow-up by itself.
- Confidence can suggest an offer draft; it cannot authorize delivery or confident personal phrasing.
- No proposal may hide missing evidence behind a numeric score.
- No proposal may expand raw transcripts, private message bodies, prompts, secrets, or artifact contents by default.
- Privacy/surface projection happens before proposal construction, not as a post-hoc redaction pass.
- "I have something for you" requires a verified openable thread, held artifact, or selected gift. Uncertain salience uses uncertain language.

## Required capsule schema

A memory/insight volunteering capsule should contain, at minimum:

```json
{
  "capsule_id": "capsule:<stable-id>",
  "subject": "short stable subject label",
  "claim": "one compact claim or insight being volunteered",
  "source_type": "sensorium_event|candidate|thread|artifact|hindsight_recall|session_lcm|operator_correction|other",
  "evidence_refs": [
    {"kind": "event|candidate|thread|artifact|memory|session|task", "id": "...", "quote_or_field": "optional compact cited field"}
  ],
  "negative_evidence_refs": [],
  "do_not_know": ["missing or stale context"],
  "sensitivity": "public_safe|private|sensitive",
  "allowed_surfaces": ["local"],
  "privacy_projection": {
    "raw_body_included": false,
    "private_detail_projected": true,
    "projection_notes": "compact reason"
  },
  "replay_key": "stable deterministic key"
}
```

Invalid capsule -> hold for evidence repair or silent drop. Do not upgrade an invalid capsule by adding a confident model summary.

## Required proposal schema

A volunteering proposal is derived from a valid capsule:

```json
{
  "proposal_id": "vprop:<stable-id>",
  "capsule_id": "capsule:<stable-id>",
  "decision": "NO_VOLUNTEER|SUBCONSCIOUS_HOLD|ATTENTION_INBOX|CONSCIOUS_REVIEW|PREPARE_OFFER_DRAFT|HOLD_FOR_EVIDENCE_REPAIR",
  "confidence": 0.0,
  "confidence_label": "low|medium|high",
  "confidence_components": {
    "evidence_coverage": 0.0,
    "freshness": 0.0,
    "recurrence_or_novelty": 0.0,
    "identity_or_relationship_relevance": 0.0,
    "actionability_or_time_sensitivity": 0.0,
    "operator_preference_or_correction": 0.0,
    "verified_openable_subject": 0.0,
    "privacy_risk_penalty": 0.0,
    "staleness_penalty": 0.0,
    "budget_or_energy_penalty": 0.0,
    "phrasing_risk_penalty": 0.0
  },
  "why_now": "one compact reason",
  "authorization_state": "internal_only|conscious_required|artifact_required|delivery_forbidden|delivery_approved",
  "allowed_next_actions": ["surface_pointer", "open_thread", "ask_conscious_review"],
  "forbidden_next_actions": ["write_memory", "deliver_message"],
  "proposed_human_phrase": null,
  "replay_key": "stable deterministic key"
}
```

Scores must be explainable by the component map. If components cannot be computed, use `HOLD_FOR_EVIDENCE_REPAIR`, not a guessed score.

## Threshold policy

These are design defaults; implementation may tune exact cutoffs only with tests and a changelog.

| Condition | Required result |
| --- | --- |
| Capsule missing required evidence refs | `HOLD_FOR_EVIDENCE_REPAIR` or `NO_VOLUNTEER` |
| Citation integrity below 0.80 | `HOLD_FOR_EVIDENCE_REPAIR` |
| Privacy/surface mismatch | `delivery_forbidden`; no public/foreign surface |
| Confidence < 0.45 | `NO_VOLUNTEER` / silent no-action receipt |
| 0.45 <= confidence < 0.65 | `SUBCONSCIOUS_HOLD` |
| 0.65 <= confidence < 0.78 | `ATTENTION_INBOX` only |
| 0.78 <= confidence < 0.88 | `CONSCIOUS_REVIEW` |
| confidence >= 0.88 | `CONSCIOUS_REVIEW`; optional `PREPARE_OFFER_DRAFT` only if an openable subject/artifact exists |

No threshold permits autonomous memory writes or delivery.

## Source-type rules

- Explicit user corrections and taste locks: high priority for Conscious review, but still require provenance and a conscious retention/write choice.
- Relational/private salience: require memory/context grounding, surface privacy checks, and verified openable subject before confident offer language.
- Operational claims: require current evidence from the original source when accessible; session history alone is not proof of current state.
- Creative insights: can be saved as design/taste residue after Conscious review; do not normalize into generic productivity notes.
- Memory-pressure probes: may propose a recall capsule; they cannot directly mutate Hindsight/Memory/LCM.

## Relationship to existing Sensorium surfaces

- `review_synthesis`: the intended evidence-contract primitive underneath this protocol. If it is not available or accepted, proposals remain doctrine/design-only or must carry equivalent explicit evidence refs.
- `volunteer_cards`: read-only orientation handles. Their `confidence` field is a navigational hint, not truth, memory, or delivery authority.
- `sensorium(status|ingest|open|update|reach_out)`: exactly five pull-based conscious actions. `reach_out` records or prepares a Conscious decision with `execute=False`; it is not delivery. Opening/reviewing a thread or preparing reach-out is not the same as authorizing durable memory or outbound action, which requires separate explicit configuration and an adapter-backed actuator outside the ordinary live call.
- Prepare-only actuators: may prepare artifacts only after a conscious decision ref; output remains not delivered until a separate explicit delivery gate.

## Acceptance probes for implementation

Any implementation of this protocol must prove:

1. Valid proposal construction fails closed when evidence refs are missing or stale.
2. A high-confidence proposal cannot call Hindsight retain, Memory add, LCM write, Kanban create, outbox delivery, or platform send.
3. Privacy/surface mismatch prevents confident phrase generation and delivery authorization.
4. The component map and replay key are deterministic for the same compact inputs.
5. Raw transcripts, secrets, prompt bodies, and full artifact bodies do not appear in proposal/status/dashboard output.
6. `volunteer_cards` or similar status surfaces label their confidence as advisory/navigation-only.
7. The only route from proposal to durable write/delivery is a conscious authorization receipt plus any configured operator/policy gate.

## Decision

Adopt this as the formal Memory Volunteering Protocol for Sensorium architecture. It is a design contract now; code should implement it only behind the accepted evidence-contract layer and with the acceptance probes above.
