# Agent Sensorium

Bounded autonomous inner lifecycle for Hermes agents: compact signals, filtered events, candidates, dormant conscious thread capsules, and pull-based review.

## Pipeline

```
Sensors -> Signals -> [Gate] -> Events -> Candidates -> [Dispatcher] -> Conscious Threads
```

1. **Sensors** emit raw **Signals** (observations, corrections, artifacts).
2. A deterministic **Gate** promotes strong signals to **Events** based on strength and kind thresholds.
3. Events create **Candidates** with weighted pressure scores.
4. A **Dispatcher** promotes the top candidate into a dormant **Conscious Thread** capsule.
5. A tiny active-session pointer can mention that an eligible thread exists when the current surface is allowed and cooldown is open. The pointer is only a doorway, not the capsule.

## MVP Limitations

- **Pull-based only** — no proactive messages, no DM delivery.
- **No model-backed Subconscious pass** — promotion is deterministic.
- **No platform thread creation** — threads are internal state only.
- **No relational autonomy** — no REACH_OUT decisions.
- **No external task creation** — no Kanban/research/media tasks.
- **No scheduled automation** — tick runs manually or via explicit invocation.

## Tools

| Tool | Description |
|------|-------------|
| `sensorium_status` | Read-only state snapshot: counts, top candidates, visible threads |
| `sensorium_ingest_signal` | Ingest a signal and promote if threshold met |
| `sensorium_dispatch_once` | Select top candidate and create one dormant thread |
| `sensorium_candidate_update` | Suppress / hold / cancel / mark_reviewed a candidate |
| `sensorium_attention_pointer` | Preview the small active-session pointer for a surface; read-only and non-mutating |
| `sensorium_thread_open` | Open a compact conscious-thread capsule when the requested surface is allowed |
| `sensorium_thread_update` | Close / hold / resume / archive / pin / unpin a conscious thread with a receipt |
| `sensorium_compact` | Archive expired candidates and threads with receipts |

## Pointer vs capsule boundary

The active-session pointer is a doorway, not awareness itself. It may reveal only:

- that one eligible dormant/held Sensorium thread exists;
- the thread id and short title;
- the operator phrase for opening it, such as “take it up”.

The pointer must not include capsule internals such as continuity notes, decision logs, open questions, or private operational memory. Those are returned only by `sensorium_thread_open` after the requested surface passes the thread’s `allowed_surfaces` gate.

There are two pointer paths:

- `sensorium_attention_pointer` is a **preview tool**. It does not mutate state or write cooldown receipts.
- The `pre_llm_call` hook is the **presentation path**. When it injects a pointer into the active turn, it writes a `pointer.presented` receipt so cooldown can suppress repeats.

The injected pointer context is model-facing validation scaffolding. It can include explicit instructions such as “if the user says take it up, call `sensorium_thread_open`.” Final user-facing UX should stay smaller and more natural.

## Command

```
/sensorium [status|threads|pointer|open|thread|dispatch|compact|help]
```

- **status** (default) — compact overview with counts and top items
- **threads** — visible dormant/held threads with origin info
- **pointer [surface]** — preview the small doorway that may be injected into an active session when surface/privacy/cooldown gates allow it
- **open [thread_id|latest] [surface]** — open a compact thread capsule if the requested surface is allowed
- **thread <thread_id|latest> <close|hold|resume|archive|pin|unpin|mark_reviewed> [reason]** — update thread lifecycle/pin state with a receipt
- **dispatch** — dry-run dispatch preview (never mutates via command)
- **compact** — archive expired items
- **help** — usage reference

## Conscious Review Checklist

When reviewing a dormant thread, choose exactly one action:

- [ ] **Suppress** — noise, not actionable, discard
- [ ] **Hold** — interesting but not urgent; revisit later
- [ ] **Save** — convert to workflow guidance or reference note
- [ ] **Close** — resolved or no longer relevant
- [ ] **Create follow-up** — bounded, specific next action only

### Review boundaries

- Do NOT auto-send messages to the operator.
- Do NOT create external tasks without explicit approval.
- Do NOT escalate beyond the current review scope.
- Do NOT assume urgency from pressure scores alone.
- Do NOT act on expired or archived threads.
