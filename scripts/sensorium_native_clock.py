#!/usr/bin/env python3
"""Bounded native Sensorium clock: deterministic sensing -> Subconscious advisory.

The clock deliberately stops before Conscious activation. It runs deterministic
Sensorium sensors, fingerprints bounded Event/Candidate source material, and only
when that material changes asks one cheap Hermes session for DROP, SAVE, or one
typed CREATE_CONSCIOUS_TASK decision. The existing foreground Conscious doorway
owns opening and settlement. This clock may sample the existing read-only
``kanban_pressure`` sensor, but never mutates Kanban tasks, creates threads,
dispatches workers, or delivers outbound messages.

Healthy and unchanged runs are silent for no-agent cron use. ``--json`` prints the
compact local receipt for manual verification.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

try:
    import fcntl
except ImportError:  # pragma: no cover - WSL/Linux production target
    fcntl = None


def _add_import_path() -> Path:
    here = Path(__file__).resolve()
    candidates = (here.parent.parent, Path.home() / ".hermes" / "plugins" / "agent-sensorium")
    for root in candidates:
        if (root / "agent_sensorium").exists():
            sys.path.insert(0, str(root))
            return root
    raise RuntimeError("agent-sensorium plugin root not found")


PLUGIN_ROOT = _add_import_path()

from agent_sensorium.config import default_instance_name  # noqa: E402
from agent_sensorium.admission import build_admission_plan, context_for_binding  # noqa: E402
from agent_sensorium.attempts import (  # noqa: E402
    advance_attempt, bind_attempt, classify_failure, recover_interrupted_attempt,
    retry_gate, run_bounded_command, start_attempt, terminalize_attempt,
)
from agent_sensorium.schemas import truncate_text, utc_now_iso  # noqa: E402
from agent_sensorium.store import SensoriumStore  # noqa: E402
from agent_sensorium.subconscious import (  # noqa: E402
    ADVISORY_SOURCE_EXCLUDED_KINDS,
    DIRECT_CONSCIOUS_KINDS,
    is_advisory_source_kind,
)
from agent_sensorium.tools import handle_sensorium_subconscious_advisory  # noqa: E402

POLICY_VERSION = "2026-08-31.native-clock-v3"
CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def _json_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _json_read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _lock_nonblocking(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(path, "w", encoding="utf-8")
    if fcntl is None:
        return lock_file
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        return None
    lock_file.write(json.dumps({"pid": os.getpid(), "ts": utc_now_iso()}))
    lock_file.flush()
    return lock_file


def _release_lock(lock_file) -> None:
    try:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()
    except Exception:
        pass


def _candidate_priority_key(candidate: dict) -> tuple:
    """Make live pressure causal without hashing pressure-only decay."""
    try:
        pressure = float(candidate.get("pressure") or 0.0)
    except (TypeError, ValueError):
        pressure = 0.0
    return (-pressure, str(candidate.get("created_at") or ""), str(candidate.get("id") or ""))


def _source_material(store: SensoriumStore, *, event_limit: int, candidate_limit: int) -> dict:
    plan = build_admission_plan(store, candidate_limit=candidate_limit)
    binding = plan["selection"]
    bound = (
        context_for_binding(store, binding, source_decisions=plan["source_decisions"])
        if binding is not None else None
    )
    events = list((bound or {}).get("events") or [])[-max(1, event_limit):]
    candidates = [(bound or {}).get("candidate")] if bound is not None else []
    return {
        "policy_version": plan["policy_version"],
        "candidate_order": "descending_live_pressure_then_created_at_then_id",
        "selection": binding,
        "eligible_count": plan["eligible_count"],
        "suppressed_counts": plan["suppressed_counts"],
        "source_decisions": plan["source_decisions"],
        "events": [
            {
                "id": event.get("id"),
                "kind": event.get("kind"),
                "summary": truncate_text(event.get("summary", ""), 240),
                "correlation_keys": sorted(str(key) for key in event.get("correlation_keys") or []),
            }
            for event in events
        ],
        "candidates": [
            {
                "id": candidate.get("id"),
                "kind": candidate.get("kind"),
                "summary": truncate_text(candidate.get("summary", ""), 240),
                "fingerprint": candidate.get("fingerprint"),
                "event_ids": sorted(str(event_id) for event_id in candidate.get("event_ids") or []),
            }
            for candidate in candidates if isinstance(candidate, dict)
        ],
    }


def _signature(material: dict) -> str:
    payload = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _extract_json_object(text: str) -> dict:
    cleaned = re.sub(r"```(?:json)?\s*|```", "", text, flags=re.IGNORECASE).strip()
    decoder = json.JSONDecoder()
    best: dict | None = None
    for index, character in enumerate(cleaned):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(cleaned[index:])
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        best = value
        if value.get("action") in {"DROP", "SAVE", "CREATE_CONSCIOUS_TASK"}:
            return value
    if best is not None:
        return best
    raise ValueError("bounded Hermes Subconscious session returned no JSON object")


def _advisory_prompt(context: dict) -> str:
    payload = {
        "role": "sensorium_subconscious_native_clock",
        "policy_version": POLICY_VERSION,
        "authority": {
            "allowed": ["DROP", "SAVE", "CREATE_CONSCIOUS_TASK"],
            "create_at_most_one_typed_internal_candidate": True,
            "create_requires_exactly_one_source_candidate_id": True,
            "read_only_memory_is_normal_cognition": True,
            "allowed_tools": ["hindsight_recall", "hindsight_reflect"],
            "use_memory_when_it_can_deepen_or_change_interpretation": True,
            "maximum_retrieval_rounds": 1,
            "forbidden_tools": ["memory", "hindsight_retain"],
            "no_outbound_delivery": True,
            "no_tasks_or_workers": True,
            "no_thread_or_aperture_opening": True,
            "no_memory_or_file_writes": True,
            "return_only_json": True,
        },
        "direct_pressure_kinds_excluded": sorted(DIRECT_CONSCIOUS_KINDS),
        "advisory_source_kinds_excluded": sorted(ADVISORY_SOURCE_EXCLUDED_KINDS),
        "context": context,
        "required_schema": {
            "action": "DROP | SAVE | CREATE_CONSCIOUS_TASK",
            "rationale": "short reason",
            "event_ids": ["evt_id"],
            "candidate_ids": ["cand_id"],
            "pressure": "optional 0..1",
            "conscious_task": {
                "request_type": "THINK | PRIVATE_EXPRESSION | SAVE | UPDATE_MEMORY_OR_SKILL | CREATE_FOLLOWUP | DELEGATE_WORK",
                "title": "required only for CREATE_CONSCIOUS_TASK",
                "why": "required only for CREATE_CONSCIOUS_TASK",
                "expected_decision": "required only for CREATE_CONSCIOUS_TASK",
            },
        },
    }
    return (
        "Act as a bounded Subconscious pass. Decide whether the compact private Sensorium material "
        "contains one unresolved pattern that deserves later Conscious attention. Prefer DROP when it is "
        "noise, stale, duplicate, already settled, or merely high pressure without meaning. SAVE means "
        "retain the advisory receipt without opening Conscious attention. CREATE_CONSCIOUS_TASK only for "
        "one coherent unresolved item worth a future foreground choice, and it must name exactly one existing "
        "candidate_id from the provided candidates. Use SAVE if no single source candidate owns it. "
        "Hindsight recall and reflection are normal parts of this Subconscious pass: use them freely "
        "in one parallel retrieval round whenever memory could deepen, connect, complicate, or change "
        "the meaning of the present material. Do not require immediate actionability before consulting "
        "memory. Never call built-in memory mutation or hindsight_retain. Return exactly "
        "one JSON object.\n\n" + json.dumps(payload, separators=(",", ":"))
    )


def _python_executable(plugin_root: Path) -> str:
    candidate = plugin_root / ".venv" / "bin" / "python"
    return str(candidate) if candidate.exists() else sys.executable


def _run_sensors(
    *, instance: str, state_dir: str | None, plugin_root: Path, timeout_seconds: int,
    run_command: CommandRunner,
) -> dict:
    command = [
        _python_executable(plugin_root),
        str(plugin_root / "scripts" / "sensorium_tick.py"),
        "--instance", instance,
        "--all-sensors",
        "--codex-usage",
        "--memory-reflection",
        "--sensor", "research_source_feeds",
        "--json",
    ]
    if state_dir:
        command.extend(["--state-dir", state_dir])
    started = time.monotonic()
    completed = run_bounded_command(
        command, cwd=str(plugin_root), text=True, capture_output=True,
        timeout=timeout_seconds, runner=run_command)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "sensorium_tick failed")[-800:]
        raise RuntimeError(detail)
    try:
        parsed = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"sensorium_tick returned invalid JSON: {exc}") from exc
    if not parsed.get("success"):
        raise RuntimeError(f"sensorium_tick reported failure: {parsed.get('errors')}")
    return {
        "success": True,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "steps": sorted(
            key for key in parsed
            if key not in {"success", "instance", "dry_run", "config", "status"}
        ),
    }


def _run_subconscious_session(
    *, context: dict, hermes_cli: str, provider: str, model: str,
    timeout_seconds: int, run_command: CommandRunner,
) -> tuple[dict, dict]:
    command = [
        hermes_cli,
        "chat", "-Q",
        "--provider", provider,
        "-m", model,
        # Use the explicit memory toolset so Hindsight recall/reflect are
        # available as normal read-only cognition without falling through to
        # every configured CLI toolset. Mutation remains forbidden by policy.
        "-t", "memory",
        "--source", "sensorium-native-clock",
        "--session-purpose", "autonomous",
        "--max-turns", "6",
        "--run-budget", "270",
        "-q", _advisory_prompt(context),
    ]
    started = time.monotonic()
    completed = run_bounded_command(
        command, cwd=str(Path.home() / ".hermes" / "hermes-agent"), text=True,
        capture_output=True, timeout=timeout_seconds, runner=run_command)
    meta = {
        "provider": provider,
        "model": model,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "returncode": completed.returncode,
    }
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "Hermes Subconscious session failed")[-800:]
        raise RuntimeError(detail)
    return _extract_json_object(completed.stdout), meta


def run_once(args: argparse.Namespace, *, run_command: CommandRunner = subprocess.run) -> dict:
    store = SensoriumStore(instance=args.instance, state_dir=args.state_dir)
    store.ensure_dirs()
    latest_path = store.root / "last_native_clock.json"
    state_path = store.root / "native_clock_state.json"
    lock_file = _lock_nonblocking(store.root / "locks" / "native_clock.lock")
    if lock_file is None:
        result = {
            "success": True,
            "action": "skipped_locked",
            "instance": args.instance,
            "ts": utc_now_iso(),
        }
        _json_write(latest_path, result)
        return result

    try:
        started = time.monotonic()
        total_timeout_seconds = getattr(args, "total_timeout_seconds", 570)
        cleanup_reserve_seconds = getattr(args, "cleanup_reserve_seconds", 30)
        internal_deadline = started + total_timeout_seconds
        prior = _json_read(state_path)
        recovery = recover_interrupted_attempt(prior)
        if recovery == "owner_alive":
            result = {"success": True, "action": "skipped_attempt_owned", "instance": args.instance, "ts": utc_now_iso()}
            _json_write(latest_path, result)
            return result
        start_attempt(
            prior, session_purpose="autonomous", source_revision=None, ordinal=0,
            stage="sensing", deadline_seconds=total_timeout_seconds)
        _json_write(state_path, prior)
        sensor_result = {"success": True, "action": "skipped_by_flag"}
        if not args.skip_sensors:
            try:
                sensor_result = _run_sensors(
                    instance=args.instance, state_dir=args.state_dir,
                    plugin_root=Path(args.plugin_root),
                    timeout_seconds=max(1, int(min(
                        args.sensor_timeout_seconds,
                        internal_deadline - time.monotonic() - cleanup_reserve_seconds))),
                    run_command=run_command)
            except Exception as exc:
                terminalize_attempt(prior, success=False, failure_class=classify_failure(exc, stage="sensing"))
                result = {"success": False, "action": "sensor_failed", "instance": args.instance,
                          "ts": utc_now_iso(), "reason": type(exc).__name__}
                _json_write(state_path, prior)
                _json_write(latest_path, result)
                return result

        material = _source_material(
            store,
            event_limit=args.event_limit,
            candidate_limit=args.candidate_limit,
        )
        binding = material.get("selection")
        signature = str((binding or {}).get("admission_key") or "")
        prompt_sha256 = (
            hashlib.sha256(_advisory_prompt(material).encode("utf-8")).hexdigest()
            if binding is not None else None
        )
        base = {
            "success": True,
            "instance": args.instance,
            "ts": utc_now_iso(),
            "policy_version": POLICY_VERSION,
            "source_signature": signature,
            "source_revision": signature or None,
            "prompt_sha256": prompt_sha256,
            "source_counts": {
                "events": len(material["events"]),
                "candidates": len(material["candidates"]),
            },
            "sensors": sensor_result,
        }
        if binding is None:
            action = "skipped_empty" if not store.read_jsonl("candidates") else "skipped_no_eligible_source"
            result = {**base, "action": action}
            terminalize_attempt(prior, success=True)
            _json_write(state_path, prior)
            _json_write(latest_path, result)
            return result
        allowed, gate_reason, ordinal = retry_gate(prior, signature)
        if not allowed:
            result = {
                **base,
                "action": f"skipped_{gate_reason}",
                "reason": prior.get("last_failure_reason", ""),
            }
            terminalize_attempt(prior, success=True)
            _json_write(state_path, prior)
            _json_write(latest_path, result)
            return result

        bind_attempt(prior, source_revision=signature, ordinal=ordinal, stage="reasoning")
        _json_write(state_path, prior)

        try:
            advisory, session_meta = _run_subconscious_session(
                context=material,
                hermes_cli=args.hermes_cli,
                provider=args.provider,
                model=args.model,
                timeout_seconds=max(1, int(min(
                    args.hermes_timeout_seconds,
                    internal_deadline - time.monotonic() - cleanup_reserve_seconds))),
                run_command=run_command,
            )
            advance_attempt(prior, "applying")
            _json_write(state_path, prior)
            applied = json.loads(handle_sensorium_subconscious_advisory(
                instance=args.instance,
                state_dir=args.state_dir,
                advisory_output=advisory,
                dry_run=False,
                enabled=True,
                record_receipt=True,
                admission_binding=binding,
            ))
            if not applied.get("success"):
                raise RuntimeError(applied.get("error") or "Subconscious advisory apply failed")
            data = applied.get("data") or {}
            result = {
                **base,
                "action": "processed_changed_material",
                "advisory_action": data.get("output_action") or advisory.get("action"),
                "candidate_id": data.get("candidate_id"),
                "reason": data.get("reason") or advisory.get("rationale", ""),
                "session": session_meta,
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
            next_state = {
                **prior,
                "last_processed_signature": signature,
                "last_processed_at": result["ts"],
                "last_failed_signature": None,
                "last_failed_epoch": None,
                "last_failure_reason": "",
            }
            terminalize_attempt(next_state, success=True,
                                disposition_ref=str(data.get("candidate_id") or advisory.get("action") or ""))
        except Exception as exc:
            result = {
                **base,
                "success": False,
                "action": "subconscious_session_failed",
                "reason": str(exc)[:800],
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
            next_state = {
                **prior,
                "last_failed_signature": signature,
                "last_failed_epoch": time.time(),
                "last_failure_reason": result["reason"],
            }
            terminalize_attempt(
                next_state, success=False,
                failure_class=classify_failure(exc, stage=(prior.get("active_attempt") or {}).get("stage", "reasoning")))
        _json_write(state_path, next_state)
        _json_write(latest_path, result)
        return result
    finally:
        _release_lock(lock_file)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance", default=default_instance_name())
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--plugin-root", default=str(PLUGIN_ROOT))
    parser.add_argument("--event-limit", type=int, default=50)
    parser.add_argument("--candidate-limit", type=int, default=50)
    parser.add_argument("--failure-cooldown-seconds", type=int, default=1800)
    # The deterministic sensor pass also runs the due-gated Hindsight memory
    # reflection probe.  The probe's own cadence keeps calls sparse, but an
    # actually-due reflection may legitimately take up to three minutes.
    parser.add_argument("--sensor-timeout-seconds", type=int, default=240)
    # Hindsight recall/reflect are ordinary Subconscious cognition and may run
    # slowly under local contention. Give the bounded pass a full 15-minute
    # envelope rather than making reflection nominally available but prone to
    # premature termination.
    parser.add_argument("--hermes-timeout-seconds", type=int, default=300)
    parser.add_argument("--total-timeout-seconds", type=int, default=570)
    parser.add_argument("--cleanup-reserve-seconds", type=int, default=30)
    parser.add_argument("--skip-sensors", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--json", action="store_true", dest="print_json")
    parser.add_argument(
        "--hermes-cli",
        default=os.environ.get("HERMES_CLI", str(Path.home() / ".local" / "bin" / "hermes")),
    )
    parser.add_argument("--provider", default=os.environ.get("SENSORIUM_SUBCONSCIOUS_PROVIDER", "openai-codex"))
    parser.add_argument("--model", default=os.environ.get("SENSORIUM_SUBCONSCIOUS_MODEL", "gpt-5.6-luna"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = run_once(args)
    except Exception as exc:
        result = {
            "success": False,
            "action": "clock_failed",
            "instance": args.instance,
            "ts": utc_now_iso(),
            "reason": str(exc)[:800],
        }
        try:
            store = SensoriumStore(instance=args.instance, state_dir=args.state_dir)
            store.ensure_dirs()
            _json_write(store.root / "last_native_clock.json", result)
        except Exception:
            pass
    if args.print_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    elif not result.get("success"):
        print(f"Sensorium native clock failed: {result.get('reason', 'unknown error')}", file=sys.stderr)
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
