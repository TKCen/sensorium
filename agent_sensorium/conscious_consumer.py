"""One bounded local-first Conscious consumer for subconscious advisories.

This module is deliberately not a model runner or dispatcher. A caller supplies
one strict structured choice; this module owns one aperture item through the
canonical aperture owner, applies the existing reach-out policy when needed,
and settles the aperture explicitly. Local preparation stores authored content
in the existing outbox without creating a thread or worker request.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from .conscious_aperture import (
    open_conscious_aperture,
    resolve_conscious_aperture,
    settle_conscious_aperture_item,
    valid_authority_token,
)
from .conscious_reachout import (
    apply_conscious_reachout_decision,
    evaluate_conscious_reachout_policy,
)
from .config import load_instance_config
from .outbox import source_revision_key
from .schemas import parse_utc_z_checkpoint, truncate_text, utc_now_iso
from .store import SensoriumStore

VALID_CONSCIOUS_DECISIONS = frozenset({"SILENCE", "HOLD", "REACH_OUT"})
CONSCIOUS_ADVISORY_KIND = "subconscious_advisory"
MAX_REACH_OUT_CHARS = 500
_DECISION_FIELDS = {
    "SILENCE": frozenset({"decision", "reason"}),
    "HOLD": frozenset({"decision", "reason", "return_at"}),
    "REACH_OUT": frozenset({"decision", "reason", "message"}),
}
_GENERIC_MESSAGE_TERMS = frozenset({
    "alert",
    "candidate",
    "detected",
    "notification",
    "pressure",
    "queue",
    "salience",
    "task",
    "reminder",
})
_GENERIC_MESSAGE_RE = re.compile(r"\b(?:" + "|".join(_GENERIC_MESSAGE_TERMS) + r")\b", re.IGNORECASE)


def _message_hash(message: str) -> str:
    return hashlib.sha256(message.encode("utf-8")).hexdigest()[:16]


def parse_conscious_decision(raw: object) -> dict[str, str]:
    """Parse exactly one SILENCE, HOLD, or REACH_OUT decision.

    The strict boundary is intentional: model-shaped or caller-shaped payloads
    must not smuggle extra lifecycle fields into this local consumer.
    """
    if isinstance(raw, str):
        import json

        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("Conscious decision must be a JSON object") from exc
    if not isinstance(raw, dict):
        raise ValueError("Conscious decision must be an object")

    decision = raw.get("decision")
    if not isinstance(decision, str):
        raise ValueError("Conscious decision must include decision")
    decision = decision.strip().upper()
    if decision not in VALID_CONSCIOUS_DECISIONS:
        raise ValueError(f"invalid Conscious decision: {decision}")
    expected = _DECISION_FIELDS[decision]
    actual = frozenset(raw)
    extra = sorted(actual - expected)
    missing = sorted(expected - actual)
    if extra:
        raise ValueError(f"unexpected fields: {extra}")
    if missing:
        raise ValueError(f"missing fields: {missing}")

    reason = raw.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("reason must be a non-empty string")
    normalized = {"decision": decision, "reason": reason.strip()}

    if decision == "HOLD":
        return_at = raw.get("return_at")
        checkpoint = parse_utc_z_checkpoint(return_at)
        if checkpoint is None:
            raise ValueError("return_at must be an exact UTC-Z checkpoint")
        normalized["return_at"] = checkpoint[0]
    elif decision == "REACH_OUT":
        message = raw.get("message")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message must be a non-empty string")
        message = " ".join(message.split())
        if len(message) < 8 or _GENERIC_MESSAGE_RE.search(message):
            raise ValueError("REACH_OUT requires a specific authored message")
        if len(message) > MAX_REACH_OUT_CHARS:
            raise ValueError(f"REACH_OUT message must be at most {MAX_REACH_OUT_CHARS} characters")
        if "i have something for you" in message.casefold():
            raise ValueError("REACH_OUT cannot claim an unavailable openable artifact")
        normalized["message"] = message
    return normalized


def build_conscious_source_packet(aperture_result: dict[str, Any]) -> dict[str, Any]:
    """Build the compact source packet passed to a Conscious chooser."""
    aperture = aperture_result.get("aperture")
    if not isinstance(aperture, list) or len(aperture) != 1:
        raise ValueError("a bounded Conscious source packet requires exactly one aperture item")
    item = aperture[0]
    if not isinstance(item, dict) or not item.get("candidate_id"):
        raise ValueError("aperture item is missing candidate identity")
    source_binding = item.get("source_binding") or {}
    task: dict[str, Any] = {}
    task_value = item.get("conscious_task")
    if isinstance(task_value, dict):
        task = task_value
    return {
        "candidate_id": item["candidate_id"],
        "kind": item.get("kind", ""),
        "aperture_id": aperture_result.get("aperture_id") or item.get("aperture_id", ""),
        "consumer_id": item.get("consumer_id", ""),
        "summary": truncate_text(item.get("summary", ""), 220),
        "pressure": item.get("pressure"),
        "event_ids": list(source_binding.get("event_ids") or item.get("event_ids") or []),
        "source_candidate_ids": list(
            source_binding.get("source_candidate_ids") or item.get("source_candidate_ids") or []
        ),
        "source_candidate_fingerprint": (
            source_binding.get("source_fingerprint")
            or item.get("source_candidate_fingerprint", "")
        ),
        "sensitivity": item.get("sensitivity", "private"),
        "allowed_surfaces": list(item.get("allowed_surfaces") or ["local"]),
        "conscious_task": {
            "id": task.get("id", ""),
            "request_type": task.get("request_type", ""),
            "title": truncate_text(task.get("title", ""), 120),
            "why": truncate_text(task.get("why", ""), 240),
            "expected_decision": truncate_text(task.get("expected_decision", ""), 240),
        },
    }


def _parse_now(now: str | None) -> tuple[str, datetime]:
    value = now or utc_now_iso()
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return value, parsed.astimezone(timezone.utc)


def _retry_checkpoint(now: str | None) -> str:
    _, parsed = _parse_now(now)
    return (parsed + timedelta(hours=1)).isoformat(timespec="seconds").replace("+00:00", "Z")


def _no_item_result(aperture_result: dict[str, Any]) -> dict[str, Any]:
    candidate_ids = list(aperture_result.get("candidate_ids") or [])
    action = aperture_result.get("action")
    if action in {"opened_aperture", "active_aperture_exists"} and not candidate_ids:
        action = "no_eligible_advisory"
    return {
        "success": bool(aperture_result.get("success")),
        "action": action or "no_eligible_advisory",
        "dry_run": bool(aperture_result.get("dry_run")),
        "aperture_action": aperture_result.get("action"),
        "candidate_ids": candidate_ids,
    }


def _settle(
    store: SensoriumStore,
    *,
    packet: dict[str, Any],
    decision: str,
    reason: str,
    return_at: str | None,
    dry_run: bool,
    now: str | None,
) -> dict[str, Any]:
    result = settle_conscious_aperture_item(
        store,
        candidate_id=packet["candidate_id"],
        aperture_id=packet.get("aperture_id"),
        consumer_id=packet.get("consumer_id"),
        decision=decision,
        reason=truncate_text(reason, 240),
        return_at=return_at,
        dry_run=dry_run,
        now=now,
    )
    return result


def consume_conscious_advisory(
    store: SensoriumStore,
    *,
    decision: object,
    config: dict | None = None,
    dry_run: bool = True,
    now: str | None = None,
    stale_after_minutes: int = 180,
    expected_candidate_id: str | None = None,
    expected_aperture_id: str | None = None,
    expected_source_candidate_ids: list[str] | None = None,
    expected_source_candidate_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Apply one bounded caller-supplied choice to one advisory candidate."""
    parsed = parse_conscious_decision(decision)
    if config is None:
        config, _ = load_instance_config(state_dir=str(store.root))

    expected_values = (
        expected_candidate_id,
        expected_aperture_id,
        expected_source_candidate_ids,
        expected_source_candidate_fingerprint,
    )
    has_expected_authority = any(value is not None for value in expected_values)
    complete_expected_authority = (
        valid_authority_token(expected_candidate_id)
        and valid_authority_token(expected_aperture_id)
        and isinstance(expected_source_candidate_ids, list)
        and all(isinstance(value, str) for value in expected_source_candidate_ids)
        and isinstance(expected_source_candidate_fingerprint, str)
        and bool(expected_source_candidate_fingerprint)
    )
    if has_expected_authority and not complete_expected_authority:
        return {
            "success": False,
            "action": "source_revision_mismatch",
            "error": "source_revision_mismatch",
            "candidate_id": str(expected_candidate_id or ""),
        }

    if has_expected_authority:
        aperture_result = resolve_conscious_aperture(
            store,
            candidate_id=str(expected_candidate_id),
            aperture_id=str(expected_aperture_id),
            consumer_id="conscious-session",
            now=now,
        )
        if not aperture_result.get("success"):
            return {
                "success": False,
                "action": "source_revision_mismatch",
                "error": "source_revision_mismatch",
                "candidate_id": str(expected_candidate_id),
            }
        if (
            aperture_result.get("_candidate_source_fingerprint")
            != expected_source_candidate_fingerprint
        ):
            return {
                "success": False,
                "action": "source_revision_mismatch",
                "error": "source_revision_mismatch",
                "candidate_id": str(expected_candidate_id),
            }
    else:
        # Intentional legacy choice cycle: callers without expected identity
        # still ask the aperture owner to select one current advisory.
        aperture_result = open_conscious_aperture(
            store,
            aperture_size=1,
            max_active_sessions=1,
            stale_after_minutes=stale_after_minutes,
            dry_run=dry_run,
            now=now,
            candidate_kind=CONSCIOUS_ADVISORY_KIND,
            consumer_id="conscious-session",
        )
    aperture = aperture_result.get("aperture")
    if not aperture_result.get("success") or not isinstance(aperture, list) or len(aperture) != 1:
        return _no_item_result(aperture_result)
    packet = build_conscious_source_packet(aperture_result)
    if (
        expected_candidate_id is not None
        and (
            packet.get("candidate_id") != expected_candidate_id
            or (expected_aperture_id and packet.get("aperture_id") != expected_aperture_id)
            or (
                expected_source_candidate_fingerprint is not None
                and packet.get("source_candidate_fingerprint") != expected_source_candidate_fingerprint
            )
            or (
                expected_source_candidate_ids is not None
                and packet.get("source_candidate_ids") != list(expected_source_candidate_ids)
            )
        )
    ):
        return {
            "success": False,
            "action": "source_revision_mismatch",
            "error": "source_revision_mismatch",
            "candidate_id": expected_candidate_id,
        }
    base = {
        "candidate_id": packet["candidate_id"],
        "aperture_id": packet.get("aperture_id", ""),
        "source_candidate_ids": packet["source_candidate_ids"],
        "source_candidate_fingerprint": packet["source_candidate_fingerprint"],
        "decision": parsed["decision"],
        "dry_run": dry_run,
    }

    if dry_run:
        if parsed["decision"] == "REACH_OUT":
            policy = evaluate_conscious_reachout_policy(
                store,
                decision="reach_out",
                actor_tier="conscious",
                reason=parsed["reason"],
                message=parsed["message"],
                surface="local",
                target_ref="local",
                target={},
                sensitivity=packet.get("sensitivity", "private"),
                config=config,
                now=now,
            )
            if not policy.get("success"):
                return {**base, "success": False, "action": "reach_out_denied", "error": policy["error"]}
        result = {**base, "success": True, "action": f"would_apply_{parsed['decision'].lower()}"}
        if parsed["decision"] == "REACH_OUT":
            result.update({
                "message_hash": _message_hash(parsed["message"]),
                "message_chars": len(parsed["message"]),
                "prepared": {"status": "prepared", "openable": True},
            })
        elif parsed["decision"] == "HOLD":
            result["return_at"] = parsed["return_at"]
        return result

    if parsed["decision"] == "SILENCE":
        settled = _settle(
            store,
            packet=packet,
            decision="REVIEWED",
            reason=f"Conscious silence: {parsed['reason']}",
            return_at=None,
            dry_run=False,
            now=now,
        )
        return {
            **base,
            "success": settled.get("success", False),
            "action": "settled_silence",
            "settlement": settled,
        }

    if parsed["decision"] == "HOLD":
        settled = _settle(
            store,
            packet=packet,
            decision="HELD",
            reason=f"Conscious hold: {parsed['reason']}",
            return_at=parsed["return_at"],
            dry_run=False,
            now=now,
        )
        return {
            **base,
            "success": settled.get("success", False),
            "action": "settled_hold",
            "return_at": parsed["return_at"],
            "settlement": settled,
        }

    try:
        prepared = apply_conscious_reachout_decision(
            store,
            decision="reach_out",
            actor_tier="conscious",
            source="bounded_conscious_consumer",
            reason=parsed["reason"],
            message=parsed["message"],
            surface="local",
            target_ref="local",
            target={},
            sensitivity=packet.get("sensitivity", "private"),
            origin_candidate_id=packet["candidate_id"],
            source_candidate_ids=packet.get("source_candidate_ids"),
            source_candidate_fingerprint=packet.get("source_candidate_fingerprint", ""),
            config=config,
            execute=False,
            now=now,
        )
    except Exception:
        prepared = {"success": False, "error": "outbox_prepare_failed"}

    if not prepared.get("success"):
        error = str(prepared.get("error") or "outbox_prepare_failed")
        held = _settle(
            store,
            packet=packet,
            decision="HELD",
            reason=f"Reach-out preparation denied ({error}); retained for retry.",
            return_at=_retry_checkpoint(now),
            dry_run=False,
            now=now,
        )
        return {
            **base,
            "success": False,
            "action": "reach_out_denied_held",
            "error": error,
            "settlement": held,
        }

    receipt: dict[str, Any] = {}
    receipt_value = prepared.get("receipt")
    if isinstance(receipt_value, dict):
        receipt = receipt_value
    outbox_id = str(receipt.get("outbox_id") or "")
    prepared_data = prepared.get("outbox") if isinstance(prepared.get("outbox"), dict) else None
    if not outbox_id and prepared_data:
        outbox_id = str(prepared_data.get("id") or "")
    if not outbox_id:
        held = _settle(
            store,
            packet=packet,
            decision="HELD",
            reason="Reach-out preparation returned no openable outbox id; retained for retry.",
            return_at=_retry_checkpoint(now),
            dry_run=False,
            now=now,
        )
        return {
            **base,
            "success": False,
            "action": "reach_out_denied_held",
            "error": "outbox_id_missing",
            "settlement": held,
        }

    message_hash = _message_hash(parsed["message"])
    outbox_row = next(
        (row for row in store.read_jsonl("outbox") if row.get("id") == outbox_id),
        None,
    )
    prepared_source_fingerprint = str((outbox_row or {}).get("source_candidate_fingerprint") or "")
    expected_source_ids = list(packet.get("source_candidate_ids") or [])
    expected_source_revision = source_revision_key(
        candidate_id=packet["candidate_id"],
        source_candidate_ids=expected_source_ids,
        source_candidate_fingerprint=packet.get("source_candidate_fingerprint", ""),
    )
    if (
        not isinstance(outbox_row, dict)
        or outbox_row.get("status") != "prepared"
        or outbox_row.get("origin_thread_id") != ""
        or outbox_row.get("origin_candidate_id") != packet["candidate_id"]
        or outbox_row.get("surface") != "local"
        or outbox_row.get("delivery_mode") != "context_pointer"
        or outbox_row.get("target") != {}
        or outbox_row.get("allowed_surfaces") != ["local"]
        or outbox_row.get("source_candidate_ids") != expected_source_ids
        or prepared_source_fingerprint != packet.get("source_candidate_fingerprint", "")
        or outbox_row.get("source_revision_key") != expected_source_revision
        or outbox_row.get("message_preview") != parsed["message"]
        or str(outbox_row.get("content_hash") or "").lower() != message_hash
        or outbox_row.get("content_length") != len(parsed["message"])
    ):
        held = _settle(
            store,
            packet=packet,
            decision="HELD",
            reason="Prepared outbox could not be verified as openable; retained for retry.",
            return_at=_retry_checkpoint(now),
            dry_run=False,
            now=now,
        )
        return {
            **base,
            "success": False,
            "action": "reach_out_denied_held",
            "error": "outbox_not_openable",
            "settlement": held,
        }
    settled = _settle(
        store,
        packet=packet,
        decision="SETTLED",
        reason=f"Prepared outbox {outbox_id} ({message_hash}); aperture settled.",
        return_at=None,
        dry_run=False,
        now=now,
    )
    if not settled.get("success"):
        return {
            **base,
            "success": False,
            "action": "prepared_reach_out_unsettled",
            "outbox_id": outbox_id,
            "message_hash": message_hash,
            "error": "aperture_settlement_failed",
        }
    return {
        **base,
        "success": True,
        "action": "prepared_reach_out",
        "outbox_id": outbox_id,
        "message_hash": str(outbox_row.get("content_hash") or message_hash),
        "message_chars": len(str(outbox_row.get("message_preview") or parsed["message"])),
        "prepared": {"id": outbox_id, "status": "prepared", "openable": True},
        "settlement": {
            "action": settled.get("action"),
            "new_status": settled.get("new_status"),
        },
    }