"""Policy-gated conscious reach-out decisions.

This is the safe door-handle layer: sensors and Subconscious may create
pressure, but only the Conscious tier can choose to prepare or deliver a
message. Receipts are compact and do not store the message body; any outbound
body belongs to the dispatch call / prepared outbox record, not the decision
receipt.
"""

from __future__ import annotations

import hashlib
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any

from .outbox import DiscordAdapter, DIRECT_DELIVERY_MODES, prepare_outbox_request
from .schemas import SENSITIVITY_RANK, truncate_text, utc_now_iso
from .store import SensoriumStore

CONSCIOUS_REACHOUT_RECEIPT_TYPE = "conscious_reachout.decision"
CONSCIOUS_REACHOUT_DENIED_TYPE = "conscious_reachout.denied"
CONSCIOUS_REACHOUT_DELIVERED_TYPE = "conscious_reachout.delivered"

REACHOUT_DECISIONS = frozenset({
    "no_action",
    "hold",
    "prepare_message",
    "prepare_artifact",
    "reach_out",
    "deliver_prepared",
})

CONSCIOUS_REACHOUT_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "allowed_surfaces": ["local"],
    "allowed_targets": [],
    "max_sensitivity": "private",
    "direct_delivery_enabled": False,
    "cooldown_minutes": 240,
    "max_message_chars": 1200,
    "default_delivery_mode": "context_pointer",
}

_DIRECT_DECISIONS = {"reach_out", "deliver_prepared"}
_CONTENT_DECISIONS = {"prepare_message", "reach_out", "deliver_prepared"}


def _merged_reachout_config(config: dict | None = None) -> dict[str, Any]:
    cfg = deepcopy(CONSCIOUS_REACHOUT_DEFAULTS)
    if not isinstance(config, dict):
        return cfg
    raw_obj = config.get("conscious_reachout") if isinstance(config.get("conscious_reachout"), dict) else config
    raw: dict[str, Any] = raw_obj if isinstance(raw_obj, dict) else {}
    for key in (
        "enabled",
        "allowed_surfaces",
        "allowed_targets",
        "max_sensitivity",
        "direct_delivery_enabled",
        "cooldown_minutes",
        "max_message_chars",
        "default_delivery_mode",
    ):
        if key in raw:
            cfg[key] = raw[key]
    cfg["allowed_surfaces"] = _string_list(cfg.get("allowed_surfaces")) or []
    cfg["allowed_targets"] = _string_list(cfg.get("allowed_targets")) or []
    try:
        cfg["cooldown_minutes"] = max(0, int(cfg.get("cooldown_minutes") or 0))
    except Exception:
        cfg["cooldown_minutes"] = CONSCIOUS_REACHOUT_DEFAULTS["cooldown_minutes"]
    try:
        cfg["max_message_chars"] = max(1, min(4000, int(cfg.get("max_message_chars") or 1200)))
    except Exception:
        cfg["max_message_chars"] = CONSCIOUS_REACHOUT_DEFAULTS["max_message_chars"]
    if cfg.get("max_sensitivity") not in SENSITIVITY_RANK:
        cfg["max_sensitivity"] = "private"
    if cfg.get("default_delivery_mode") not in {"context_pointer", "peripheral_reference", *DIRECT_DELIVERY_MODES}:
        cfg["default_delivery_mode"] = "context_pointer"
    cfg["enabled"] = bool(cfg.get("enabled"))
    cfg["direct_delivery_enabled"] = bool(cfg.get("direct_delivery_enabled"))
    return cfg


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str):
            text = item.strip()
            if text and text not in out:
                out.append(text)
    return out[:32]


def _parse_utc(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(ts).astimezone(timezone.utc)
    except Exception:
        return None


def _target_ref(surface: str, target_ref: str, target: dict | None) -> str:
    if target_ref:
        return target_ref.strip()
    data = target if isinstance(target, dict) else {}
    if surface == "discord":
        channel = data.get("channel_id") or data.get("dm_channel_id") or data.get("thread_id")
        if channel:
            return f"discord:{channel}"
    return surface or "local"


def _content_hash(message: str) -> str:
    return hashlib.sha256(message.encode()).hexdigest()[:16]


def _target_label(target_ref: Any) -> str:
    text = str(target_ref or "").strip()
    if not text:
        return ""
    surface = text.split(":", 1)[0].lower()
    if not surface.replace("_", "").replace("-", "").isalnum():
        surface = "target"
    return f"{surface}:{_content_hash(text)[:8]}"


def _receipt_base(*, now: str, decision: str, actor_tier: str, source: str, surface: str, target_ref: str, reason: str, sensitivity: str, message: str) -> dict[str, Any]:
    message_len = len(message or "")
    return {
        "ts": now,
        "type": CONSCIOUS_REACHOUT_RECEIPT_TYPE,
        "decision": decision,
        "actor_tier": actor_tier,
        "source": source or "manual_review",
        "surface": surface,
        "target_ref": target_ref,
        "reason": truncate_text(reason, 240) if reason else "",
        "sensitivity": sensitivity,
        "message_hash": _content_hash(message) if message else "",
        "message_chars": message_len,
        "delivery_authorized": False,
        "outbound_delivery": False,
        "background_action_allowed": False,
    }


def _append_denied(store: SensoriumStore, receipt: dict[str, Any], reason: str) -> dict[str, Any]:
    denied = {**receipt, "type": CONSCIOUS_REACHOUT_DENIED_TYPE, "blocked_reason": reason}
    store.append_jsonl("decisions", denied)
    return {"success": False, "error": reason, "receipt": denied}


def _cooldown_active(decisions: list[dict[str, Any]], *, now_dt: datetime, cooldown_minutes: int, target_ref: str) -> bool:
    if cooldown_minutes <= 0:
        return False
    threshold = now_dt - timedelta(minutes=cooldown_minutes)
    for row in reversed(decisions[-1000:]):
        if row.get("type") != CONSCIOUS_REACHOUT_DELIVERED_TYPE:
            continue
        if target_ref and row.get("target_ref") != target_ref:
            continue
        ts = _parse_utc(row.get("ts"))
        if ts and ts >= threshold:
            return True
    return False


def apply_conscious_reachout_decision(
    store: SensoriumStore,
    *,
    decision: str,
    actor_tier: str = "conscious",
    source: str = "manual_review",
    reason: str = "",
    message: str = "",
    surface: str = "local",
    target_ref: str = "",
    target: dict | None = None,
    sensitivity: str = "private",
    thread_id: str = "",
    config: dict | None = None,
    execute: bool = False,
    adapter: DiscordAdapter | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Choose/prepare/deliver a conscious reach-out with compact receipts.

    `execute=True` performs adapter-backed delivery only when direct delivery is
    enabled and a Discord adapter is supplied. Without execute it records a
    prepared/authorized decision only. Subconscious callers can only choose
    no_action/hold; they can never prepare or deliver.
    """
    cfg = _merged_reachout_config(config)
    store.ensure_dirs()
    now_iso = now or utc_now_iso()
    now_dt = _parse_utc(now_iso) or datetime.now(timezone.utc)
    decision = str(decision or "").strip()
    surface = str(surface or "local").strip()
    message = truncate_text(message.strip(), int(cfg["max_message_chars"])) if message else ""
    target = target if isinstance(target, dict) else {}
    target_ref = _target_ref(surface, target_ref, target)
    sensitivity = sensitivity if sensitivity in SENSITIVITY_RANK else "private"
    receipt = _receipt_base(
        now=now_iso,
        decision=decision,
        actor_tier=actor_tier,
        source=source,
        surface=surface,
        target_ref=target_ref,
        reason=reason,
        sensitivity=sensitivity,
        message=message,
    )
    if thread_id:
        receipt["thread_id"] = thread_id

    if decision not in REACHOUT_DECISIONS:
        return _append_denied(store, receipt, "invalid_decision")
    if not cfg.get("enabled"):
        return _append_denied(store, receipt, "reachout_disabled")
    if actor_tier != "conscious" and decision not in {"no_action", "hold"}:
        return _append_denied(store, receipt, "subconscious_may_not_reach_out")
    if surface not in cfg["allowed_surfaces"]:
        return _append_denied(store, receipt, "surface_not_allowed")
    if cfg["allowed_targets"] and target_ref not in cfg["allowed_targets"]:
        return _append_denied(store, receipt, "target_not_allowed")
    if SENSITIVITY_RANK[sensitivity] > SENSITIVITY_RANK[cfg["max_sensitivity"]]:
        return _append_denied(store, receipt, "sensitivity_exceeds_policy")
    if decision in _CONTENT_DECISIONS and not message:
        return _append_denied(store, receipt, "missing_message")
    if decision in _DIRECT_DECISIONS and _cooldown_active(
        store.read_jsonl("decisions"),
        now_dt=now_dt,
        cooldown_minutes=int(cfg["cooldown_minutes"]),
        target_ref=target_ref,
    ):
        return _append_denied(store, receipt, "cooldown_active")

    if decision in {"no_action", "hold", "prepare_artifact"}:
        store.append_jsonl("decisions", receipt)
        return {"success": True, "receipt": receipt}

    prepared_outbox: dict[str, Any] | None = None
    delivery_mode = cfg["default_delivery_mode"]
    if decision in _DIRECT_DECISIONS and cfg.get("direct_delivery_enabled") and surface == "discord":
        # Choose a direct mode only at the conscious actuator boundary.
        delivery_mode = "discord_dm_bound_session" if target.get("dm_channel_id") else "discord_channel_thread"

    if thread_id:
        outbox_cfg = {
            "enabled": True,
            "direct_modes_enabled": bool(cfg.get("direct_delivery_enabled")),
            "allowed_delivery_modes": ["context_pointer", "peripheral_reference", delivery_mode],
            "discord": {"enabled": bool(cfg.get("direct_delivery_enabled"))},
        }
        prepared = prepare_outbox_request(
            store,
            thread_id=thread_id,
            request_type="REACH_OUT",
            surface=surface,
            delivery_mode=delivery_mode,
            target=target,
            title="Conscious reach-out",
            message_preview=message,
            content_hash=_content_hash(message),
            config=outbox_cfg,
            dry_run=False,
        )
        if not prepared.get("success"):
            return _append_denied(store, receipt, str(prepared.get("error") or "outbox_prepare_failed"))
        prepared_outbox = prepared.get("data") if isinstance(prepared.get("data"), dict) else None
        receipt["outbox_id"] = prepared_outbox.get("id") if prepared_outbox else ""
        receipt["delivery_mode"] = delivery_mode

    if decision == "prepare_message" or not execute:
        receipt["delivery_authorized"] = decision in _DIRECT_DECISIONS
        receipt["requires_separate_dispatch"] = decision in _DIRECT_DECISIONS
        store.append_jsonl("decisions", receipt)
        return {"success": True, "receipt": receipt, "outbox": prepared_outbox}

    if not cfg.get("direct_delivery_enabled"):
        return _append_denied(store, receipt, "direct_delivery_disabled")
    if surface != "discord":
        return _append_denied(store, receipt, "direct_delivery_surface_unsupported")
    if adapter is None:
        return _append_denied(store, receipt, "missing_delivery_adapter")

    try:
        if target.get("dm_channel_id"):
            platform_refs = adapter.send_message(channel_id=str(target.get("dm_channel_id")), content=message)
        else:
            platform_refs = adapter.create_thread(
                channel_id=str(target.get("channel_id") or ""),
                name="Conscious reach-out",
                message_content=message,
            )
    except Exception:
        return _append_denied(store, receipt, "delivery_failed")

    delivered = {
        **receipt,
        "type": CONSCIOUS_REACHOUT_DELIVERED_TYPE,
        "delivery_authorized": True,
        "outbound_delivery": True,
        "delivery_mode": delivery_mode,
        "platform_refs": {
            key: str(value)[:120]
            for key, value in (platform_refs or {}).items()
            if key in {"message_id", "channel_id", "thread_id"}
        },
    }
    store.append_jsonl("decisions", delivered)
    return {"success": True, "receipt": delivered, "outbox": prepared_outbox, "platform_refs": platform_refs}


def conscious_reachout_metrics(decisions: list[dict[str, Any]], *, limit: int = 500) -> dict[str, Any]:
    rows = [r for r in decisions[-limit:] if isinstance(r, dict) and str(r.get("type") or "").startswith("conscious_reachout.")]
    decision_breakdown: dict[str, int] = {}
    blocked_breakdown: dict[str, int] = {}
    recent: list[dict[str, Any]] = []
    for row in rows:
        decision = str(row.get("decision") or "unknown")
        decision_breakdown[decision] = decision_breakdown.get(decision, 0) + 1
        if row.get("type") == CONSCIOUS_REACHOUT_DENIED_TYPE:
            reason = str(row.get("blocked_reason") or "unknown")
            blocked_breakdown[reason] = blocked_breakdown.get(reason, 0) + 1
        recent.append({
            "ts": row.get("ts") if isinstance(row.get("ts"), str) and len(row.get("ts")) <= 40 else "",
            "type": row.get("type"),
            "decision": decision,
            "surface": row.get("surface"),
            "target_ref_label": _target_label(row.get("target_ref")),
            "delivered": row.get("type") == CONSCIOUS_REACHOUT_DELIVERED_TYPE,
            "blocked_reason": row.get("blocked_reason", ""),
        })
    return {
        "receipt_type": CONSCIOUS_REACHOUT_RECEIPT_TYPE,
        "receipt_count": len(rows),
        "prepared_count": sum(1 for r in rows if r.get("decision") == "prepare_message" and r.get("type") == CONSCIOUS_REACHOUT_RECEIPT_TYPE),
        "authorized_count": sum(1 for r in rows if bool(r.get("delivery_authorized"))),
        "delivered_count": sum(1 for r in rows if r.get("type") == CONSCIOUS_REACHOUT_DELIVERED_TYPE),
        "blocked_count": sum(1 for r in rows if r.get("type") == CONSCIOUS_REACHOUT_DENIED_TYPE),
        "decision_breakdown": decision_breakdown,
        "blocked_breakdown": blocked_breakdown,
        "recent": recent[-8:],
        "window_limit": limit,
    }
