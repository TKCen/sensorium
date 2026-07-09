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


def test_liveness_timestamp_projection_and_candidate_reasons_are_closed(tmp_path, monkeypatch):
    mod = _load_dashboard(monkeypatch, tmp_path / "sensorium" / "demo")
    hostile = mod._liveness_item(
        state="reviewing", reason_code="reviewing_open_aperture", observed_at="RAW_TIME_SENTINEL",
        source="candidate_status", actionable=False, terminal=False,
    )
    valid = mod._liveness_item(
        state="held", reason_code="candidate_held", observed_at="2026-07-09T14:00:00+02:00",
        source="candidate_status", actionable=False, terminal=False,
    )
    fresh = mod._candidate_liveness({"status": "in_conscious_aperture", "updated_at": "2026-07-09T12:00:00Z", "conscious_aperture": {"opened_at": "2099-01-01T00:00:00Z"}})
    stale = mod._candidate_liveness({"status": "in_conscious_aperture", "updated_at": "2026-07-09T12:00:00Z", "conscious_aperture": {"state": "stale"}})

    assert hostile["observed_at"] is None
    assert valid["observed_at"] == "2026-07-09T12:00:00Z"
    assert fresh["reason_code"] == "reviewing_open_aperture"
    assert stale["reason_code"] == "stale_aperture"
    assert "RAW_TIME_SENTINEL" not in json.dumps([hostile, valid, fresh, stale], sort_keys=True)


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


def test_snapshot_hash_labels_secret_shaped_text_and_corrupt_labels(tmp_path, monkeypatch):
    root = tmp_path / "sensorium" / "demo"
    sentinel = "sk-t9-final...4242"
    _append_jsonl(root / "signals" / "inbox.jsonl", {
        "id": "sig_t9",
        "sensor": sentinel,
        "source": sentinel,
        "kind": sentinel,
        "summary": f"raw transcript {sentinel}",
        "strength_hint": 0.95,
        "pressure_level": sentinel,
        "transition": sentinel,
        "ts": "2026-06-21T12:00:00Z",
    })
    _append_jsonl(root / "events.jsonl", {
        "id": "evt_t9",
        "kind": sentinel,
        "summary": f"event summary {sentinel}",
        "source_signal_ids": ["sig_t9"],
        "ts": "2026-06-21T12:01:00Z",
    })
    _append_jsonl(root / "candidates.jsonl", {
        "id": "cand_t9",
        "kind": sentinel,
        "status": "candidate",
        "pressure": 0.8,
        "summary": f"candidate summary {sentinel}",
        "event_ids": ["evt_t9"],
        "kanban_settlement": {"decision": "SAVE", "reason_label": sentinel},
        "updated_at": "2026-06-21T12:02:00Z",
    })
    _append_jsonl(root / "decisions.jsonl", {
        "type": "kanban.settlement.applied",
        "candidate_id": "cand_t9",
        "reason_label": sentinel,
        "ts": "2026-06-21T12:03:00Z",
    })

    mod = _load_dashboard(monkeypatch, root)
    data = asyncio.run(mod.snapshot(instance="demo"))
    payload = json.dumps(data, sort_keys=True)

    assert sentinel not in payload
    assert data["top_candidates"][0]["kind"].startswith("candidate_kind#")
    assert data["top_candidates"][0]["summary"].startswith("candidate_summary#")
    trace = data["perception_traces"][0]
    assert trace["kind"].startswith("candidate_kind#")
    assert trace["summary"].startswith("candidate_summary#")
    assert trace["events"][0]["kind"].startswith("event_kind#")
    assert trace["events"][0]["summary"].startswith("event_summary#")
    assert trace["signals"][0]["kind"].startswith("signal_kind#")
    assert trace["signals"][0]["sensor"].startswith("sensor#")
    assert trace["settlement"]["reason"].startswith("reason#")
    assert data["decisions"][0]["reason"].startswith("reason#")


def test_dashboard_inner_life_routes_are_read_only_and_privacy_safe(tmp_path, monkeypatch):
    root = tmp_path / "sensorium" / "demo"
    sentinel = "sk-t9-route...4242"
    registry_dir = root / "sensors"
    registry_dir.mkdir(parents=True)
    (registry_dir / "registry.json").write_text(json.dumps({
        "version": 2,
        "blocks": {sentinel: {"type": sentinel, "status": sentinel, "enabled": True}},
    }))
    (registry_dir / "edges.json").write_text(json.dumps({"edges": [{"from": sentinel, "to": sentinel, "kind": sentinel}]}))
    run_state_dir = registry_dir / "run_state"
    run_state_dir.mkdir()
    (run_state_dir / f"{sentinel}.json").write_text(json.dumps({"status": sentinel, "reason": sentinel}))
    _append_jsonl(root / "inner_life" / "dampeners.jsonl", {
        "type": "dampener_effect",
        "source_node": sentinel,
        "target_node": sentinel,
        "mode": sentinel,
        "reason": sentinel,
    })
    _append_jsonl(root / "inner_life" / "blockers.jsonl", {
        "type": "blocker_effect",
        "source_node": sentinel,
        "target_node": sentinel,
        "policy_id": sentinel,
        "reason_label": sentinel,
    })

    mod = _load_dashboard(monkeypatch, root)
    routes = {route.path: sorted(route.methods) for route in mod.router.routes if hasattr(route, "methods")}

    for path in ["/registry", "/probe-audit", "/dampeners", "/blockers"]:
        assert routes[path] == ["GET"]
    assert all(set(methods) <= {"GET", "HEAD"} for methods in routes.values())
    # A direct dashboard triage POST has no handler; review mutations belong to
    # the conscious/admin path, never to this read-only router.
    assert "/artifacts/{artifact_id}/triage" not in routes

    payloads = [
        asyncio.run(mod.registry(instance="demo")),
        asyncio.run(mod.probe_audit(instance="demo")),
        asyncio.run(mod.dampeners(instance="demo")),
        asyncio.run(mod.blockers(instance="demo")),
    ]
    serialized = json.dumps(payloads, sort_keys=True)
    assert sentinel not in serialized
    assert "block#" in serialized
    assert "reason#" in serialized
