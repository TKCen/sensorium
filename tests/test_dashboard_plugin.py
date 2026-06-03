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
    root = tmp_path / "sensorium" / "sera"
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

    data = asyncio.run(mod.snapshot(instance="sera"))

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
    root = tmp_path / "sensorium" / "sera"
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
    quiet_latest = kanban_root / "sera_tick_quiet.latest.json"
    quiet_latest.write_text(json.dumps({"ok": True, "ts": "2026-06-03T04:47:41Z"}))

    old = 1_780_000_000
    fresh = old + 600
    os.utime(state_latest, (old, old))
    os.utime(root / "signals" / "inbox.jsonl", (fresh, fresh))
    os.utime(quiet_latest, (fresh + 1, fresh + 1))

    mod = _load_dashboard(monkeypatch, root)

    data = asyncio.run(mod.snapshot(instance="sera"))

    freshness = data["freshness"]
    assert data["state_mtime"] == freshness["canonical_latest_mtime"]
    assert freshness["legacy_state_latest"]["deprecated"] is True
    assert freshness["legacy_state_latest"]["excluded_from_canonical"] is True
    assert freshness["jsonl"]["signals"]["mtime"] is not None
    assert freshness["sera_tick_quiet_latest"]["mtime"] is not None
    assert freshness["canonical_latest_mtime"] != freshness["legacy_state_latest"]["mtime"]
    reset_at = data["budgets"]["dispatch"]["reset_at"]
    assert reset_at != "2026-05-28T20:27:09Z"
