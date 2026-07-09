"""Tests for read-only held-artifact verification projections."""

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


def _setup_env(tmp_path, monkeypatch):
    api = _load_dashboard_api()
    root = tmp_path / "triage_test_env"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(api, "DEFAULT_ROOT", root)
    monkeypatch.setattr(api, "DEFAULT_INSTANCE", "triage_test")

    # Construct compliant instance config
    config = {
        "instance_name": "triage_test",
        "allowed_surfaces": ["local", "discord"],
        "max_sensitivity": "private",
        "media_gift_policy": {
            "enabled": True,
            "conscious_may": ["approve_delivery", "decline", "choose_silence", "block_delivery"],
            "require_why_now_for": ["approve_delivery"],
            "delivery": {
                "enabled": True,
                "allowed_surfaces": ["local", "discord"],
                "allowed_targets": [],
                "cooldown_hours": 0.0,
            }
        }
    }
    (root / "instance.config.json").write_text(
        json.dumps(config, indent=2), encoding="utf-8"
    )
    return api, root


def test_artifact_verification_via_snapshot(tmp_path, monkeypatch):
    api, root = _setup_env(tmp_path, monkeypatch)

    # 1. Compliant file under approved directory (tmp_path is under /tmp)
    compliant_file = tmp_path / "compliant.md"
    compliant_file.write_text("STATUS: DONE\nARTIFACT: /some/path\nHello world", encoding="utf-8")

    # 2. Noncompliant file
    noncompliant_file = tmp_path / "noncompliant.txt"
    noncompliant_file.write_text("Hello without status header\nSTATUS: NONE", encoding="utf-8")

    # 3. Missing file
    missing_file_path = tmp_path / "missing.md"

    # 4. Path traversal / unsafe path (e.g. /etc/passwd or ../../secret)
    # We will register them in artifacts.jsonl and check they return UNVERIFIED/security_warning.

    _append_jsonl(root, "threads.jsonl", {"id": "sth_1", "status": "dormant"})
    
    # Write to artifacts.jsonl
    _append_jsonl(
        root,
        "artifacts.jsonl",
        {
            "id": "art_compliant",
            "kind": "text",
            "status": "recorded",
            "delivery_state": "held_for_review",
            "ref_path": str(compliant_file),
            "source_refs": {"thread_id": "sth_1"},
            "allowed_surfaces": ["local", "discord"],
            "updated_at": "2026-07-04T12:00:00Z",
        },
    )
    _append_jsonl(
        root,
        "artifacts.jsonl",
        {
            "id": "art_noncompliant",
            "kind": "text",
            "status": "recorded",
            "delivery_state": "held_for_review",
            "ref_path": str(noncompliant_file),
            "source_refs": {"thread_id": "sth_1"},
            "allowed_surfaces": ["local", "discord"],
            "updated_at": "2026-07-04T12:00:00Z",
        },
    )
    _append_jsonl(
        root,
        "artifacts.jsonl",
        {
            "id": "art_missing",
            "kind": "text",
            "status": "recorded",
            "delivery_state": "held_for_review",
            "ref_path": str(missing_file_path),
            "source_refs": {"thread_id": "sth_1"},
            "allowed_surfaces": ["local", "discord"],
            "updated_at": "2026-07-04T12:00:00Z",
        },
    )
    _append_jsonl(
        root,
        "artifacts.jsonl",
        {
            "id": "art_unsafe_traversal",
            "kind": "text",
            "status": "recorded",
            "delivery_state": "held_for_review",
            "ref_path": "/etc/passwd",
            "source_refs": {"thread_id": "sth_1"},
            "allowed_surfaces": ["local", "discord"],
            "updated_at": "2026-07-04T12:00:00Z",
        },
    )
    _append_jsonl(
        root,
        "artifacts.jsonl",
        {
            "id": "art_unsafe_relative",
            "kind": "text",
            "status": "recorded",
            "delivery_state": "held_for_review",
            "ref_path": "../../secret",
            "source_refs": {"thread_id": "sth_1"},
            "allowed_surfaces": ["local", "discord"],
            "updated_at": "2026-07-04T12:00:00Z",
        },
    )
    _append_jsonl(
        root,
        "artifacts.jsonl",
        {
            "id": "art_unsafe_sibling",
            "kind": "text",
            "status": "recorded",
            "delivery_state": "held_for_review",
            "ref_path": "/home/entity/.hermes2/secret.txt",
            "source_refs": {"thread_id": "sth_1"},
            "allowed_surfaces": ["local", "discord"],
            "updated_at": "2026-07-04T12:00:00Z",
        },
    )

    # Fetch snapshot
    snapshot_data = asyncio.run(api.snapshot(instance="triage_test"))
    assert snapshot_data["ok"] is True
    
    artifacts = {a["id"]: a for a in snapshot_data["artifacts"]}
    
    # Assert verification status and details for each artifact
    assert artifacts["art_compliant"]["verification"]["status"] == "VERIFIED_COMPLIANT"
    assert artifacts["art_compliant"]["verification"]["error_details"] is None

    assert artifacts["art_noncompliant"]["verification"]["status"] == "NONCOMPLIANT"
    assert artifacts["art_noncompliant"]["verification"]["error_details"] == "missing_status_marker"

    assert artifacts["art_missing"]["verification"]["status"] == "MISSING_FILE"
    assert artifacts["art_missing"]["verification"]["error_details"] == "file_not_found"

    assert artifacts["art_unsafe_traversal"]["verification"]["status"] == "UNVERIFIED"
    assert artifacts["art_unsafe_traversal"]["verification"]["error_details"] == "security_warning"

    assert artifacts["art_unsafe_relative"]["verification"]["status"] == "UNVERIFIED"
    assert artifacts["art_unsafe_relative"]["verification"]["error_details"] == "security_warning"

    assert artifacts["art_unsafe_sibling"]["verification"]["status"] == "UNVERIFIED"
    assert artifacts["art_unsafe_sibling"]["verification"]["error_details"] == "security_warning"


def test_dashboard_has_no_triage_handler_and_snapshot_is_non_mutating(tmp_path, monkeypatch):
    api, root = _setup_env(tmp_path, monkeypatch)

    compliant_file = tmp_path / "compliant.md"
    compliant_file.write_text("STATUS: DONE\nARTIFACT: /some/path\nHello world", encoding="utf-8")

    _append_jsonl(root, "threads.jsonl", {"id": "sth_1", "status": "dormant"})
    _append_jsonl(
        root,
        "artifacts.jsonl",
        {
            "id": "art_held",
            "kind": "text",
            "status": "recorded",
            "delivery_state": "held_for_review",
            "ref_path": str(compliant_file),
            "source_refs": {"thread_id": "sth_1"},
            "allowed_surfaces": ["local", "discord"],
            "updated_at": "2026-07-04T12:00:00Z",
        },
    )

    routes = {route.path: set(route.methods) for route in api.router.routes if hasattr(route, "methods")}
    artifacts_path = root / "artifacts.jsonl"
    before = artifacts_path.read_bytes()

    assert not hasattr(api, "triage_artifact")
    assert "/artifacts/{artifact_id}/triage" not in routes
    assert all(methods <= {"GET", "HEAD"} for methods in routes.values())

    snapshot_data = asyncio.run(api.snapshot(instance="triage_test"))
    artifact = {a["id"]: a for a in snapshot_data["artifacts"]}["art_held"]

    assert artifact["delivery_state"] == "held_for_review"
    assert artifact["verification"]["status"] == "VERIFIED_COMPLIANT"
    assert artifacts_path.read_bytes() == before
    assert not (root / "decisions.jsonl").exists()
