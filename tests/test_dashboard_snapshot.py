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
    root = tmp_path / "sera"
    root.mkdir(parents=True)
    monkeypatch.setattr(api, "DEFAULT_ROOT", root)
    monkeypatch.setattr(api, "DEFAULT_INSTANCE", "sera")

    (root / "instance.config.json").write_text(
        json.dumps(
            {
                "instance_name": "sera",
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
    root = tmp_path / "sera"
    monkeypatch.setattr(api, "DEFAULT_ROOT", root)
    monkeypatch.setattr(api, "DEFAULT_INSTANCE", "sera")

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
