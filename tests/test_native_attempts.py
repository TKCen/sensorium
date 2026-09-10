from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_sensorium import attempts


def _failed(state: dict, ordinal: int, when: str) -> None:
    attempts.start_attempt(
        state, session_purpose="autonomous", source_revision="rev-1",
        ordinal=ordinal, stage="reasoning", deadline_seconds=60, now=when)
    attempts.terminalize_attempt(
        state, success=False, failure_class="provider_subprocess_timeout", now=when)


def test_candidate_module_is_imported_and_history_is_bounded():
    assert Path(attempts.__file__).resolve() == Path(__file__).parents[1] / "agent_sensorium" / "attempts.py"
    state: dict = {}
    for index in range(20):
        attempts.start_attempt(
            state, session_purpose="autonomous", source_revision=f"rev-{index}",
            ordinal=1, stage="reasoning", deadline_seconds=60,
            now=f"2026-09-09T00:{index:02d}:00Z")
        attempts.terminalize_attempt(state, success=True, now=f"2026-09-09T00:{index:02d}:01Z")
    assert state["attempt_schema_version"] == 1
    assert len(state["attempt_history"]) == 16
    assert state["active_attempt"] is None


def test_retry_delays_and_cap_are_revision_bound():
    state: dict = {}
    _failed(state, 1, "2026-09-09T00:00:00Z")
    assert attempts.retry_gate(state, "rev-1", now="2026-09-09T00:29:59Z")[:2] == (False, "retry_cooldown")
    assert attempts.retry_gate(state, "rev-1", now="2026-09-09T00:30:00Z") == (True, "ready", 2)
    _failed(state, 2, "2026-09-09T00:30:00Z")
    assert attempts.retry_gate(state, "rev-1", now="2026-09-09T02:29:59Z")[:2] == (False, "retry_cooldown")
    assert attempts.retry_gate(state, "rev-1", now="2026-09-09T02:30:00Z") == (True, "ready", 3)
    _failed(state, 3, "2026-09-09T02:30:00Z")
    assert attempts.retry_gate(state, "rev-1", now="2026-09-10T00:00:00Z")[:2] == (False, "retry_exhausted")
    assert attempts.retry_gate(state, "rev-2", now="2026-09-09T00:00:01Z") == (True, "ready", 1)


def test_retry_state_survives_more_than_sixteen_sensing_receipts():
    state: dict = {}
    for ordinal, when in ((1, "2026-09-09T00:00:00Z"), (2, "2026-09-09T00:30:00Z"), (3, "2026-09-09T02:30:00Z")):
        _failed(state, ordinal, when)
    for index in range(20):
        attempts.start_attempt(state, session_purpose="autonomous", source_revision=None, ordinal=0,
                               stage="sensing", deadline_seconds=60, now=f"2026-09-10T00:{index:02d}:00Z")
        attempts.terminalize_attempt(state, success=True, now=f"2026-09-10T00:{index:02d}:01Z")
    assert len(state["attempt_history"]) == 16
    assert not any(row.get("source_revision") == "rev-1" for row in state["attempt_history"])
    assert state["retry_state"] == {"source_revision": "rev-1", "last_ordinal": 3,
                                     "retry_not_before": None, "last_status": "failed"}
    assert attempts.retry_gate(state, "rev-1", now="2026-09-11T00:00:00Z")[:2] == (False, "retry_exhausted")


def test_recovery_requires_expiry_or_dead_exact_owner(monkeypatch):
    state: dict = {}
    active = attempts.start_attempt(
        state, session_purpose="autonomous", source_revision="rev", ordinal=1,
        stage="reasoning", deadline_seconds=3600, now="2026-09-09T00:00:00Z")
    monkeypatch.setattr(attempts, "process_start_token", lambda pid: active["owner_start_token"])
    assert attempts.recover_interrupted_attempt(state, now="2026-09-09T00:01:00Z") == "owner_alive"
    monkeypatch.setattr(attempts, "process_start_token", lambda pid: "reused-pid-token")
    assert attempts.recover_interrupted_attempt(state, now="2026-09-09T00:01:00Z") == "recovered"
    row = state["attempt_history"][-1]
    assert row["failure_class"] == "interrupted_outer_kill"


def test_expired_exact_live_owner_is_recovered(monkeypatch):
    state: dict = {}
    active = attempts.start_attempt(state, session_purpose="autonomous", source_revision="rev", ordinal=1,
                                    stage="reasoning", deadline_seconds=60, now="2026-09-09T00:00:00Z")
    monkeypatch.setattr(attempts, "process_start_token", lambda pid: active["owner_start_token"])
    assert attempts.recover_interrupted_attempt(state, now="2026-09-09T00:01:00Z") == "recovered"
    assert state["retry_state"]["last_status"] == "failed"


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="Linux /proc required")
def test_actual_outer_kill_is_recovered_from_persisted_state(tmp_path):
    state_file = tmp_path / "state.json"
    code = ("import json,time; from agent_sensorium.attempts import start_attempt; s={}; "
            "start_attempt(s,session_purpose='autonomous',source_revision='rev-kill',ordinal=1,"
            "stage='reasoning',deadline_seconds=60); "
            f"open({str(state_file)!r},'w').write(json.dumps(s)); time.sleep(30)")
    proc = subprocess.Popen([sys.executable, "-c", code], cwd=str(Path(__file__).parents[1]))
    deadline = time.monotonic() + 3
    state = None
    while time.monotonic() < deadline:
        try:
            state = json.loads(state_file.read_text())
        except (OSError, json.JSONDecodeError):
            time.sleep(0.01)
            continue
        break
    assert isinstance(state, dict)
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait(timeout=2)
    assert attempts.recover_interrupted_attempt(state) == "recovered"
    assert state["attempt_history"][-1]["failure_class"] == "interrupted_outer_kill"


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="Linux /proc required")
def test_real_timeout_kills_setsid_descendant(tmp_path):
    pid_file = tmp_path / "pids.json"
    child_code = "import os,time; os.setsid(); time.sleep(30)"
    parent_code = (
        "import json,os,subprocess,sys,time; "
        f"p=subprocess.Popen([sys.executable,'-c',{child_code!r}]); "
        f"open({str(pid_file)!r},'w').write(json.dumps([os.getpid(),p.pid])); "
        "time.sleep(30)"
    )
    with pytest.raises(subprocess.TimeoutExpired):
        attempts.run_bounded_command(
            [sys.executable, "-c", parent_code], timeout=0.5, cwd=str(tmp_path))
    pids = json.loads(pid_file.read_text())
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline and any(Path(f"/proc/{pid}").exists() for pid in pids):
        time.sleep(0.05)
    assert not any(Path(f"/proc/{pid}").exists() for pid in pids)


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="Linux /proc required")
def test_direct_exit_cleans_term_ignoring_setsid_descendant(tmp_path):
    pid_file = tmp_path / "child.pid"
    child_code = "import os,signal,time; os.setsid(); signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(30)"
    parent_code = ("import subprocess,sys,time; "
                   f"p=subprocess.Popen([sys.executable,'-c',{child_code!r}]); "
                   f"open({str(pid_file)!r},'w').write(str(p.pid)); time.sleep(.2)")
    with pytest.raises(subprocess.TimeoutExpired):
        attempts.run_bounded_command([sys.executable, "-c", parent_code], timeout=2, cwd=str(tmp_path))
    child_pid = int(pid_file.read_text())
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and attempts._pid_running(child_pid):
        time.sleep(0.02)
    assert not attempts._pid_running(child_pid)


@pytest.mark.parametrize("exc,stage,expected", [
    (ValueError("invalid JSON transport"), "reasoning", "invalid_disposition"),
    (RuntimeError("turn iteration exhaustion"), "reasoning", "turn_exhaustion"),
    (RuntimeError("memory unavailable"), "reasoning", "memory_unavailable"),
    (RuntimeError("deterministic apply failed"), "applying", "apply_failure"),
])
def test_failure_classification_is_specific(exc, stage, expected):
    assert attempts.classify_failure(exc, stage=stage) == expected
