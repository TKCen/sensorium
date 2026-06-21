"""Tests for the read-only Perception Trace feature in the Agent Sensorium dashboard API."""

import asyncio
import importlib.util
import json
from pathlib import Path


def _load_dashboard(monkeypatch, state_dir: Path):
    monkeypatch.setenv("SENSORIUM_STATE_DIR", str(state_dir))
    path = Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("sensorium_dashboard_perception_trace_test", path)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _append_jsonl(path: Path, row: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def test_full_happy_path_trace_stitches_signal_event_settlement(tmp_path, monkeypatch):
    """One candidate with event -> signal, SAVE settlement, all stages reached, green band."""
    root = tmp_path / "sensorium" / "demo"

    _append_jsonl(root / "signals" / "inbox.jsonl", {
        "id": "sig_001",
        "kind": "inference_budget_pressure",
        "sensor": "sensorium.codex_usage",
        "summary": "usage spike detected",
        "strength_hint": 0.9,
        "pressure_level": "critical",
        "transition": "normal_to_critical",
        "ts": "2026-06-01T10:00:00Z",
    })
    _append_jsonl(root / "events.jsonl", {
        "id": "evt_001",
        "type": "pressure_event",
        "kind": "pressure_spike",
        "summary": "budget pressure crossed threshold",
        "strength": 0.85,
        "source_signal_ids": ["sig_001"],
        "ts": "2026-06-01T10:01:00Z",
    })
    _append_jsonl(root / "candidates.jsonl", {
        "id": "cand_001",
        "kind": "budget_pressure",
        "status": "accepted",
        "pressure": 0.85,
        "summary": "Codex usage exceeding weekly budget",
        "correlation_keys": ["codex-budget", "weekly-pace"],
        "event_ids": ["evt_001"],
        "created_at": "2026-06-01T10:02:00Z",
        "updated_at": "2026-06-01T10:05:00Z",
        "kanban_settlement": {
            "decision": "SAVE",
            "reason": "High priority budget issue requires attention",
            "intake_task_id": "task_intake_001",
            "review_task_id": "task_review_001",
            "settled_at": "2026-06-01T10:10:00Z",
            "conscious_task_ref": {
                "task_id": "conscious_task_001",
                "thread_id": "thread_001",
            },
        },
    })
    _append_jsonl(root / "decisions.jsonl", {
        "ts": "2026-06-01T10:10:00Z",
        "type": "kanban.settlement.applied",
        "decision": "SAVE",
        "candidate_id": "cand_001",
        "intake_task_id": "task_intake_001",
        "review_task_id": "task_review_001",
        "reason": "High priority budget issue requires attention",
    })

    mod = _load_dashboard(monkeypatch, root)
    data = asyncio.run(mod.snapshot(instance="demo"))

    traces = data["perception_traces"]
    assert len(traces) == 1
    trace = traces[0]

    # Stitching: signals and events linked correctly
    assert trace["candidate_id"] == "cand_001"
    assert len(trace["events"]) == 1
    assert trace["events"][0]["id"] == "evt_001"
    assert trace["events"][0]["signal_ids"] == ["sig_001"]
    assert len(trace["signals"]) == 1
    assert trace["signals"][0]["id"] == "sig_001"
    assert trace["signals"][0]["sensor"] == "sensorium.codex_usage"

    # Settlement
    assert trace["settlement"] is not None
    assert trace["settlement"]["decision"] == "SAVE"
    assert trace["settlement"]["reason"].startswith("reason#")
    assert trace["settlement"]["intake_task_id"].startswith("intake_task#")
    assert trace["settlement"]["review_task_id"].startswith("review_task#")
    assert trace["settlement"]["settled_at"] == "2026-06-01T10:10:00Z"
    assert trace["settlement"]["conscious_task_id"].startswith("conscious_task_id#")
    assert trace["settlement"]["conscious_thread_id"].startswith("conscious_thread_id#")
    assert trace["settlement"]["unresolved"] is False
    assert "High priority budget issue requires attention" not in json.dumps(trace, sort_keys=True)

    # All stages reached
    stage_map = {s["key"]: s for s in trace["stages"]}
    assert stage_map["signal"]["reached"] is True
    assert stage_map["event"]["reached"] is True
    assert stage_map["candidate"]["reached"] is True
    assert stage_map["kanban"]["reached"] is True
    assert stage_map["decision"]["reached"] is True
    assert stage_map["settled"]["reached"] is True

    assert trace["band"] == "green"
    assert trace["flags"] == []


def test_wrong_turn_candidate_no_events_high_pressure(tmp_path, monkeypatch):
    """Candidate with no event_ids and pressure>=0.5: no_source_events + pending_unsettled, red band."""
    root = tmp_path / "sensorium" / "demo"

    _append_jsonl(root / "candidates.jsonl", {
        "id": "cand_wrong",
        "kind": "anomaly",
        "status": "candidate",
        "pressure": 0.75,
        "summary": "Orphaned candidate with no event lineage",
        "event_ids": [],
        "created_at": "2026-06-01T09:00:00Z",
        "updated_at": "2026-06-01T09:00:00Z",
    })

    mod = _load_dashboard(monkeypatch, root)
    data = asyncio.run(mod.snapshot(instance="demo"))

    traces = data["perception_traces"]
    assert len(traces) == 1
    trace = traces[0]

    assert "no_source_events" in trace["flags"]
    assert "pending_unsettled" in trace["flags"]
    assert trace["band"] == "red"
    assert trace["settlement"] is None
    assert trace["events"] == []
    assert trace["signals"] == []


def test_dropped_candidate_yields_yellow_band_dropped_flag(tmp_path, monkeypatch):
    """Candidate with status=suppressed and DROP settlement: dropped flag, yellow band.

    The candidate must reference at least one event so that 'no_source_events' does not
    trigger (which would force red). With events present, the dropped status wins yellow.
    """
    root = tmp_path / "sensorium" / "demo"

    _append_jsonl(root / "events.jsonl", {
        "id": "evt_drop",
        "kind": "noise_event",
        "summary": "Low-signal noise event",
        "strength": 0.2,
        "source_signal_ids": [],
        "ts": "2026-06-01T07:55:00Z",
    })
    _append_jsonl(root / "candidates.jsonl", {
        "id": "cand_drop",
        "kind": "noise",
        "status": "suppressed",
        "pressure": 0.3,
        "summary": "Low-signal noise candidate suppressed",
        "event_ids": ["evt_drop"],
        "created_at": "2026-06-01T08:00:00Z",
        "updated_at": "2026-06-01T08:30:00Z",
        "kanban_settlement": {
            "decision": "DROP",
            "reason": "Not actionable noise",
            "intake_task_id": "task_drop_intake",
            "review_task_id": "task_drop_review",
            "settled_at": "2026-06-01T08:30:00Z",
            "conscious_task_ref": {},
        },
    })

    mod = _load_dashboard(monkeypatch, root)
    data = asyncio.run(mod.snapshot(instance="demo"))

    traces = data["perception_traces"]
    assert len(traces) == 1
    trace = traces[0]

    assert "dropped" in trace["flags"]
    assert trace["band"] == "yellow"
    assert trace["settlement"] is not None
    assert trace["settlement"]["decision"] == "DROP"


def test_reviewed_candidate_without_settlement_is_visible_gap(tmp_path, monkeypatch):
    """A reviewed candidate with no settlement receipt is not active, but still a yellow trace gap."""
    root = tmp_path / "sensorium" / "demo"

    _append_jsonl(root / "events.jsonl", {
        "id": "evt_reviewed_gap",
        "kind": "relational_salience",
        "summary": "Review happened but no settlement was propagated",
        "strength": 0.7,
        "source_signal_ids": [],
        "ts": "2026-06-01T08:00:00Z",
    })
    _append_jsonl(root / "candidates.jsonl", {
        "id": "cand_reviewed_gap",
        "kind": "relational_salience",
        "status": "reviewed",
        "pressure": 0.7,
        "summary": "Reviewed candidate missing canonical settlement metadata",
        "event_ids": ["evt_reviewed_gap"],
        "created_at": "2026-06-01T08:00:00Z",
        "updated_at": "2026-06-01T08:30:00Z",
    })

    mod = _load_dashboard(monkeypatch, root)
    data = asyncio.run(mod.snapshot(instance="demo"))

    trace = data["perception_traces"][0]
    assert "reviewed_without_settlement" in trace["flags"]
    assert trace["band"] == "yellow"
    assert trace["settlement"] is None


def test_bound_ordering_more_than_8_candidates_returns_8_newest_first(tmp_path, monkeypatch):
    """Create 10 candidates with distinct updated_at; assert max 8 returned newest-first."""
    root = tmp_path / "sensorium" / "demo"

    for i in range(10):
        _append_jsonl(root / "candidates.jsonl", {
            "id": f"cand_{i:02d}",
            "kind": "test",
            "status": "accepted",
            "pressure": 0.1,
            "summary": f"Candidate {i}",
            "event_ids": [],
            "created_at": f"2026-06-01T{i:02d}:00:00Z",
            "updated_at": f"2026-06-01T{i:02d}:00:00Z",
        })

    mod = _load_dashboard(monkeypatch, root)
    data = asyncio.run(mod.snapshot(instance="demo"))

    traces = data["perception_traces"]
    assert len(traces) == 8

    # Newest first: updated_at descending
    updated_ats = [t["updated_at"] for t in traces]
    assert updated_ats == sorted(updated_ats, reverse=True)

    # The 2 oldest (cand_00, cand_01) should be excluded
    ids = [t["candidate_id"] for t in traces]
    assert "cand_00" not in ids
    assert "cand_01" not in ids


def test_missing_event_ids_appear_in_missing_event_ids_field(tmp_path, monkeypatch):
    """Candidate references an event id that does not exist: it surfaces in missing_event_ids."""
    root = tmp_path / "sensorium" / "demo"

    _append_jsonl(root / "candidates.jsonl", {
        "id": "cand_gap",
        "kind": "test",
        "status": "candidate",
        "pressure": 0.6,
        "summary": "Candidate with missing event reference",
        "event_ids": ["evt_exists", "evt_missing"],
        "created_at": "2026-06-01T12:00:00Z",
        "updated_at": "2026-06-01T12:00:00Z",
    })
    _append_jsonl(root / "events.jsonl", {
        "id": "evt_exists",
        "kind": "pressure_event",
        "summary": "Real event",
        "strength": 0.5,
        "source_signal_ids": [],
        "ts": "2026-06-01T11:55:00Z",
    })
    # evt_missing is intentionally NOT written to events.jsonl

    mod = _load_dashboard(monkeypatch, root)
    data = asyncio.run(mod.snapshot(instance="demo"))

    traces = data["perception_traces"]
    assert len(traces) == 1
    trace = traces[0]

    assert "evt_missing" in trace["missing_event_ids"]
    assert "evt_exists" not in trace["missing_event_ids"]
    # The existing event should still appear
    assert len(trace["events"]) == 1
    assert trace["events"][0]["id"] == "evt_exists"
    # no_source_events should NOT be flagged since one event does exist
    assert "no_source_events" not in trace["flags"]


def test_counts_perception_traces_and_wrong_turns(tmp_path, monkeypatch):
    """counts.perception_traces == len(traces) and counts.perception_wrong_turns counts red+yellow.

    Band assignment rules (from _trace_lifecycle):
    - red: no_source_events OR settlement_unresolved (wins over all others)
    - yellow: dropped status OR pending_unsettled (when no red condition)
    - green: decision in {SAVE, PROMOTE_CONSCIOUS} (when no red/yellow condition)
    - neutral: otherwise

    To get distinct bands, each candidate needs events (to avoid forced-red from
    no_source_events). The red candidate explicitly has no events to trigger that flag.
    """
    root = tmp_path / "sensorium" / "demo"

    # Shared events for green and yellow candidates (need events to avoid no_source_events)
    _append_jsonl(root / "events.jsonl", {
        "id": "evt_green",
        "kind": "test_event",
        "summary": "Green candidate event",
        "strength": 0.8,
        "source_signal_ids": [],
        "ts": "2026-06-01T09:55:00Z",
    })
    _append_jsonl(root / "events.jsonl", {
        "id": "evt_yellow",
        "kind": "test_event",
        "summary": "Yellow candidate event",
        "strength": 0.2,
        "source_signal_ids": [],
        "ts": "2026-06-01T07:55:00Z",
    })

    # green: accepted with SAVE settlement and has events -> green band
    _append_jsonl(root / "candidates.jsonl", {
        "id": "cand_green",
        "kind": "test",
        "status": "accepted",
        "pressure": 0.8,
        "summary": "Green candidate",
        "event_ids": ["evt_green"],
        "created_at": "2026-06-01T10:00:00Z",
        "updated_at": "2026-06-01T10:00:00Z",
        "kanban_settlement": {
            "decision": "SAVE",
            "reason": "Important",
            "intake_task_id": "t1",
            "review_task_id": "t2",
            "settled_at": "2026-06-01T10:05:00Z",
            "conscious_task_ref": {},
        },
    })
    # red: candidate, high pressure, no events -> no_source_events -> red band
    _append_jsonl(root / "candidates.jsonl", {
        "id": "cand_red",
        "kind": "test",
        "status": "candidate",
        "pressure": 0.7,
        "summary": "Red candidate",
        "event_ids": [],
        "created_at": "2026-06-01T09:00:00Z",
        "updated_at": "2026-06-01T09:00:00Z",
    })
    # yellow: suppressed/dropped with events -> dropped flag -> yellow band
    _append_jsonl(root / "candidates.jsonl", {
        "id": "cand_yellow",
        "kind": "test",
        "status": "suppressed",
        "pressure": 0.2,
        "summary": "Yellow candidate",
        "event_ids": ["evt_yellow"],
        "created_at": "2026-06-01T08:00:00Z",
        "updated_at": "2026-06-01T08:00:00Z",
        "kanban_settlement": {
            "decision": "DROP",
            "reason": "Noise",
            "intake_task_id": "t3",
            "review_task_id": "t4",
            "settled_at": "2026-06-01T08:05:00Z",
            "conscious_task_ref": {},
        },
    })

    mod = _load_dashboard(monkeypatch, root)
    data = asyncio.run(mod.snapshot(instance="demo"))

    traces = data["perception_traces"]
    counts = data["counts"]

    assert counts["perception_traces"] == len(traces)
    assert counts["perception_traces"] == 3

    wrong_turn_count = sum(1 for t in traces if t["band"] in {"red", "yellow"})
    assert counts["perception_wrong_turns"] == wrong_turn_count
    assert counts["perception_wrong_turns"] == 2


def test_snapshot_does_not_mutate_jsonl_files(tmp_path, monkeypatch):
    """snapshot() must never mutate Sensorium state files (read-only guarantee)."""
    root = tmp_path / "sensorium" / "demo"

    _append_jsonl(root / "signals" / "inbox.jsonl", {
        "id": "sig_ro",
        "kind": "test_signal",
        "sensor": "test.sensor",
        "summary": "Read-only test signal",
        "strength_hint": 0.5,
        "pressure_level": "normal",
        "transition": "stable",
        "ts": "2026-06-01T07:00:00Z",
    })
    _append_jsonl(root / "events.jsonl", {
        "id": "evt_ro",
        "kind": "test_event",
        "summary": "Read-only test event",
        "strength": 0.5,
        "source_signal_ids": ["sig_ro"],
        "ts": "2026-06-01T07:01:00Z",
    })
    _append_jsonl(root / "candidates.jsonl", {
        "id": "cand_ro",
        "kind": "test",
        "status": "candidate",
        "pressure": 0.3,
        "summary": "Read-only test candidate",
        "event_ids": ["evt_ro"],
        "created_at": "2026-06-01T07:02:00Z",
        "updated_at": "2026-06-01T07:02:00Z",
    })
    _append_jsonl(root / "decisions.jsonl", {
        "ts": "2026-06-01T07:03:00Z",
        "type": "kanban.settlement.applied",
        "decision": "SAVE",
        "candidate_id": "cand_ro",
        "reason": "Saved for review",
    })

    # Capture byte-for-byte content of every JSONL file before calling snapshot
    jsonl_files = [
        root / "signals" / "inbox.jsonl",
        root / "events.jsonl",
        root / "candidates.jsonl",
        root / "decisions.jsonl",
    ]
    before = {str(p): p.read_bytes() for p in jsonl_files if p.exists()}

    mod = _load_dashboard(monkeypatch, root)
    asyncio.run(mod.snapshot(instance="demo"))

    # Assert files are byte-for-byte unchanged
    for path_str, original_bytes in before.items():
        current_bytes = Path(path_str).read_bytes()
        assert current_bytes == original_bytes, (
            f"snapshot() mutated {path_str}: expected {len(original_bytes)} bytes, "
            f"got {len(current_bytes)} bytes"
        )

    # Also assert no new JSONL files were created by snapshot()
    for p in jsonl_files:
        if p not in [Path(ps) for ps in before]:
            assert not p.exists(), f"snapshot() created unexpected file {p}"
