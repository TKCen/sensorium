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

    data = _snapshot(api)

    assert data["ok"] is True
    assert data["counts"]["prepared_outbox"] == 1
    assert data["counts"]["actionable_outbox"] == 0
    assert data["counts"]["lifecycle_warnings"] == 0
    assert data["health"]["status"] == "quiet"
    assert data["outbox"][0]["safety"]["label"] == "historical_prepared_pointer"
    assert data["outbox"][0]["safety"]["outbound_delivery"] is False
    assert data["outbox"][0]["safety"]["attached_action_id"] == "tact_lakmus"
    assert data["actions"][0]["outbox_refs"] == ["obx_lakmus"]
    assert data["artifacts"][0]["id"] == "art_audio"
    assert data["counts"]["artifact_groups"] == 1
    assert data["artifact_groups"][0]["id"] == "action:tact_lakmus"
    assert data["artifact_groups"][0]["count"] == 2
    assert data["artifact_groups"][0]["kinds"] == {"audio": 1, "text": 1}


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
