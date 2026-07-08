"""Mediated-presence artifact records for Sensorium conscious threads.

This module stores references to artifacts created by a conscious choice or a
prepared action. It deliberately stores refs/metadata only: raw private prompts,
scripts, transcripts, and message bodies belong in files referenced by path or
hash, not inline in Sensorium state that may later be summarized on a surface.
"""

from __future__ import annotations

import json
from copy import deepcopy

from .actions import attach_action_ref
from .config import visible_on_surface
from .schemas import merge_sensitivity, new_id, truncate_text, utc_now_iso
from .store import SensoriumStore

VALID_ARTIFACT_KINDS = {"text", "audio", "image", "video"}
VALID_HANDOFF_MODES = {"pillow_dm", "present_thread", "both_later"}
# V0 is a record/attachment lane only. States that imply outbound delivery are
# intentionally absent; delivery/outbox remains a separate conscious choice.
VALID_DELIVERY_STATES = {
    "not_delivered",
    "prepared",
    "held_for_review",
    "delivery_blocked",
    "delivery_cancelled",
    "silenced",
}
OUTBOUND_DELIVERY_STATES = {"delivered", "sent", "posted", "dispatched", "queued"}

RAW_PRIVATE_KEYS = {
    "body",
    "content",
    "file_content",
    "full_prompt",
    "message",
    "negative_prompt",
    "private_prompt",
    "prompt",
    "raw",
    "raw_prompt",
    "script",
    "script_text",
    "text",
    "transcript",
}

ARTIFACT_DEFAULTS: dict = {
    "max_path_chars": 1000,
    "max_why_chars": 500,
    "max_json_chars": 2000,
    "max_string_value_chars": 500,
    "default_sensitivity": "private",
    "default_allowed_surfaces": ["local"],
}


def _merged_config(config: dict | None = None) -> dict:
    cfg = deepcopy(ARTIFACT_DEFAULTS)
    if config:
        cfg.update(config)
    return cfg


def _denied(error: str, detail: str) -> dict:
    return {"success": False, "error": error, "detail": detail}


def _find_by_id(rows: list[dict], item_id: str) -> dict | None:
    for row in rows:
        if row.get("id") == item_id:
            return row
    return None


def _contains_raw_private_key(value) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).strip().lower() in RAW_PRIVATE_KEYS:
                return True
            if _contains_raw_private_key(child):
                return True
    elif isinstance(value, list):
        return any(_contains_raw_private_key(child) for child in value)
    return False


def _bounded_value(value, *, max_string: int):
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return truncate_text(value, max_string)
    if isinstance(value, list):
        return [_bounded_value(v, max_string=max_string) for v in value[:20]]
    if isinstance(value, dict):
        bounded = {}
        for key, child in list(value.items())[:40]:
            if isinstance(key, str):
                bounded[truncate_text(key, 120)] = _bounded_value(child, max_string=max_string)
        return bounded
    return truncate_text(str(value), max_string)


def _bound_mapping(
    value: dict | None,
    *,
    config: dict,
    field_name: str,
) -> tuple[dict, dict | None]:
    if not value:
        return {}, None
    if not isinstance(value, dict):
        return {}, _denied("invalid_mapping", f"{field_name} must be a dict.")
    if _contains_raw_private_key(value):
        return {}, _denied(
            "raw_private_material_not_allowed",
            f"{field_name} may contain hashes/refs/metadata, not raw prompts, scripts, transcripts, or message bodies.",
        )
    bounded_raw = _bounded_value(value, max_string=int(config.get("max_string_value_chars", 500)))
    bounded = bounded_raw if isinstance(bounded_raw, dict) else {}
    serialized = json.dumps(bounded, separators=(",", ":"), sort_keys=True)
    if len(serialized) > int(config.get("max_json_chars", 2000)):
        return {}, _denied(
            "metadata_too_large",
            f"{field_name} is {len(serialized)} chars after bounding; max is {config.get('max_json_chars', 2000)}.",
        )
    return bounded, None


def _normalize_surfaces(surfaces: list[str] | None, default: list[str]) -> list[str]:
    raw = surfaces if surfaces is not None else default
    result = []
    for surface in raw or []:
        if isinstance(surface, str) and surface.strip() and surface.strip() not in result:
            result.append(surface.strip())
    return result


def _thread_ref_payload(artifact: dict, now: str) -> dict:
    return {
        "type": "artifact_ref",
        "artifact_id": artifact.get("id", ""),
        "artifact_kind": artifact.get("kind", ""),
        "delivery_state": artifact.get("delivery_state", ""),
        "intended_handoff_mode": artifact.get("intended_handoff_mode", ""),
        "ts": now,
    }


def _update_thread_attachment(store: SensoriumStore, artifact: dict, now: str) -> None:
    thread_id = artifact.get("source_refs", {}).get("thread_id", "")
    if not thread_id:
        return
    threads = store.read_jsonl("threads")
    thread = _find_by_id(threads, thread_id)
    if thread is None:
        return
    thread.setdefault("interaction_refs", []).append(_thread_ref_payload(artifact, now))
    thread.setdefault("decision_log", []).append({
        "ts": now,
        "type": "artifact.recorded",
        "artifact_id": artifact.get("id", ""),
        "artifact_kind": artifact.get("kind", ""),
    })
    thread["summary_dirty"] = True
    if not thread.get("dirty_since"):
        thread["dirty_since"] = now
    thread["updated_at"] = now
    store.rewrite_jsonl("threads", threads)


def _infer_sources(
    store: SensoriumStore,
    *,
    source_thread_id: str,
    source_candidate_id: str,
    source_action_id: str,
) -> tuple[dict, dict | None, dict | None, dict | None, dict | None]:
    """Return source_refs plus thread/action/candidate objects when present."""
    thread = None
    action = None
    candidate = None

    threads = store.read_jsonl("threads")
    actions = store.read_jsonl("thread_actions")
    candidates = store.read_jsonl("candidates")

    if source_action_id:
        action = _find_by_id(actions, source_action_id)
        if action is None:
            return {}, None, None, None, _denied("action_not_found", f"Action '{source_action_id}' not found.")
        if not source_thread_id:
            source_thread_id = action.get("origin_thread_id", "")
        if not source_candidate_id:
            source_candidate_id = action.get("origin_candidate_id", "")

    if source_thread_id:
        thread = _find_by_id(threads, source_thread_id)
        if thread is not None and not source_candidate_id:
            source_candidate_id = thread.get("origin_candidate_id", "")

    if source_candidate_id:
        candidate = _find_by_id(candidates, source_candidate_id)

    refs = {
        "thread_id": source_thread_id or "",
        "candidate_id": source_candidate_id or "",
        "action_id": source_action_id or "",
    }
    return refs, thread, action, candidate, None


def store_artifact(
    store: SensoriumStore,
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
) -> dict:
    """Store a private-by-default artifact ref and optionally attach it to thread/action.

    The function never generates media, dispatches outbox messages, or stores raw
    prompt/content material. It records only the artifact reference and compact
    provenance needed for later conscious review.
    """
    cfg = _merged_config(config)
    now = utc_now_iso()

    kind = (kind or "").strip()
    if kind not in VALID_ARTIFACT_KINDS:
        return _denied("invalid_artifact_kind", f"kind must be one of {sorted(VALID_ARTIFACT_KINDS)}")
    if not ref_path or not str(ref_path).strip():
        return _denied("missing_ref_path", "ref_path must be a non-empty file path or external ref.")
    intended_handoff_mode = (intended_handoff_mode or "present_thread").strip()
    if intended_handoff_mode not in VALID_HANDOFF_MODES:
        return _denied("invalid_handoff_mode", f"intended_handoff_mode must be one of {sorted(VALID_HANDOFF_MODES)}")
    delivery_state = (delivery_state or "not_delivered").strip()
    if delivery_state in OUTBOUND_DELIVERY_STATES:
        return _denied(
            "outbound_delivery_not_allowed",
            "Artifact records may not claim sent/delivered/queued delivery in v0; use outbox/delivery after conscious choice.",
        )
    if delivery_state not in VALID_DELIVERY_STATES:
        return _denied("invalid_delivery_state", f"delivery_state must be one of {sorted(VALID_DELIVERY_STATES)}")

    provenance_bounded, err = _bound_mapping(provenance, config=cfg, field_name="provenance")
    if err:
        return err
    capacity_bounded, err = _bound_mapping(capacity_requirements, config=cfg, field_name="capacity_requirements")
    if err:
        return err
    hooks_bounded, err = _bound_mapping(feedback_hooks, config=cfg, field_name="feedback_hooks")
    if err:
        return err

    store.ensure_dirs()
    source_refs, thread, action, _candidate, err = _infer_sources(
        store,
        source_thread_id=source_thread_id,
        source_candidate_id=source_candidate_id,
        source_action_id=source_action_id,
    )
    if err:
        return err

    sensitivity_values = [sensitivity or str(cfg.get("default_sensitivity", "private"))]
    if thread:
        sensitivity_values.append(thread.get("sensitivity", "private"))
    if action:
        sensitivity_values.append(action.get("sensitivity", "private"))
    final_sensitivity = merge_sensitivity(sensitivity_values)

    final_surfaces = _normalize_surfaces(
        allowed_surfaces,
        list(cfg.get("default_allowed_surfaces") or ["local"]),
    )
    if thread:
        final_surfaces = sorted(set(final_surfaces) & set(thread.get("allowed_surfaces") or []))
    if action:
        final_surfaces = sorted(set(final_surfaces) & set(action.get("allowed_surfaces") or []))

    artifact = {
        "id": new_id("art"),
        "ts": now,
        "updated_at": now,
        "status": "recorded",
        "kind": kind,
        "ref_path": truncate_text(str(ref_path).strip(), int(cfg.get("max_path_chars", 1000))),
        "provenance": provenance_bounded,
        "privacy": final_sensitivity,
        "sensitivity": final_sensitivity,
        "allowed_surfaces": final_surfaces,
        "why_created": truncate_text(why_created or "", int(cfg.get("max_why_chars", 500))),
        "intended_handoff_mode": intended_handoff_mode,
        "delivery_state": delivery_state,
        "capacity_requirements": capacity_bounded,
        "source_refs": source_refs,
        "feedback_hooks": hooks_bounded,
    }

    store.append_jsonl("artifacts", artifact)

    attach_receipt = None
    if source_refs.get("action_id"):
        attach_result = attach_action_ref(
            store,
            action_id=source_refs["action_id"],
            kind="artifact_ref",
            ref_id=artifact["id"],
            metadata={
                "artifact_kind": kind,
                "delivery_state": delivery_state,
                "intended_handoff_mode": intended_handoff_mode,
            },
        )
        if attach_result.get("success"):
            attach_receipt = attach_result.get("receipt")

    _update_thread_attachment(store, artifact, now)

    receipt = {
        "ts": now,
        "type": "artifact.recorded",
        "artifact_id": artifact["id"],
        "artifact_kind": kind,
        "thread_id": source_refs.get("thread_id", ""),
        "action_id": source_refs.get("action_id", ""),
        "delivery_state": delivery_state,
        "intended_handoff_mode": intended_handoff_mode,
        "outbound_delivery": False,
    }
    store.append_jsonl("decisions", receipt)

    result = {"success": True, "data": artifact, "receipt": receipt}
    if attach_receipt:
        result["attachment_receipt"] = attach_receipt
    return result


def list_artifacts(
    store: SensoriumStore,
    *,
    thread_id: str | None = None,
    action_id: str | None = None,
    kind: str | None = None,
    limit: int = 20,
) -> list[dict]:
    store.ensure_dirs()
    artifacts = store.read_jsonl("artifacts")
    if thread_id:
        artifacts = [a for a in artifacts if a.get("source_refs", {}).get("thread_id") == thread_id]
    if action_id:
        artifacts = [a for a in artifacts if a.get("source_refs", {}).get("action_id") == action_id]
    if kind:
        artifacts = [a for a in artifacts if a.get("kind") == kind]
    return artifacts[-limit:]


def compact_artifacts_for_thread(
    store: SensoriumStore,
    thread_id: str,
    *,
    surface: str = "local",
    instance_config: dict | None = None,
) -> list[dict]:
    """Return surface-safe artifact refs for an opened thread capsule.

    No file paths, provenance, hashes, prompts, or raw content are included here.
    The full artifact record remains available only through explicit local tools.
    """
    inst_cfg = instance_config or {}
    visible = []
    for artifact in list_artifacts(store, thread_id=thread_id, limit=100):
        if not visible_on_surface(artifact, surface, inst_cfg):
            continue
        visible.append({
            "id": artifact.get("id"),
            "kind": artifact.get("kind"),
            "delivery_state": artifact.get("delivery_state"),
            "intended_handoff_mode": artifact.get("intended_handoff_mode"),
            "why_created": truncate_text(artifact.get("why_created", ""), 120),
        })
    return visible
