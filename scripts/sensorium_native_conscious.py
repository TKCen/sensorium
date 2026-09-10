#!/usr/bin/env python3
"""One bounded native Conscious choice over exactly one current aperture item."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable


def _add_import_path() -> Path:
    here = Path(__file__).resolve()
    for root in (here.parent.parent, Path.home() / ".hermes" / "plugins" / "agent-sensorium"):
        if (root / "agent_sensorium").exists():
            sys.path.insert(0, str(root))
            return root
    raise RuntimeError("agent-sensorium plugin root not found")


PLUGIN_ROOT = _add_import_path()

from agent_sensorium.config import default_instance_name  # noqa: E402
from agent_sensorium.attempts import (  # noqa: E402
    advance_attempt, classify_failure, reconcile_applied_attempt,
    recover_interrupted_attempt, retry_gate, run_bounded_command, start_attempt,
    terminalize_attempt,
)
from agent_sensorium.conscious_aperture import (  # noqa: E402
    open_conscious_aperture,
    settle_conscious_aperture_item,
)
from agent_sensorium.conscious_consumer import (  # noqa: E402
    CONSCIOUS_ADVISORY_KIND,
    build_conscious_source_packet,
    consume_conscious_advisory,
    parse_conscious_decision,
)
from agent_sensorium.outbox import source_revision_key  # noqa: E402
from agent_sensorium.schemas import parse_utc_z_checkpoint, utc_now_iso  # noqa: E402
from agent_sensorium.store import SensoriumStore  # noqa: E402

DEFAULT_PROVIDER = "openai-codex"
DEFAULT_MODEL = "gpt-5.6-sol"
POLICY_VERSION = "2026-08-31.native-conscious-v2"
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


def acquire_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(path, "w", encoding="utf-8")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        return None
    lock_file.write(json.dumps({"pid": os.getpid(), "ts": utc_now_iso()}))
    lock_file.flush()
    return lock_file


def release_lock(lock_file) -> None:
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()
    except Exception:
        pass


def _now_value(now: str | None) -> str:
    return now or utc_now_iso()


def build_conscious_prompt(packet: dict[str, Any]) -> str:
    """Build the complete model prompt from one already-open source packet."""
    payload = {
        "role": "sensorium_native_conscious_choice",
        "policy_version": POLICY_VERSION,
        "source_packet": packet,
        "authority": {
            "allowed_decisions": ["SILENCE", "HOLD", "REACH_OUT"],
            "exact_candidate_binding_required": True,
            "optional_read_only_continuity": True,
            "allowed_tools": [
                "hindsight_recall",
                "hindsight_reflect",
                "session_search",
                "lcm_*",
            ],
            "maximum_retrieval_rounds": 1,
            "forbidden_tools": ["memory", "hindsight_retain"],
            "no_memory_or_file_writes": True,
            "no_actions": True,
            "no_delivery": True,
            "openable_artifact_exists_before_choice": False,
            "return_only_json": True,
        },
        "required_schema": {
            "SILENCE": {
                "decision": "SILENCE",
                "candidate_id": "exact source packet candidate_id",
                "source_candidate_fingerprint": "exact source packet fingerprint",
                "reason": "short reason",
            },
            "HOLD": {
                "decision": "HOLD",
                "candidate_id": "exact source packet candidate_id",
                "source_candidate_fingerprint": "exact source packet fingerprint",
                "reason": "short reason",
                "return_at": "future UTC-Z checkpoint YYYY-MM-DDTHH:MM:SSZ",
            },
            "REACH_OUT": {
                "decision": "REACH_OUT",
                "candidate_id": "exact source packet candidate_id",
                "source_candidate_fingerprint": "exact source packet fingerprint",
                "reason": "short reason",
                "message": "specific personal message or question, at most 500 characters",
            },
        },
    }
    return (
        "Make one bounded Conscious choice for the configured recipient. Choose SILENCE unless "
        "the source merits a present-tense HOLD or a specific personal REACH_OUT whose value "
        "is the configured recipient receiving it now. Never write generic pressure, task, reminder, queue, or "
        "notification language. Do not claim that you have something ready: no openable artifact "
        "exists before this choice. Preserve self-authorship and the relationship. You may use at most "
        "one round of read-only continuity retrieval when historical or relational context could "
        "materially change the choice. Use only Hindsight recall/reflect, session_search, or LCM read "
        "tools. Never write memory or files, call memory or hindsight_retain, take actions, prepare "
        "delivery, or report progress. Return exactly one JSON object and no "
        "markdown, code fence, or surrounding prose.\n\n"
        + json.dumps(payload, separators=(",", ":"))
    )


def _extract_session_metadata(stderr: str | None) -> dict[str, str]:
    """Extract only the known Hermes session footer from stderr diagnostics."""
    if not isinstance(stderr, str):
        return {}
    session_ids = []
    for line in stderr.splitlines():
        line = line.strip()
        if not line.startswith("session_id:"):
            continue
        session_id = line[len("session_id:"):].strip()
        if not session_id or any(character.isspace() for character in session_id):
            raise ValueError("Conscious session footer has an invalid session_id")
        session_ids.append(session_id)
    if len(session_ids) > 1:
        raise ValueError("Conscious session returned multiple session_id footers")
    return {"session_id": session_ids[0]} if session_ids else {}


def extract_conscious_transport(
    text: str,
    stderr: str | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Parse strict stdout JSON plus optional known Hermes stderr metadata."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Conscious session returned no JSON")
    cleaned = text.strip()
    decoder = json.JSONDecoder()
    try:
        value, end = decoder.raw_decode(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError("Conscious session returned invalid JSON transport") from exc
    if not isinstance(value, dict):
        raise ValueError("Conscious session JSON must be an object")
    if cleaned[end:].strip():
        raise ValueError("Conscious session returned unexpected transport framing")
    return value, _extract_session_metadata(stderr)


def extract_conscious_response(text: str) -> dict[str, Any]:
    """Accept one strict decision object from the bounded Hermes envelope."""
    value, _ = extract_conscious_transport(text)
    return value


def parse_model_decision(raw: object, packet: dict[str, Any], *, now: str | None = None) -> dict[str, str]:
    """Validate the model response and bind it to the exact aperture packet."""
    if isinstance(raw, str):
        raw = extract_conscious_response(raw)
    if not isinstance(raw, dict):
        raise ValueError("Conscious model response must be an object")
    decision = raw.get("decision")
    if not isinstance(decision, str):
        raise ValueError("Conscious model response must include decision")
    decision = decision.strip().upper()
    expected = {
        "SILENCE": {"decision", "candidate_id", "source_candidate_fingerprint", "reason"},
        "HOLD": {"decision", "candidate_id", "source_candidate_fingerprint", "reason", "return_at"},
        "REACH_OUT": {"decision", "candidate_id", "source_candidate_fingerprint", "reason", "message"},
    }.get(decision)
    if expected is None:
        raise ValueError(f"invalid Conscious decision: {decision}")
    actual = set(raw)
    extra = sorted(actual - expected)
    missing = sorted(expected - actual)
    if extra:
        raise ValueError(f"unexpected fields: {extra}")
    if missing:
        raise ValueError(f"missing fields: {missing}")
    candidate_id = raw.get("candidate_id")
    fingerprint = raw.get("source_candidate_fingerprint")
    if candidate_id != packet.get("candidate_id"):
        raise ValueError("candidate_id does not match the exact source packet")
    if not isinstance(fingerprint, str) or not fingerprint or fingerprint != packet.get("source_candidate_fingerprint"):
        raise ValueError("source_candidate_fingerprint does not match the exact source packet")
    if not isinstance(raw.get("reason"), str) or not raw["reason"].strip():
        raise ValueError("reason must be a non-empty string")

    choice = {"decision": decision, "reason": raw["reason"].strip()}
    if decision == "HOLD":
        checkpoint = parse_utc_z_checkpoint(raw.get("return_at"))
        if checkpoint is None:
            raise ValueError("return_at must be an exact UTC-Z checkpoint")
        if now is not None:
            current = parse_utc_z_checkpoint(now)
            if current is not None and checkpoint[1] <= current[1]:
                raise ValueError("return_at must be in the future")
        choice["return_at"] = checkpoint[0]
    elif decision == "REACH_OUT":
        # Reuse the canonical consumer envelope after the binding fields have
        # been checked. It rejects generic language, unavailable-artifact claims,
        # and messages over the physical 500-character body limit.
        parsed = parse_conscious_decision({
            "decision": decision,
            "reason": raw["reason"],
            "message": raw["message"],
        })
        choice["message"] = parsed["message"]
    return choice


def _run_model_session(
    *,
    packet: dict[str, Any],
    hermes_cli: str,
    provider: str,
    model: str,
    timeout_seconds: int,
    run_command: CommandRunner,
) -> tuple[dict[str, Any], dict[str, Any]]:
    command = [
        hermes_cli,
        "chat",
        "-Q",
        "--provider",
        provider,
        "-m",
        model,
        "-t",
        "context_engine,memory,session_search",
        "--source",
        "sensorium-native-conscious",
        "--session-purpose",
        "autonomous",
        "--max-turns",
        "6",
        "--run-budget",
        "210",
        "-q",
        build_conscious_prompt(packet),
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
        raise RuntimeError((completed.stderr or completed.stdout or "Conscious session failed")[-800:])
    value, session_metadata = extract_conscious_transport(completed.stdout, completed.stderr)
    if session_metadata:
        meta.update(session_metadata)
    return value, meta


def _base_result(args: argparse.Namespace, *, now: str, packet: dict[str, Any] | None = None) -> dict[str, Any]:
    result = {
        "success": True,
        "instance": args.instance,
        "ts": now,
        "policy_version": POLICY_VERSION,
    }
    if packet:
        result.update({
            "candidate_id": packet.get("candidate_id"),
            "aperture_id": packet.get("aperture_id"),
            "source_candidate_fingerprint": packet.get("source_candidate_fingerprint"),
        })
    return result


def _failure_retry_checkpoint(now: str) -> str:
    parsed = parse_utc_z_checkpoint(now)
    base = parsed[1] if parsed is not None else datetime.now(timezone.utc)
    return (base + timedelta(hours=1)).isoformat(timespec="seconds").replace("+00:00", "Z")


def _compact_failure_reason(exc: BaseException) -> str:
    if isinstance(exc, subprocess.TimeoutExpired):
        return "model session timed out"
    detail = " ".join(str(exc).split())
    if not detail:
        detail = type(exc).__name__
    return detail[:240]


def _failure_result(
    store: SensoriumStore,
    args: argparse.Namespace,
    *,
    now: str,
    packet: dict[str, Any],
    action: str,
    reason: str,
    opened_by_this_run: bool,
) -> dict[str, Any]:
    result = {
        **_base_result(args, now=now, packet=packet),
        "success": False,
        "action": action,
        "reason": reason[:240],
        "failure_reason": reason[:240],
        "opened_by_this_run": opened_by_this_run,
    }
    # Failure is not a semantic choice. Preserve unresolved source and leave only
    # execution ownership to the canonical lease-expiry recovery path.
    result["lease_release"] = "canonical_expiry"
    return result


def _canonical_disposition_ref(store: SensoriumStore, revision: str) -> str | None:
    """Return an exact canonical settlement reference for one bound revision."""
    for candidate in store.read_jsonl("candidates"):
        candidate_revision = source_revision_key(
            candidate_id=str(candidate.get("id") or ""),
            source_candidate_ids=candidate.get("source_candidate_ids"),
            source_candidate_fingerprint=str(candidate.get("source_candidate_fingerprint") or ""),
        )
        if candidate_revision != revision:
            continue
        aperture = candidate.get("conscious_aperture") or {}
        receipts = candidate.get("conscious_settlements") or []
        receipt = aperture.get("settlement_receipt")
        if isinstance(receipt, dict):
            receipts = [*receipts, receipt]
        for row in reversed(receipts):
            if not (
                isinstance(row, dict)
                and row.get("type") == "conscious.aperture.settled"
                and row.get("candidate_id") == candidate.get("id")
                and row.get("new_status") == candidate.get("status")
            ):
                continue
            return ":".join(str(row.get(key) or "") for key in (
                "type", "candidate_id", "aperture_id", "decision", "ts",
            ))
    return None


def scheduler_output(result: dict[str, Any], outbox_rows: list[dict[str, Any]]) -> str:
    """Return only an explicitly opted-in prepared body; all other choices are silent."""
    if result.get("action") != "prepared_reach_out":
        return ""
    outbox_id = result.get("outbox_id")
    row = next((row for row in outbox_rows if row.get("id") == outbox_id), None)
    if not isinstance(row, dict) or row.get("status") != "prepared":
        return ""
    return str(row.get("message_preview") or "")


def run_once(
    args: argparse.Namespace,
    *,
    run_command: CommandRunner = subprocess.run,
    now: str | None = None,
) -> dict[str, Any]:
    store = SensoriumStore(instance=args.instance, state_dir=args.state_dir)
    store.ensure_dirs()
    receipt_path = store.root / "last_conscious_clock.json"
    state_path = store.root / "conscious_clock_state.json"
    timestamp = _now_value(now)
    opened_by_this_run = False
    lock_file = acquire_lock(store.root / "locks" / "conscious_clock.lock")
    if lock_file is None:
        result = _base_result(args, now=timestamp)
        result.update({"action": "skipped_locked"})
        _json_write(receipt_path, result)
        return result

    try:
        prior = _json_read(state_path)
        interrupted = dict(prior.get("active_attempt") or {})
        recovery = recover_interrupted_attempt(prior, now=timestamp)
        if recovery == "owner_alive":
            result = {**_base_result(args, now=timestamp), "action": "skipped_attempt_owned"}
            _json_write(receipt_path, result)
            return result
        if recovery == "recovered":
            revision = str(interrupted.get("source_revision") or "")
            disposition_ref = (
                _canonical_disposition_ref(store, revision)
                if interrupted.get("stage") == "applying" and revision else None
            )
            if disposition_ref and reconcile_applied_attempt(
                prior, source_revision=revision, disposition_ref=disposition_ref,
            ):
                result = {
                    **_base_result(args, now=timestamp),
                    "action": "recovered_canonical_disposition",
                    "source_revision": revision,
                    "disposition_ref": disposition_ref,
                }
                _json_write(state_path, prior)
                _json_write(receipt_path, result)
                return result
            _json_write(state_path, prior)
        inspection = open_conscious_aperture(
            store,
            aperture_size=1,
            max_active_sessions=1,
            stale_after_minutes=args.stale_after_minutes,
            dry_run=True,
            now=timestamp,
            candidate_kind=CONSCIOUS_ADVISORY_KIND,
            consumer_id="conscious-session",
        )
        aperture = inspection.get("aperture")
        if not inspection.get("success"):
            result = {**_base_result(args, now=timestamp), **inspection}
            _json_write(receipt_path, result)
            return result
        if not isinstance(aperture, list) or len(aperture) != 1:
            result = {**_base_result(args, now=timestamp), "action": "no_current_advisory"}
            _json_write(receipt_path, result)
            return result

        # A dry inspection is not the application. If there is no active item,
        # open exactly the inspected candidate and then rebuild the packet from
        # the committed aperture so the model sees the actual current id.
        if inspection.get("action") == "would_open_aperture":
            opened = open_conscious_aperture(
                store,
                aperture_size=1,
                max_active_sessions=1,
                stale_after_minutes=args.stale_after_minutes,
                dry_run=False,
                now=timestamp,
                candidate_kind=CONSCIOUS_ADVISORY_KIND,
                consumer_id="conscious-session",
            )
            if not opened.get("success") or not isinstance(opened.get("aperture"), list) or len(opened["aperture"]) != 1:
                result = {**_base_result(args, now=timestamp), **opened}
                _json_write(receipt_path, result)
                return result
            opened_by_this_run = True
            inspection = opened
        packet = build_conscious_source_packet(inspection)
        if not packet.get("source_candidate_fingerprint"):
            result = _failure_result(
                store,
                args,
                now=timestamp,
                packet=packet,
                action="source_binding_missing",
                reason="source_binding_missing",
                opened_by_this_run=opened_by_this_run,
            )
            _json_write(receipt_path, result)
            return result
        revision = source_revision_key(
            candidate_id=packet["candidate_id"],
            source_candidate_ids=packet.get("source_candidate_ids"),
            source_candidate_fingerprint=packet.get("source_candidate_fingerprint", ""),
        )
        allowed, gate_reason, ordinal = retry_gate(prior, revision, now=timestamp)
        if not allowed:
            result = {
                **_base_result(args, now=timestamp, packet=packet),
                "action": f"skipped_{gate_reason}",
                "reason": prior.get("last_failure_reason", ""),
            }
            _json_write(receipt_path, result)
            return result

        total_timeout_seconds = getattr(args, "total_timeout_seconds", 270)
        cleanup_reserve_seconds = getattr(args, "cleanup_reserve_seconds", 30)
        start_attempt(
            prior, session_purpose="autonomous", source_revision=revision, ordinal=ordinal,
            stage="reasoning", deadline_seconds=total_timeout_seconds, now=timestamp)
        _json_write(state_path, prior)
        internal_deadline = time.monotonic() + total_timeout_seconds
        try:
            raw, session = _run_model_session(
                packet=packet,
                hermes_cli=args.hermes_cli,
                provider=args.provider,
                model=args.model,
                timeout_seconds=max(1, int(min(
                    args.timeout_seconds,
                    internal_deadline - time.monotonic() - cleanup_reserve_seconds))),
                run_command=run_command,
            )
            choice = parse_model_decision(raw, packet, now=timestamp)
            advance_attempt(prior, "applying")
            _json_write(state_path, prior)
            applied = consume_conscious_advisory(
                store,
                decision=choice,
                dry_run=False,
                now=timestamp,
                stale_after_minutes=args.stale_after_minutes,
                expected_candidate_id=packet["candidate_id"],
                expected_aperture_id=packet.get("aperture_id"),
                expected_source_candidate_ids=packet.get("source_candidate_ids"),
                expected_source_candidate_fingerprint=packet.get("source_candidate_fingerprint"),
            )
            if not applied.get("success"):
                raise RuntimeError(applied.get("error") or "deterministic Conscious application failed")
            result = {
                **_base_result(args, now=timestamp, packet=packet),
                "action": applied.get("action"),
                "decision": choice["decision"],
                "session": session,
                "body_hash": applied.get("message_hash", ""),
                "body_chars": applied.get("message_chars", 0),
                "outbox_id": applied.get("outbox_id", ""),
            }
            next_state = {
                **prior,
                "last_processed_revision": revision,
                "last_processed_at": timestamp,
                "last_failed_revision": None,
                "last_failed_epoch": None,
                "last_failure_reason": "",
            }
            terminalize_attempt(
                next_state, success=True,
                disposition_ref=str(applied.get("outbox_id") or applied.get("action") or choice["decision"]))
        except Exception as exc:
            result = _failure_result(
                store,
                args,
                now=timestamp,
                packet=packet,
                action="conscious_session_failed",
                reason=_compact_failure_reason(exc),
                opened_by_this_run=opened_by_this_run,
            )
            next_state = {
                **prior,
                "last_failed_revision": revision,
                "last_failed_epoch": time.time(),
                "last_failure_reason": result["reason"],
            }
            terminalize_attempt(
                next_state, success=False,
                failure_class=classify_failure(
                    exc, stage=(prior.get("active_attempt") or {}).get("stage", "reasoning")))
        _json_write(state_path, next_state)
        _json_write(receipt_path, result)
        return result
    finally:
        release_lock(lock_file)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance", default=default_instance_name())
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--plugin-root", default=str(PLUGIN_ROOT))
    parser.add_argument("--hermes-cli", default=os.environ.get("HERMES_CLI", str(Path.home() / ".local" / "bin" / "hermes")))
    parser.add_argument("--provider", default=DEFAULT_PROVIDER)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--timeout-seconds", type=int, default=240)
    parser.add_argument("--total-timeout-seconds", type=int, default=270)
    parser.add_argument("--cleanup-reserve-seconds", type=int, default=30)
    parser.add_argument("--failure-cooldown-seconds", type=int, default=1800)
    parser.add_argument("--stale-after-minutes", type=int, default=180)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--emit-reachout", action="store_true")
    parser.add_argument("--json", action="store_true", dest="print_json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.emit_reachout and args.print_json:
        raise SystemExit("--emit-reachout and --json are mutually exclusive")
    try:
        result = run_once(args)
    except Exception as exc:
        result = {"success": False, "action": "conscious_clock_failed", "reason": str(exc)[:800]}
    if args.emit_reachout:
        store = SensoriumStore(instance=args.instance, state_dir=args.state_dir)
        print(scheduler_output(result, store.read_jsonl("outbox")), end="")
    elif args.print_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    elif not result.get("success"):
        print(f"Sensorium Conscious clock failed: {result.get('reason', 'unknown error')}", file=sys.stderr)
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
