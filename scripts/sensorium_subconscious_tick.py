#!/usr/bin/env python3
"""Event-gated Agent Sensorium Subconscious tick.

This is intentionally separate from deterministic sensor ticks. It acquires a
nonblocking per-instance lock, fingerprints source Events/Candidates, and only
runs the cheap model-backed Subconscious advisory when there is new source
material. Normal success/idle is silent for no-agent cron use; latest state is
written under the Sensorium instance directory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - WSL/Linux expected in production
    fcntl = None


def _add_import_path() -> None:
    here = Path(__file__).resolve()
    repo_root = here.parent.parent
    plugin_root = Path.home() / ".hermes" / "plugins" / "agent-sensorium"
    for root in (repo_root, plugin_root):
        if (root / "agent_sensorium").exists():
            sys.path.insert(0, str(root))
            return


_add_import_path()

from agent_sensorium.schemas import utc_now_iso  # noqa: E402
from agent_sensorium.store import SensoriumStore  # noqa: E402
from agent_sensorium.tools import handle_sensorium_subconscious_advisory  # noqa: E402


SOURCE_CANDIDATE_KINDS_EXCLUDED = {"subconscious_advisory"}
ADVISORY_POLICY_VERSION = "2026-05-26.2"


def _json_write(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True), encoding="utf-8")


def _json_read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _lock_nonblocking(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(lock_path, "w", encoding="utf-8")
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


def _source_material(store: SensoriumStore, *, event_limit: int, candidate_limit: int) -> dict:
    events = store.read_jsonl("events")[-event_limit:]
    candidates = [
        c for c in store.read_jsonl("candidates")
        if c.get("status", "candidate") == "candidate"
        and c.get("kind") not in SOURCE_CANDIDATE_KINDS_EXCLUDED
    ]
    candidates.sort(key=lambda c: (str(c.get("kind", "")), str(c.get("id", ""))))
    candidates = candidates[-candidate_limit:]
    return {
        "advisory_policy_version": ADVISORY_POLICY_VERSION,
        "events": [
            {
                "id": e.get("id"),
                "ts": e.get("ts"),
                "kind": e.get("kind"),
                "summary": e.get("summary"),
                "strength": e.get("strength"),
                "correlation_keys": e.get("correlation_keys") or [],
            }
            for e in events
        ],
        "source_candidates": [
            {
                "id": c.get("id"),
                "kind": c.get("kind"),
                "summary": c.get("summary"),
                "pressure": c.get("pressure"),
                "fingerprint": c.get("fingerprint"),
                "event_ids": c.get("event_ids") or [],
                "correlation_keys": c.get("correlation_keys") or [],
            }
            for c in candidates
        ],
    }


def _signature(material: dict) -> str:
    payload = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def run_once(args: argparse.Namespace) -> dict:
    store = SensoriumStore(instance=args.instance, state_dir=args.state_dir)
    store.ensure_dirs()
    state_path = store.root / "subconscious_tick_state.json"
    latest_path = store.root / "last_subconscious_tick.json"
    lock_path = store.root / "locks" / "subconscious_tick.lock"

    lock_file = _lock_nonblocking(lock_path)
    if lock_file is None:
        result = {
            "success": True,
            "action": "skipped_locked",
            "instance": args.instance,
            "ts": utc_now_iso(),
            "reason": "another subconscious tick is already running",
        }
        _json_write(latest_path, result)
        return result

    try:
        prior = _json_read(state_path)
        material = _source_material(store, event_limit=args.event_limit, candidate_limit=args.candidate_limit)
        sig = _signature(material)
        has_material = bool(material["events"] or material["source_candidates"])
        now_epoch = time.time()

        result_base = {
            "instance": args.instance,
            "ts": utc_now_iso(),
            "advisory_policy_version": ADVISORY_POLICY_VERSION,
            "source_signature": sig,
            "source_counts": {
                "events": len(material["events"]),
                "source_candidates": len(material["source_candidates"]),
            },
        }

        if not has_material:
            result = {**result_base, "success": True, "action": "skipped_empty"}
            _json_write(latest_path, result)
            _json_write(state_path, {**prior, "last_empty_at": result["ts"]})
            return result

        if not args.force and prior.get("last_processed_signature") == sig:
            result = {**result_base, "success": True, "action": "skipped_unchanged"}
            _json_write(latest_path, result)
            return result

        if (
            not args.force
            and prior.get("last_failed_signature") == sig
            and now_epoch - float(prior.get("last_failed_epoch", 0)) < args.failure_cooldown_seconds
        ):
            result = {
                **result_base,
                "success": True,
                "action": "skipped_failure_cooldown",
                "cooldown_seconds": args.failure_cooldown_seconds,
            }
            _json_write(latest_path, result)
            return result

        advisory_config = {"model_enabled": True}
        if args.model:
            advisory_config["model"] = args.model
        if args.provider:
            advisory_config["model_provider"] = args.provider
        if args.base_url:
            advisory_config["model_base_url"] = args.base_url

        raw = json.loads(handle_sensorium_subconscious_advisory(
            instance=args.instance,
            state_dir=args.state_dir,
            dry_run=not args.enable_candidate_creation,
            enabled=True,
            config=advisory_config,
            record_receipt=True,
        ))
        data = raw.get("data") if raw.get("success") else None
        action = (data or {}).get("action")
        result = {
            **result_base,
            "success": bool(raw.get("success")),
            "action": "ran_advisory",
            "advisory_action": action,
            "model_used": (data or {}).get("model_used"),
            "model_provider": (data or {}).get("model_provider"),
            "model": (data or {}).get("model"),
            "candidate_id": (data or {}).get("candidate_id"),
            "dry_run": (data or {}).get("dry_run"),
            "reason": (data or {}).get("reason") or raw.get("error", ""),
        }
        _json_write(latest_path, result)

        next_state = {**prior, "last_attempt_signature": sig, "last_attempt_at": result["ts"]}
        if raw.get("success") and action != "model_unavailable":
            next_state.update({
                "last_processed_signature": sig,
                "last_processed_at": result["ts"],
                "last_failed_signature": None,
                "last_failed_epoch": None,
            })
        else:
            next_state.update({
                "last_failed_signature": sig,
                "last_failed_epoch": now_epoch,
                "last_failure_reason": result.get("reason", ""),
            })
        _json_write(state_path, next_state)
        return result
    finally:
        try:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Event-gated Agent Sensorium Subconscious tick")
    parser.add_argument("--instance", default="default")
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--event-limit", type=int, default=50)
    parser.add_argument("--candidate-limit", type=int, default=50)
    parser.add_argument("--failure-cooldown-seconds", type=int, default=900)
    parser.add_argument("--enable-candidate-creation", action="store_true", help="Allow internal subconscious_advisory candidates")
    parser.add_argument("--force", action="store_true", help="Ignore unchanged/failure cooldown gates")
    parser.add_argument("--json", action="store_true", dest="print_json", help="Print result JSON")
    parser.add_argument("--model", default=None)
    parser.add_argument("--provider", default=None)
    parser.add_argument("--base-url", default=None)
    args = parser.parse_args(argv)

    try:
        result = run_once(args)
    except Exception as exc:
        result = {"success": False, "action": "error", "ts": utc_now_iso(), "instance": args.instance, "error": str(exc)}
        try:
            store = SensoriumStore(instance=args.instance, state_dir=args.state_dir)
            store.ensure_dirs()
            _json_write(store.root / "last_subconscious_tick.json", result)
        except Exception:
            pass
        print(json.dumps(result, indent=2), file=sys.stderr)
        return 1

    if args.print_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
