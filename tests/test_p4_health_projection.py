"""Focused P4 truthful-health projection proof."""
import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path

FIXED = "2026-09-10T10:00:00Z"


def _load(root: Path, monkeypatch):
    monkeypatch.setenv("SENSORIUM_STATE_DIR", str(root))
    monkeypatch.setenv("SENSORIUM_METRICS_DIR", str(root.parent / "metrics"))
    path = Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("p4_health_projection_api", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "_now", lambda: FIXED)
    return mod


def _write(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _append(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value) + "\n")


def _snapshot(mod):
    return asyncio.run(mod.snapshot(instance="demo"))


def _runtime(mod):
    return asyncio.run(mod.runtime_status(instance="demo"))


def test_empty_is_unknown_and_checkpoint_projection_has_snapshot_runtime_parity(tmp_path, monkeypatch):
    root = tmp_path / "state" / "demo"
    root.mkdir(parents=True)
    mod = _load(root, monkeypatch)
    empty = _snapshot(mod)
    assert empty["health"]["status"] == "unknown"
    assert empty["health"]["attention"] == {
        "status": "unknown", "reason_code": "no_observation", "counts_by_state": {},
        "observation_scope": "bounded_dashboard_snapshot",
    }
    held = {"id": "cand_due", "status": "held", "held_return": {"reason_code": "time_checkpoint", "not_before": "2026-09-10T09:59:59Z"}}
    _append(root / "candidates.jsonl", held)
    snap, runtime = _snapshot(mod), _runtime(mod)
    assert snap["health"]["attention"]["status"] == "overdue"
    node = next(n for n in runtime["nodes"] if n["kind"] == "candidate")
    assert node["status"] == node["liveness"]["state"] == "overdue"
    assert runtime["health"]["attention"] == snap["health"]["attention"]
    monkeypatch.setattr(mod, "_now", lambda: "2026-09-10T09:00:00Z")
    assert _snapshot(mod)["health"]["attention"]["status"] == "awaiting_checkpoint"
    assert next(n for n in _runtime(mod)["nodes"] if n["kind"] == "candidate")["status"] == "awaiting_checkpoint"


def test_execution_projects_current_terminal_expired_and_malformed_without_old_failure_poison(tmp_path, monkeypatch):
    root = tmp_path / "state" / "demo"
    root.mkdir(parents=True)
    mod = _load(root, monkeypatch)
    base = {"attempt_id": "a1", "started_at": "2026-09-10T09:55:00Z", "deadline_at": "2026-09-10T10:05:00Z"}
    _write(root / "native_clock_state.json", {"active_attempt": {**base, "status": "active"}, "attempt_history": []})
    assert _snapshot(mod)["health"]["execution"]["status"] == "active"
    _write(root / "native_clock_state.json", {"active_attempt": {**base, "deadline_at": "2026-09-10T09:59:00Z", "status": "active"}, "attempt_history": []})
    assert _snapshot(mod)["health"]["execution"]["status"] == "stale"
    failed = {**base, "status": "failed", "completed_at": "2026-09-10T09:57:00Z", "failure_class": "private failure must not leak"}
    succeeded = {**base, "attempt_id": "a2", "status": "succeeded", "completed_at": "2026-09-10T09:58:00Z"}
    _write(root / "native_clock_state.json", {"active_attempt": None, "attempt_history": [failed, succeeded]})
    payload = _snapshot(mod)
    assert payload["health"]["execution"] == {"status": "succeeded", "reason_code": "attempt_succeeded", "observed_at": "2026-09-10T09:58:00Z"}
    assert "private failure" not in json.dumps(payload)
    _write(root / "native_clock_state.json", {"active_attempt": {"attempt_id": "a3", "status": "active"}, "attempt_history": []})
    assert _snapshot(mod)["health"]["execution"]["reason_code"] == "attempt_receipt_malformed"


def test_sensing_diagnostics_are_bounded_exact_and_never_claim_observation(tmp_path, monkeypatch):
    root = tmp_path / "state" / "demo"
    blocks = {
        "absent": {"type": "sensor", "enabled": True},
        "bad": {"type": "sensor", "enabled": True},
        "wrong": {"type": "sensor", "enabled": True},
        "huge": {"type": "sensor", "enabled": True},
        "stale": {"type": "sensor", "enabled": True, "min_interval_seconds": 60},
        "fresh": {"type": "sensor", "enabled": True, "min_interval_seconds": 3600},
        "unknown_cadence": {"type": "sensor", "enabled": True},
        "disabled": {"type": "sensor", "enabled": False},
        "memory": {"type": "memory_reflector", "enabled": True},
        "../RAW_SECRET_TOKEN": {"type": "sensor", "enabled": True},
    }
    _write(root / "sensors" / "registry.json", {"blocks": blocks})
    run = root / "sensors" / "run_state"
    run.mkdir(parents=True)
    (run / "bad.json").write_text("not-json", encoding="utf-8")
    _write(run / "wrong.json", {"id": "other", "status": "ok", "ok": True, "last_run_at": "2026-09-10T09:59:00Z"})
    (run / "huge.json").write_text("{" + "x" * 70000, encoding="utf-8")
    for source, at in [("stale", "2026-09-10T09:00:00Z"), ("fresh", "2026-09-10T09:59:00Z"), ("unknown_cadence", "2026-09-10T09:59:00Z")]:
        _write(run / f"{source}.json", {"id": source, "status": "ok", "ok": True, "emitted": False, "last_run_at": at})
    mod = _load(root, monkeypatch)
    payload = _snapshot(mod)
    sensing = payload["health"]["sensing"]
    rows = {row["id"]: row for row in sensing["sources"]}
    assert sensing["status"] == "stale" and sensing["truncated"] is False
    assert rows["absent"]["producer_status"] == "no_receipt"
    assert rows["bad"]["reason_code"] == "receipt_malformed"
    assert rows["wrong"]["reason_code"] == "identity_mismatch"
    assert rows["huge"]["producer_status"] == "malformed"
    assert rows["stale"]["producer_status"] == "stale"
    assert rows["fresh"]["reason_code"] == "attempt_completed_observation_unknown"
    assert rows["unknown_cadence"]["reason_code"] == "cadence_unknown"
    assert rows["disabled"]["producer_status"] == "disabled"
    assert rows["memory"]["reason_code"] == "projection_unconnected"
    assert all(row["observation_status"] == "unknown" for row in sensing["sources"])
    assert "RAW_SECRET_TOKEN" not in json.dumps(payload)


def test_recent_exact_signal_only_proves_activity_and_gets_preserve_inputs(tmp_path, monkeypatch):
    root = tmp_path / "state" / "demo"
    _write(root / "sensors" / "registry.json", {"blocks": {"exact": {"type": "sensor"}, "other": {"type": "sensor"}}})
    _append(root / "signals" / "inbox.jsonl", {"id": "old", "sensor": "other", "ts": "2026-09-08T10:00:00Z"})
    _append(root / "signals" / "inbox.jsonl", {"id": "new", "sensor": "exact", "ts": "2026-09-10T09:59:00Z"})
    mod = _load(root, monkeypatch)
    before_paths = sorted(p for p in root.rglob("*") if p.is_file())
    before = {p.relative_to(root).as_posix(): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in before_paths}
    for _ in range(2):
        _snapshot(mod)
        runtime = _runtime(mod)
    nodes = {n["id"]: n for n in runtime["nodes"]}
    assert nodes["sensor:exact"]["status"] == "active"
    assert nodes["sensor:exact"]["observation_status"] == "unknown"
    assert nodes["sensor:other"]["status"] == "unknown"
    after_paths = sorted(p for p in root.rglob("*") if p.is_file())
    after = {p.relative_to(root).as_posix(): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in after_paths}
    assert after == before
    routes = {route.path: route.methods for route in mod.router.routes if hasattr(route, "methods")}
    assert all(set(methods) <= {"GET", "HEAD"} for methods in routes.values())
