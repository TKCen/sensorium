"""Owner-seam tests for the detached prospective recorder.

These tests deliberately patch only the recorder hook.  The canonical owner
return values and every canonical JSONL byte must remain identical either way.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_sensorium import conscious_aperture, sensors, tools
from agent_sensorium.conscious_aperture import open_conscious_aperture
from agent_sensorium.prospective_evidence import (
    ProspectiveEvidenceCapture,
    attention_class_for_candidate_kind,
    attention_snapshot_evidence,
    correction_stage_for_signal_kind,
)
from agent_sensorium.store import SensoriumStore


def _candidate(candidate_id: str, *, kind: str = "subconscious_advisory", sources=None) -> dict:
    return {
        "id": candidate_id,
        "status": "candidate",
        "kind": kind,
        "pressure": 0.8,
        "summary": "opaque test summary mentioning correction external creative",
        "event_ids": [],
        "source_candidate_ids": list(sources or []),
        "correlation_keys": ["not-a-kind:external"],
        "sensitivity": "private",
        "allowed_surfaces": ["local"],
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "conscious_task": {
            "id": f"ctask-{candidate_id}",
            "request_type": "THINK",
            "title": "Task",
            "why": "why",
            "expected_decision": "decide",
        },
    }


def _canonical_bytes(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes() for path in sorted(root.rglob("*.jsonl"))
    }


@pytest.mark.parametrize(
    ("kind", "stage"),
    [
        ("explicit_correction", "correction"),
        ("correction", "correction"),
        ("user_correction", "correction"),
        ("retraction", "retraction"),
    ],
)
def test_structured_correction_owner_uses_only_exact_kind(tmp_path, monkeypatch, kind, stage):
    calls = []
    monkeypatch.setattr(tools, "observe_after_success", lambda *args: calls.append(args[2:]))
    result = json.loads(
        tools.handle_sensorium_ingest_signal(
            signal={
                "sensor": "test",
                "source": "manual",
                "kind": kind,
                "summary": "ordinary prose, not a parser",
                "strength_hint": 0.2,
            },
            instance="test",
            state_dir=str(tmp_path / kind),
            config={},
        )
    )
    assert result["success"]
    assert [call[0] for call in calls] == ["source_observed", stage]
    evidence = calls[-1][1]
    assert evidence["contradiction_evidence"] is (stage == "correction")
    assert evidence["retraction_evidence"] is (stage == "retraction")


def test_correction_does_not_infer_from_text_or_unsupported_kind(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(tools, "observe_after_success", lambda *args: calls.append(args[2:]))
    result = json.loads(
        tools.handle_sensorium_ingest_signal(
            signal={
                "sensor": "test",
                "source": "manual",
                "kind": "not_a_correction",
                "summary": "this is a correction and retraction",
                "strength_hint": 0.2,
            },
            instance="test",
            state_dir=str(tmp_path / "unknown"),
            config={},
        )
    )
    assert result["success"]
    assert [call[0] for call in calls] == ["source_observed"]
    assert correction_stage_for_signal_kind("explicit_correction ") is None
    assert correction_stage_for_signal_kind("User_Correction") is None
    assert correction_stage_for_signal_kind("user_correction in prose") is None
    assert correction_stage_for_signal_kind("retraction in prose") is None


def test_exact_attention_mapping_and_same_window_mixed_rule():
    expected = {
        "relational_salience": "relational",
        "embodiment_insight": "embodied",
        "creative_pull": "creative",
        "mnemonic": "mnemonic_identity",
        "identity": "mnemonic_identity",
        "design_insight": "operational",
        "durable_importance": "operational",
        "explicit_correction": "operational",
        "user_correction": "operational",
        "external_evidence": "external",
    }
    assert {kind: attention_class_for_candidate_kind(kind) for kind in expected} == expected
    assert attention_class_for_candidate_kind("external_evidence in summary") == "unknown"
    selected = [_candidate("advisory", sources=["external", "creative"])]
    transaction = [
        *selected,
        _candidate("external", kind="external_evidence"),
        _candidate("creative", kind="creative_pull"),
    ]
    assert attention_snapshot_evidence(selected, transaction) == {
        "attention_classes": ["creative", "external", "unknown"],
        "same_window_external_protected": True,
    }
    assert (
        attention_snapshot_evidence(
            selected, [*selected, _candidate("external", kind="external_evidence")]
        )["same_window_external_protected"]
        is False
    )


def test_real_operator_signal_default_is_exact_user_correction():
    signal = sensors.operator_signal(summary="ordinary correction")
    assert signal["kind"] == "user_correction"
    assert correction_stage_for_signal_kind(signal["kind"]) == "correction"
    assert attention_class_for_candidate_kind(signal["kind"]) == "operational"


def test_real_conscious_open_emits_one_post_success_bounded_snapshot(tmp_path, monkeypatch):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "state"))
    store.ensure_dirs()
    advisory = _candidate("advisory", sources=["external", "creative"])
    store.append_jsonl("candidates", advisory)
    store.append_jsonl("candidates", _candidate("external", kind="external_evidence"))
    store.append_jsonl("candidates", _candidate("creative", kind="creative_pull"))
    calls = []
    monkeypatch.setattr(
        conscious_aperture, "observe_after_success", lambda *args: calls.append(args[2:])
    )
    result = open_conscious_aperture(
        store, aperture_size=1, dry_run=False, now="2026-01-01T01:00:00Z"
    )
    assert result["action"] == "opened_aperture"
    assert [call[0] for call in calls] == ["opened", "attention_snapshot"]
    assert calls[-1][1] == {
        "candidate_id": "advisory",
        "attention_classes": ["creative", "external", "unknown"],
        "same_window_external_protected": True,
    }
    assert store.read_jsonl("candidates")[0]["status"] == "in_conscious_aperture"


def test_attention_snapshot_stays_bound_to_open_transaction_after_unlock(tmp_path, monkeypatch):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "state"))
    store.ensure_dirs()
    advisory = _candidate("advisory", sources=["external", "creative"])
    store.append_jsonl("candidates", advisory)
    store.append_jsonl("candidates", _candidate("external", kind="external_evidence"))
    store.append_jsonl("candidates", _candidate("creative", kind="creative_pull"))
    calls = []

    def mutate_after_open(root, config, stage, evidence):
        calls.append((stage, evidence))
        if stage == "opened":
            # This mutation happens after the canonical opening lock is
            # released. It must not rewrite that opening's observer snapshot.
            rows = store.read_jsonl("candidates")
            for row in rows:
                if row.get("id") in {"external", "creative"}:
                    row["kind"] = "design_insight"
            store.rewrite_jsonl("candidates", rows)

    monkeypatch.setattr(conscious_aperture, "observe_after_success", mutate_after_open)
    result = open_conscious_aperture(
        store, aperture_size=1, dry_run=False, now="2026-01-01T01:00:00Z"
    )

    assert result["action"] == "opened_aperture"
    assert "_observer_attention_snapshot" not in result
    assert [stage for stage, _ in calls] == ["opened", "attention_snapshot"]
    assert calls[-1][1] == {
        "candidate_id": "advisory",
        "attention_classes": ["creative", "external", "unknown"],
        "same_window_external_protected": True,
    }


def test_owner_hook_failure_has_zero_canonical_influence_for_source_and_conscious_open(
    tmp_path, monkeypatch
):
    # Accepted non-promoted source owner: return and canonical bytes are equal.
    signal = {
        "id": "sig-fixed",
        "ts": "2026-01-01T00:00:00Z",
        "sensor": "test",
        "source": "manual",
        "kind": "note",
        "summary": "low pressure",
        "strength_hint": 0.2,
    }
    baseline_root = tmp_path / "source-baseline"
    baseline = tools.handle_sensorium_ingest_signal(
        signal=signal, instance="test", state_dir=str(baseline_root), config={}
    )
    failure_root = tmp_path / "source-failure"
    monkeypatch.setattr(
        ProspectiveEvidenceCapture,
        "observe",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("recorder failed")),
    )
    failed = tools.handle_sensorium_ingest_signal(
        signal=signal, instance="test", state_dir=str(failure_root), config={}
    )
    assert failed == baseline
    assert _canonical_bytes(failure_root) == _canonical_bytes(baseline_root)

    # Conscious open including both its opened and attention-snapshot hook paths.
    def setup(root):
        store = SensoriumStore(instance="test", state_dir=str(root))
        store.ensure_dirs()
        store.append_jsonl("candidates", _candidate("one"))
        return store

    baseline_store = setup(tmp_path / "open-baseline")
    baseline_result = open_conscious_aperture(
        baseline_store, aperture_size=1, dry_run=False, now="2026-01-01T01:00:00Z"
    )
    failed_store = setup(tmp_path / "open-failure")
    failed_result = open_conscious_aperture(
        failed_store, aperture_size=1, dry_run=False, now="2026-01-01T01:00:00Z"
    )
    # UUIDs differ, so compare the stable owner contract and each mutation shape.
    assert {
        k: baseline_result[k] for k in ("success", "action", "candidate_ids", "selected_count")
    } == {k: failed_result[k] for k in ("success", "action", "candidate_ids", "selected_count")}
    assert [row["status"] for row in baseline_store.read_jsonl("candidates")] == [
        row["status"] for row in failed_store.read_jsonl("candidates")
    ]
    assert (
        len(baseline_store.read_jsonl("decisions"))
        == len(failed_store.read_jsonl("decisions"))
        == 1
    )
