"""Receipt normalization and graph-link tests for decisions.jsonl."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

from agent_sensorium.settlement import apply_kanban_settlement, candidate_ref_label, evidence_ref_label
from agent_sensorium.store import SensoriumStore


SENTINEL = "sk-receipt-raw-transcript-123456"


@pytest.fixture
def store(tmp_path):
    state_dir = tmp_path / "sensorium"
    s = SensoriumStore(instance="test", state_dir=str(state_dir))
    s.ensure_dirs()
    return s


def _candidate(candidate_id="cand_receipt_1"):
    return {
        "id": candidate_id,
        "status": "candidate",
        "kind": "subconscious_advisory",
        "pressure": 0.7,
        "summary": "compact candidate summary",
        "event_ids": ["evt_receipt_1"],
        "correlation_keys": ["receipt-key"],
        "fingerprint": "fp_receipt_1",
        "sensitivity": "private",
        "allowed_surfaces": ["local", "dashboard"],
        "created_at": "2026-06-20T10:00:00Z",
        "updated_at": "2026-06-20T10:00:00Z",
    }


def _load_dashboard_api():
    path = Path(__file__).parent.parent / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("agent_sensorium_dashboard_api_receipt_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_settlement_receipt_is_normalized_compact_and_idempotent(store):
    store.append_jsonl("candidates", _candidate())

    first = apply_kanban_settlement(
        store,
        decision="SAVE",
        candidate_id="cand_receipt_1",
        intake_task_id="kt_intake_receipt_1",
        review_task_id="kt_review_receipt_1",
        reason=f"operator pasted raw transcript {SENTINEL}",
    )
    second = apply_kanban_settlement(
        store,
        decision="SAVE",
        candidate_id="cand_receipt_1",
        intake_task_id="kt_intake_receipt_1",
        review_task_id="kt_review_receipt_1",
        reason=f"operator pasted raw transcript {SENTINEL}",
    )

    assert first["action"] == "settled"
    assert second["action"] == "already_settled"
    receipts = [r for r in store.read_jsonl("decisions") if r.get("type") == "kanban.settlement.applied"]
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt["schema"] == "sensorium.decision_receipt.v1"
    assert receipt["receipt_kind"] == "settlement"
    assert receipt["subject_ref"] == {"type": "candidate", "id": candidate_ref_label("cand_receipt_1")}
    assert "cand_receipt_1" not in json.dumps(receipt, sort_keys=True)
    assert receipt["decision"] == "SAVE"
    assert receipt["outcome"] == "reviewed"
    assert receipt["decided_by"]["kind"] == "kanban_review"
    assert receipt["created_at"] == receipt["ts"]
    assert receipt["idempotency_key"].startswith("receipt:")
    assert receipt["surface"] == "kanban"
    assert receipt["sensitivity"] == "private"
    assert receipt["allowed_surfaces"] == ["local", "dashboard"]
    assert {ref["type"] for ref in receipt["evidence_refs"]} >= {"candidate", "event", "intake_task", "review_task"}
    assert SENTINEL not in json.dumps(receipt, sort_keys=True)
    assert "raw transcript" not in json.dumps(receipt, sort_keys=True)


def test_unresolved_settlement_attempt_also_has_single_normalized_receipt(store):
    first = apply_kanban_settlement(
        store,
        decision="DROP",
        candidate_id="missing_candidate",
        event_id="evt_missing",
        intake_task_id="kt_intake_missing",
        reason=f"failed to match raw log {SENTINEL}",
    )
    second = apply_kanban_settlement(
        store,
        decision="DROP",
        candidate_id="missing_candidate",
        event_id="evt_missing",
        intake_task_id="kt_intake_missing",
        reason=f"failed to match raw log {SENTINEL}",
    )

    assert first["action"] == "no_candidate_match"
    assert second["action"] == "no_candidate_match"
    receipts = [r for r in store.read_jsonl("decisions") if r.get("type") == "kanban.settlement.unresolved"]
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt["schema"] == "sensorium.decision_receipt.v1"
    assert receipt["outcome"] == "unresolved"
    assert receipt["subject_ref"] == {"type": "candidate", "id": candidate_ref_label("missing_candidate")}
    assert receipt["idempotency_key"].startswith("receipt:")
    assert SENTINEL not in json.dumps(receipt, sort_keys=True)
    assert "raw log" not in json.dumps(receipt, sort_keys=True)
    assert "missing_candidate" not in json.dumps(receipt, sort_keys=True)


def test_dashboard_graph_projects_receipt_links_without_raw_content(store, monkeypatch):
    store.append_jsonl("candidates", _candidate())
    apply_kanban_settlement(
        store,
        decision="DROP",
        candidate_id="cand_receipt_1",
        intake_task_id="kt_intake_receipt_1",
        review_task_id="kt_review_receipt_1",
        reason=f"raw transcript {SENTINEL}",
    )

    api = _load_dashboard_api()
    monkeypatch.setattr(api, "DEFAULT_ROOT", Path(store.root))
    monkeypatch.setattr(api, "DEFAULT_INSTANCE", "test")

    data = asyncio.run(api.graph())

    assert data["ok"] is True
    assert data["meta"]["privacy"] == "compact_only"
    receipt_nodes = [n for n in data["nodes"] if n.get("kind") == "receipt"]
    candidate_nodes = [n for n in data["nodes"] if n.get("kind") == "candidate"]
    assert len(receipt_nodes) == 1
    assert receipt_nodes[0]["receipt_type"] == "kanban.settlement.applied"
    assert receipt_nodes[0]["decision"] == "DROP"
    assert any(e for e in data["edges"] if e.get("kind") == "settles" and e.get("to") == receipt_nodes[0]["id"])
    assert candidate_nodes[0]["id"] == f"candidate:{candidate_ref_label('cand_receipt_1')}"
    assert "cand_receipt_1" not in json.dumps(data, sort_keys=True)


@pytest.mark.parametrize(
    "field",
    ["candidate_id", "event_id", "fingerprint", "correlation_keys", "review_task_id", "candidate_kind"],
)
def test_secret_shaped_join_scalars_never_leak_to_decisions_or_graph(store, monkeypatch, field):
    """Adversarial regression for the R8 finding: secret-shaped join scalars
    (candidate id, event id, fingerprint, correlation key, review task id,
    candidate kind) must never appear raw in decisions.jsonl or GET /graph,
    even though they are needed internally to resolve/join the candidate."""
    sentinel = "sk-secret-join-scalar-987654"
    candidate_id = sentinel if field == "candidate_id" else "cand_secret_safe"
    event_id = sentinel if field == "event_id" else "evt_secret_safe"
    fingerprint = sentinel if field == "fingerprint" else "fp_secret_safe"
    correlation_keys = [sentinel] if field == "correlation_keys" else ["ck_secret_safe"]
    review_task_id = sentinel if field == "review_task_id" else "kt_review_secret_safe"
    candidate_kind = sentinel if field == "candidate_kind" else "subconscious_advisory"

    store.append_jsonl(
        "candidates",
        {
            "id": candidate_id,
            "status": "candidate",
            "kind": candidate_kind,
            "pressure": 0.7,
            "summary": "compact candidate summary",
            "event_ids": [event_id],
            "correlation_keys": correlation_keys,
            "fingerprint": fingerprint,
            "sensitivity": "private",
            "allowed_surfaces": ["local"],
            "created_at": "2026-06-20T10:00:00Z",
            "updated_at": "2026-06-20T10:00:00Z",
        },
    )
    apply_kanban_settlement(
        store,
        decision="SAVE",
        candidate_id=candidate_id,
        event_id=event_id,
        fingerprint=fingerprint,
        correlation_keys=correlation_keys,
        intake_task_id="kt_intake_secret_safe",
        review_task_id=review_task_id,
        reason="safe review reason",
    )

    decisions_json = json.dumps(store.read_jsonl("decisions"), sort_keys=True)
    assert sentinel not in decisions_json

    api = _load_dashboard_api()
    monkeypatch.setattr(api, "DEFAULT_ROOT", Path(store.root))
    monkeypatch.setattr(api, "DEFAULT_INSTANCE", "test")
    graph_json = json.dumps(asyncio.run(api.graph()), sort_keys=True)
    assert sentinel not in graph_json


def test_secret_shaped_conscious_task_ref_never_leaks_to_decisions_graph_or_snapshot(store, monkeypatch):
    """PROMOTE_CONSCIOUS with secret-shaped conscious_task_ref subfields.

    Regression for the lead's continuation finding: `conscious_task_ref` is
    itself a corruptible/secret-shaped-id-bearing structure (task_id,
    thread_id, board, kanban_task_id, candidate_id, conscious_task_id all
    originate from the Conscious-promotion bridge caller). Every one of those
    subfields must be absent, raw, from decisions.jsonl, GET /graph, and the
    GET /snapshot perception-trace settlement projection.
    """
    sentinel = "sk-conscious...3456"
    store.append_jsonl("candidates", _candidate(candidate_id="cand_conscious_safe"))
    conscious_ref = {
        "task_id": sentinel,
        "thread_id": sentinel,
        "board": sentinel,
        "kanban_task_id": sentinel,
        "candidate_id": sentinel,
        "conscious_task_id": sentinel,
        "kind": "internal_conscious_task_candidate",
        "promoted_at": "2026-06-20T10:00:00Z",
    }
    apply_kanban_settlement(
        store,
        decision="PROMOTE_CONSCIOUS",
        candidate_id="cand_conscious_safe",
        intake_task_id="kt_intake_conscious_safe",
        review_task_id="kt_review_conscious_safe",
        conscious_task_ref=conscious_ref,
        reason="promote into bounded Conscious aperture",
    )

    decisions_json = json.dumps(store.read_jsonl("decisions"), sort_keys=True)
    assert sentinel not in decisions_json

    api = _load_dashboard_api()
    monkeypatch.setattr(api, "DEFAULT_ROOT", Path(store.root))
    monkeypatch.setattr(api, "DEFAULT_INSTANCE", "test")

    graph_json = json.dumps(asyncio.run(api.graph()), sort_keys=True)
    assert sentinel not in graph_json

    snapshot_data = asyncio.run(api.snapshot(instance="test"))
    assert sentinel not in json.dumps(snapshot_data, sort_keys=True)


def test_legacy_receipt_subject_ref_is_sanitized_in_graph_and_snapshot(store, monkeypatch):
    """Legacy/corrupt normalized receipts may carry raw subject_ref scalars.

    GET projections must fail closed: do not trust a persisted subject_ref.id or
    subject_ref.type as already safe unless it is demonstrably an opaque label or
    closed-vocabulary type.
    """
    raw_subject_id = "***...4242"
    raw_subject_type = "sk-subject-type...4242"
    store.append_jsonl(
        "decisions",
        {
            "schema": "sensorium.decision_receipt.v1",
            "receipt_kind": "settlement",
            "ts": "2026-06-20T10:00:00Z",
            "created_at": "2026-06-20T10:00:00Z",
            "type": "kanban.settlement.applied",
            "subject_ref": {"type": raw_subject_type, "id": raw_subject_id},
            "decision": "SAVE",
            "outcome": "reviewed",
            "idempotency_key": "receipt:subject-ref-regression",
            "surface": "kanban",
            "allowed_surfaces": ["local"],
            "sensitivity": "private",
            "raw_content": False,
            "evidence_refs": [{"type": "candidate", "ref": "candidate#safe"}],
            "reason_label": "reason#safe",
        },
    )

    api = _load_dashboard_api()
    monkeypatch.setattr(api, "DEFAULT_ROOT", Path(store.root))
    monkeypatch.setattr(api, "DEFAULT_INSTANCE", "test")

    graph_data = asyncio.run(api.graph(instance="test"))
    snapshot_data = asyncio.run(api.snapshot(instance="test"))
    graph_json = json.dumps(graph_data, sort_keys=True)
    snapshot_json = json.dumps(snapshot_data, sort_keys=True)

    assert raw_subject_id not in graph_json
    assert raw_subject_id not in snapshot_json
    assert raw_subject_type not in graph_json
    assert raw_subject_type not in snapshot_json
    safe_subject_id = candidate_ref_label(raw_subject_id)
    assert any(node.get("id") == f"candidate:{safe_subject_id}" for node in graph_data["nodes"])
    receipt_nodes = [node for node in graph_data["nodes"] if node.get("kind") == "receipt"]
    assert receipt_nodes[0]["subject_ref"] == {"type": "unknown", "id": safe_subject_id}
    assert snapshot_data["decisions"][0]["subject_ref"] == {"type": "unknown", "id": safe_subject_id}


def test_legacy_receipt_evidence_refs_are_sanitized_in_graph_and_snapshot(store, monkeypatch):
    """Legacy/corrupt evidence refs must not be echoed raw by GET surfaces."""
    raw_refs = {
        "candidate": "RAW_CANDIDATE_SECRET_SK_4242",
        "event": "RAW_EVENT_SECRET_SK_4242",
        "fingerprint": "RAW_FINGERPRINT_SECRET_SK_4242",
        "correlation": "RAW_CORRELATION_SECRET_SK_4242",
        "review_task": "RAW_REVIEW_TASK_SECRET_SK_4242",
    }
    store.append_jsonl(
        "decisions",
        {
            "schema": "sensorium.decision_receipt.v1",
            "receipt_kind": "settlement",
            "ts": "2026-06-20T10:00:00Z",
            "created_at": "2026-06-20T10:00:00Z",
            "type": "kanban.settlement.applied",
            "subject_ref": {"type": "candidate", "id": candidate_ref_label("safe-evidence-subject")},
            "decision": "SAVE",
            "outcome": "reviewed",
            "idempotency_key": "receipt:evidence-ref-regression",
            "surface": "kanban",
            "allowed_surfaces": ["local"],
            "sensitivity": "private",
            "raw_content": False,
            "evidence_refs": [
                {"type": ref_type, "ref": raw_ref}
                for ref_type, raw_ref in raw_refs.items()
            ] + [
                "RAW_EVIDENCE_SCALAR_SECRET_SK_4242",
                {"type": "RAW_REF_TYPE_SECRET_SK_4242", "ref": "RAW_UNKNOWN_REF_SECRET_SK_4242"},
            ],
            "reason_label": "reason#safe",
        },
    )

    api = _load_dashboard_api()
    monkeypatch.setattr(api, "DEFAULT_ROOT", Path(store.root))
    monkeypatch.setattr(api, "DEFAULT_INSTANCE", "test")

    graph_data = asyncio.run(api.graph(instance="test"))
    snapshot_data = asyncio.run(api.snapshot(instance="test"))
    graph_json = json.dumps(graph_data, sort_keys=True)
    snapshot_json = json.dumps(snapshot_data, sort_keys=True)

    for raw_ref in list(raw_refs.values()) + [
        "RAW_EVIDENCE_SCALAR_SECRET_SK_4242",
        "RAW_REF_TYPE_SECRET_SK_4242",
        "RAW_UNKNOWN_REF_SECRET_SK_4242",
    ]:
        assert raw_ref not in graph_json
        assert raw_ref not in snapshot_json

    receipt_nodes = [node for node in graph_data["nodes"] if node.get("kind") == "receipt"]
    assert len(receipt_nodes) == 1
    assert receipt_nodes[0]["evidence_refs"] == [
        {"type": ref_type, "ref": evidence_ref_label(ref_type, raw_ref)}
        for ref_type, raw_ref in raw_refs.items()
    ]


def test_legacy_receipt_candidate_id_and_idempotency_key_are_sanitized(store, monkeypatch):
    """Legacy/corrupt receipt join scalars must not leak through GET surfaces."""
    raw_candidate_id = "sk-leg...date...4242"
    raw_idempotency_key = "skidem4242"
    store.append_jsonl(
        "decisions",
        {
            "schema": "sensorium.decision_receipt.v1",
            "receipt_kind": "settlement",
            "ts": "2026-06-20T10:00:00Z",
            "created_at": "2026-06-20T10:00:00Z",
            "type": "kanban.settlement.applied",
            "subject_ref": {"type": "candidate", "id": "safe-subject-for-hash"},
            "candidate_id": raw_candidate_id,
            "decision": "SAVE",
            "outcome": "reviewed",
            "idempotency_key": f"receipt:{raw_idempotency_key}",
            "surface": "kanban",
            "allowed_surfaces": ["local"],
            "sensitivity": "private",
            "raw_content": False,
            "evidence_refs": [{"type": "candidate", "ref": "candidate#safe"}],
            "reason_label": "reason#safe",
        },
    )

    api = _load_dashboard_api()
    monkeypatch.setattr(api, "DEFAULT_ROOT", Path(store.root))
    monkeypatch.setattr(api, "DEFAULT_INSTANCE", "test")

    snapshot_data = asyncio.run(api.snapshot(instance="test"))
    graph_data = asyncio.run(api.graph(instance="test"))
    snapshot_json = json.dumps(snapshot_data, sort_keys=True)
    graph_json = json.dumps(graph_data, sort_keys=True)

    assert raw_candidate_id not in snapshot_json
    assert raw_candidate_id not in graph_json
    assert raw_idempotency_key not in graph_json
    assert snapshot_data["decisions"][0]["candidate_id"] == candidate_ref_label(raw_candidate_id)
    receipt_nodes = [node for node in graph_data["nodes"] if node.get("kind") == "receipt"]
    assert len(receipt_nodes) == 1
    assert receipt_nodes[0]["id"].startswith("receipt:")
    assert raw_idempotency_key not in receipt_nodes[0]["id"]


def test_graph_hash_labels_secret_shaped_surface_metadata(store, monkeypatch):
    sentinel = "api_key_graph_surface_8899"
    candidate_id = "cand_graph_surface"
    subject_label = candidate_ref_label(candidate_id)
    store.append_jsonl(
        "candidates",
        {
            **_candidate(candidate_id=candidate_id),
            "sensitivity": sentinel,
            "allowed_surfaces": [sentinel],
        },
    )
    store.append_jsonl(
        "decisions",
        {
            "schema": "sensorium.decision_receipt.v1",
            "receipt_kind": "settlement",
            "ts": "2026-06-20T10:00:00Z",
            "created_at": "2026-06-20T10:00:00Z",
            "type": "kanban.settlement.applied",
            "subject_ref": {"type": "candidate", "id": subject_label},
            "decision": "SAVE",
            "outcome": "reviewed",
            "idempotency_key": "receipt:graph-surface-regression",
            "surface": sentinel,
            "allowed_surfaces": [sentinel],
            "sensitivity": sentinel,
            "raw_content": False,
            "evidence_refs": [{"type": "candidate", "ref": "candidate#safe"}],
            "reason_label": "reason#safe",
        },
    )

    api = _load_dashboard_api()
    monkeypatch.setattr(api, "DEFAULT_ROOT", Path(store.root))
    monkeypatch.setattr(api, "DEFAULT_INSTANCE", "test")

    graph_data = asyncio.run(api.graph(instance="test"))
    graph_json = json.dumps(graph_data, sort_keys=True)

    assert sentinel not in graph_json
    candidate_node = next(node for node in graph_data["nodes"] if node.get("kind") == "candidate")
    receipt_node = next(node for node in graph_data["nodes"] if node.get("kind") == "receipt")
    assert candidate_node["sensitivity"].startswith("sensitivity#")
    assert candidate_node["allowed_surfaces"][0].startswith("surface#")
    assert receipt_node["surface"].startswith("surface#")
    assert receipt_node["sensitivity"].startswith("sensitivity#")
    assert receipt_node["allowed_surfaces"][0].startswith("surface#")
