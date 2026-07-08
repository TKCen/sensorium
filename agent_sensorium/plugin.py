"""Hermes plugin registration surface."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_LIVE_TOOLSET = "agent-sensorium-live"


def _default_instance() -> str:
    """Return configured default instance for commands/tools without explicit instance."""
    from .config import default_instance_name

    return default_instance_name("default")


def _arg_instance(args: dict[str, Any]) -> str:
    value = args.get("instance")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return _default_instance()


def _schema(name: str, description: str, properties: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties or {},
            "additionalProperties": False,
        },
    }


def register(ctx) -> None:
    """Register Agent Sensorium plugin tools, command, and bundled skill."""
    from .commands import handle_sensorium_command
    from .live_turn import (
        build_live_ingest_receipt,
        normalize_live_turn_intent,
        should_ingest_live_residue,
    )
    from .pointers import handle_pointer_pre_llm
    from .pre_llm_salience import handle_salience_pre_llm
    from .store import SensoriumStore
    from .config import load_instance_config
    from .conscious_reachout import apply_conscious_reachout_decision
    from .tools import (
        handle_sensorium_attention_pointer,
        handle_sensorium_candidate_open,
        handle_sensorium_ingest_signal,
        handle_sensorium_status,
        handle_sensorium_thread_open,
        handle_sensorium_thread_update,
    )

    def _live_result(payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False)

    def _loads_result(raw: str) -> dict[str, Any]:
        try:
            return json.loads(raw or "{}")
        except Exception:
            return {"success": False, "error": "invalid_sensorium_result", "raw": raw}

    def _handle_live_sensorium(args: dict[str, Any], **kw) -> str:
        """Compact foreground Sensorium aperture.

        The live model surface stays intentionally tiny. Granular operations stay
        behind internal handlers, config files, scripts, dashboard, and CLI paths.
        """
        action = str(args.get("action") or "status").strip().lower()
        instance = _arg_instance(args)
        state_dir = args.get("state_dir")
        surface = str(args.get("surface") or kw.get("platform") or "local").strip() or "local"
        text = str(args.get("text") or "").strip()
        target_id = str(args.get("id") or "latest").strip() or "latest"

        if action == "status":
            # Forward reference_id + surface so the conscious layer can pin
            # to a specific subject instead of guessing across the rotating
            # top-N. Both kwargs are optional and ignored if absent.
            ref_id = str(args.get("reference_id") or args.get("id") or "").strip()
            # Live surface keyword may already be present; only pass it if it
            # is non-empty to preserve the default for callers that did not
            # set it.
            status_kwargs: dict = {"instance": instance, "state_dir": state_dir}
            if ref_id and ref_id != "latest":
                status_kwargs["reference_id"] = ref_id
            if surface:
                status_kwargs["surface"] = surface
            status = _loads_result(handle_sensorium_status(**status_kwargs))
            pointer = _loads_result(handle_sensorium_attention_pointer(
                instance=instance, state_dir=state_dir, surface=surface,
            ))
            data = status.get("data") or {}
            response_data = {
                "counts": data.get("counts", {}),
                "top_candidates": data.get("top_candidates", [])[:3],
                "top_threads": data.get("top_threads", [])[:3],
                "volunteer_cards": data.get("volunteer_cards", [])[:3],
                "pointer": (pointer.get("data") or {}),
                "ts": data.get("ts"),
            }
            if "exact_subject" in data:
                response_data["exact_subject"] = data["exact_subject"]
            return _live_result({
                "success": bool(status.get("success", True)),
                "instance": instance,
                "data": response_data,
                "error": status.get("error"),
            })

        if action == "ingest":
            kind = str(args.get("kind") or "operator_salience").strip() or "operator_salience"
            if not text:
                return _live_result({"success": False, "instance": instance, "error": "text_required"})
            try:
                strength = float(args.get("strength") or 0.75)
            except (TypeError, ValueError):
                strength = 0.75
            strength = max(0.0, min(1.0, strength))
            allowed_surfaces = ["local"] if surface == "local" else ["local", surface]
            intent = normalize_live_turn_intent(
                foreground_action_taken=args.get("foreground_action_taken", False),
                foreground_resolution=args.get("foreground_resolution", "none"),
                residue=args.get("residue", "later_review"),
                durable_capture=args.get("durable_capture", "none"),
                background_action_allowed=args.get("background_action_allowed", False),
            )
            should_ingest, ingest_reason = should_ingest_live_residue(intent)
            store = SensoriumStore(instance=instance, state_dir=state_dir)
            store.ensure_dirs()
            if not should_ingest:
                receipt = build_live_ingest_receipt(
                    text=text,
                    kind=kind,
                    surface=surface,
                    intent=intent,
                    ingested=False,
                    skipped_reason=ingest_reason,
                )
                store.append_jsonl("decisions", receipt)
                return _live_result({
                    "success": True,
                    "instance": instance,
                    "data": {
                        "ingested": False,
                        "reason": ingest_reason,
                        "receipt": receipt,
                    },
                })

            signal = {
                "sensor": "active_session",
                "source": "foreground_tool",
                "kind": kind,
                "summary": text[:500],
                "strength_hint": strength,
                "sensitivity": "private",
                "allowed_surfaces": allowed_surfaces,
                "correlation_keys": [
                    "active-session",
                    f"surface:{surface}",
                    kind,
                    f"live-residue:{intent['residue']}",
                    f"foreground:{intent['foreground_resolution']}",
                ],
                "live_turn_intent": intent,
            }
            raw_result = handle_sensorium_ingest_signal(signal=signal, instance=instance, state_dir=state_dir)
            parsed = _loads_result(raw_result)
            data = parsed.get("data") or {}
            signal_id = str(data.get("signal_id") or "")
            receipt = build_live_ingest_receipt(
                text=text,
                kind=kind,
                surface=surface,
                intent=intent,
                signal_id=signal_id,
                ingested=bool(parsed.get("success")),
                skipped_reason="" if parsed.get("success") else str(parsed.get("error") or "ingest_failed"),
            )
            store.append_jsonl("decisions", receipt)
            if isinstance(data, dict):
                data["live_turn_receipt"] = receipt
                parsed["data"] = data
            return _live_result(parsed)

        if action == "open":
            # Acceptance criterion (b): open semantics can recover relevant
            # candidate details when no openable thread exists or when the
            # agent/operator passed an explicit candidate id. This keeps the
            # doorway honest — the LLM does not have to invent "Thread X is
            # openable" to surface saved-research residue. We always try the
            # thread first so existing callers/tests keep working.
            thread_result = _loads_result(handle_sensorium_thread_open(
                thread_id=target_id, surface=surface, instance=instance, state_dir=state_dir,
            ))
            if thread_result.get("success"):
                return _live_result(thread_result)
            if target_id and target_id.startswith("cand_"):
                cand_result = _loads_result(handle_sensorium_candidate_open(
                    candidate_id=target_id, surface=surface,
                    instance=instance, state_dir=state_dir,
                ))
                if cand_result.get("success"):
                    return _live_result(cand_result)
                return _live_result(cand_result)
            return _live_result(thread_result)

        if action == "update":
            keyword = str(args.get("keyword") or "mark_reviewed").strip().lower()
            return handle_sensorium_thread_update(
                thread_id=target_id,
                action=keyword,
                reason=text,
                instance=instance,
                state_dir=state_dir,
            )

        if action == "reach_out":
            # Foreground/conscious lane only. This records/prepares a bounded
            # reach-out decision; direct delivery still requires explicit config
            # and an adapter-backed dispatch path outside this tiny live surface.
            store = SensoriumStore(instance=instance, state_dir=state_dir)
            store.ensure_dirs()
            instance_config, _ = load_instance_config(state_dir=str(store.root))
            decision = str(args.get("decision") or "prepare_message").strip().lower()
            target = args.get("target") if isinstance(args.get("target"), dict) else {}
            target_ref = str(args.get("target_ref") or surface).strip()
            if not args.get("target_ref") and surface == "discord" and isinstance(target, dict):
                chan = target.get("channel_id") or target.get("dm_channel_id")
                if chan:
                    target_ref = f"discord:{chan}"
            result = apply_conscious_reachout_decision(
                store,
                decision=decision,
                actor_tier="conscious",
                source="foreground_live_tool",
                reason=str(args.get("reason") or "foreground conscious reach-out").strip(),
                message=text,
                surface=surface,
                target_ref=target_ref,
                target=target,
                thread_id="" if target_id == "latest" else target_id,
                outbox_id=str(args.get("outbox_id") or "").strip(),
                config=instance_config,
                execute=False,
            )
            return _live_result({"success": bool(result.get("success")), "instance": instance, "data": result, "error": result.get("error")})

        return _live_result({
            "success": False,
            "instance": instance,
            "error": "invalid_action",
            "allowed_actions": ["status", "ingest", "open", "update", "reach_out"],
        })

    ctx.register_tool(
        name="sensorium",
        toolset=_LIVE_TOOLSET,
        schema=_schema(
            "sensorium",
            "Compact live Sensorium aperture: status, ingest deferred salience, open a pointer/thread, update by keyword, or record a conscious reach-out preparation.",
            {
                "action": {
                    "type": "string",
                    "enum": ["status", "ingest", "open", "update", "reach_out"],
                    "description": "status|ingest|open|update|reach_out",
                },
                "text": {"type": "string", "description": "Short salience summary, update reason, or reach-out message body."},
                "kind": {"type": "string", "description": "Optional salience kind for ingest."},
                "decision": {
                    "type": "string",
                    "enum": ["no_action", "hold", "prepare_message", "prepare_artifact", "reach_out", "deliver_prepared"],
                    "description": "For reach_out: conscious decision posture; live surface executes false by default.",
                },
                "reason": {"type": "string", "description": "For reach_out: compact conscious reason."},
                "target_ref": {"type": "string", "description": "For reach_out: compact allowed target ref such as local or discord:<channel>."},
                "target": {"type": "object", "description": "For reach_out: optional target map, e.g. channel_id or dm_channel_id."},
                "outbox_id": {"type": "string", "description": "For reach_out deliver_prepared: prepared outbox request id."},
                "strength": {"type": "number", "description": "Optional ingest strength 0-1."},
                "foreground_action_taken": {
                    "type": "boolean",
                    "description": "For ingest: true if the live turn already answered/acted/patched/retained/decided on this item.",
                },
                "foreground_resolution": {
                    "type": "string",
                    "enum": ["full", "partial", "none", "explicit_no_action"],
                    "description": "For ingest: how much the foreground turn resolved before any Sensorium residue capture.",
                },
                "residue": {
                    "type": "string",
                    "enum": ["none", "watch", "later_review", "pattern_pressure"],
                    "description": "For ingest: compact unresolved residue to preserve; use none when foreground fully settled it.",
                },
                "durable_capture": {
                    "type": "string",
                    "enum": ["none", "memory", "skill", "docs", "task", "artifact"],
                    "description": "For ingest: where the item was already captured outside Sensorium, if anywhere.",
                },
                "background_action_allowed": {
                    "type": "boolean",
                    "description": "For ingest receipts only; defaults false and does not authorize outbound action.",
                },
                "id": {"type": "string", "description": "Thread id, or latest."},
                "keyword": {
                    "type": "string",
                    "description": "Update keyword: close, hold, resume, archive, mark_reviewed, pin, or unpin.",
                },
                "surface": {"type": "string", "description": "local or discord; defaults local."},
            },
        ),
        handler=_handle_live_sensorium,
        description="Compact foreground Sensorium keyword tool.",
    )

    # Admin Sensorium operations intentionally stay behind internal Python
    # handlers, scripts, config files, and dashboard/CLI surfaces. Do not expose
    # them as live model tools; the compact `sensorium` aperture above is the
    # only LLM-facing plugin tool.

    ctx.register_command(
        "sensorium",
        lambda raw_args: handle_sensorium_command(raw_args, instance=_default_instance()),
        description="Pull-based sensorium status and review.",
        args_hint="status|threads|pointer|open|thread|dispatch|compact|help",
    )

    ctx.register_hook(
        "pre_llm_call",
        lambda **kw: handle_pointer_pre_llm(
            instance=_default_instance(),
            platform=kw.get("platform") or "local",
            session_id=kw.get("session_id") or "",
            state_dir=kw.get("state_dir"),
            current_text=kw.get("current_text") or kw.get("text") or kw.get("user_text") or "",
            messages=kw.get("messages"),
        ),
    )

    ctx.register_hook(
        "pre_llm_call",
        lambda **kw: handle_salience_pre_llm(
            instance=_default_instance(),
            platform=kw.get("platform") or "local",
            session_id=kw.get("session_id") or "",
            state_dir=kw.get("state_dir"),
        ),
    )

    skill_path = Path(__file__).resolve().parents[1] / "skills" / "agent-sensorium" / "SKILL.md"
    if skill_path.exists():
        ctx.register_skill(
            "agent-sensorium",
            skill_path,
            description="Review and operate Agent Sensorium candidates and conscious thread capsules.",
        )
