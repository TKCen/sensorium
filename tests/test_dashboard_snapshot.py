"""Tests for the read-only Agent Sensorium dashboard snapshot API."""

import asyncio
import importlib.util
import json
from pathlib import Path


def _load_dashboard_api():
    path = Path(__file__).parent.parent / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("agent_sensorium_dashboard_api_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _append_jsonl(root: Path, name: str, row: dict):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, separators=(",", ":")) + "\n")


def _snapshot(api):
    return asyncio.run(api.snapshot())


def _runtime_status(api):
    return asyncio.run(api.runtime_status())


def _trace(api, *, node_id: str | None = None, edge_id: str | None = None):
    return asyncio.run(api.trace(node_id=node_id, edge_id=edge_id))


def test_snapshot_surfaces_completed_lakmus_outbox_as_historical_pointer(tmp_path, monkeypatch):
    api = _load_dashboard_api()
    root = tmp_path / "demo"
    root.mkdir(parents=True)
    monkeypatch.setattr(api, "DEFAULT_ROOT", root)
    monkeypatch.setattr(api, "DEFAULT_INSTANCE", "demo")

    (root / "instance.config.json").write_text(
        json.dumps(
            {
                "instance_name": "demo",
                "allowed_surfaces": ["local", "discord"],
                "max_sensitivity": "private",
                "outbox": {
                    "allowed_delivery_modes": ["peripheral_reference", "context_pointer"],
                    "enable_direct_discord": False,
                },
            }
        ),
        encoding="utf-8",
    )
    _append_jsonl(
        root,
        "threads.jsonl",
        {
            "id": "sth_lakmus",
            "status": "closed",
            "conscious_task": {"title": "Lakmus handoff"},
            "origin_candidate_id": "cand_lakmus",
            "allowed_surfaces": ["local", "discord"],
            "updated_at": "2026-05-31T17:56:03Z",
        },
    )
    _append_jsonl(
        root,
        "thread_actions.jsonl",
        {
            "id": "tact_lakmus",
            "status": "acted",
            "outcome": "completed",
            "origin_thread_id": "sth_lakmus",
            "origin_candidate_id": "cand_lakmus",
            "title": "Prepare formal handoff artifacts",
            "attachments": [
                {"kind": "artifact_ref", "ref_id": "art_text"},
                {"kind": "outbox_request", "ref_id": "obx_lakmus"},
            ],
            "updated_at": "2026-05-31T19:22:53Z",
        },
    )
    _append_jsonl(
        root,
        "artifacts.jsonl",
        {
            "id": "art_text",
            "kind": "text",
            "status": "recorded",
            "delivery_state": "held_for_review",
            "ref_path": "/tmp/handoff.txt",
            "source_refs": {"thread_id": "sth_lakmus", "action_id": "tact_lakmus"},
            "updated_at": "2026-05-31T16:31:04Z",
        },
    )
    _append_jsonl(
        root,
        "artifacts.jsonl",
        {
            "id": "art_audio",
            "kind": "audio",
            "status": "recorded",
            "delivery_state": "held_for_review",
            "ref_path": "/tmp/handoff.mp3",
            "source_refs": {"thread_id": "sth_lakmus", "action_id": "tact_lakmus"},
            "updated_at": "2026-05-31T16:32:04Z",
        },
    )
    _append_jsonl(
        root,
        "outbox.jsonl",
        {
            "id": "obx_lakmus",
            "status": "prepared",
            "origin_thread_id": "sth_lakmus",
            "origin_candidate_id": "cand_lakmus",
            "request_type": "PRIVATE_EXPRESSION",
            "surface": "discord",
            "delivery_mode": "context_pointer",
            "title": "Thread-pickup handoff is prepared",
            "message_preview": "Prepared pointer only.",
            "media_refs": ["art_text", "art_audio"],
            "updated_at": "2026-05-31T16:31:04Z",
        },
    )

    data = asyncio.run(api.snapshot(instance="demo"))

    assert data["ok"] is True
    assert data["counts"]["prepared_outbox"] == 0
    assert data["counts"]["prepared_outbox_raw"] == 1
    assert data["counts"]["open_outbox"] == 0
    assert data["counts"]["open_outbox_raw"] == 1
    assert data["counts"]["historical_outbox"] == 1
    assert data["counts"]["actionable_outbox"] == 0
    assert data["counts"]["lifecycle_warnings"] == 0
    assert data["health"]["status"] == "quiet"
    assert data["outbox"][0]["safety"]["label"] == "historical_prepared_pointer"
    assert data["outbox"][0]["liveness"]["state"] == "settled"
    assert data["outbox"][0]["liveness"]["reason_code"] == "historical_prepared_pointer"
    assert data["actions"][0]["liveness"]["state"] == "settled"
    assert data["outbox"][0]["safety"]["outbound_delivery"] is False
    assert data["outbox"][0]["safety"]["dispatch_requires_execute"] is False
    assert data["outbox"][0]["safety"]["attached_action_id"] == "tact_lakmus"
    trace = _trace(api, node_id="outbox:obx_lakmus")
    assert trace["subject"]["status"] == "settled"
    assert trace["contents"]["status"] == "settled"
    assert "no dispatch receipt yet" not in json.dumps(trace)
    assert "historical prepared pointer" in json.dumps(trace)
    assert data["actions"][0]["outbox_refs"] == ["obx_lakmus"]
    assert data["artifacts"][0]["id"] == "art_audio"
    assert data["counts"]["artifact_groups"] == 1
    assert data["artifact_groups"][0]["id"] == "action:tact_lakmus"
    assert data["artifact_groups"][0]["count"] == 2
    assert data["artifact_groups"][0]["kinds"] == {"audio": 1, "text": 1}


def test_snapshot_projects_closed_liveness_for_all_families_and_fails_closed(tmp_path, monkeypatch):
    api = _load_dashboard_api()
    root = tmp_path / "demo"
    root.mkdir(parents=True)
    monkeypatch.setattr(api, "DEFAULT_ROOT", root)
    monkeypatch.setattr(api, "DEFAULT_INSTANCE", "demo")
    sentinel = "RAW_SECRET_SENTINEL_SHOULD_NOT_LEAK"
    _append_jsonl(root, "candidates.jsonl", {"id": "candidate_1", "status": "candidate", "pressure": 0.9, "updated_at": "2026-07-09T12:00:00Z"})
    _append_jsonl(root, "candidates.jsonl", {"id": sentinel, "status": sentinel, "summary": sentinel})
    _append_jsonl(root, "threads.jsonl", {"id": "thread_held", "status": "held", "updated_at": "2026-07-09T12:00:00Z"})
    _append_jsonl(root, "threads.jsonl", {"id": "thread_hostile", "status": sentinel})
    _append_jsonl(root, "thread_actions.jsonl", {"id": "action_acted", "status": "acted", "updated_at": "2026-07-09T12:00:00Z"})
    _append_jsonl(root, "thread_actions.jsonl", {"id": "action_hostile", "status": sentinel})
    _append_jsonl(root, "outbox.jsonl", {"id": "outbox_prepared", "status": "prepared", "updated_at": "2026-07-09T12:00:00Z"})
    _append_jsonl(root, "outbox.jsonl", {"id": "outbox_hostile", "status": sentinel})

    data = asyncio.run(api.snapshot(instance="demo"))
    assert data.get("ok") is True, data
    expected = {"state", "reason_code", "observed_at", "source", "actionable", "terminal", "related_refs"}
    families = {"candidate": "top_candidates", "thread": "threads", "action": "actions", "outbox": "outbox"}
    for family in families.values():
        assert data[family]
        for item in data[family]:
            assert set(item["liveness"]) == expected
            assert item["liveness"]["state"] in {"active", "reviewing", "blocked", "held", "prepared", "settled", "stale", "error", "quiet", "unknown"}
            assert item["liveness"]["related_refs"] == []
    assert next(item for item in data["threads"] if item["id"] == "thread_held")["liveness"]["state"] == "held"
    assert next(item for item in data["actions"] if item["id"] == "action_acted")["liveness"] == {
        "state": "settled", "reason_code": "action_acted", "observed_at": "2026-07-09T12:00:00Z",
        "source": "action_status", "actionable": False, "terminal": True, "related_refs": [],
    }
    assert next(item for item in data["outbox"] if item["id"] == "outbox_prepared")["liveness"]["state"] == "prepared"
    assert api._thread_item({"id": "closed_thread", "status": "closed"})["liveness"]["state"] == "settled"
    assert api._thread_item({"id": "archived_thread", "status": "archived"})["liveness"]["terminal"] is True
    assert api._thread_item({"id": "hostile_thread", "status": sentinel})["liveness"]["state"] == "unknown"
    assert sentinel not in json.dumps(data, sort_keys=True)


def test_snapshot_open_outbox_counts_exclude_historical_prepared_pointer_only(tmp_path, monkeypatch):
    api = _load_dashboard_api()
    root = tmp_path / "demo"
    root.mkdir(parents=True)
    monkeypatch.setattr(api, "DEFAULT_ROOT", root)
    monkeypatch.setattr(api, "DEFAULT_INSTANCE", "demo")

    _append_jsonl(root, "threads.jsonl", {"id": "thread_archived_1", "status": "archived", "updated_at": "2026-05-31T12:00:00Z"})
    _append_jsonl(root, "threads.jsonl", {"id": "thread_live_1", "status": "held", "updated_at": "2026-05-31T12:01:00Z"})
    _append_jsonl(
        root,
        "thread_actions.jsonl",
        {
            "id": "action_acted_1",
            "status": "acted",
            "origin_thread_id": "thread_archived_1",
            "attachments": [{"kind": "outbox_request", "ref_id": "obx_historical_1"}],
            "updated_at": "2026-05-31T12:02:00Z",
        },
    )
    _append_jsonl(
        root,
        "outbox.jsonl",
        {
            "id": "obx_historical_1",
            "status": "prepared",
            "origin_thread_id": "thread_archived_1",
            "delivery_mode": "context_pointer",
            "updated_at": "2026-05-31T12:03:00Z",
        },
    )
    _append_jsonl(
        root,
        "outbox.jsonl",
        {
            "id": "obx_live_1",
            "status": "prepared",
            "origin_thread_id": "thread_live_1",
            "delivery_mode": "context_pointer",
            "updated_at": "2026-05-31T12:04:00Z",
        },
    )
    _append_jsonl(
        root,
        "outbox.jsonl",
        {
            "id": "obx_failed_1",
            "status": "failed",
            "origin_thread_id": "thread_live_1",
            "delivery_mode": "context_pointer",
            "updated_at": "2026-05-31T12:05:00Z",
        },
    )

    data = _snapshot(api)

    assert data["counts"]["prepared_outbox"] == 1
    assert data["counts"]["prepared_outbox_raw"] == 2
    assert data["counts"]["open_outbox"] == 2
    assert data["counts"]["open_outbox_raw"] == 3
    assert data["counts"]["historical_outbox"] == 1
    labels = {item["id"]: item["safety"]["label"] for item in data["outbox"]}
    assert labels["obx_historical_1"] == "historical_prepared_pointer"
    assert labels["obx_live_1"] == "prepared_pointer_only"
    assert labels["obx_failed_1"] == "dispatch_failed"


def test_snapshot_exposes_conscious_reachout_metrics_without_message_body(tmp_path, monkeypatch):
    api = _load_dashboard_api()
    root = tmp_path / "demo"
    monkeypatch.setattr(api, "DEFAULT_ROOT", root)
    monkeypatch.setattr(api, "DEFAULT_INSTANCE", "demo")

    _append_jsonl(
        root,
        "decisions.jsonl",
        {
            "ts": "2026-06-27T10:00:00Z",
            "type": "conscious_reachout.decision",
            "decision": "prepare_message",
            "surface": "discord",
            "target_ref": "discord:chan_1",
            "reason": "selected",
            "message_hash": "abc123",
            "message_chars": 44,
        },
    )
    _append_jsonl(
        root,
        "decisions.jsonl",
        {
            "ts": "2026-06-27T10:01:00Z",
            "type": "conscious_reachout.denied",
            "decision": "reach_out",
            "surface": "discord",
            "target_ref": "discord:chan_1",
            "blocked_reason": "cooldown_active",
            "message_hash": "def456",
            "message_chars": 18,
        },
    )

    data = _snapshot(api)
    metrics = data["conscious_reachout_metrics"]
    assert metrics["receipt_type"] == "conscious_reachout.decision"
    assert metrics["receipt_count"] == 2
    assert metrics["prepared_count"] == 1
    assert metrics["blocked_count"] == 1
    assert metrics["blocked_breakdown"] == {"cooldown_active": 1}
    assert "direct chosen" not in json.dumps(metrics)
    assert "chan_1" not in json.dumps(metrics)


def test_runtime_status_projects_flow_edges_and_trace_contents(tmp_path, monkeypatch):
    api = _load_dashboard_api()
    root = tmp_path / "demo"
    monkeypatch.setattr(api, "DEFAULT_ROOT", root)
    monkeypatch.setattr(api, "DEFAULT_INSTANCE", "demo")
    (root / "sensors").mkdir(parents=True)
    (root / "sensors" / "registry.json").write_text(
        json.dumps({"blocks": {"sensor_a": {"type": "sensor", "label": "Sensor A"}}}),
        encoding="utf-8",
    )
    _append_jsonl(root, "signals/inbox.jsonl", {"id": "sig_1", "sensor": "sensor_a", "kind": "cue", "summary": "compact cue", "ts": "2026-06-27T10:00:00Z"})
    _append_jsonl(root, "events.jsonl", {"id": "evt_1", "source_signal_ids": ["sig_1"], "kind": "signal_promoted", "ts": "2026-06-27T10:01:00Z"})
    _append_jsonl(root, "candidates.jsonl", {"id": "cand_1", "status": "candidate", "kind": "salience", "summary": "review this", "pressure": 0.88, "event_ids": ["evt_1"], "updated_at": "2026-06-27T10:02:00Z"})
    _append_jsonl(root, "threads.jsonl", {"id": "thread_1", "status": "held", "origin_candidate_id": "cand_1", "conscious_task": {"title": "Review cue"}, "updated_at": "2026-06-27T10:03:00Z"})
    _append_jsonl(root, "thread_actions.jsonl", {"id": "act_1", "status": "prepared", "origin_candidate_id": "cand_1", "origin_thread_id": "thread_1", "title": "Prepare response", "attachments": [{"kind": "outbox_request", "ref_id": "obx_1"}], "updated_at": "2026-06-27T10:04:00Z"})
    _append_jsonl(root, "outbox.jsonl", {"id": "obx_1", "status": "prepared", "origin_candidate_id": "cand_1", "origin_thread_id": "thread_1", "request_type": "REACH_OUT", "surface": "discord", "delivery_mode": "context_pointer", "message_preview": "Prepared pointer.", "updated_at": "2026-06-27T10:05:00Z"})

    data = _runtime_status(api)
    assert data["ok"] is True
    edge_kinds = {edge["kind"] for edge in data["edges"]}
    assert {"signal_to_candidate", "candidate_to_thread", "thread_to_action", "action_to_outbox"}.issubset(edge_kinds)
    assert data["meta"]["edge_count"] == len(data["edges"])
    assert all(edge["observed"] is True and edge["projection"] is True for edge in data["edges"])

    candidate_node_id = next(node["id"] for node in data["nodes"] if node["kind"] == "candidate")
    trace = _trace(api, node_id=candidate_node_id)
    assert trace["subject"]["kind"] == "candidate"
    assert trace["contents"]["summary"] == "review this"
    assert trace["contents"]["event_count"] == 1
    assert any(ref["kind"] == "signal" for ref in trace["upstream"])

    runtime_edge_id = next(edge["id"] for edge in data["edges"] if edge["kind"] == "signal_to_candidate")
    edge_trace = _trace(api, edge_id=runtime_edge_id)
    assert edge_trace["subject"]["origin"] == "runtime_projection"
    assert edge_trace["contents"]["role"] == "runtime_projection_edge"
    assert edge_trace["contents"]["observed"] is True
    assert "not a complete traversal log" in json.dumps(edge_trace)


def test_snapshot_warns_on_unattached_prepared_outbox(tmp_path, monkeypatch):
    api = _load_dashboard_api()
    root = tmp_path / "demo"
    monkeypatch.setattr(api, "DEFAULT_ROOT", root)
    monkeypatch.setattr(api, "DEFAULT_INSTANCE", "demo")

    _append_jsonl(
        root,
        "threads.jsonl",
        {
            "id": "sth_open",
            "status": "held",
            "conscious_task": {"title": "Open thread"},
            "allowed_surfaces": ["local"],
            "updated_at": "2026-05-31T12:00:00Z",
        },
    )
    _append_jsonl(
        root,
        "outbox.jsonl",
        {
            "id": "obx_unattached",
            "status": "prepared",
            "origin_thread_id": "sth_open",
            "request_type": "PRIVATE_EXPRESSION",
            "surface": "local",
            "delivery_mode": "context_pointer",
            "media_refs": [],
            "updated_at": "2026-05-31T12:00:00Z",
        },
    )

    data = _snapshot(api)

    assert data["counts"]["lifecycle_warnings"] == 1
    assert data["health"]["status"] == "needs_review"
    assert data["lifecycle_warnings"][0]["label"] == "prepared_outbox_unattached"

_EXPLAIN_SENTINEL = "sk-exp...5566"


def test_explanation_route_is_get_only_and_compact():
    api = _load_dashboard_api()
    routes = {r.path: r for r in api.router.routes if hasattr(r, "methods")}
    assert "/explanation" in routes
    assert set(routes["/explanation"].methods) == {"GET"}


def test_explanation_route_returns_safe_explanation_without_sentinel_leak(tmp_path, monkeypatch):
    api = _load_dashboard_api()
    root = tmp_path / "demo"
    monkeypatch.setattr(api, "DEFAULT_ROOT", root)
    monkeypatch.setattr(api, "DEFAULT_INSTANCE", "demo")

    _append_jsonl(
        root,
        "candidates.jsonl",
        {
            "id": _EXPLAIN_SENTINEL,
            "kind": _EXPLAIN_SENTINEL,
            "status": "candidate",
            "pressure": 0.66,
            "summary": _EXPLAIN_SENTINEL,
            "event_ids": [f"{_EXPLAIN_SENTINEL}-evt"],
            "correlation_keys": [_EXPLAIN_SENTINEL],
            "updated_at": "2026-06-12T12:00:00Z",
        },
    )
    _append_jsonl(
        root,
        "inner_life/dampeners.jsonl",
        {
            "type": "dampener_effect",
            "source_node": "node#deadbeefdead",
            "target_node": "node#cafef00dcafe",
            "mode": "cooldown",
            "before_pressure": 0.66,
            "after_pressure": 0.0,
            "reason": "cooldown_active",
            "precedence": "dampens",
        },
    )

    data = asyncio.run(api.explanation(subject_id=_EXPLAIN_SENTINEL))

    assert data["ok"] is True
    assert data["instance"] == "demo"
    explanation = data["explanation"]
    assert explanation["pressure"] == 0.66
    assert explanation["inhibitors"]
    assert _EXPLAIN_SENTINEL not in json.dumps(data, sort_keys=True)
    assert _EXPLAIN_SENTINEL not in explanation["subject_id"]


def test_explanation_route_never_echoes_secret_shaped_dampener_mode(tmp_path, monkeypatch):
    """A corrupt/legacy dampener sidecar can carry an arbitrary `mode` value.
    The API must safe-label unknown modes instead of echoing them verbatim,
    while a known mode (e.g. cooldown) still renders usefully."""
    api = _load_dashboard_api()
    root = tmp_path / "demo"
    monkeypatch.setattr(api, "DEFAULT_ROOT", root)
    monkeypatch.setattr(api, "DEFAULT_INSTANCE", "demo")

    secret_mode = f"{_EXPLAIN_SENTINEL}-mode"
    _append_jsonl(
        root,
        "candidates.jsonl",
        {
            "id": "cand_secret_mode",
            "kind": "subconscious_advisory",
            "status": "candidate",
            "pressure": 0.71,
            "updated_at": "2026-06-12T12:00:00Z",
        },
    )
    _append_jsonl(
        root,
        "inner_life/dampeners.jsonl",
        {
            "type": "dampener_effect",
            "source_node": f"{_EXPLAIN_SENTINEL}-source",
            "target_node": f"{_EXPLAIN_SENTINEL}-target",
            "mode": secret_mode,
            "before_pressure": 0.71,
            "after_pressure": 0.11,
            "reason": _EXPLAIN_SENTINEL,
        },
    )

    data = asyncio.run(api.explanation(subject_id="cand_secret_mode"))

    assert data["ok"] is True
    serialized = json.dumps(data, sort_keys=True)
    assert _EXPLAIN_SENTINEL not in serialized
    assert secret_mode not in serialized
    inhibitor = data["explanation"]["inhibitors"][0]
    assert inhibitor["label"] != secret_mode
    assert inhibitor["label"].startswith("mode#")


def test_explanation_route_handles_unknown_subject_without_crash(tmp_path, monkeypatch):
    api = _load_dashboard_api()
    root = tmp_path / "demo"
    monkeypatch.setattr(api, "DEFAULT_ROOT", root)
    monkeypatch.setattr(api, "DEFAULT_INSTANCE", "demo")

    data = asyncio.run(api.explanation(subject_id="ghost"))
    assert data["ok"] is True
    assert "subject_record_unavailable" in data["explanation"]["warnings"]


def test_resolve_instance_accepts_omitted_and_legacy_default(monkeypatch, tmp_path):
    api = _load_dashboard_api()
    root = tmp_path / "demo"
    monkeypatch.setattr(api, "DEFAULT_ROOT", root)
    monkeypatch.setattr(api, "DEFAULT_INSTANCE", "demo")

    assert api._resolve_instance(None) == ("demo", root)
    assert api._resolve_instance("default") == ("default", root)
    assert api._resolve_instance("demo") == ("demo", root)


def test_resolve_instance_accepts_simple_sibling_name(monkeypatch, tmp_path):
    api = _load_dashboard_api()
    root = tmp_path / "demo"
    monkeypatch.setattr(api, "DEFAULT_ROOT", root)
    monkeypatch.setattr(api, "DEFAULT_INSTANCE", "demo")

    assert api._resolve_instance("other-instance_1") == ("other-instance_1", root.parent / "other-instance_1")


def test_resolve_instance_rejects_traversal_and_unsafe_names(monkeypatch, tmp_path):
    api = _load_dashboard_api()
    root = tmp_path / "demo"
    monkeypatch.setattr(api, "DEFAULT_ROOT", root)
    monkeypatch.setattr(api, "DEFAULT_INSTANCE", "demo")

    for bad in ["..", "../evil", "evil/../default", "a/b", "a\\b", "/etc/passwd", "", "   ", "evil\n", ".ssh", ".evil"]:
        assert api._resolve_instance(bad) is None, bad


def test_explanation_route_rejects_traversal_instance_without_filesystem_read(tmp_path, monkeypatch):
    api = _load_dashboard_api()
    root = tmp_path / "base" / "default"
    outside = tmp_path / "evil"
    monkeypatch.setattr(api, "DEFAULT_ROOT", root)
    monkeypatch.setattr(api, "DEFAULT_INSTANCE", "default")

    _append_jsonl(
        outside,
        "candidates.jsonl",
        {
            "id": "cand-outside",
            "kind": "subconscious_advisory",
            "status": "candidate",
            "pressure": 0.91,
            "summary": "outside instance row should not be reachable through ../ instance",
        },
    )

    default_data = asyncio.run(api.explanation(subject_id="cand-outside"))
    assert default_data["ok"] is True
    assert "subject_record_unavailable" in default_data["explanation"]["warnings"]

    for bad in ["../evil", "evil\n"]:
        traversal_data = asyncio.run(api.explanation(subject_id="cand-outside", instance=bad))
        assert traversal_data == {"ok": False, "error": "invalid_instance"}
        assert bad not in json.dumps(traversal_data)
        assert str(outside) not in json.dumps(traversal_data)
        assert "0.91" not in json.dumps(traversal_data)


def test_attention_route_rejects_traversal_instance(tmp_path, monkeypatch):
    api = _load_dashboard_api()
    root = tmp_path / "demo"
    monkeypatch.setattr(api, "DEFAULT_ROOT", root)
    monkeypatch.setattr(api, "DEFAULT_INSTANCE", "demo")

    for bad in ["../evil", "evil\n"]:
        data = asyncio.run(api.attention(instance=bad))
        assert data == {"ok": False, "error": "invalid_instance"}


def test_snapshot_route_rejects_traversal_instance(tmp_path, monkeypatch):
    api = _load_dashboard_api()
    root = tmp_path / "demo"
    monkeypatch.setattr(api, "DEFAULT_ROOT", root)
    monkeypatch.setattr(api, "DEFAULT_INSTANCE", "demo")

    for bad in ["../evil", "evil\n", "..%2f..%2fevil"]:
        data = asyncio.run(api.snapshot(instance=bad))
        assert data == {"ok": False, "error": "invalid_instance"}


def test_snapshot_separates_live_pressure_from_historical_residue(tmp_path, monkeypatch):
    api = _load_dashboard_api()
    root = tmp_path / "demo"
    metrics_root = tmp_path / "metrics"
    monkeypatch.setattr(api, "DEFAULT_ROOT", root)
    monkeypatch.setattr(api, "DEFAULT_INSTANCE", "demo")
    monkeypatch.setattr(api, "METRICS_DIR", metrics_root)

    metrics_root.mkdir(parents=True)
    (metrics_root / "latest.json").write_text(
        json.dumps({"open": {"open_conscious_review_tasks": 1}}),
        encoding="utf-8",
    )
    _append_jsonl(
        root,
        "candidates.jsonl",
        {
            "id": "cand_live",
            "status": "candidate",
            "pressure": 0.8,
            "summary": "Live pressure that should count as now.",
            "updated_at": "2026-06-12T12:00:00Z",
        },
    )
    _append_jsonl(
        root,
        "thread_actions.jsonl",
        {
            "id": "tact_done",
            "status": "acted",
            "outcome": "completed",
            "title": "Historical completed action",
            "updated_at": "2026-06-01T12:00:00Z",
        },
    )
    _append_jsonl(
        root,
        "artifacts.jsonl",
        {
            "id": "art_held",
            "kind": "text",
            "delivery_state": "held_for_review",
            "source_refs": {"action_id": "tact_done"},
            "updated_at": "2026-06-01T12:00:00Z",
        },
    )

    data = _snapshot(api)

    assert data["attention_footprint"]["live_items"] == 2
    assert data["attention_footprint"]["live_breakdown"]["active_candidates"] == 1
    assert data["attention_footprint"]["live_breakdown"]["open_reviews"] == 1
    assert data["attention_footprint"]["residue_items"] == 2
    assert data["attention_footprint"]["residue_breakdown"]["held_artifacts"] == 1
    assert data["attention_footprint"]["residue_breakdown"]["closed_actions"] == 1

    views = {view["id"]: view for view in data["views"]}
    assert views["overview"]["count"] == 2
    assert views["overview"]["band"] == "yellow"
    assert views["actuators"]["count"] == 0
    assert views["actuators"]["band"] == "neutral"


def test_snapshot_hash_labels_secret_shaped_artifact_projection_fields(tmp_path, monkeypatch):
    api = _load_dashboard_api()
    root = tmp_path / "demo"
    monkeypatch.setattr(api, "DEFAULT_ROOT", root)
    monkeypatch.setattr(api, "DEFAULT_INSTANCE", "demo")
    sentinel = "api_key_artifact_8899"

    _append_jsonl(
        root,
        "candidates.jsonl",
        {
            "id": "cand_artifact",
            "kind": "media_gift",
            "status": "candidate",
            "summary": f"candidate summary {sentinel}",
            "updated_at": "2026-06-21T12:00:00Z",
        },
    )
    _append_jsonl(
        root,
        "artifacts.jsonl",
        {
            "id": "art_secret",
            "kind": sentinel,
            "status": "recorded",
            "delivery_state": sentinel,
            "ref_path": f"/tmp/{sentinel}.mp4",
            "why_created": f"artifact reason {sentinel}",
            "source_refs": {"candidate_id": "cand_artifact", "thread_id": sentinel, "action_id": sentinel},
            "sensitivity": sentinel,
            "allowed_surfaces": [sentinel],
            "updated_at": "2026-06-21T12:01:00Z",
        },
    )

    data = _snapshot(api)
    payload = json.dumps(data, sort_keys=True)

    assert sentinel not in payload
    artifact = data["artifacts"][0]
    assert artifact["kind"].startswith("artifact_kind#")
    assert artifact["delivery_state"].startswith("delivery_state#")
    assert artifact["ref_name"].startswith("artifact_ref_name#")
    assert artifact["ref_path"].startswith("artifact_ref_path#")
    assert artifact["why_created"].startswith("artifact_why#")
    assert artifact["thread_id"].startswith("thread#")
    group = data["artifact_groups"][0]
    assert group["title"].startswith("artifact_group_title#")
    assert group["items"][0]["ref_path"].startswith("artifact_ref_path#")


def test_snapshot_hash_labels_secret_shaped_thread_action_outbox_projection_fields(tmp_path, monkeypatch):
    api = _load_dashboard_api()
    root = tmp_path / "demo"
    monkeypatch.setattr(api, "DEFAULT_ROOT", root)
    monkeypatch.setattr(api, "DEFAULT_INSTANCE", "demo")
    sentinel = "api_key_actuator_8899"

    _append_jsonl(
        root,
        "threads.jsonl",
        {
            "id": sentinel,
            "status": "held",
            "title": f"thread title {sentinel}",
            "origin_candidate_id": sentinel,
            "sensitivity": sentinel,
            "allowed_surfaces": [sentinel],
            "updated_at": "2026-06-21T13:00:00Z",
        },
    )
    _append_jsonl(
        root,
        "thread_actions.jsonl",
        {
            "id": sentinel,
            "status": "proposed",
            "outcome": sentinel,
            "intent": sentinel,
            "title": f"action title {sentinel}",
            "summary": f"action summary {sentinel}",
            "result_summary": f"result summary {sentinel}",
            "origin_thread_id": sentinel,
            "origin_candidate_id": sentinel,
            "attachments": [
                {"kind": "artifact_ref", "ref_id": sentinel},
                {"kind": "outbox_request", "ref_id": sentinel},
                {"kind": sentinel, "ref_id": "safe_ref"},
            ],
            "updated_at": "2026-06-21T13:01:00Z",
        },
    )
    _append_jsonl(
        root,
        "outbox.jsonl",
        {
            "id": sentinel,
            "status": "prepared",
            "origin_thread_id": sentinel,
            "origin_candidate_id": sentinel,
            "request_type": sentinel,
            "surface": sentinel,
            "delivery_mode": "context_pointer",
            "title": f"outbox title {sentinel}",
            "message_preview": f"message preview {sentinel}",
            "target": {
                "channel_id": sentinel,
                "thread_id": sentinel,
                "recipient": sentinel,
            },
            "media_refs": [sentinel],
            "platform_refs": {"discord": sentinel, sentinel: "safe_value"},
            "updated_at": "2026-06-21T13:02:00Z",
        },
    )

    data = _snapshot(api)
    payload = json.dumps(data, sort_keys=True)

    assert sentinel not in payload
    thread = data["threads"][0]
    assert thread["id"].startswith("thread#")
    assert thread["title"].startswith("thread_title#")
    assert thread["origin_candidate_id"].startswith("candidate#")
    assert thread["allowed_surfaces"][0].startswith("surface#")

    action = data["actions"][0]
    assert action["id"].startswith("action#")
    assert action["outcome"].startswith("action_outcome#")
    assert action["intent"].startswith("action_intent#")
    assert action["title"].startswith("action_title#")
    assert action["artifact_refs"][0].startswith("artifact#")
    assert action["outbox_refs"][0].startswith("outbox#")
    assert next(iter(action["attachment_kinds"])).startswith("attachment_kind#") or "artifact_ref" in action["attachment_kinds"]

    outbox = data["outbox"][0]
    assert outbox["id"].startswith("outbox#")
    assert outbox["request_type"].startswith("request_type#")
    assert outbox["surface"].startswith("surface#")
    assert outbox["title"].startswith("outbox_title#")
    assert outbox["message_preview"].startswith("message_preview#")
    assert outbox["target"]["channel_id"].startswith("target#")
    assert outbox["media_refs"][0].startswith("media_ref#")
    assert outbox["platform_refs"]["discord"].startswith("platform_ref#")
    assert outbox["safety"]["attached_action_id"].startswith("action#")
    assert any(warning["id"].startswith("action#") for warning in data["lifecycle_warnings"])


def test_snapshot_health_hash_labels_secret_shaped_legacy_dispatch_reason(tmp_path, monkeypatch):
    api = _load_dashboard_api()
    root = tmp_path / "demo"
    monkeypatch.setattr(api, "DEFAULT_ROOT", root)
    monkeypatch.setattr(api, "DEFAULT_INSTANCE", "demo")
    sentinel = "api_key_dispatch_reason_8899"
    root.mkdir(parents=True)
    (root / "state.latest.json").write_text(
        json.dumps({"last_dispatch_result": {"action": sentinel, "reason": f"legacy dispatcher raw reason {sentinel}"}}),
        encoding="utf-8",
    )

    data = _snapshot(api)
    payload = json.dumps(data, sort_keys=True)

    assert sentinel not in payload
    assert data["health"]["last_dispatch_action"].startswith("dispatch_action#")
    assert data["health"]["last_dispatch_reason"].startswith("dispatch_reason#")


def test_snapshot_hash_labels_secret_shaped_config_and_decision_fields(tmp_path, monkeypatch):
    api = _load_dashboard_api()
    root = tmp_path / "demo"
    monkeypatch.setattr(api, "DEFAULT_ROOT", root)
    monkeypatch.setattr(api, "DEFAULT_INSTANCE", "demo")
    sentinel = "api_key_config_decision_8899"
    root.mkdir(parents=True)
    (root / "instance.config.json").write_text(
        json.dumps(
            {
                "instance_name": "demo",
                "allowed_surfaces": ["local", sentinel],
                "max_sensitivity": sentinel,
                "policy_card_ref": f"policy {sentinel}",
                "outbox": {"allowed_delivery_modes": ["context_pointer", sentinel], "discord_token": sentinel},
            }
        ),
        encoding="utf-8",
    )
    _append_jsonl(
        root,
        "decisions.jsonl",
        {
            "schema": sentinel,
            "receipt_kind": sentinel,
            "ts": "2026-06-21T14:00:00Z",
            "created_at": "2026-06-21T14:00:00Z",
            "type": sentinel,
            "subject_ref": {"type": "candidate", "id": "candidate#1234567890abcdef"},
            "decision": sentinel,
            "outcome": sentinel,
            "action": sentinel,
            "reason_label": "reason#1234567890abcdef",
        },
    )

    data = _snapshot(api)
    payload = json.dumps(data, sort_keys=True)

    assert sentinel not in payload
    assert data["config"]["allowed_surfaces"][1].startswith("surface#")
    assert data["config"]["max_sensitivity"].startswith("sensitivity#")
    assert data["config"]["policy_card_ref"].startswith("policy_card_ref#")
    assert data["config"]["outbox"]["allowed_delivery_modes"][1].startswith("outbox_config#")
    secret_outbox_values = [
        value for key, value in data["config"]["outbox"].items() if key.startswith("outbox_config_key#")
    ]
    assert secret_outbox_values and secret_outbox_values[0].startswith("outbox_config#")
    decision = data["decisions"][0]
    assert decision["schema"].startswith("schema#")
    assert decision["receipt_kind"].startswith("receipt_kind#")
    assert decision["type"].startswith("decision_type#")
    assert decision["decision"].startswith("decision#")
    assert decision["outcome"].startswith("outcome#")
    assert decision["action"].startswith("decision_action#")


def test_snapshot_hash_labels_secret_shaped_trace_settlement_decision(tmp_path, monkeypatch):
    api = _load_dashboard_api()
    root = tmp_path / "demo"
    monkeypatch.setattr(api, "DEFAULT_ROOT", root)
    monkeypatch.setattr(api, "DEFAULT_INSTANCE", "demo")
    sentinel = "api_key_trace_decision_8899"
    _append_jsonl(
        root,
        "candidates.jsonl",
        {
            "id": "cand_trace_decision",
            "status": "reviewed",
            "kind": "subconscious_advisory",
            "summary": "compact candidate summary",
            "updated_at": "2026-06-21T14:30:00Z",
            "kanban_settlement": {
                "decision": sentinel,
                "settled_at": "2026-06-21T14:31:00Z",
                "intake_task_id": "intake_safe",
                "review_task_id": "review_safe",
                "reason_label": "reason#1234567890abcdef",
            },
        },
    )

    data = _snapshot(api)
    payload = json.dumps(data, sort_keys=True)

    assert sentinel not in payload
    trace = data["perception_traces"][0]
    assert trace["settlement"]["decision"].startswith("decision#")
    decision_stage = next(stage for stage in trace["stages"] if stage["key"] == "decision")
    assert decision_stage["detail"].startswith("decision#")


def test_compact_get_surfaces_hash_label_raw_transcript_markers(tmp_path, monkeypatch):
    api = _load_dashboard_api()
    root = tmp_path / "demo"
    monkeypatch.setattr(api, "DEFAULT_ROOT", root)
    monkeypatch.setattr(api, "DEFAULT_INSTANCE", "demo")
    marker = "RAW_TRANSCRIPT_BODY_DO_NOT_LEAK_8899"
    root.mkdir(parents=True)
    (root / "instance.config.json").write_text(
        json.dumps({"tick_quiet_filename": f"quiet-{marker}.json", "outbox": {"note": marker}}),
        encoding="utf-8",
    )
    (root / "sensors" / "registry.json").parent.mkdir(parents=True)
    (root / "sensors" / "registry.json").write_text(
        json.dumps({"version": marker, "blocks": {marker: {"summary": marker}}}),
        encoding="utf-8",
    )
    (root / "sensors" / "edges.json").write_text(
        json.dumps({"edges": [{"from": "safe_block", "to": marker, "kind": "feeds_back"}]}),
        encoding="utf-8",
    )
    _append_jsonl(root, "inner_life/dampeners.jsonl", {"source_node": "safe", "target_node": marker, "reason": marker})
    _append_jsonl(root, "inner_life/blockers.jsonl", {"source_node": "safe", "target_node": marker, "reason_label": marker})
    _append_jsonl(
        root,
        "signals/inbox.jsonl",
        {
            "id": "sig_raw_marker",
            "kind": "subconscious_advisory",
            "summary": marker,
            "correlation_keys": [marker],
            "ts": "2026-06-21T15:00:00Z",
        },
    )
    _append_jsonl(
        root,
        "threads.jsonl",
        {"id": "sth_raw_marker", "status": "held", "title": marker, "updated_at": "2026-06-21T15:01:00Z"},
    )
    _append_jsonl(
        root,
        "thread_actions.jsonl",
        {
            "id": "act_raw_marker",
            "status": "proposed",
            "summary": marker,
            "attachments": [{"kind": "artifact_ref", "ref_id": marker}],
            "updated_at": "2026-06-21T15:02:00Z",
        },
    )
    _append_jsonl(
        root,
        "outbox.jsonl",
        {
            "id": "obx_raw_marker",
            "status": "prepared",
            "message_preview": marker,
            "target": {"thread_id": marker},
            "media_refs": [marker],
            "platform_refs": {"discord": marker},
            "updated_at": "2026-06-21T15:03:00Z",
        },
    )
    _append_jsonl(root, "artifacts.jsonl", {"id": "art_raw_marker", "ref_path": f"/tmp/{marker}.txt"})

    payloads = {
        "registry": asyncio.run(api.registry()),
        "dampeners": asyncio.run(api.dampeners()),
        "blockers": asyncio.run(api.blockers()),
        "snapshot": _snapshot(api),
    }

    assert marker not in json.dumps(payloads, sort_keys=True)


def test_metrics_and_snapshot_metrics_hash_label_hostile_metrics_payloads(tmp_path, monkeypatch):
    api = _load_dashboard_api()
    root = tmp_path / "demo"
    metrics_root = tmp_path / "metrics"
    monkeypatch.setattr(api, "DEFAULT_ROOT", root)
    monkeypatch.setattr(api, "DEFAULT_INSTANCE", "demo")
    monkeypatch.setattr(api, "METRICS_DIR", metrics_root)
    marker = "RAW_LOG_BODY_DO_NOT_LEAK_8899"
    root.mkdir(parents=True)
    metrics_root.mkdir(parents=True)
    (metrics_root / "latest.json").write_text(
        json.dumps(
            {
                "queue": {"reason": marker, "depth": 1},
                "series_label": marker,
                marker: {"nested": marker},
            }
        ),
        encoding="utf-8",
    )
    (metrics_root / "timeseries.jsonl").write_text(
        json.dumps({"metric": marker, "value": 1, marker: marker}) + "\n",
        encoding="utf-8",
    )

    metrics_payload = asyncio.run(api.metrics())
    snapshot_payload = _snapshot(api)
    serialized = json.dumps({"metrics": metrics_payload, "snapshot_metrics": snapshot_payload["metrics"]}, sort_keys=True)

    assert marker not in serialized
    assert metrics_payload["metrics"]["latest"]["queue"]["depth"] == 1
    assert metrics_payload["metrics"]["series"][0]["value"] == 1


def test_attention_and_snapshot_state_dir_hash_label_hostile_attention_payloads(tmp_path, monkeypatch):
    api = _load_dashboard_api()
    marker = "RAW_TRANSCRIPT_BODY_DO_NOT_LEAK_9911"
    root = tmp_path / f"demo-{marker}"
    monkeypatch.setattr(api, "DEFAULT_ROOT", root)
    monkeypatch.setattr(api, "DEFAULT_INSTANCE", "demo")
    root.mkdir(parents=True)
    _append_jsonl(
        root,
        "candidates.jsonl",
        {
            "id": f"cand-{marker}",
            "status": "candidate",
            "kind": marker,
            "summary": f"candidate summary {marker}",
            "pressure": 0.8,
            "allowed_surfaces": ["local"],
            "sensitivity": "private",
            "created_at": "2026-06-21T15:10:00Z",
            "updated_at": "2026-06-21T15:11:00Z",
        },
    )
    _append_jsonl(
        root,
        "threads.jsonl",
        {
            "id": f"sth-{marker}",
            "status": "held",
            "conscious_task": {"request_type": marker, "title": f"thread title {marker}", "why": marker},
            "source_refs": [marker],
            "origin_candidate_id": f"cand-{marker}",
            "hold_reason": marker,
            "resume_trigger": marker,
            "allowed_surfaces": ["local"],
            "sensitivity": "private",
            "created_at": "2026-06-21T15:12:00Z",
            "updated_at": "2026-06-21T15:13:00Z",
        },
    )

    attention_payload = asyncio.run(api.attention())
    snapshot_payload = _snapshot(api)
    serialized = json.dumps({"attention": attention_payload, "state_dir": snapshot_payload["state_dir"]}, sort_keys=True)

    assert marker not in serialized
    assert snapshot_payload["state_dir"].startswith("state_dir#")


def test_snapshot_exposes_live_turn_metrics_without_receipt_text(tmp_path, monkeypatch):
    api = _load_dashboard_api()
    root = tmp_path / "demo"
    monkeypatch.setattr(api, "DEFAULT_ROOT", root)
    monkeypatch.setattr(api, "DEFAULT_INSTANCE", "demo")
    root.mkdir(parents=True)
    marker = "RAW_TRANSCRIPT_BODY_DO_NOT_LEAK_7722"
    _append_jsonl(
        root,
        "decisions.jsonl",
        {
            "type": "live_turn.ingest_decision",
            "ts": "2026-06-27T10:00:00Z",
            "surface": f"discord-{marker}",
            "summary": f"private receipt {marker}",
            "foreground_resolution": "full",
            "residue": "none",
            "durable_capture": "docs",
            "background_action_allowed": True,
            "ingested": False,
            "skipped_reason": "foreground_owned_no_residue",
        },
    )
    _append_jsonl(
        root,
        "decisions.jsonl",
        {
            "type": "live_turn.ingest_decision",
            "ts": "2026-06-27T10:01:00Z",
            "summary": f"watch receipt {marker}",
            "foreground_resolution": "partial",
            "residue": "watch",
            "durable_capture": "none",
            "ingested": True,
        },
    )
    _append_jsonl(
        root,
        "decisions.jsonl",
        {
            "type": "live_turn.review_decision",
            "ts": f"hostile timestamp {marker}",
            "summary": f"private review {marker}",
            "decision": "sensorium_residue_candidate",
            "reason": "salience_cue_without_capture",
            "pending_review": "true",
            "has_salience_cue": "true",
            "durable_capture_seen": "false",
        },
    )

    payload = _snapshot(api)
    metrics = payload["live_turn_metrics"]
    review_metrics = payload["live_turn_review_metrics"]
    serialized = json.dumps({"ingest": metrics, "review": review_metrics}, sort_keys=True)

    assert marker not in serialized
    assert metrics["receipt_count"] == 2
    assert metrics["ingested_count"] == 1
    assert metrics["skipped_count"] == 1
    assert metrics["foreground_owned_no_residue_count"] == 1
    assert metrics["background_action_allowed_count"] == 1
    assert metrics["residue_breakdown"] == {"none": 1, "watch": 1}
    assert metrics["recent"][0]["residue"] == "watch"
    assert review_metrics["receipt_count"] == 1
    assert review_metrics["pending_review_count"] == 1
    assert review_metrics["decision_breakdown"] == {"sensorium_residue_candidate": 1}
    assert review_metrics["reason_breakdown"] == {"salience_cue_without_capture": 1}
    assert review_metrics["recent"][0]["ts"] == ""
