#!/usr/bin/env python3
"""Open and settle a bounded Conscious aperture.

Scope: deterministic Conscious wake plumbing. The runner opens at most one bounded
aperture over internal conscious_task candidates, prints the packet Conscious
should inspect, and optionally applies explicit settlement records. Worker
requests remain untouched by this runner.

Settlement file shape: one JSON object or a list of objects accepted by
sensorium_conscious_aperture_settle.py:

    {
      "candidate_id": "cand_...",
      "aperture_id": "cap_...",  # optional; inferred from candidate when absent
      "decision": "REVIEWED | HELD | SETTLED | PREPARED_EXTERNAL_WORK",
      "reason": "short Conscious decision rationale",
      "return_at": "2026-06-07T13:00:00Z",  # optional; HELD-only future UTC checkpoint
      "external_work": { ... }     # optional recorded spec; no dispatch
    }
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_sensorium.conscious_aperture import (  # noqa: E402
    open_conscious_aperture,
    settle_conscious_aperture_item,
)
from agent_sensorium.store import SensoriumStore  # noqa: E402


def _load_settlements(path: str | None) -> list[dict]:
    if not path:
        return []
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, list):
        return [record for record in raw if isinstance(record, dict)]
    raise SystemExit("settlements file must contain a JSON object or list of objects")


def _count_worker_requests(store: SensoriumStore) -> int:
    return len(store.read_jsonl("worker_requests"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--instance", default="default")
    ap.add_argument(
        "--state-dir",
        default=None,
        help="Override Sensorium state dir (defaults to ~/.hermes/agent-sensorium/<instance>)",
    )
    ap.add_argument("--aperture-size", type=int, default=3)
    ap.add_argument("--max-active-sessions", type=int, default=1)
    ap.add_argument("--stale-after-minutes", type=int, default=180)
    ap.add_argument("--open", action="store_true", help="Persist the aperture open transition; default previews only")
    ap.add_argument("--settlements", help="JSON settlement object/list to preview or apply")
    ap.add_argument("--apply-settlements", action="store_true", help="Persist settlement records from --settlements")
    ap.add_argument("--now", default=None, help="Testing override timestamp")
    ap.add_argument("--json", action="store_true", help="Print JSON; default also prints JSON for now")
    args = ap.parse_args()

    store = SensoriumStore(instance=args.instance, state_dir=args.state_dir)
    store.ensure_dirs()
    worker_requests_before = _count_worker_requests(store)

    aperture_packet = open_conscious_aperture(
        store,
        aperture_size=args.aperture_size,
        max_active_sessions=args.max_active_sessions,
        stale_after_minutes=args.stale_after_minutes,
        dry_run=not args.open,
        now=args.now,
    )

    settlement_records = _load_settlements(args.settlements)
    settlement_results: list[dict[str, Any]] = []
    for record in settlement_records:
        settlement_results.append(settle_conscious_aperture_item(
            store,
            candidate_id=record.get("candidate_id", ""),
            aperture_id=record.get("aperture_id"),
            decision=record.get("decision", ""),
            reason=record.get("reason", ""),
            return_at=record.get("return_at"),
            external_work=record.get("external_work"),
            dry_run=not args.apply_settlements,
            now=args.now,
        ))

    worker_requests_after = _count_worker_requests(store)
    has_aperture_items = bool(aperture_packet.get("aperture"))
    output = {
        "success": bool(aperture_packet.get("success")) and all(r.get("success") for r in settlement_results),
        "action": "conscious_wake_tick",
        "instance": args.instance,
        "opened": bool(args.open),
        "aperture": aperture_packet,
        "settlements": {
            "provided": len(settlement_records),
            "applied": sum(1 for r in settlement_results if r.get("action") == "settled_aperture_item"),
            "would_apply": sum(1 for r in settlement_results if r.get("action") == "would_settle_aperture_item"),
            "already_settled": sum(1 for r in settlement_results if r.get("action") == "already_settled"),
            "results": settlement_results,
        },
        "worker_requests": {
            "before": worker_requests_before,
            "after": worker_requests_after,
            "delta": worker_requests_after - worker_requests_before,
        },
        "next_action": (
            "settle_aperture_items"
            if has_aperture_items and not settlement_records
            else "idle" if not has_aperture_items else "review_settlement_results"
        ),
        "settlement_record_shape": {
            "candidate_id": "cand_...",
            "aperture_id": "optional cap_...",
            "decision": "REVIEWED | HELD | SETTLED | PREPARED_EXTERNAL_WORK",
            "reason": "short Conscious decision rationale",
            "return_at": "optional future UTC-Z checkpoint for HELD",
            "external_work": "optional prepared spec",
        },
    }
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0 if output["success"] and output["worker_requests"]["delta"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
