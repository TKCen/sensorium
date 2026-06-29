"""Tool handlers for Agent Sensorium — callable without live Hermes runtime."""

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .attention import build_attention_inbox
from .dispatcher import current_budget_state, dispatch_once as _dispatch_once
from .gate import (
    build_pressure_pitch,
    candidate_fingerprint,
    decayed_candidate,
    DEFAULT_CONFIG,
    event_fingerprint,
    event_to_candidate,
    inhibited_by_sensor_policy,
    prune_sensor_policy,
    promote_signal_to_event,
    should_promote_signal,
    signal_fingerprint,
)
from .pointers import select_attention_pointer
from .schemas import (
    intersect_allowed_surfaces,
    merge_sensitivity,
    normalize_signal,
    truncate_text,
    utc_now_iso,
    validate_event,
    validate_signal,
)
from .store import SensoriumStore
from .sensors import (
    is_candidate_extinct,
    load_sensor_registry,
    mark_extinct,
    register_sensor_kind,
    sensor_registry_snapshot,
)
from .subconscious import run_subconscious_advisory
from .improvement import (
    record_attention_policy_decision,
    run_improvement_collector,
    summarize_improvement_state,
)
from .actions import (
    attach_action_ref,
    compact_actions_for_thread,
    list_thread_actions,
    prepare_action,
    record_action_result,
)
from .artifacts import (
    compact_artifacts_for_thread,
    list_artifacts,
    store_artifact,
)
from .media_gifts import apply_media_gift_choice
from .actuators import (
    load_actuator_registry,
    register_actuator,
    run_actuator_prepare_artifact,
)
from .conscious import (
    claim_dormant_thread,
    complete_claim,
)
from .workers import (
    dispatch_worker_request,
    list_worker_requests,
    prepare_worker_request,
    record_worker_result,
)
from .config import (
    default_instance_name,
    init_profile_config,
    list_profiles,
    load_instance_config,
    manage_attention_policy_config,
    profile_state_dir,
    read_active_profile,
    write_active_profile,
)

ARCHIVED_STATUSES = {"archived", "closed"}

VALID_CANDIDATE_ACTIONS = {"suppress", "hold", "cancel", "mark_reviewed"}
VALID_THREAD_ACTIONS = {"close", "hold", "resume", "archive", "pin", "unpin", "mark_reviewed"}
_VISIBLE_STATUSES = {"dormant", "held"}
_ALLOWED_THREAD_TRANSITIONS: dict[str, set[str]] = {
    "dormant": {"close", "hold", "archive", "mark_reviewed", "pin", "unpin"},
    "held": {"close", "resume", "archive", "mark_reviewed", "pin", "unpin"},
}


def _ok(instance: str, data) -> str:
    return json.dumps({"success": True, "instance": instance, "data": data, "error": None})


def _err(instance: str, error: str) -> str:
    return json.dumps({"success": False, "instance": instance, "data": None, "error": error})


def _file_mtime_iso(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
    except Exception:
        return None


def _legacy_state_latest_info(store: SensoriumStore, state: dict) -> dict:
    path = store.root / "state.latest.json"
    return {
        "path": str(path),
        "exists": path.exists(),
        "mtime": _file_mtime_iso(path),
        "updated_at": state.get("updated_at") if isinstance(state, dict) else None,
        "deprecated": True,
        "role": "legacy_dispatch_snapshot",
        "excluded_from_freshness": True,
    }


def handle_sensorium_status(
    *, instance: str = "default", state_dir: str | None = None,
    config_path: str | None = None,
) -> str:
    from .config import load_instance_config

    store = SensoriumStore(instance=instance, state_dir=state_dir)
    store.ensure_dirs()

    instance_config, config_diag = load_instance_config(
        config_path=config_path, state_dir=str(store.root),
    )

    candidates = store.read_jsonl("candidates")
    threads = store.read_jsonl("threads")
    state = store.read_state()

    active_candidates = [c for c in candidates if c.get("status") == "candidate"]
    active_candidates.sort(key=lambda c: c.get("pressure", 0), reverse=True)

    visible_threads = [t for t in threads if t.get("status") in ("dormant", "held")]
    visible_threads.sort(key=lambda t: t.get("created_at", ""), reverse=True)

    active_threads = [t for t in threads if t.get("status") in ("dormant", "held")]
    now_ts = utc_now_iso()
    dirty_count = sum(1 for t in active_threads if t.get("dirty_since"))
    expiring_count = 0
    starved_count = 0
    for t in active_threads:
        expires = t.get("expires_at", "")
        if expires:
            try:
                exp_dt = datetime.strptime(expires, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                now_dt = datetime.strptime(now_ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                if timedelta(0) < (exp_dt - now_dt) <= timedelta(hours=24):
                    expiring_count += 1
            except (ValueError, TypeError):
                pass
        last_interaction = t.get("last_interaction_at") or t.get("created_at", "")
        if last_interaction:
            try:
                last_dt = datetime.strptime(last_interaction, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                now_dt = datetime.strptime(now_ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                if (now_dt - last_dt) > timedelta(hours=72):
                    starved_count += 1
            except (ValueError, TypeError):
                pass

    data = {
        "instance": instance,
        "state_dir": str(store.root),
        "counts": {
            "signals": store.count_jsonl("signals"),
            "events": store.count_jsonl("events"),
            "candidates": len(candidates),
            "active_candidates": len(active_candidates),
            "threads": len(threads),
            "artifacts": store.count_jsonl("artifacts"),
            "dormant_threads": len([t for t in threads if t.get("status") == "dormant"]),
            "held_threads": len([t for t in threads if t.get("status") == "held"]),
            "closed_threads": len([t for t in threads if t.get("status") == "closed"]),
            "archived_threads": len([t for t in threads if t.get("status") == "archived"]),
            "archived_candidates": len([c for c in candidates if c.get("status") == "archived"]),
            "dirty_threads": dirty_count,
            "starved_threads": starved_count,
            "expiring_threads": expiring_count,
        },
        "top_candidates": [
            {
                "id": c["id"],
                "kind": c.get("kind"),
                "pressure": c.get("pressure"),
                "summary": truncate_text(c.get("summary", ""), 120),
            }
            for c in active_candidates[:5]
        ],
        "top_threads": [
            {
                "id": t["id"],
                "status": t.get("status"),
                "title": truncate_text(t.get("conscious_task", {}).get("title", ""), 120),
                "origin_candidate_id": t.get("origin_candidate_id"),
                "created_at": t.get("created_at"),
            }
            for t in visible_threads[:5]
        ],
        "config": config_diag,
        "budgets": current_budget_state(store, instance_config),
        "legacy_state_latest": _legacy_state_latest_info(store, state),
        "ts": utc_now_iso(),
    }

    decisions = store.read_jsonl("decisions")
    if decisions:
        latest = decisions[-1]
        data["latest_decision"] = {
            "ts": latest.get("ts"),
            "type": latest.get("type"),
            "thread_id": latest.get("thread_id"),
            "candidate_id": latest.get("candidate_id"),
            "action": latest.get("action"),
            "reason": truncate_text(latest.get("reason", ""), 80),
        }

    if state:
        if "state_version" in state:
            data["state_version"] = state.get("state_version")
        if "last_dispatch_result" in state:
            legacy_result = dict(state.get("last_dispatch_result") or {})
            raw_legacy_action = legacy_result.get("action")
            if raw_legacy_action in {"would_promote", "promoted"}:
                legacy_result["raw_legacy_action"] = raw_legacy_action
                legacy_result["action"] = "kanban_review_required"
                legacy_result["recommended_activation"] = "kanban_bridge"
            data["legacy_dispatch_result"] = {
                **legacy_result,
                "deprecated": True,
                "activation_substrate": "kanban",
                "ignored_as_activation": True,
            }
        if "locks" in state:
            data["locks"] = state.get("locks")

    return _ok(instance, data)


def _stored_signal_fingerprint(signal: dict) -> str:
    return signal.get("fingerprint") or signal_fingerprint(signal)


def _stored_event_fingerprint(event: dict) -> str:
    return event.get("fingerprint") or event_fingerprint(event)


def _stored_candidate_fingerprint(candidate: dict) -> str:
    return candidate.get("fingerprint") or candidate_fingerprint(candidate)


def _candidate_for_event_id(candidates: list[dict], event_id: str) -> dict | None:
    for candidate in candidates:
        if event_id in (candidate.get("event_ids") or []):
            return candidate
    return None


def _candidate_for_event_fingerprint(candidates: list[dict], event: dict) -> dict | None:
    event_candidate = event_to_candidate(event)
    event_candidate_fp = event_candidate.get("fingerprint") or candidate_fingerprint(event_candidate)
    for candidate in candidates:
        if _stored_candidate_fingerprint(candidate) == event_candidate_fp:
            return candidate
    return None


def _find_existing_signal(signals: list[dict], normalized: dict) -> dict | None:
    incoming_fp = normalized.get("fingerprint") or signal_fingerprint(normalized)
    incoming_id = normalized.get("id")
    for existing in signals:
        if incoming_id and existing.get("id") == incoming_id:
            return existing
        if _stored_signal_fingerprint(existing) == incoming_fp:
            return existing
    return None


def _find_existing_event(events: list[dict], incoming: dict) -> dict | None:
    incoming_fp = incoming.get("fingerprint") or event_fingerprint(incoming)
    incoming_id = incoming.get("id")
    for existing in events:
        if incoming_id and existing.get("id") == incoming_id:
            return existing
        if _stored_event_fingerprint(existing) == incoming_fp:
            return existing
    return None


def _find_event_for_signal(events: list[dict], signal_id: str) -> dict | None:
    for event in events:
        if signal_id in (event.get("source_signal_ids") or []):
            return event
    return None


GENERIC_CORRELATION_KEYS = {"active-session"}


def _specific_correlation_keys(keys: list[str] | set[str] | tuple[str, ...]) -> set[str]:
    """Return keys that are specific enough to justify candidate coalescing."""
    specific: set[str] = set()
    for key in keys or []:
        value = str(key or "").strip()
        if not value or value in GENERIC_CORRELATION_KEYS or value.startswith("surface:"):
            continue
        specific.add(value)
    return specific


def _find_related_candidate(candidates: list[dict], event: dict) -> dict | None:
    incoming_keys = _specific_correlation_keys(event.get("correlation_keys") or [])
    # Generic live-session keys such as `active-session`, `surface:discord`, or
    # the event kind itself are not enough to merge separate corrections. False
    # coalescing hides distinct salience and prevents later context pointers.
    incoming_keys.discard(str(event.get("kind") or ""))
    if not incoming_keys:
        return None
    for candidate in candidates:
        if candidate.get("status", "candidate") != "candidate":
            continue
        if candidate.get("kind") != event.get("kind"):
            continue
        candidate_keys = _specific_correlation_keys(candidate.get("correlation_keys") or [])
        candidate_keys.discard(str(candidate.get("kind") or ""))
        if incoming_keys & candidate_keys:
            return candidate
    return None


def _read_pruned_sensor_policy(
    store: SensoriumStore,
    *,
    config: dict | None = None,
    now: str | None = None,
) -> dict:
    policy = store.read_sensor_policy()
    pruned = prune_sensor_policy(policy, config=config, now=now)
    if pruned != policy:
        store.write_sensor_policy(pruned)
    return pruned


def _apply_candidate_decay(
    store: SensoriumStore,
    candidates: list[dict],
    *,
    config: dict | None = None,
    now: str | None = None,
) -> int:
    """Persist one cheap habituation pass over active candidates."""
    decayed_count = 0
    for idx, candidate in enumerate(candidates):
        if candidate.get("status", "candidate") != "candidate":
            continue
        decayed = decayed_candidate(candidate, now=now, config=config)
        silence_ttl_hours = (config or {}).get("silence_ttl_hours", DEFAULT_CONFIG["inhibition"]["ttl_hours"])
        try:
            silence_ttl_hours = float(silence_ttl_hours)
        except (TypeError, ValueError):
            silence_ttl_hours = DEFAULT_CONFIG["inhibition"]["ttl_hours"]
        if is_candidate_extinct(decayed, now=now, silence_ttl_hours=silence_ttl_hours):
            decayed = mark_extinct(decayed, reason="silence_ttl_expired")
            decayed["status"] = "suppressed"
            decayed["updated_at"] = now or utc_now_iso()
        if decayed != candidate:
            candidates[idx] = decayed
            decayed_count += 1
    if decayed_count:
        _rewrite_jsonl(store, "candidates", candidates)
    return decayed_count


def _event_to_existing_result(event: dict, candidates: list[dict]) -> dict:
    candidate = (
        _candidate_for_event_id(candidates, event.get("id", ""))
        or _candidate_for_event_fingerprint(candidates, event)
    )
    result = {
        "event_id": event.get("id"),
        "duplicate": True,
    }
    if candidate:
        result["candidate_id"] = candidate.get("id")
    return result


def _promoted_signal_existing_result(signal: dict, events: list[dict], candidates: list[dict]) -> dict:
    event = _find_event_for_signal(events, signal.get("id", ""))
    result: dict = {
        "signal_id": signal.get("id"),
        "promoted": bool(event),
        "duplicate": True,
        "reason": "duplicate_signal",
    }
    if event:
        result["event_id"] = event.get("id")
        candidate = _candidate_for_event_id(candidates, event.get("id", ""))
        if candidate:
            result["candidate_id"] = candidate.get("id")
    return result


def _coalesce_candidate_with_event(
    store: SensoriumStore,
    *,
    candidate: dict,
    candidates: list[dict],
    event: dict,
    incoming_candidate: dict,
) -> dict:
    now = utc_now_iso()
    event_ids = list(candidate.get("event_ids") or [])
    if event.get("id") not in event_ids:
        event_ids.append(event.get("id"))
    candidate["event_ids"] = event_ids

    keys = sorted(set(candidate.get("correlation_keys") or []) | set(event.get("correlation_keys") or []))
    candidate["correlation_keys"] = keys
    candidate["repetition"] = round(min(1.0, max(candidate.get("repetition", 0.0), (len(event_ids) - 1) * 0.25)), 3)
    candidate["pressure"] = round(
        min(1.0, max(candidate.get("pressure", 0.0), incoming_candidate.get("pressure", 0.0)) + 0.05 * (len(event_ids) - 1)),
        3,
    )
    candidate["sensitivity"] = merge_sensitivity([candidate.get("sensitivity", "private"), event.get("sensitivity", "private")])
    candidate["allowed_surfaces"] = intersect_allowed_surfaces([
        candidate.get("allowed_surfaces") or [],
        event.get("allowed_surfaces") or [],
    ])
    candidate["updated_at"] = now
    if not candidate.get("feedback_meta") and incoming_candidate.get("feedback_meta"):
        candidate["feedback_meta"] = incoming_candidate["feedback_meta"]
    candidate.setdefault("related_event_fingerprints", [])
    event_fp = event.get("fingerprint") or event_fingerprint(event)
    if event_fp not in candidate["related_event_fingerprints"]:
        candidate["related_event_fingerprints"].append(event_fp)

    receipt = {
        "ts": now,
        "type": "candidate.coalesced",
        "candidate_id": candidate.get("id"),
        "event_id": event.get("id"),
        "event_count": len(event_ids),
        "pressure": candidate.get("pressure"),
        "repetition": candidate.get("repetition"),
    }
    store.append_jsonl("decisions", receipt)
    _rewrite_jsonl(store, "candidates", candidates)
    return receipt


def _append_event_and_create_or_update_candidate(
    store: SensoriumStore,
    *,
    event: dict,
    config: dict | None = None,
) -> dict:
    events = store.read_jsonl("events")
    candidates = store.read_jsonl("candidates")
    _apply_candidate_decay(store, candidates, config=config)

    existing_event = _find_existing_event(events, event)
    if existing_event:
        return _event_to_existing_result(existing_event, candidates)

    store.append_jsonl("events", event)
    candidate = event_to_candidate(event, config)
    related = _find_related_candidate(candidates, event)
    if related:
        receipt = _coalesce_candidate_with_event(
            store,
            candidate=related,
            candidates=candidates,
            event=event,
            incoming_candidate=candidate,
        )
        return {
            "event_id": event["id"],
            "candidate_id": related["id"],
            "duplicate": False,
            "coalesced": True,
            "receipt": receipt,
        }

    store.append_jsonl("candidates", candidate)
    return {
        "event_id": event["id"],
        "candidate_id": candidate["id"],
        "duplicate": False,
        "coalesced": False,
    }


def handle_sensorium_sensor_config(
    *,
    action: str,
    name: str = "",
    defaults: dict | None = None,
    status: str = "active",
    instance: str = "default",
    state_dir: str | None = None,
) -> str:
    """Runtime programmable sensor registry management.

    This is configuration-only: it never runs sensors, emits signals, spawns tasks,
    or performs external action.
    """
    store = SensoriumStore(instance=instance, state_dir=state_dir)
    store.ensure_dirs()
    load_sensor_registry(store)
    try:
        if action == "list":
            registry = sensor_registry_snapshot()
        elif action in {"register", "modify", "pause", "deprecate"}:
            effective_status = {"pause": "paused", "deprecate": "deprecated"}.get(action, status)
            register_sensor_kind(name, defaults=defaults or {}, status=effective_status, store=store)
            registry = sensor_registry_snapshot()
        else:
            return _err(instance, "Invalid action. Must be one of: list, register, modify, pause, deprecate")
    except ValueError as e:
        return _err(instance, str(e))
    return _ok(instance, {"action": action, "registry": registry})


def handle_sensorium_actuator_config(
    *,
    action: str,
    name: str = "",
    entry: dict | None = None,
    status: str = "active",
    instance: str = "default",
    state_dir: str | None = None,
) -> str:
    """Runtime programmable actuator registry management.

    Configuration-only: list/register/modify/pause/deprecate actuator specs. It
    never runs scripts, emits signals, sends messages, or prepares artifacts.
    """
    store = SensoriumStore(instance=instance, state_dir=state_dir)
    store.ensure_dirs()
    action = str(action or "list").strip().lower()
    try:
        if action == "list":
            registry = load_actuator_registry(store)
        elif action in {"register", "modify", "pause", "deprecate"}:
            effective_status = {"pause": "paused", "deprecate": "deprecated"}.get(action, status)
            registry = register_actuator(store, name, entry=entry or {}, status=effective_status)
        else:
            return _err(instance, "Invalid action. Must be one of: list, register, modify, pause, deprecate")
    except ValueError as e:
        return _err(instance, str(e))
    return _ok(instance, {"action": action, "registry": registry})


def handle_sensorium_actuator_prepare(
    *,
    name: str,
    request: dict | None = None,
    config: dict | None = None,
    instance: str = "default",
    state_dir: str | None = None,
) -> str:
    """Run one hotloaded Conscious-gated actuator prepare action."""
    store = SensoriumStore(instance=instance, state_dir=state_dir)
    result = run_actuator_prepare_artifact(
        store,
        name=name,
        request=request or {},
        config=config,
    )
    if result.get("success"):
        return _ok(instance, result)
    return _err(instance, result.get("error", "actuator_prepare_failed"))


def handle_sensorium_profile(
    *,
    action: str = "list",
    profile: str = "",
    overwrite: bool = False,
    base_dir: str | None = None,
) -> str:
    """List, show, initialize, or set the default of Sensorium profiles.

    A profile is a named runtime namespace (config + state) under the Sensorium
    state root. This is configuration-only: it never runs sensors, ingests
    signals, spawns tasks, or performs external action. It can only write a
    profile's own instance.config.json (init) and the root active-profile marker
    (set_default) — not arbitrary files.
    """
    action = str(action or "list").strip().lower()
    active = read_active_profile(base_dir) or default_instance_name("default")
    try:
        if action == "list":
            names = list_profiles(base_dir)
            if active not in names:
                names = sorted(set(names) | {active})
            return _ok(active, {
                "action": action,
                "active_profile": active,
                "profiles": [
                    {"profile": name, "active": name == active} for name in names
                ],
            })
        if action == "show":
            name = profile.strip() or active
            state_dir = profile_state_dir(name, base_dir)
            config, diagnostics = load_instance_config(state_dir=str(state_dir))
            return _ok(name, {
                "action": action,
                "profile": name,
                "active": name == active,
                "state_dir": str(state_dir),
                "config": config,
                "diagnostics": diagnostics,
            })
        if action == "init":
            if not profile.strip():
                return _err(active, "profile name required for init")
            result = init_profile_config(profile, base_dir=base_dir, overwrite=bool(overwrite))
            return _ok(result["profile"], {"action": action, **result})
        if action == "set_default":
            if not profile.strip():
                return _err(active, "profile name required for set_default")
            path = write_active_profile(profile, base_dir=base_dir)
            return _ok(profile.strip(), {
                "action": action,
                "profile": profile.strip(),
                "active_profile_marker": str(path),
            })
        return _err(active, "Invalid action. Must be one of: list, show, init, set_default")
    except ValueError as e:
        return _err(active, str(e))


def handle_sensorium_ingest_signal(
    *,
    signal: dict,
    instance: str = "default",
    state_dir: str | None = None,
    config: dict | None = None,
) -> str:
    try:
        validate_signal(signal)
    except ValueError as e:
        return _err(instance, str(e))

    store = SensoriumStore(instance=instance, state_dir=state_dir)
    store.ensure_dirs()
    if config is None:
        config, _ = load_instance_config(state_dir=str(store.root))

    normalized = normalize_signal(signal)
    normalized["fingerprint"] = signal_fingerprint(normalized)

    policy = _read_pruned_sensor_policy(store, config=config)
    inhibited, inhibition_reason = inhibited_by_sensor_policy(normalized, policy, config=config)

    signals = store.read_jsonl("signals")
    existing_signal = _find_existing_signal(signals, normalized)
    if existing_signal:
        events = store.read_jsonl("events")
        candidates = store.read_jsonl("candidates")
        return _ok(instance, _promoted_signal_existing_result(existing_signal, events, candidates))

    store.append_jsonl("signals", normalized)

    if inhibited:
        return _ok(instance, {
            "signal_id": normalized["id"],
            "promoted": False,
            "duplicate": False,
            "reason": f"inhibited: {inhibition_reason}",
        })

    promoted, reason = should_promote_signal(normalized, config)
    result: dict = {
        "signal_id": normalized["id"],
        "promoted": promoted,
        "duplicate": False,
        "reason": reason,
    }

    if promoted:
        event = promote_signal_to_event(normalized, config)
        event_result = _append_event_and_create_or_update_candidate(store, event=event, config=config)
        result["event_id"] = event_result["event_id"]
        if "candidate_id" in event_result:
            result["candidate_id"] = event_result["candidate_id"]
            candidate = next((c for c in store.read_jsonl("candidates") if c.get("id") == event_result["candidate_id"]), None)
            if candidate:
                thresholds = dict(DEFAULT_CONFIG["thresholds"])
                if isinstance((config or {}).get("thresholds"), dict):
                    thresholds.update((config or {})["thresholds"])
                if candidate.get("pressure", 0.0) >= thresholds["candidate_pressure"]:
                    result["pitch"] = build_pressure_pitch(
                        candidate,
                        events=store.read_jsonl("events"),
                        threshold=thresholds["candidate_pressure"],
                    )
        result["coalesced"] = event_result.get("coalesced", False)

    return _ok(instance, result)


def handle_sensorium_ingest_event(
    *,
    event: dict,
    instance: str = "default",
    state_dir: str | None = None,
    config: dict | None = None,
) -> str:
    """Ingest an already-promoted trusted event and create or update a candidate."""
    try:
        validate_event(event)
    except ValueError as e:
        return _err(instance, str(e))

    store = SensoriumStore(instance=instance, state_dir=state_dir)
    store.ensure_dirs()
    if config is None:
        config, _ = load_instance_config(state_dir=str(store.root))

    incoming = dict(event)
    incoming.setdefault("source_signal_ids", [])
    incoming.setdefault("signal_count", len(incoming.get("source_signal_ids") or []))
    incoming.setdefault("strength", 0.5)
    incoming.setdefault("correlation_keys", [])
    incoming.setdefault("sensitivity", "private")
    incoming.setdefault("allowed_surfaces", ["local"])
    incoming.setdefault("expires_at", "")
    incoming["fingerprint"] = event_fingerprint(incoming)

    try:
        validate_event(incoming)
    except ValueError as e:
        return _err(instance, str(e))

    result = _append_event_and_create_or_update_candidate(store, event=incoming, config=config)
    return _ok(instance, result)


def handle_sensorium_dispatch_once(
    *,
    instance: str = "default",
    state_dir: str | None = None,
    dry_run: bool = True,
    config: dict | None = None,
) -> str:
    """Compatibility dispatcher surface.

    Default is read-only advisory. Mutating dormant-thread creation is disabled
    unless config.legacy_thread_dispatch_enabled=True so Kanban remains the only
    activation/ticketing substrate.
    """
    store = SensoriumStore(instance=instance, state_dir=state_dir)
    store.ensure_dirs()
    if config is None:
        config, _ = load_instance_config(state_dir=str(store.root))
    result = _dispatch_once(store, dry_run=dry_run, config=config)
    return _ok(instance, result)


def handle_sensorium_subconscious_advisory(
    *,
    instance: str = "default",
    state_dir: str | None = None,
    dry_run: bool = True,
    enabled: bool = False,
    advisory_output: dict | None = None,
    config: dict | None = None,
    record_receipt: bool = True,
) -> str:
    """Run one bounded Subconscious advisory pass.

    Disabled by default. When enabled with config.model_enabled=true, the core
    can call a cheap OpenAI-compatible model over bounded context only; callers
    may also provide a precomputed advisory_output.
    """
    store = SensoriumStore(instance=instance, state_dir=state_dir)
    try:
        result = run_subconscious_advisory(
            store,
            advisory_output=advisory_output,
            enabled=enabled,
            dry_run=dry_run,
            config=config,
            record_receipt=record_receipt,
        )
    except ValueError as e:
        return _err(instance, str(e))
    return _ok(instance, result)


def handle_sensorium_improvement_collect(
    *,
    instance: str = "default",
    state_dir: str | None = None,
    dry_run: bool = False,
    bridge_state: dict | None = None,
    kanban_tasks: list[dict] | None = None,
    config: dict | None = None,
) -> str:
    """Run deterministic Sensorium self-improvement evidence collection."""
    store = SensoriumStore(instance=instance, state_dir=state_dir)
    effective_config = config
    if effective_config is None:
        instance_config, _ = load_instance_config(state_dir=str(store.root))
        effective_config = {"attention_policy": instance_config.get("attention_policy", {})}
    try:
        result = run_improvement_collector(
            store,
            bridge_state=bridge_state,
            kanban_tasks=kanban_tasks,
            dry_run=dry_run,
            config=effective_config,
        )
    except ValueError as e:
        return _err(instance, str(e))
    return _ok(instance, result)


def handle_sensorium_attention_policy_decide(
    *,
    candidate_id: str,
    decision: str,
    reason: str,
    future_tendency_delta: str,
    verification_condition: str,
    rollback_condition: str,
    decided_by: str = "conscious",
    decision_ref: str = "",
    implementation_ref: str = "",
    instance: str = "default",
    state_dir: str | None = None,
) -> str:
    """Record a conscious attention-policy-review decision receipt."""
    store = SensoriumStore(instance=instance, state_dir=state_dir)
    try:
        result = record_attention_policy_decision(
            store,
            candidate_id=candidate_id,
            decision=decision,
            reason=reason,
            future_tendency_delta=future_tendency_delta,
            verification_condition=verification_condition,
            rollback_condition=rollback_condition,
            decided_by=decided_by,
            decision_ref=decision_ref,
            implementation_ref=implementation_ref,
        )
    except ValueError as e:
        return _err(instance, str(e))
    return _ok(instance, result)


def handle_sensorium_attention_policy_manage(
    *,
    action: str,
    instance: str = "default",
    state_dir: str | None = None,
    config_path: str | None = None,
    rule: str = "",
    patch: dict | None = None,
    key: str = "",
    value: object | None = None,
    reason: str = "",
    future_tendency_delta: str = "",
    verification_condition: str = "",
    rollback_condition: str = "",
    actor: str = "conscious",
    decision_ref: str = "",
) -> str:
    """Apply a narrow declarative attention-policy config mutation."""
    store = SensoriumStore(instance=instance, state_dir=state_dir)
    store.ensure_dirs()
    try:
        result = manage_attention_policy_config(
            action=action,
            config_path=config_path,
            state_dir=str(store.root),
            rule=rule,
            patch=patch,
            key=key,
            value=value,
            reason=reason,
            future_tendency_delta=future_tendency_delta,
            verification_condition=verification_condition,
            rollback_condition=rollback_condition,
            actor=actor,
            decision_ref=decision_ref,
        )
    except ValueError as e:
        return _err(instance, str(e))
    store.append_jsonl("decisions", result["receipt"])
    return _ok(instance, result)


def handle_sensorium_improvement_status(
    *,
    instance: str = "default",
    state_dir: str | None = None,
) -> str:
    store = SensoriumStore(instance=instance, state_dir=state_dir)
    store.ensure_dirs()
    return _ok(instance, summarize_improvement_state(store))


def handle_sensorium_candidate_update(
    *,
    candidate_id: str,
    action: str,
    reason: str = "",
    instance: str = "default",
    state_dir: str | None = None,
) -> str:
    if action not in VALID_CANDIDATE_ACTIONS:
        return _err(instance, f"Invalid action '{action}'. Must be one of: {sorted(VALID_CANDIDATE_ACTIONS)}")

    store = SensoriumStore(instance=instance, state_dir=state_dir)
    store.ensure_dirs()

    candidates = store.read_jsonl("candidates")
    target = None
    for c in candidates:
        if c.get("id") == candidate_id:
            target = c
            break

    if target is None:
        return _err(instance, f"Candidate '{candidate_id}' not found.")

    old_status = target.get("status", "candidate")
    if action == "suppress":
        new_status = "suppressed"
    elif action == "hold":
        new_status = "held"
    elif action == "cancel":
        new_status = "cancelled"
    elif action == "mark_reviewed":
        new_status = "reviewed"
    else:
        new_status = old_status

    target["status"] = new_status
    target["updated_at"] = utc_now_iso()
    if action in {"suppress", "cancel", "mark_reviewed"}:
        reason_lower = reason.lower()
        if action == "suppress" or "reject" in reason_lower or "silence" in reason_lower:
            extinguished = mark_extinct(target, reason=reason or action)
            target.clear()
            target.update(extinguished)
            policy = _read_pruned_sensor_policy(store)
            existing_inhibitions = policy.get("inhibitions")
            inhibitions = existing_inhibitions if isinstance(existing_inhibitions, list) else []
            created_at = utc_now_iso()
            expires_at = (
                datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                + timedelta(hours=DEFAULT_CONFIG["inhibition"]["ttl_hours"])
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            inhibition = {
                "kind": target.get("kind", ""),
                "correlation_keys": list(target.get("correlation_keys", []) or []),
                "reason": reason or action,
                "candidate_id": candidate_id,
                "created_at": created_at,
                "expires_at": expires_at,
            }
            if inhibition["kind"] and inhibition not in inhibitions:
                inhibitions.append(inhibition)
                policy["inhibitions"] = inhibitions
                store.write_sensor_policy(prune_sensor_policy(policy))

    receipt = {
        "ts": utc_now_iso(),
        "type": "candidate.updated",
        "candidate_id": candidate_id,
        "action": action,
        "old_status": old_status,
        "new_status": new_status,
        "reason": reason,
    }
    store.append_jsonl("decisions", receipt)

    _rewrite_jsonl(store, "candidates", candidates)

    return _ok(instance, {
        "candidate_id": candidate_id,
        "action": action,
        "old_status": old_status,
        "new_status": new_status,
        "receipt": receipt,
    })


def _find_thread(threads: list[dict], thread_id: str | None = None) -> dict | None:
    visible = [t for t in threads if t.get("status") in ("dormant", "held")]
    visible.sort(key=lambda t: t.get("created_at", ""), reverse=True)
    if not thread_id or thread_id == "latest":
        return visible[0] if visible else None
    for t in threads:
        if t.get("id") == thread_id:
            return t
    return None


def _compact_thread_capsule(thread: dict) -> dict:
    task = thread.get("conscious_task", {})
    return {
        "thread_id": thread.get("id"),
        "status": thread.get("status"),
        "title": task.get("title", ""),
        "conscious_task": {
            "id": task.get("id"),
            "request_type": task.get("request_type"),
            "why": task.get("why"),
            "expected_decision": task.get("expected_decision"),
        },
        "origin_candidate_id": thread.get("origin_candidate_id"),
        "continuity_summary": thread.get("continuity_summary", []),
        "open_questions": thread.get("open_questions", []),
        "next_prompt_to_operator": thread.get("next_prompt_to_operator", ""),
        "summary_dirty": bool(thread.get("summary_dirty")),
        "dirty_since": thread.get("dirty_since"),
        "source_refs": thread.get("source_refs", []),
        "sensitivity": thread.get("sensitivity", "private"),
        "allowed_surfaces": thread.get("allowed_surfaces", []),
        "hold_reason": thread.get("hold_reason", ""),
        "resume_trigger": thread.get("resume_trigger", ""),
        "last_interaction_at": thread.get("last_interaction_at"),
        "created_at": thread.get("created_at"),
        "updated_at": thread.get("updated_at"),
        "expires_at": thread.get("expires_at"),
    }


def handle_sensorium_thread_open(
    *,
    thread_id: str = "latest",
    surface: str = "local",
    instance: str = "default",
    state_dir: str | None = None,
    config_path: str | None = None,
) -> str:
    from .config import load_instance_config, visible_on_surface

    store = SensoriumStore(instance=instance, state_dir=state_dir)
    store.ensure_dirs()

    instance_config, _ = load_instance_config(
        config_path=config_path, state_dir=str(store.root),
    )

    threads = store.read_jsonl("threads")
    target = _find_thread(threads, thread_id)
    if target is None:
        return _err(instance, f"Thread '{thread_id or 'latest'}' not found.")
    if target.get("status") not in _VISIBLE_STATUSES:
        return _err(instance, f"Thread '{target.get('id')}' is {target.get('status')} and cannot be opened.")
    if not visible_on_surface(target, surface, instance_config):
        return _err(instance, f"Thread '{target.get('id')}' is not allowed on surface '{surface}'.")

    capsule = _compact_thread_capsule(target)
    actions = compact_actions_for_thread(
        store, target.get("id", ""),
        surface=surface, instance_config=instance_config,
    )
    if actions:
        capsule["actions"] = actions
        capsule["action_count"] = len(actions)
    artifacts = compact_artifacts_for_thread(
        store, target.get("id", ""),
        surface=surface, instance_config=instance_config,
    )
    if artifacts:
        capsule["artifacts"] = artifacts
        capsule["artifact_count"] = len(artifacts)
    return _ok(instance, capsule)


def _thread_feedback_outcome(action: str) -> str:
    if action == "archive":
        return "operator_rejected"
    return "completed"


def _build_thread_feedback_signal(store: SensoriumStore, *, thread: dict, action: str, reason: str) -> dict:
    origin_candidate_id = thread.get("origin_candidate_id") or ""
    origin_candidate = None
    if origin_candidate_id:
        for candidate in store.read_jsonl("candidates"):
            if candidate.get("id") == origin_candidate_id:
                origin_candidate = candidate
                break

    caused_by = {
        "thread_id": thread.get("id", ""),
        "action": action,
    }
    if origin_candidate_id:
        caused_by["origin_candidate_id"] = origin_candidate_id

    summary_bits = [f"Thread {thread.get('id', '')} {action}"]
    if reason:
        summary_bits.append(reason)

    return {
        "sensor": "sensorium.thread_update",
        "source": "feedback",
        "kind": "task_result",
        "summary": ": ".join(summary_bits),
        "actor": "operator",
        "strength_hint": 0.5,
        "caused_by": caused_by,
        "outcome": _thread_feedback_outcome(action),
        "feedback_scope": "operator_evaluation",
        "correlation_keys": (origin_candidate.get("correlation_keys") or []) if origin_candidate else [],
        "sensitivity": thread.get("sensitivity", "private"),
        "allowed_surfaces": thread.get("allowed_surfaces") or ["local"],
        "source_ref": thread.get("id", ""),
    }


def handle_sensorium_thread_update(
    *,
    thread_id: str,
    action: str,
    reason: str = "",
    resume_trigger: str = "",
    emit_feedback: bool = False,
    instance: str = "default",
    state_dir: str | None = None,
) -> str:
    if action not in VALID_THREAD_ACTIONS:
        return _err(instance, f"Invalid action '{action}'. Must be one of: {sorted(VALID_THREAD_ACTIONS)}")

    store = SensoriumStore(instance=instance, state_dir=state_dir)
    store.ensure_dirs()
    threads = store.read_jsonl("threads")
    target = _find_thread(threads, thread_id)
    if target is None:
        return _err(instance, f"Thread '{thread_id}' not found.")

    old_status = target.get("status", "dormant")
    old_pinned = bool(target.get("pinned"))

    allowed = _ALLOWED_THREAD_TRANSITIONS.get(old_status)
    if allowed is None:
        return _err(instance, f"Thread '{target.get('id')}' is {old_status} and cannot be updated.")
    if action not in allowed:
        return _err(instance, f"Action '{action}' is not allowed on a {old_status} thread.")
    if action == "pin" and old_pinned:
        return _err(instance, f"Thread '{target.get('id')}' is already pinned.")
    if action == "unpin" and not old_pinned:
        return _err(instance, f"Thread '{target.get('id')}' is not pinned.")

    now = utc_now_iso()

    if action == "close":
        target["status"] = "closed"
    elif action == "hold":
        target["status"] = "held"
        target["hold_reason"] = reason
        if resume_trigger:
            target["resume_trigger"] = resume_trigger
    elif action == "resume":
        target["status"] = "dormant"
        target["hold_reason"] = ""
        target["resume_trigger"] = ""
    elif action == "archive":
        target["status"] = "archived"
    elif action == "mark_reviewed":
        target["status"] = "closed"
    elif action == "pin":
        target["pinned"] = True
    elif action == "unpin":
        target["pinned"] = False

    target["last_interaction_at"] = now
    target["updated_at"] = now
    if target.get("status") in {"closed", "archived"}:
        _mark_origin_candidate_reviewed(
            store,
            origin_candidate_id=target.get("origin_candidate_id"),
            thread_id=target.get("id"),
            now=now,
            reason=reason,
        )
        _write_settlement_hint(store, thread=target, now=now)
    receipt = {
        "ts": now,
        "type": "thread.updated",
        "thread_id": target.get("id"),
        "action": action,
        "old_status": old_status,
        "new_status": target.get("status", old_status),
        "old_pinned": old_pinned,
        "new_pinned": bool(target.get("pinned")),
        "reason": reason,
    }
    target.setdefault("decision_log", []).append(receipt)
    store.append_jsonl("decisions", receipt)

    if emit_feedback and target.get("status") in {"closed", "archived"}:
        feedback_signal_raw = handle_sensorium_ingest_signal(
            signal=_build_thread_feedback_signal(store, thread=target, action=action, reason=reason),
            instance=instance,
            state_dir=state_dir,
        )
        feedback_signal_result = json.loads(feedback_signal_raw)
        if not feedback_signal_result.get("success"):
            return _err(instance, feedback_signal_result.get("error") or "Failed to emit feedback signal.")
        feedback_signal_id = (feedback_signal_result.get("data") or {}).get("signal_id", "")
        feedback_receipt = {
            "ts": now,
            "type": "thread.feedback_emitted",
            "thread_id": target.get("id"),
            "origin_candidate_id": target.get("origin_candidate_id"),
            "feedback_signal_id": feedback_signal_id,
            "outcome": _thread_feedback_outcome(action),
            "feedback_scope": "operator_evaluation",
            "action": action,
            "reason": reason,
        }
        store.append_jsonl("decisions", feedback_receipt)
        receipt["feedback_emitted"] = True
        receipt["feedback_signal_id"] = feedback_signal_id

    _rewrite_jsonl(store, "threads", threads)
    return _ok(instance, receipt)


def _mark_origin_candidate_reviewed(
    store: SensoriumStore,
    *,
    origin_candidate_id: str | None,
    thread_id: str | None,
    now: str,
    reason: str,
) -> dict | None:
    """Mark the originating candidate reviewed when its conscious thread terminates."""
    if not origin_candidate_id:
        return None

    candidates = store.read_jsonl("candidates")
    target = None
    for candidate in candidates:
        if candidate.get("id") == origin_candidate_id:
            target = candidate
            break
    if target is None:
        return None

    old_status = target.get("status", "candidate")
    if old_status != "candidate":
        return None

    target["status"] = "reviewed"
    target["updated_at"] = now
    receipt = {
        "ts": now,
        "type": "candidate.updated",
        "candidate_id": origin_candidate_id,
        "thread_id": thread_id,
        "action": "mark_reviewed",
        "old_status": old_status,
        "new_status": "reviewed",
        "reason": reason or "origin thread closed",
    }
    store.append_jsonl("decisions", receipt)
    _rewrite_jsonl(store, "candidates", candidates)
    return receipt


def _write_settlement_hint(store: SensoriumStore, *, thread: dict, now: str) -> None:
    origin_id = thread.get("origin_candidate_id")
    if not origin_id:
        return
    candidates = store.read_jsonl("candidates")
    origin = None
    for c in candidates:
        if c.get("id") == origin_id:
            origin = c
            break
    receipt = {
        "ts": now,
        "type": "thread.settlement",
        "thread_id": thread.get("id"),
        "origin_candidate_id": origin_id,
        "correlation_keys": (origin.get("correlation_keys") or []) if origin else [],
        "fingerprint": (origin.get("fingerprint") or "") if origin else "",
        "settlement_type": thread.get("status", "closed"),
    }
    store.append_jsonl("decisions", receipt)


def handle_sensorium_attention_pointer(
    *,
    instance: str = "default",
    state_dir: str | None = None,
    surface: str = "local",
    config: dict | None = None,
    config_path: str | None = None,
) -> str:
    from .config import load_instance_config

    store = SensoriumStore(instance=instance, state_dir=state_dir)
    store.ensure_dirs()
    instance_config, _ = load_instance_config(
        config_path=config_path, state_dir=str(store.root),
    )
    return _ok(instance, select_attention_pointer(
        store, surface=surface, config=config, instance_config=instance_config,
    ))


def handle_sensorium_compact(
    *, instance: str = "default", state_dir: str | None = None
) -> str:
    store = SensoriumStore(instance=instance, state_dir=state_dir)
    store.ensure_dirs()

    now = utc_now_iso()
    candidates = store.read_jsonl("candidates")
    threads = store.read_jsonl("threads")

    archived_candidates: list[str] = []
    archived_threads: list[str] = []
    receipts: list[dict] = []

    for c in candidates:
        status = c.get("status", "candidate")
        if status in ARCHIVED_STATUSES:
            continue
        expires = c.get("expires_at", "")
        is_expired = bool(expires) and expires <= now
        is_terminal = status in ("suppressed", "cancelled")
        if is_expired or is_terminal:
            receipt = {
                "ts": now,
                "type": "compact.candidate_archived",
                "candidate_id": c["id"],
                "reason": "expired" if is_expired else f"terminal_status:{status}",
                "previous_status": status,
            }
            c["status"] = "archived"
            c["updated_at"] = now
            archived_candidates.append(c["id"])
            receipts.append(receipt)
            store.append_jsonl("decisions", receipt)

    for t in threads:
        status = t.get("status", "dormant")
        if status == "archived":
            continue
        if t.get("pinned"):
            continue
        expires = t.get("expires_at", "")
        if expires and expires <= now:
            receipt = {
                "ts": now,
                "type": "compact.thread_archived",
                "thread_id": t["id"],
                "reason": "expired",
                "previous_status": status,
            }
            t["status"] = "archived"
            t["updated_at"] = now
            archived_threads.append(t["id"])
            receipts.append(receipt)
            store.append_jsonl("decisions", receipt)

    if archived_candidates:
        _rewrite_jsonl(store, "candidates", candidates)
    if archived_threads:
        _rewrite_jsonl(store, "threads", threads)

    return _ok(instance, {
        "archived_candidates": archived_candidates,
        "archived_threads": archived_threads,
        "receipts_written": len(receipts),
    })


def handle_sensorium_service_threads(
    *,
    instance: str = "default",
    state_dir: str | None = None,
    config: dict | None = None,
    now: str | None = None,
) -> str:
    """Deterministic thread service pass: TTL archival, starvation/dirty/expiring reports."""
    store = SensoriumStore(instance=instance, state_dir=state_dir)
    store.ensure_dirs()

    now_ts = now or utc_now_iso()
    threads = store.read_jsonl("threads")
    candidates = store.read_jsonl("candidates")
    decayed_candidates = _apply_candidate_decay(store, candidates, config=config, now=now_ts)

    cfg = config or {}
    raw_thresholds = cfg.get("thresholds")
    thresholds = raw_thresholds if isinstance(raw_thresholds, dict) else {}
    starvation_hours = cfg.get("starvation_hours", thresholds.get("starvation_hours", 72))
    expiring_window_hours = cfg.get("expiring_window_hours", thresholds.get("expiring_window_hours", 24))

    active_statuses = {"dormant", "held"}
    archived_ids: list[str] = []
    starved_ids: list[str] = []
    dirty_ids: list[str] = []
    expiring_ids: list[str] = []
    receipts: list[dict] = []
    changed = False

    for t in threads:
        status = t.get("status", "dormant")
        if status not in active_statuses:
            continue

        expires = t.get("expires_at", "")
        if expires and expires <= now_ts and not t.get("pinned"):
            receipt = {
                "ts": now_ts,
                "type": "service.thread_archived",
                "thread_id": t["id"],
                "reason": "ttl_expired",
                "previous_status": status,
            }
            t["status"] = "archived"
            t["updated_at"] = now_ts
            archived_ids.append(t["id"])
            receipts.append(receipt)
            store.append_jsonl("decisions", receipt)
            changed = True
            continue

        last_interaction = t.get("last_interaction_at") or t.get("created_at", "")
        if last_interaction:
            try:
                last_dt = datetime.strptime(last_interaction, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                now_dt = datetime.strptime(now_ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                if (now_dt - last_dt) > timedelta(hours=starvation_hours):
                    starved_ids.append(t["id"])
            except (ValueError, TypeError):
                pass

        if t.get("dirty_since"):
            dirty_ids.append(t["id"])

        if expires:
            try:
                exp_dt = datetime.strptime(expires, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                now_dt = datetime.strptime(now_ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                remaining = exp_dt - now_dt
                if timedelta(0) < remaining <= timedelta(hours=expiring_window_hours):
                    expiring_ids.append(t["id"])
            except (ValueError, TypeError):
                pass

    if changed:
        _rewrite_jsonl(store, "threads", threads)

    data = {
        "serviced": len(archived_ids) + len(starved_ids) + len(dirty_ids) + len(expiring_ids),
        "archived": archived_ids,
        "starved": starved_ids,
        "dirty": dirty_ids,
        "expiring": expiring_ids,
        "receipts_written": len(receipts),
        "decayed_candidates": decayed_candidates,
    }
    return _ok(instance, data)


def _rewrite_jsonl(store: SensoriumStore, name: str, items: list[dict]) -> None:
    store.rewrite_jsonl(name, items)


def handle_sensorium_attention_inbox(
    *,
    instance: str = "default",
    state_dir: str | None = None,
    surface: str = "local",
    limit: int = 50,
    config_path: str | None = None,
) -> str:
    # Read-only aperture: do not call ensure_dirs() or create default state
    # just because an operator/dashboard looked at the inbox.
    store = SensoriumStore(instance=instance, state_dir=state_dir)
    inbox = build_attention_inbox(
        store, surface=surface, config_path=config_path, limit=limit,
    )
    return _ok(instance, inbox)


def handle_sensorium_outbox_prepare(
    *,
    thread_id: str,
    request_type: str = "THINK",
    surface: str = "local",
    delivery_mode: str = "",
    target: dict | None = None,
    title: str = "",
    message_preview: str = "",
    content_hash: str = "",
    origin_candidate_id: str = "",
    dry_run: bool = True,
    config: dict | None = None,
    instance: str = "default",
    state_dir: str | None = None,
) -> str:
    from .outbox import OUTBOX_DEFAULTS, prepare_outbox_request

    store = SensoriumStore(instance=instance, state_dir=state_dir)
    result = prepare_outbox_request(
        store,
        thread_id=thread_id,
        request_type=request_type,
        surface=surface,
        delivery_mode=delivery_mode or OUTBOX_DEFAULTS["default_delivery_mode"],
        target=target or {},
        title=title,
        message_preview=message_preview,
        content_hash=content_hash,
        origin_candidate_id=origin_candidate_id,
        config=config,
        dry_run=dry_run,
    )
    if result.get("success"):
        return _ok(instance, result)
    return _err(instance, result.get("detail") or result.get("error", "outbox_prepare_failed"))


def handle_sensorium_outbox_dispatch(
    *,
    outbox_id: str,
    execute: bool = False,
    config: dict | None = None,
    instance: str = "default",
    state_dir: str | None = None,
) -> str:
    from .outbox import dispatch_outbox_request

    store = SensoriumStore(instance=instance, state_dir=state_dir)
    store.ensure_dirs()
    result = dispatch_outbox_request(
        store,
        outbox_id=outbox_id,
        config=config,
        execute=execute,
    )
    if result.get("success"):
        return _ok(instance, result)
    return _err(instance, result.get("detail") or result.get("error", "outbox_dispatch_failed"))


def handle_sensorium_worker_prepare(
    *,
    thread_id: str,
    worker_type: str,
    title: str,
    task_summary: str = "",
    target: dict | None = None,
    profile: dict | None = None,
    config: dict | None = None,
    instance: str = "default",
    state_dir: str | None = None,
) -> str:
    store = SensoriumStore(instance=instance, state_dir=state_dir)
    result = prepare_worker_request(
        store,
        thread_id=thread_id,
        worker_type=worker_type,
        title=title,
        task_summary=task_summary,
        target=target,
        profile=profile,
        config=config,
    )
    if result.get("success"):
        return _ok(instance, result)
    return _err(instance, result.get("detail") or result.get("error", "worker_prepare_failed"))


def handle_sensorium_worker_dispatch(
    *,
    worker_request_id: str,
    execute: bool = False,
    config: dict | None = None,
    instance: str = "default",
    state_dir: str | None = None,
) -> str:
    store = SensoriumStore(instance=instance, state_dir=state_dir)
    store.ensure_dirs()
    result = dispatch_worker_request(
        store,
        worker_request_id=worker_request_id,
        config=config,
        execute=execute,
    )
    if result.get("success"):
        return _ok(instance, result)
    return _err(instance, result.get("detail") or result.get("error", "worker_dispatch_failed"))


def handle_sensorium_worker_result(
    *,
    worker_request_id: str,
    outcome: str,
    result_summary: str = "",
    output_refs: list[dict] | None = None,
    error_summary: str = "",
    config: dict | None = None,
    instance: str = "default",
    state_dir: str | None = None,
) -> str:
    store = SensoriumStore(instance=instance, state_dir=state_dir)
    result = record_worker_result(
        store,
        worker_request_id=worker_request_id,
        outcome=outcome,
        result_summary=result_summary,
        output_refs=output_refs,
        error_summary=error_summary,
        config=config,
    )
    if not result.get("success"):
        return _err(instance, result.get("detail") or result.get("error", "worker_result_failed"))

    feedback_signal = result.get("feedback_signal")
    feedback_signal_id = ""
    if feedback_signal:
        feedback_raw = handle_sensorium_ingest_signal(
            signal=feedback_signal,
            instance=instance,
            state_dir=state_dir,
        )
        feedback_result = json.loads(feedback_raw)
        if feedback_result.get("success"):
            feedback_signal_id = (feedback_result.get("data") or {}).get("signal_id", "")

        now = utc_now_iso()
        feedback_receipt = {
            "ts": now,
            "type": "worker.feedback_emitted",
            "thread_id": result["data"].get("origin_thread_id"),
            "worker_request_id": worker_request_id,
            "feedback_signal_id": feedback_signal_id,
            "outcome": outcome,
            "feedback_scope": "system_action",
        }
        store.append_jsonl("decisions", feedback_receipt)

    result_data = {
        "data": result["data"],
        "receipt": result["receipt"],
        "feedback_signal_id": feedback_signal_id,
    }
    return _ok(instance, result_data)


def handle_sensorium_worker_status(
    *,
    thread_id: str | None = None,
    status: str | None = None,
    limit: int = 20,
    instance: str = "default",
    state_dir: str | None = None,
) -> str:
    store = SensoriumStore(instance=instance, state_dir=state_dir)
    items = list_worker_requests(store, thread_id=thread_id, status=status, limit=limit)
    return _ok(instance, {"worker_requests": items, "count": len(items)})


# --- Mediated-presence gift policy handler ---


def handle_sensorium_media_gift_decide(
    *,
    decision: str,
    actor_tier: str = "conscious",
    source: str = "inner_salience",
    why_now: str = "",
    reason: str = "",
    thread_id: str = "",
    artifact_id: str = "",
    surface: str = "",
    target_ref: str = "",
    config: dict | None = None,
    config_path: str | None = None,
    instance: str = "default",
    state_dir: str | None = None,
) -> str:
    store = SensoriumStore(instance=instance, state_dir=state_dir)
    instance_config, _ = load_instance_config(
        config_path=config_path, state_dir=str(store.root),
    )
    if isinstance(config, dict):
        merged_policy = dict(instance_config.get("media_gift_policy") or {})
        merged_policy.update(config)
        instance_config["media_gift_policy"] = merged_policy
    result = apply_media_gift_choice(
        store,
        decision=decision,
        actor_tier=actor_tier,
        source=source,
        why_now=why_now,
        reason=reason,
        thread_id=thread_id,
        artifact_id=artifact_id,
        surface=surface,
        target_ref=target_ref,
        config=instance_config,
    )
    if result.get("success"):
        return _ok(instance, result)
    return _err(
        instance,
        result.get("detail") or result.get("error", "media_gift_decision_failed"),
    )


# --- Mediated-presence artifact tool handlers ---


def handle_sensorium_artifact_store(
    *,
    kind: str,
    ref_path: str,
    provenance: dict | None = None,
    why_created: str = "",
    intended_handoff_mode: str = "present_thread",
    delivery_state: str = "not_delivered",
    capacity_requirements: dict | None = None,
    source_thread_id: str = "",
    source_candidate_id: str = "",
    source_action_id: str = "",
    feedback_hooks: dict | None = None,
    sensitivity: str | None = None,
    allowed_surfaces: list[str] | None = None,
    config: dict | None = None,
    instance: str = "default",
    state_dir: str | None = None,
) -> str:
    store = SensoriumStore(instance=instance, state_dir=state_dir)
    result = store_artifact(
        store,
        kind=kind,
        ref_path=ref_path,
        provenance=provenance,
        why_created=why_created,
        intended_handoff_mode=intended_handoff_mode,
        delivery_state=delivery_state,
        capacity_requirements=capacity_requirements,
        source_thread_id=source_thread_id,
        source_candidate_id=source_candidate_id,
        source_action_id=source_action_id,
        feedback_hooks=feedback_hooks,
        sensitivity=sensitivity,
        allowed_surfaces=allowed_surfaces,
        config=config,
    )
    if result.get("success"):
        return _ok(instance, result.get("data") or {})
    return _err(
        instance,
        result.get("detail") or result.get("error", "artifact_store_failed"),
    )


def handle_sensorium_artifact_status(
    *,
    thread_id: str | None = None,
    action_id: str | None = None,
    kind: str | None = None,
    limit: int = 20,
    instance: str = "default",
    state_dir: str | None = None,
) -> str:
    store = SensoriumStore(instance=instance, state_dir=state_dir)
    items = list_artifacts(
        store, thread_id=thread_id, action_id=action_id, kind=kind, limit=limit,
    )
    return _ok(instance, items)


# --- Thread action tool handlers (Phase 9C) ---


def handle_sensorium_action_prepare(
    *,
    thread_id: str,
    intent: str,
    title: str,
    summary: str = "",
    why_now: str = "",
    refs: dict | None = None,
    resume_trigger: str = "",
    config: dict | None = None,
    instance: str = "default",
    state_dir: str | None = None,
) -> str:
    store = SensoriumStore(instance=instance, state_dir=state_dir)
    result = prepare_action(
        store,
        thread_id=thread_id,
        intent=intent,
        title=title,
        summary=summary,
        why_now=why_now,
        refs=refs,
        resume_trigger=resume_trigger,
        config=config,
    )
    if result.get("success"):
        return _ok(instance, result)
    return _err(
        instance,
        result.get("detail") or result.get("error", "action_prepare_failed"),
    )


def handle_sensorium_action_attach(
    *,
    action_id: str,
    kind: str,
    ref_id: str,
    metadata: dict | None = None,
    config: dict | None = None,
    instance: str = "default",
    state_dir: str | None = None,
) -> str:
    store = SensoriumStore(instance=instance, state_dir=state_dir)
    result = attach_action_ref(
        store,
        action_id=action_id,
        kind=kind,
        ref_id=ref_id,
        metadata=metadata,
        config=config,
    )
    if result.get("success"):
        return _ok(instance, result)
    return _err(
        instance,
        result.get("detail") or result.get("error", "action_attach_failed"),
    )


def handle_sensorium_action_result(
    *,
    action_id: str,
    outcome: str,
    result_summary: str = "",
    closed_reason: str = "",
    config: dict | None = None,
    instance: str = "default",
    state_dir: str | None = None,
) -> str:
    store = SensoriumStore(instance=instance, state_dir=state_dir)
    result = record_action_result(
        store,
        action_id=action_id,
        outcome=outcome,
        result_summary=result_summary,
        closed_reason=closed_reason,
        config=config,
    )
    if not result.get("success"):
        return _err(
            instance,
            result.get("detail") or result.get("error", "action_result_failed"),
        )

    feedback_signal = result.get("feedback_signal")
    feedback_signal_id = ""
    if feedback_signal:
        feedback_raw = handle_sensorium_ingest_signal(
            signal=feedback_signal,
            instance=instance,
            state_dir=state_dir,
        )
        feedback_result = json.loads(feedback_raw)
        if feedback_result.get("success"):
            feedback_signal_id = (
                (feedback_result.get("data") or {}).get("signal_id", "")
            )

        now = utc_now_iso()
        feedback_receipt = {
            "ts": now,
            "type": "action.feedback_emitted",
            "thread_id": result["data"].get("origin_thread_id"),
            "action_id": action_id,
            "feedback_signal_id": feedback_signal_id,
            "outcome": outcome,
            "feedback_scope": "system_action",
        }
        store.append_jsonl("decisions", feedback_receipt)

    result_data = {
        "data": result["data"],
        "receipt": result["receipt"],
        "feedback_signal_id": feedback_signal_id,
    }
    return _ok(instance, result_data)


def handle_sensorium_action_status(
    *,
    thread_id: str | None = None,
    status: str | None = None,
    limit: int = 20,
    instance: str = "default",
    state_dir: str | None = None,
) -> str:
    store = SensoriumStore(instance=instance, state_dir=state_dir)
    items = list_thread_actions(
        store, thread_id=thread_id, status=status, limit=limit,
    )
    return _ok(instance, {"thread_actions": items, "count": len(items)})


# --- Background conscious lease tool handlers ---


def handle_sensorium_conscious_claim(
    *,
    actor: str = "",
    mode: str = "",
    thread_id: str = "",
    config: dict | None = None,
    instance: str = "default",
    state_dir: str | None = None,
) -> str:
    store = SensoriumStore(instance=instance, state_dir=state_dir)
    result = claim_dormant_thread(
        store,
        actor=actor,
        mode=mode,
        thread_id=thread_id,
        config=config,
    )
    if result.get("success"):
        return _ok(instance, result)
    return _err(
        instance,
        result.get("detail") or result.get("error", "conscious_claim_failed"),
    )


def handle_sensorium_conscious_complete(
    *,
    thread_id: str,
    lease_id: str,
    outcome: str = "",
    notes: str = "",
    instance: str = "default",
    state_dir: str | None = None,
) -> str:
    store = SensoriumStore(instance=instance, state_dir=state_dir)
    result = complete_claim(
        store,
        thread_id=thread_id,
        lease_id=lease_id,
        outcome=outcome,
        notes=notes,
    )
    if result.get("success"):
        return _ok(instance, result)
    return _err(
        instance,
        result.get("detail") or result.get("error", "conscious_complete_failed"),
    )
