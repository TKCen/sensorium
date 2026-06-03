"""Conscious-choice policy gate for mediated-presence gifts.

This module is intentionally a policy/receipt layer, not a delivery worker.
It lets Subconscious propose, lets Conscious choose preparation/silence/decline,
and gates any later delivery authorization behind explicit conscious receipt,
configured surfaces, cooldowns, and artifact-first continuity.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone, timedelta

from .config import sanitize_media_gift_policy, visible_on_surface
from .schemas import truncate_text, utc_now_iso
from .store import SensoriumStore

VALID_GIFT_DECISIONS = {
    "propose",
    "prepare_thread_artifact",
    "offer_choice",
    "choose_silence",
    "decline",
    "approve_delivery",
    "block_delivery",
}
VALID_ACTOR_TIERS = {"subconscious", "conscious", "operator", "test"}
VALID_SOURCES = {
    "inner_salience",
    "operator_prompt",
    "manual_review",
    "worker_result",
    "scheduler",
    "test",
}
CONSCIOUS_ONLY_DECISIONS = {
    "prepare_thread_artifact",
    "offer_choice",
    "choose_silence",
    "decline",
    "approve_delivery",
    "block_delivery",
}


def _parse_ts(ts: str) -> datetime | None:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _find_by_id(rows: list[dict], item_id: str) -> dict | None:
    for row in rows:
        if row.get("id") == item_id:
            return row
    return None


def _load_context(store: SensoriumStore, *, thread_id: str, artifact_id: str) -> tuple[dict | None, dict | None]:
    thread = None
    artifact = None
    if artifact_id:
        artifact = _find_by_id(store.read_jsonl("artifacts"), artifact_id)
        if artifact is not None and not thread_id:
            thread_id = artifact.get("source_refs", {}).get("thread_id", "")
    if thread_id:
        thread = _find_by_id(store.read_jsonl("threads"), thread_id)
    return thread, artifact


def _receipt_base(
    *,
    now: str,
    decision: str,
    actor_tier: str,
    source: str,
    thread_id: str,
    artifact_id: str,
    surface: str,
    target_ref: str,
    why_now: str,
    reason: str,
) -> dict:
    return {
        "ts": now,
        "type": "media_gift.choice",
        "decision": decision,
        "actor_tier": actor_tier,
        "source": source,
        "thread_id": thread_id,
        "artifact_id": artifact_id,
        "surface": surface,
        "target_ref": truncate_text(target_ref or "", 160),
        "why_now": truncate_text(why_now or "", 500),
        "reason": truncate_text(reason or "", 500),
        "outbound_delivery": False,
        "scheduler_spawned": False,
    }


def _write_denial(store: SensoriumStore, receipt: dict, reason: str, detail: str) -> dict:
    denied = {
        **receipt,
        "type": "media_gift.denied",
        "denied_reason": reason,
        "detail": truncate_text(detail, 500),
        "delivery_authorized": False,
    }
    store.append_jsonl("decisions", denied)
    return {"success": False, "error": reason, "detail": detail, "receipt": denied}


def _latest_delivery_receipt(
    store: SensoriumStore,
    *,
    surface: str,
    target_ref: str,
) -> dict | None:
    for receipt in reversed(store.read_jsonl("decisions")):
        if receipt.get("type") != "media_gift.delivery_approved":
            continue
        if receipt.get("surface") != surface:
            continue
        if (receipt.get("target_ref") or "") != (target_ref or ""):
            continue
        return receipt
    return None


def _cooldown_active(
    store: SensoriumStore,
    *,
    surface: str,
    target_ref: str,
    cooldown_hours: float,
    now_dt: datetime,
) -> tuple[bool, str]:
    if cooldown_hours <= 0:
        return False, ""
    previous = _latest_delivery_receipt(store, surface=surface, target_ref=target_ref)
    if not previous:
        return False, ""
    previous_dt = _parse_ts(previous.get("ts", ""))
    if previous_dt is None:
        return False, ""
    next_allowed = previous_dt + timedelta(hours=cooldown_hours)
    if now_dt < next_allowed:
        return True, next_allowed.strftime("%Y-%m-%dT%H:%M:%SZ")
    return False, ""


def _update_artifact_delivery_state(store: SensoriumStore, artifact_id: str, state: str, now: str) -> None:
    artifacts = store.read_jsonl("artifacts")
    changed = False
    for artifact in artifacts:
        if artifact.get("id") != artifact_id:
            continue
        artifact["delivery_state"] = state
        artifact["updated_at"] = now
        changed = True
        break
    if changed:
        store.rewrite_jsonl("artifacts", artifacts)


def apply_media_gift_choice(
    store: SensoriumStore,
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
    now: str | None = None,
) -> dict:
    """Apply the mediated-gift conscious-choice policy and write a receipt.

    This function never dispatches outbound messages, creates cron jobs, or
    broadens public surfaces. `approve_delivery` only records an authorization
    receipt and marks an artifact as prepared for a separate explicitly gated
    delivery path.
    """
    store.ensure_dirs()
    instance_config = deepcopy(config or {})
    policy = sanitize_media_gift_policy(instance_config.get("media_gift_policy") or {})
    now_ts = now or utc_now_iso()
    now_dt = _parse_ts(now_ts) or datetime.now(timezone.utc)

    decision = (decision or "").strip()
    actor_tier = (actor_tier or "conscious").strip()
    source = (source or "inner_salience").strip()
    thread_id = (thread_id or "").strip()
    artifact_id = (artifact_id or "").strip()
    surface = (surface or "").strip()
    target_ref = (target_ref or "").strip()
    why_now = (why_now or "").strip()
    reason = (reason or "").strip()

    receipt = _receipt_base(
        now=now_ts,
        decision=decision,
        actor_tier=actor_tier,
        source=source,
        thread_id=thread_id,
        artifact_id=artifact_id,
        surface=surface,
        target_ref=target_ref,
        why_now=why_now,
        reason=reason,
    )

    if not policy.get("enabled", True):
        return _write_denial(store, receipt, "media_gift_policy_disabled", "Media gift policy is disabled.")
    if decision not in VALID_GIFT_DECISIONS:
        return _write_denial(store, receipt, "invalid_decision", f"decision must be one of {sorted(VALID_GIFT_DECISIONS)}")
    if actor_tier not in VALID_ACTOR_TIERS:
        return _write_denial(store, receipt, "invalid_actor_tier", f"actor_tier must be one of {sorted(VALID_ACTOR_TIERS)}")
    if source not in VALID_SOURCES:
        return _write_denial(store, receipt, "invalid_source", f"source must be one of {sorted(VALID_SOURCES)}")

    if actor_tier == "subconscious" and decision not in set(policy.get("subconscious_may") or []):
        return _write_denial(
            store,
            receipt,
            "subconscious_may_only_propose",
            "Subconscious may propose a media gift but may not prepare, decline, silence, approve, or deliver it.",
        )
    if actor_tier in {"conscious", "test"} and decision not in set(policy.get("conscious_may") or []):
        return _write_denial(
            store,
            receipt,
            "decision_not_allowed_for_conscious",
            f"{decision} is not allowed by media_gift_policy.conscious_may.",
        )
    if decision in CONSCIOUS_ONLY_DECISIONS and actor_tier not in {"conscious", "test"}:
        return _write_denial(
            store,
            receipt,
            "conscious_choice_required",
            f"{decision} requires a conscious choice receipt.",
        )

    why_required = set(policy.get("require_why_now_for") or [])
    if decision in why_required and not why_now:
        return _write_denial(store, receipt, "why_now_required", f"{decision} requires a bounded why_now.")

    if source == "scheduler" and decision == "approve_delivery" and not policy.get("scheduler_delivery_enabled"):
        return _write_denial(
            store,
            receipt,
            "scheduler_delivery_disabled",
            "Scheduler/no-agent ticks may not approve mediated gift delivery.",
        )

    if source == "operator_prompt":
        receipt["direct_prompt_mode"] = policy.get("direct_prompt_mode", "choice_required")
        receipt["operator_prompt_not_binding"] = True

    if decision == "choose_silence" and not policy.get("silence_allowed", True):
        return _write_denial(store, receipt, "silence_disabled", "Policy does not allow silence as a valid mediated-gift outcome.")

    thread, artifact = _load_context(store, thread_id=thread_id, artifact_id=artifact_id)
    if artifact is not None and not receipt["thread_id"]:
        receipt["thread_id"] = artifact.get("source_refs", {}).get("thread_id", "")
    if thread is not None and not receipt["thread_id"]:
        receipt["thread_id"] = thread.get("id", "")

    if decision in {"prepare_thread_artifact", "approve_delivery"}:
        if thread_id and thread is None:
            return _write_denial(store, receipt, "thread_not_found", f"Thread '{thread_id}' not found.")

    if decision == "approve_delivery":
        delivery_cfg = policy.get("delivery") or {}
        if not delivery_cfg.get("enabled", False):
            return _write_denial(store, receipt, "delivery_policy_disabled", "Gift delivery authorization is disabled until explicitly configured.")
        if policy.get("artifact_first_required", True):
            if not artifact_id:
                return _write_denial(store, receipt, "artifact_first_required", "Delivery approval requires an existing thread artifact id.")
            if artifact is None:
                return _write_denial(store, receipt, "artifact_not_found", f"Artifact '{artifact_id}' not found.")
            if not artifact.get("source_refs", {}).get("thread_id"):
                return _write_denial(store, receipt, "artifact_thread_ref_required", "Gift artifact must be attached to a Sensorium thread before delivery approval.")
        if not surface:
            return _write_denial(store, receipt, "surface_required", "Delivery approval requires an explicit surface.")
        configured_surfaces = set(delivery_cfg.get("allowed_surfaces") or [])
        instance_surfaces = set(instance_config.get("allowed_surfaces") or ["local"])
        if surface not in configured_surfaces or surface not in instance_surfaces:
            return _write_denial(
                store,
                receipt,
                "surface_not_configured",
                f"Surface '{surface}' must be present in both media_gift_policy.delivery.allowed_surfaces and instance allowed_surfaces.",
            )
        if artifact is not None and not visible_on_surface(artifact, surface, instance_config):
            return _write_denial(
                store,
                receipt,
                "artifact_surface_not_allowed",
                "Artifact policy does not allow this surface; delivery approval would broaden visibility.",
            )
        if surface == "public":
            return _write_denial(store, receipt, "public_surface_forbidden", "Mediated gifts cannot be approved for public surfaces.")
        allowed_targets = set(delivery_cfg.get("allowed_targets") or [])
        if allowed_targets and target_ref not in allowed_targets:
            return _write_denial(
                store,
                receipt,
                "target_not_configured",
                "Target must be explicitly listed in media_gift_policy.delivery.allowed_targets.",
            )
        active, next_allowed = _cooldown_active(
            store,
            surface=surface,
            target_ref=target_ref,
            cooldown_hours=float(delivery_cfg.get("cooldown_hours", 24)),
            now_dt=now_dt,
        )
        if active:
            return _write_denial(
                store,
                receipt,
                "cooldown_active",
                f"Gift delivery approval is cooling down for this surface/target until {next_allowed}.",
            )
        receipt = {
            **receipt,
            "type": "media_gift.delivery_approved",
            "delivery_authorized": True,
            "requires_separate_dispatch": True,
            "artifact_first_required": bool(policy.get("artifact_first_required", True)),
            "cooldown_hours": float(delivery_cfg.get("cooldown_hours", 24)),
        }
        store.append_jsonl("decisions", receipt)
        if artifact_id:
            _update_artifact_delivery_state(store, artifact_id, "prepared", now_ts)
        return {"success": True, "data": receipt, "receipt": receipt}

    type_by_decision = {
        "propose": "media_gift.proposed",
        "prepare_thread_artifact": "media_gift.prepare_authorized",
        "offer_choice": "media_gift.choice_offered",
        "choose_silence": "media_gift.silence_chosen",
        "decline": "media_gift.declined",
        "block_delivery": "media_gift.delivery_blocked",
    }
    receipt = {
        **receipt,
        "type": type_by_decision[decision],
        "delivery_authorized": False,
        "silence_valid": decision == "choose_silence",
    }
    store.append_jsonl("decisions", receipt)
    return {"success": True, "data": receipt, "receipt": receipt}
