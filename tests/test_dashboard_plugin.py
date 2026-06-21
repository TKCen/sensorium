"""Dashboard API tests for Agent Sensorium."""

import asyncio
import importlib.util
import json
import os
from pathlib import Path


def _load_dashboard(monkeypatch, state_dir: Path):
    monkeypatch.setenv("SENSORIUM_STATE_DIR", str(state_dir))
    path = Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("sensorium_dashboard_plugin_test", path)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _append_jsonl(path: Path, row: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def test_snapshot_exposes_recent_signals_for_dashboard_drilldown(tmp_path, monkeypatch):
    root = tmp_path / "sensorium" / "demo"
    _append_jsonl(root / "signals" / "inbox.jsonl", {
        "id": "sig_codex",
        "sensor": "sensorium.codex_usage_pressure",
        "source": "provider_budget",
        "kind": "inference_budget_pressure",
        "summary": "weekly_over_expected=20pp used=34% elapsed=14%",
        "strength_hint": 0.95,
        "sensitivity": "local_only",
        "allowed_surfaces": ["local"],
        "correlation_keys": ["codex-openai-energy", "codex-openai-energy:weekly_pace"],
        "pressure_level": "critical",
        "transition": "degraded_to_critical",
        "ts": "2026-06-01T16:17:36Z",
    })
    mod = _load_dashboard(monkeypatch, root)

    data = asyncio.run(mod.snapshot(instance="demo"))

    assert data["recent_signals"] == [{
        "id": "sig_codex",
        "sensor": "sensorium.codex_usage_pressure",
        "source": "provider_budget",
        "kind": "inference_budget_pressure",
        "summary": "weekly_over_expected=20pp used=34% elapsed=14%",
        "strength_hint": 0.95,
        "pressure_level": "critical",
        "transition": "degraded_to_critical",
        "correlation_keys": ["codex-openai-energy", "codex-openai-energy:weekly_pace"],
        "sensitivity": "local_only",
        "allowed_surfaces": ["local"],
        "ts": "2026-06-01T16:17:36Z",
    }]


def test_snapshot_separates_canonical_freshness_from_legacy_state_latest(tmp_path, monkeypatch):
    root = tmp_path / "sensorium" / "demo"
    root.mkdir(parents=True)
    state_latest = root / "state.latest.json"
    state_latest.write_text(json.dumps({
        "updated_at": "2026-05-28T19:27:09Z",
        "budgets": {
            "dispatch": {
                "capacity": 10,
                "remaining": 10,
                "window_seconds": 3600,
                "reset_at": "2026-05-28T20:27:09Z",
            }
        },
    }))
    _append_jsonl(root / "signals" / "inbox.jsonl", {"id": "sig_fresh", "ts": "2026-06-03T04:47:39Z"})
    kanban_root = root.parent / "kanban"
    kanban_root.mkdir(parents=True)
    quiet_latest = kanban_root / "sensorium_tick_quiet.latest.json"
    quiet_latest.write_text(json.dumps({"ok": True, "ts": "2026-06-03T04:47:41Z"}))

    old = 1_780_000_000
    fresh = old + 600
    os.utime(state_latest, (old, old))
    os.utime(root / "signals" / "inbox.jsonl", (fresh, fresh))
    os.utime(quiet_latest, (fresh + 1, fresh + 1))

    mod = _load_dashboard(monkeypatch, root)

    data = asyncio.run(mod.snapshot(instance="demo"))

    freshness = data["freshness"]
    assert data["state_mtime"] == freshness["canonical_latest_mtime"]
    assert freshness["legacy_state_latest"]["deprecated"] is True
    assert freshness["legacy_state_latest"]["excluded_from_canonical"] is True
    assert freshness["jsonl"]["signals"]["mtime"] is not None
    assert freshness["tick_quiet_latest"]["mtime"] is not None
    assert freshness["canonical_latest_mtime"] != freshness["legacy_state_latest"]["mtime"]
    reset_at = data["budgets"]["dispatch"]["reset_at"]
    assert reset_at != "2026-05-28T20:27:09Z"


def test_snapshot_exposes_posture_aligned_dashboard_views(tmp_path, monkeypatch):
    root = tmp_path / "sensorium" / "demo"
    _append_jsonl(root / "signals" / "inbox.jsonl", {
        "id": "sig_residue",
        "kind": "design_insight",
        "summary": "Compact unresolved posture residue",
        "ts": "2026-06-05T07:30:00Z",
    })
    _append_jsonl(root / "candidates.jsonl", {
        "id": "cand_residue",
        "kind": "design_insight",
        "status": "candidate",
        "pressure": 0.8,
        "summary": "Needs later review",
        "event_ids": [],
        "created_at": "2026-06-05T07:31:00Z",
    })
    _append_jsonl(root / "thread_actions.jsonl", {
        "id": "act_open",
        "status": "prepared",
        "title": "Review retained residue",
        "updated_at": "2026-06-05T07:32:00Z",
    })

    mod = _load_dashboard(monkeypatch, root)

    data = asyncio.run(mod.snapshot(instance="demo"))

    views = {view["id"]: view for view in data["views"]}
    assert list(views) == ["overview", "perception", "substrate", "actuators"]
    assert views["overview"]["band"] == "yellow"
    assert views["overview"]["count"] >= 2
    assert views["perception"]["label"] == "Perception"
    assert views["perception"]["count"] >= 1
    assert "Compact residue" in views["perception"]["summary"]
    assert views["substrate"]["count"] >= 1
    assert views["actuators"]["band"] == "yellow"


def test_snapshot_hash_labels_password_key_like_compact_refs(tmp_path, monkeypatch):
    """Compact-looking refs with key/password semantics are still private.

    Regression for R8: prefix-shaped values such as ``cand_*``, ``evt_*`` and
    ``sig_*`` are allowed through only when they are benign dashboard refs. If
    their content names password/key material, snapshot must expose stable
    opaque labels instead of echoing the raw strings.
    """
    root = tmp_path / "sensorium" / "demo"
    raw_candidate_id = "cand_passwordreset4242"
    raw_event_id = "evt_passwordreset4242"
    raw_missing_event_id = "event_apikey4242"
    raw_signal_id = "sig_privatekey4242"
    raw_signal_correlation = "api_key_4242"
    raw_candidate_correlation = "privatekeycorrelation4242"

    _append_jsonl(root / "signals" / "inbox.jsonl", {
        "id": raw_signal_id,
        "sensor": "r8.test",
        "source": "adversarial",
        "kind": "test_signal",
        "summary": "safe compact summary",
        "strength_hint": 0.9,
        "correlation_keys": [raw_signal_correlation],
        "ts": "2026-06-21T12:00:00Z",
    })
    _append_jsonl(root / "events.jsonl", {
        "id": raw_event_id,
        "kind": "test_event",
        "summary": "safe compact event summary",
        "strength": 0.8,
        "source_signal_ids": [raw_signal_id],
        "ts": "2026-06-21T12:01:00Z",
    })
    _append_jsonl(root / "candidates.jsonl", {
        "id": raw_candidate_id,
        "kind": "subconscious_advisory",
        "status": "candidate",
        "pressure": 0.9,
        "summary": "safe compact candidate summary",
        "event_ids": [raw_event_id, raw_missing_event_id],
        "correlation_keys": [raw_candidate_correlation],
        "created_at": "2026-06-21T12:02:00Z",
        "updated_at": "2026-06-21T12:02:00Z",
    })

    mod = _load_dashboard(monkeypatch, root)
    data = asyncio.run(mod.snapshot(instance="demo"))
    payload = json.dumps(data, sort_keys=True)

    for raw in [
        raw_candidate_id,
        raw_event_id,
        raw_missing_event_id,
        raw_signal_id,
        raw_signal_correlation,
        raw_candidate_correlation,
    ]:
        assert raw not in payload

    trace = data["perception_traces"][0]
    assert trace["candidate_id"].startswith("candidate#")
    assert trace["events"][0]["id"].startswith("event#")
    assert trace["signals"][0]["id"].startswith("signal#")
    assert trace["missing_event_ids"][0].startswith("event#")
    assert trace["correlation_keys"][0].startswith("correlation#")
    assert data["recent_signals"][0]["correlation_keys"][0].startswith("correlation#")
