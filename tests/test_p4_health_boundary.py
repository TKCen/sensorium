"""Boundary regressions ported from the independent P4 review, plus same-class controls."""
import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path

import os
import pytest

FIXED = "2026-09-10T10:00:00Z"
REPO = Path(__file__).resolve().parents[1]


def load_api(root: Path, monkeypatch):
    monkeypatch.setenv("SENSORIUM_STATE_DIR", str(root))
    monkeypatch.setenv("SENSORIUM_METRICS_DIR", str(root.parent / "metrics"))
    path = REPO / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("p4_exact_head_review_api", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "DEFAULT_ROOT", root)
    monkeypatch.setattr(mod, "DEFAULT_INSTANCE", "demo")
    monkeypatch.setattr(mod, "_now", lambda: FIXED)
    return mod


def write(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def filesig(*roots: Path):
    out = {}
    for root in roots:
        for p in sorted(root.rglob("*")):
            if p.is_file():
                out[str(p)] = (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
    return out


def gets(mod):
    return asyncio.run(mod.snapshot(instance="demo")), asyncio.run(mod.runtime_status(instance="demo"))


def base_attempt(**changes):
    row = {
        "attempt_id": "attempt-safe",
        "status": "succeeded",
        "started_at": "2026-09-10T09:50:00Z",
        "deadline_at": "2026-09-10T10:05:00Z",
        "completed_at": "2026-09-10T09:55:00Z",
    }
    row.update(changes)
    return row


def test_latest_terminal_failed_native_attempt_is_projected_without_raw_context(tmp_path, monkeypatch):
    root = tmp_path / "state" / "demo"
    root.mkdir(parents=True)
    old_success = base_attempt(attempt_id="old-success", completed_at="2026-09-10T09:52:00Z")
    latest_failure = base_attempt(
        attempt_id="latest-failure", status="failed", completed_at="2026-09-10T09:58:00Z",
        failure_class="RAW_FAILURE_CONTEXT_DO_NOT_LEAK", source_revision="RAW_SOURCE_CONTEXT_DO_NOT_LEAK",
    )
    write(root / "native_clock_state.json", {"active_attempt": None, "attempt_history": [old_success, latest_failure]})
    mod = load_api(root, monkeypatch)
    before = filesig(root)
    snap, runtime = gets(mod)
    after = filesig(root)
    expected = {"status": "failed", "reason_code": "attempt_failed", "observed_at": "2026-09-10T09:58:00Z"}
    assert snap["health"]["execution"] == runtime["health"]["execution"] == expected
    assert "RAW_FAILURE_CONTEXT" not in json.dumps([snap, runtime], sort_keys=True)
    assert "RAW_SOURCE_CONTEXT" not in json.dumps([snap, runtime], sort_keys=True)
    assert after == before


def test_explicit_generic_failed_receipt_is_projected_without_raw_context(tmp_path, monkeypatch):
    root = tmp_path / "state" / "demo"
    write(root / "sensors" / "registry.json", {"blocks": {"source_a": {"type": "sensor", "enabled": True}}})
    write(root / "sensors" / "run_state" / "source_a.json", {
        "id": "source_a", "status": "error", "ok": False, "exit_code": 7, "emitted": False,
        "last_run_at": "2026-09-10T09:59:00Z", "reason": "RAW_GENERIC_FAILURE_CONTEXT_DO_NOT_LEAK",
    })
    mod = load_api(root, monkeypatch)
    before = filesig(root)
    snap, runtime = gets(mod)
    after = filesig(root)
    for payload in (snap, runtime):
        sensing = payload["health"]["sensing"]
        assert sensing["status"] == "failed"
        assert sensing["counts_by_status"] == {"failed": 1}
        assert sensing["sources"] == [{
            "id": "source_a", "producer_status": "failed", "reason_code": "attempt_failed",
            "attempt_at": "2026-09-10T09:59:00Z", "observation_status": "unknown",
        }]
        assert "RAW_GENERIC_FAILURE_CONTEXT" not in json.dumps(payload, sort_keys=True)
    assert after == before


def test_malformed_native_status_container_fails_closed_in_gets(tmp_path, monkeypatch):
    root = tmp_path / "state" / "demo"
    root.mkdir(parents=True)
    write(root / "native_clock_state.json", {"active_attempt": None, "attempt_history": [base_attempt(status=[])]})
    mod = load_api(root, monkeypatch)
    snap, runtime = gets(mod)
    expected = {"status": "unknown", "reason_code": "attempt_receipt_malformed", "observed_at": None}
    assert snap["health"]["execution"] == runtime["health"]["execution"] == expected


def test_malformed_generic_status_container_fails_closed_in_gets(tmp_path, monkeypatch):
    root = tmp_path / "state" / "demo"
    write(root / "sensors" / "registry.json", {"blocks": {"source_a": {"type": "sensor"}}})
    write(root / "sensors" / "run_state" / "source_a.json", {
        "id": "source_a", "status": {}, "last_run_at": "2026-09-10T09:59:00Z"
    })
    mod = load_api(root, monkeypatch)
    snap, runtime = gets(mod)
    for payload in (snap, runtime):
        row = payload["health"]["sensing"]["sources"][0]
        assert row["producer_status"] == "malformed"
        assert row["reason_code"] == "receipt_malformed"


def test_symlinked_sensor_parent_cannot_escape_exact_instance_root(tmp_path, monkeypatch):
    root = tmp_path / "state" / "demo"
    outside = tmp_path / "outside" / "sensors"
    outside.mkdir(parents=True)
    write(outside / "registry.json", {"blocks": {"outside_source": {"type": "sensor"}}})
    write(outside / "run_state" / "outside_source.json", {
        "id": "outside_source", "status": "ok", "ok": True, "last_run_at": "2026-09-10T09:59:00Z"
    })
    root.mkdir(parents=True)
    (root / "sensors").symlink_to(outside, target_is_directory=True)
    mod = load_api(root, monkeypatch)
    before = filesig(root, outside)
    snap, runtime = gets(mod)
    after = filesig(root, outside)
    for payload in (snap, runtime):
        assert payload["health"]["sensing"]["sources"] == []
        assert "outside_source" not in json.dumps(payload, sort_keys=True)
    assert after == before


def test_temporally_inconsistent_native_attempts_are_malformed(tmp_path, monkeypatch):
    root = tmp_path / "state" / "demo"
    root.mkdir(parents=True)
    cases = [
        base_attempt(completed_at="2026-09-10T09:40:00Z"),
        base_attempt(completed_at="2026-09-10T10:30:00Z"),
        base_attempt(status="active", started_at="2026-09-10T10:30:00Z", completed_at=None),
        base_attempt(status="active", deadline_at="2026-09-10T09:40:00Z", completed_at=None),
    ]
    mod = load_api(root, monkeypatch)
    for attempt in cases:
        write(root / "native_clock_state.json", {"active_attempt": attempt if attempt["status"] == "active" else None, "attempt_history": [] if attempt["status"] == "active" else [attempt]})
        snap, runtime = gets(mod)
        expected = {"status": "unknown", "reason_code": "attempt_receipt_malformed", "observed_at": None}
        assert snap["health"]["execution"] == runtime["health"]["execution"] == expected


def test_future_generic_attempt_timestamp_is_malformed_not_fresh(tmp_path, monkeypatch):
    root = tmp_path / "state" / "demo"
    write(root / "sensors" / "registry.json", {"blocks": {"source_a": {"type": "sensor", "min_interval_seconds": 60}}})
    write(root / "sensors" / "run_state" / "source_a.json", {
        "id": "source_a", "status": "ok", "ok": True, "last_run_at": "2026-09-10T10:30:00Z"
    })
    mod = load_api(root, monkeypatch)
    snap, runtime = gets(mod)
    for payload in (snap, runtime):
        row = payload["health"]["sensing"]["sources"][0]
        assert row["producer_status"] == "malformed"
        assert row["reason_code"] == "receipt_malformed"


@pytest.mark.parametrize("status", [[], {}, 42, False, "bogus"])
def test_native_status_shapes_do_not_fall_back_to_older_success(tmp_path, monkeypatch, status):
    root = tmp_path / "state" / "demo"
    mod = load_api(root, monkeypatch)
    for active in (False, True):
        bad = base_attempt(status=status)
        write(root / "native_clock_state.json", {
            "active_attempt": bad if active else None,
            "attempt_history": [base_attempt()] if active else [base_attempt(), bad],
        })
        for payload in gets(mod):
            assert payload["health"]["execution"]["reason_code"] == "attempt_receipt_malformed"


@pytest.mark.parametrize("status", [[], {}, True, 42])
def test_generic_status_shapes_fail_closed(tmp_path, monkeypatch, status):
    root = tmp_path / "state" / "demo"
    write(root / "sensors/registry.json", {"blocks": {"source_a": {"type": "sensor"}}})
    write(root / "sensors/run_state/source_a.json", {
        "id": "source_a", "status": status, "ok": True, "last_run_at": "2026-09-10T09:59:00Z",
    })
    mod = load_api(root, monkeypatch)
    for payload in gets(mod):
        assert payload["health"]["sensing"]["sources"][0]["reason_code"] == "receipt_malformed"


@pytest.mark.parametrize("kind", [[], {}, True, 42])
def test_registry_kind_shapes_fail_closed(tmp_path, monkeypatch, kind):
    root = tmp_path / "state" / "demo"
    write(root / "sensors/registry.json", {"blocks": {"source_a": {"type": kind}}})
    mod = load_api(root, monkeypatch)
    for payload in gets(mod):
        assert payload["health"]["sensing"]["sources"][0]["producer_status"] == "malformed"


@pytest.mark.parametrize("target", ["run_state_parent", "registry_leaf", "native_leaf"])
def test_diagnostic_symlink_boundaries_do_not_read_outside_files(tmp_path, monkeypatch, target):
    root = tmp_path / "state" / "demo"
    outside = tmp_path / "outside"
    outside.mkdir()
    write(root / "sensors/registry.json", {"blocks": {"source_a": {"type": "sensor"}}})
    write(outside / "source_a.json", {
        "id": "source_a", "status": "ok", "ok": True, "last_run_at": "2026-09-10T09:59:00Z",
    })
    write(outside / "registry.json", {"blocks": {"outside_source": {"type": "sensor"}}})
    write(outside / "native.json", {"attempt_history": [base_attempt()]})
    if target == "run_state_parent":
        (root / "sensors/run_state").symlink_to(outside, target_is_directory=True)
    elif target == "registry_leaf":
        (root / "sensors/registry.json").unlink()
        (root / "sensors/registry.json").symlink_to(outside / "registry.json")
    else:
        (root / "native_clock_state.json").symlink_to(outside / "native.json")
    forbidden = {(p.stat().st_dev, p.stat().st_ino) for p in outside.iterdir()}
    fdopen, reads = os.fdopen, []
    def guarded(fd, *args, **kwargs):
        st = os.fstat(fd)
        if (st.st_dev, st.st_ino) in forbidden:
            reads.append(fd)
            raise AssertionError("outside-root descriptor read")
        return fdopen(fd, *args, **kwargs)
    monkeypatch.setattr(os, "fdopen", guarded)
    mod = load_api(root, monkeypatch)
    before = filesig(root, outside)
    for payload in gets(mod):
        assert "outside_source" not in json.dumps(payload)
        if target == "run_state_parent":
            assert payload["health"]["sensing"]["sources"][0]["producer_status"] == "malformed"
        elif target == "native_leaf":
            assert payload["health"]["execution"]["reason_code"] == "attempt_receipt_malformed"
    assert reads == [] and filesig(root, outside) == before


def test_real_terminal_completion_after_deadline_remains_valid_history(tmp_path, monkeypatch):
    root = tmp_path / "state" / "demo"
    attempt = base_attempt(deadline_at="2026-09-10T09:51:00Z")
    write(root / "native_clock_state.json", {"active_attempt": None, "attempt_history": [attempt]})
    mod = load_api(root, monkeypatch)
    for payload in gets(mod):
        assert payload["health"]["execution"]["status"] == "succeeded"


@pytest.mark.parametrize("target", ["native_clock_state.json", "sensors/run_state/source_a.json", "sensors/registry.json"])
def test_deep_json_fails_closed_without_parser_recursion_error(tmp_path, monkeypatch, target):
    root = tmp_path / "state" / "demo"
    write(root / "sensors/registry.json", {"blocks": {"source_a": {"type": "sensor"}}})
    path = root / target
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[" * 2000 + "]" * 2000)
    mod = load_api(root, monkeypatch)
    before = filesig(root)
    for payload in gets(mod):
        if target == "native_clock_state.json":
            assert payload["health"]["execution"]["reason_code"] == "attempt_receipt_malformed"
        elif target.endswith("source_a.json"):
            assert payload["health"]["sensing"]["sources"][0]["producer_status"] == "malformed"
        else:
            assert payload["health"]["sensing"]["sources"] == []
    assert filesig(root) == before
