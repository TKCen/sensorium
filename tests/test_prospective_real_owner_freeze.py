"""Real-owner 48-case freeze; no direct recorder calls or quota patches."""

from __future__ import annotations
import json
from datetime import datetime, timezone
from agent_sensorium import prospective_evidence
from agent_sensorium.conscious_aperture import (
    open_conscious_aperture,
    settle_conscious_aperture_item,
)
from agent_sensorium.prospective_evidence import ProspectiveEvidenceCapture
from agent_sensorium.store import SensoriumStore
from agent_sensorium.tools import handle_sensorium_ingest_signal

CONTROL = {
    "enabled": True,
    "start_at": "2026-01-01T00:00:00Z",
    "expires_at": "2026-01-15T00:00:00Z",
}


def signal(identifier, source, **extra):
    return {
        "id": identifier,
        "ts": "2026-01-02T00:00:00Z",
        "sensor": "fixture",
        "source": source,
        "kind": "note",
        "summary": "canonical structured fixture",
        "strength_hint": 0.2,
        "correlation_keys": [identifier],
        **extra,
    }


def candidate(identifier, kind, sources=()):
    return {
        "id": identifier,
        "status": "candidate",
        "kind": kind,
        "pressure": 0.8,
        "summary": "canonical precondition",
        "event_ids": [],
        "source_candidate_ids": list(sources),
        "correlation_keys": [],
        "sensitivity": "private",
        "allowed_surfaces": ["local"],
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "conscious_task": {
            "id": "ctask-" + identifier,
            "request_type": "THINK",
            "title": "fixture",
            "why": "fixture",
            "expected_decision": "decide",
        },
    }


def test_actual_production_owners_freeze_exact_sufficient_48(tmp_path, monkeypatch):
    root = tmp_path / "state"
    config = {"prospective_evidence_capture": CONTROL}
    root.mkdir()
    (root / "instance.config.json").write_text(json.dumps(config))
    capture = ProspectiveEvidenceCapture(root, CONTROL)
    assert capture.activate()
    store = SensoriumStore(instance="fixture", state_dir=str(root))
    store.ensure_dirs()
    for i in range(6):
        store.append_jsonl("candidates", candidate(f"origin-{i}", "external_evidence"))
        store.append_jsonl("candidates", candidate(f"zz-external-{i}", "external_evidence"))
        store.append_jsonl("candidates", candidate(f"yy-creative-{i}", "creative_pull"))
    monkeypatch.setattr(
        prospective_evidence,
        "_now",
        lambda value=None: (
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            if value
            else datetime(2026, 1, 2, tzinfo=timezone.utc)
        ),
    )
    conscious_ids = []
    for i in range(12):
        manual = json.loads(
            handle_sensorium_ingest_signal(
                signal=signal(
                    f"manual-{i}",
                    "manual",
                    transition="state_changed",
                    strength_hint=0.91 if i < 6 else 0.2,
                ),
                instance="fixture",
                state_dir=str(root),
                config=config,
            )
        )
        assert manual["success"]
        if i < 6:
            conscious_ids.append(manual["data"]["candidate_id"])
        assert json.loads(
            handle_sensorium_ingest_signal(
                signal=signal(f"session-{i}", "hermes_session", transition="state_changed"),
                instance="fixture",
                state_dir=str(root),
                config=config,
            )
        )["success"]
        assert json.loads(
            handle_sensorium_ingest_signal(
                signal=signal(f"live-{i}", "machine", liveness=True),
                instance="fixture",
                state_dir=str(root),
                config=config,
            )
        )["success"]
    for i in range(6):
        assert json.loads(
            handle_sensorium_ingest_signal(
                signal=signal(f"correction-{i}", "artifact", kind="explicit_correction"),
                instance="fixture",
                state_dir=str(root),
                config=config,
            )
        )["success"]
        feedback = signal(
            f"feedback-{i}",
            "feedback",
            kind="task_result",
            caused_by={"action_id": f"action-{i}", "origin_candidate_id": f"origin-{i}"},
            outcome="completed",
            feedback_scope="system_action",
        )
        assert json.loads(
            handle_sensorium_ingest_signal(
                signal=feedback, instance="fixture", state_dir=str(root), config=config
            )
        )["success"]
    rewritten = []
    for row in store.read_jsonl("candidates"):
        if row.get("id") in conscious_ids:
            row = dict(row)
            row["kind"] = "subconscious_advisory"
            row["source_candidate_ids"] = [
                f"zz-external-{conscious_ids.index(row['id'])}",
                f"yy-creative-{conscious_ids.index(row['id'])}",
            ]
            row["conscious_task"] = candidate("x", "subconscious_advisory")["conscious_task"]
        rewritten.append(row)
    store.rewrite_jsonl("candidates", rewritten)
    for i in range(6):
        opened = open_conscious_aperture(
            store, aperture_size=1, dry_run=False, now=f"2026-01-02T0{i}:00:00Z"
        )
        assert opened["candidate_ids"] and opened["candidate_ids"][0] in conscious_ids
        item = opened["aperture"][0]
        assert settle_conscious_aperture_item(
            store,
            candidate_id=item["candidate_id"],
            aperture_id=item["aperture_id"],
            consumer_id=item["consumer_id"],
            decision="SETTLED",
            reason="fixture",
            dry_run=False,
            now=f"2026-01-02T0{i}:05:00Z",
        )["success"]
    assert prospective_evidence._wait_for_observation_queue(10)
    result = capture.closeout(now="2026-01-15T00:00:00Z")
    assert result["verdict"] == "SUFFICIENT_PRIVATE_EVIDENCE", result
    counts = json.loads(capture.manifest_path.read_text())["counts"]
    assert (
        result["counts"]["total"] == 48 and counts["material"] >= 24 and counts["no_effect"] >= 12
    )
    assert (
        counts["dependence"] >= 6
        and counts["cross_domain"] >= 6
        and counts["contradiction_retraction"] >= 6
        and counts["mixed_attention"] >= 6
    )
    assert (
        counts["source_classes"] >= 4
        and counts["largest_source_class"] <= 16
        and counts["unknown"] <= 12
    )
