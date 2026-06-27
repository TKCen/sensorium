#!/usr/bin/env python3
"""Bounded live-turn review probe.

This is a deterministic/manual harness: it reviews only supplied turn snippets and
optionally appends a transcript-free live_turn.review_decision receipt. It never
reads full session transcripts and never creates Sensorium signals.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_sensorium.config import default_instance_name  # noqa: E402
from agent_sensorium.live_turn import build_turn_review_receipt, review_turn_for_residue  # noqa: E402
from agent_sensorium.store import SensoriumStore  # noqa: E402


def _csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bounded Sensorium live-turn review probe")
    parser.add_argument("--instance", default=default_instance_name())
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--user-text", default="")
    parser.add_argument("--assistant-text", default="")
    parser.add_argument("--tool-actions", default="", help="Comma-separated compact tool/action labels")
    parser.add_argument("--memory-written", action="store_true")
    parser.add_argument("--skill-updated", action="store_true")
    parser.add_argument("--docs-updated", action="store_true")
    parser.add_argument("--sensorium-ingested", action="store_true")
    parser.add_argument("--patch-or-artifact-written", action="store_true")
    parser.add_argument("--explicit-no-action", action="store_true")
    parser.add_argument("--append-receipt", action="store_true", help="Append the safe review receipt to decisions.jsonl")
    args = parser.parse_args(argv)

    review = review_turn_for_residue(
        user_text=args.user_text,
        assistant_text=args.assistant_text,
        tool_actions=_csv(args.tool_actions),
        memory_written=args.memory_written,
        skill_updated=args.skill_updated,
        docs_updated=args.docs_updated,
        sensorium_ingested=args.sensorium_ingested,
        patch_or_artifact_written=args.patch_or_artifact_written,
        explicit_no_action=args.explicit_no_action,
    )
    receipt = build_turn_review_receipt(review=review)
    appended = False
    if args.append_receipt:
        store = SensoriumStore(instance=args.instance, state_dir=args.state_dir)
        store.ensure_dirs()
        store.append_jsonl("decisions", receipt)
        appended = True

    print(json.dumps({
        "success": True,
        "instance": args.instance,
        "appended": appended,
        "review": review,
        "receipt": receipt,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
