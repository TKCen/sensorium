"""Durable bounded-attempt helpers for native Sensorium clocks.

Callers hold their existing clock lock while reading/writing state.  This module
never opens a second store or lock and never makes semantic lifecycle choices.
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

ATTEMPT_SCHEMA_VERSION = 1
MAX_ATTEMPT_HISTORY = 16
RETRY_DELAYS_SECONDS = (1800, 7200)
TERMINAL_STATUSES = {"succeeded", "failed"}


def _utc(value: str | None = None) -> datetime:
    if value:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def utc_iso(value: datetime | None = None) -> str:
    return (value or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")


def process_start_token(pid: int) -> str | None:
    """Linux boot-relative process start token; None means no exact live owner."""
    try:
        # comm is parenthesized and may contain spaces; fields after the final ')' start at field 3.
        rest = open(f"/proc/{int(pid)}/stat", encoding="utf-8").read().rsplit(")", 1)[1].split()
        return rest[19]  # field 22 (starttime)
    except (OSError, ValueError, IndexError):
        return None


def exact_owner_alive(attempt: dict[str, Any]) -> bool:
    try:
        pid = int(attempt.get("owner_pid"))
    except (TypeError, ValueError):
        return False
    expected = str(attempt.get("owner_start_token") or "")
    actual = process_start_token(pid)
    return bool(expected and actual and expected == actual)


def _append_history(state: dict[str, Any], attempt: dict[str, Any]) -> None:
    history = list(state.get("attempt_history") or [])
    history.append(dict(attempt))
    state["attempt_history"] = history[-MAX_ATTEMPT_HISTORY:]


def _valid_retry_entry(value: Any, source_revision: str) -> bool:
    return bool(
        isinstance(value, dict)
        and value.get("source_revision") == source_revision
        and isinstance(value.get("last_ordinal"), int)
        and not isinstance(value.get("last_ordinal"), bool)
        and int(value["last_ordinal"]) >= 0
        and value.get("last_status") in {"active", "succeeded", "failed"}
        and (value.get("retry_not_before") is None or isinstance(value.get("retry_not_before"), str))
    )


def _retry_states_for_update(state: dict[str, Any], source_revision: str) -> dict[str, Any]:
    """Return a writable copy without laundering present durable corruption."""
    if "retry_states" not in state:
        return {}
    states = state.get("retry_states")
    if not isinstance(states, dict):
        raise ValueError("retry_states_malformed")
    if source_revision in states and not _valid_retry_entry(states[source_revision], source_revision):
        raise ValueError("retry_state_malformed")
    return dict(states)


def _set_retry_state(state: dict[str, Any], entry: dict[str, Any]) -> None:
    """Update the compatibility projection and retain every semantic revision."""
    revision = str(entry["source_revision"])
    value = dict(entry)
    states = _retry_states_for_update(state, revision)
    states[revision] = value
    state["retry_states"] = states
    state["retry_state"] = dict(value)


def recover_interrupted_attempt(state: dict[str, Any], *, now: str | None = None) -> str | None:
    """Terminalize only an expired attempt or one whose exact PID/start-token owner is dead."""
    active = state.get("active_attempt")
    if not isinstance(active, dict) or active.get("status") in TERMINAL_STATUSES:
        return None
    current = _utc(now)
    try:
        expired = _utc(str(active.get("deadline_at"))) <= current
    except (TypeError, ValueError):
        expired = True
    if exact_owner_alive(active) and not expired:
        return "owner_alive"
    terminalize_attempt(
        state, success=False, failure_class="interrupted_outer_kill", now=utc_iso(current))
    return "recovered"


def reconcile_applied_attempt(
    state: dict[str, Any], *, source_revision: str, disposition_ref: str,
) -> bool:
    """Replace an interrupted applying receipt only when canonical apply proof exists."""
    history = state.get("attempt_history")
    if not isinstance(history, list) or not history:
        return False
    row = history[-1]
    if not (
        isinstance(row, dict)
        and row.get("failure_class") == "interrupted_outer_kill"
        and row.get("stage") == "applying"
        and row.get("source_revision") == source_revision
        and disposition_ref
    ):
        return False
    _retry_states_for_update(state, source_revision)
    row.update(
        status="succeeded", failure_class=None, retry_not_before=None,
        disposition_ref=disposition_ref,
    )
    _set_retry_state(state, {
        "source_revision": source_revision,
        "last_ordinal": int(row.get("ordinal") or 0),
        "retry_not_before": None,
        "last_status": "succeeded",
    })
    return True


def retry_gate(
    state: dict[str, Any], source_revision: str, *, now: str | None = None,
    due_revisit: bool = False,
) -> tuple[bool, str, int]:
    """One initial try plus two revision-bound retries; force is intentionally irrelevant."""
    states = state.get("retry_states")
    if "retry_states" in state and not isinstance(states, dict):
        return False, "retry_state_malformed", 1
    if isinstance(states, dict) and source_revision in states:
        retry = states[source_revision]
        if not _valid_retry_entry(retry, source_revision):
            return False, "retry_state_malformed", 1
    else:
        retry = state.get("retry_state")
    if not _valid_retry_entry(retry, source_revision):
        # Legacy derivation is deliberately limited to evidence still present.
        relevant = [
            row for row in state.get("attempt_history") or []
            if row.get("source_revision") == source_revision and int(row.get("ordinal") or 0) > 0
        ]
        last = relevant[-1] if relevant else {}
        retry = {
            "source_revision": source_revision,
            "last_ordinal": max((int(row.get("ordinal") or 0) for row in relevant), default=0),
            "retry_not_before": last.get("retry_not_before"),
            "last_status": last.get("status") if last.get("status") in {"active", "succeeded", "failed"} else "succeeded",
        }
    assert isinstance(retry, dict)
    if due_revisit:
        return True, "ready", 1
    last_ordinal = int(retry.get("last_ordinal") or 0)
    next_ordinal = last_ordinal + 1
    if last_ordinal >= 3 and retry.get("last_status") == "failed":
        return False, "retry_exhausted", next_ordinal
    if retry.get("last_status") == "active":
        return False, "retry_active", last_ordinal
    if retry.get("last_status") == "failed":
        retry_at = retry.get("retry_not_before")
        if retry_at:
            try:
                if _utc(now) < _utc(str(retry_at)):
                    return False, "retry_cooldown", next_ordinal
            except ValueError:
                return False, "retry_cooldown", next_ordinal
    return True, "ready", next_ordinal


def start_attempt(
    state: dict[str, Any], *, session_purpose: str, source_revision: str | None,
    ordinal: int, stage: str, deadline_seconds: int, now: str | None = None,
) -> dict[str, Any]:
    if session_purpose != "autonomous":
        raise ValueError("native attempts require session_purpose=autonomous")
    if source_revision is not None and int(ordinal) > 0:
        _retry_states_for_update(state, source_revision)
    current = _utc(now)
    token = process_start_token(os.getpid()) or "unknown"
    attempt = {
        "attempt_id": uuid.uuid4().hex,
        "session_purpose": session_purpose,
        "source_revision": source_revision,
        "owner_pid": os.getpid(),
        "owner_start_token": token,
        "started_at": utc_iso(current),
        "deadline_at": utc_iso(current + timedelta(seconds=int(deadline_seconds))),
        "ordinal": int(ordinal),
        "stage": stage,
        "status": "active",
    }
    state["attempt_schema_version"] = ATTEMPT_SCHEMA_VERSION
    state["active_attempt"] = attempt
    state.setdefault("attempt_history", [])
    if source_revision is not None and int(ordinal) > 0:
        _set_retry_state(state, {
            "source_revision": source_revision,
            "last_ordinal": int(ordinal),
            "retry_not_before": None,
            "last_status": "active",
        })
    return attempt


def bind_attempt(state: dict[str, Any], *, source_revision: str, ordinal: int, stage: str) -> dict[str, Any]:
    active = state.get("active_attempt")
    if not isinstance(active, dict) or active.get("status") != "active":
        raise ValueError("no active attempt to bind")
    _retry_states_for_update(state, source_revision)
    active.update(source_revision=source_revision, ordinal=int(ordinal), stage=stage)
    _set_retry_state(state, {
        "source_revision": source_revision,
        "last_ordinal": int(ordinal),
        "retry_not_before": None,
        "last_status": "active",
    })
    return active


def advance_attempt(state: dict[str, Any], stage: str) -> None:
    active = state.get("active_attempt")
    if not isinstance(active, dict) or active.get("status") != "active":
        raise ValueError("no active attempt to advance")
    active["stage"] = stage


def terminalize_attempt(
    state: dict[str, Any], *, success: bool, failure_class: str | None = None,
    disposition_ref: str | None = None, now: str | None = None,
) -> dict[str, Any]:
    active = state.get("active_attempt")
    if not isinstance(active, dict):
        raise ValueError("no active attempt to terminalize")
    ordinal = int(active.get("ordinal") or 0)
    if active.get("source_revision") is not None and ordinal > 0:
        _retry_states_for_update(state, str(active["source_revision"]))
    completed = _utc(now)
    active["status"] = "succeeded" if success else "failed"
    active["failure_class"] = None if success else (failure_class or "unknown_failure")
    active["completed_at"] = utc_iso(completed)
    delay = RETRY_DELAYS_SECONDS[ordinal - 1] if not success and 1 <= ordinal <= 2 else None
    active["retry_not_before"] = utc_iso(completed + timedelta(seconds=delay)) if delay else None
    if disposition_ref:
        active["disposition_ref"] = disposition_ref
    if active.get("source_revision") is not None and ordinal > 0:
        _set_retry_state(state, {
            "source_revision": active["source_revision"],
            "last_ordinal": ordinal,
            "retry_not_before": active["retry_not_before"],
            "last_status": active["status"],
        })
    _append_history(state, active)
    state["active_attempt"] = None
    return active


def classify_failure(exc: BaseException, *, stage: str) -> str:
    if isinstance(exc, subprocess.TimeoutExpired):
        return "provider_subprocess_timeout" if stage == "reasoning" else "sensor_failure"
    text = str(exc).lower()
    if "memory" in text and ("unavailable" in text or "failed" in text):
        return "memory_unavailable"
    if "invalid json" in text or "no json" in text or "invalid conscious" in text:
        return "invalid_disposition"
    if "iteration" in text or "exhaust" in text:
        return "turn_exhaustion"
    if "exited with" in text or "returncode" in text or "session failed" in text:
        return "nonzero_child"
    if stage == "applying":
        return "apply_failure"
    return "sensor_failure" if stage == "sensing" else "provider_failure"


def _descendant_pids(root_pid: int) -> set[int]:
    parents: dict[int, int] = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return set()
    for name in entries:
        if not name.isdigit():
            continue
        try:
            rest = open(f"/proc/{name}/stat", encoding="utf-8").read().rsplit(")", 1)[1].split()
            parents[int(name)] = int(rest[1])  # field 4 ppid
        except (OSError, ValueError, IndexError):
            continue
    found: set[int] = set()
    frontier = {root_pid}
    while frontier:
        children = {pid for pid, ppid in parents.items() if ppid in frontier and pid not in found}
        found.update(children)
        frontier = children
    return found


def _pid_running(pid: int) -> bool:
    try:
        rest = open(f"/proc/{pid}/stat", encoding="utf-8").read().rsplit(")", 1)[1].split()
        return rest[0] != "Z"
    except (OSError, ValueError, IndexError):
        return False


def kill_process_tree(
    proc: subprocess.Popen[str], *, known_descendants: dict[int, str] | None = None,
    root_start_token: str | None = None,
) -> None:
    expected = dict(known_descendants or {})
    for pid in _descendant_pids(proc.pid):
        token = process_start_token(pid)
        if token:
            expected[pid] = token
    if root_start_token:
        expected[proc.pid] = root_start_token
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if root_start_token and process_start_token(proc.pid) == root_start_token:
            with _suppress_os_error():
                os.killpg(os.getpgid(proc.pid), sig)
        for pid, token in expected.items():
            if process_start_token(pid) == token:
                with _suppress_os_error():
                    os.kill(pid, sig)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and any(
            process_start_token(pid) == token and _pid_running(pid)
            for pid, token in expected.items()
        ):
            time.sleep(0.01)
        if not any(
            process_start_token(pid) == token and _pid_running(pid)
            for pid, token in expected.items()
        ):
            break
    with _suppress_os_error():
        proc.wait(timeout=0.2)


class _suppress_os_error:
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc, tb):
        return bool(exc_type and issubclass(exc_type, OSError))


def run_bounded_command(
    command: list[str], *, timeout: float, cwd: str, text: bool = True,
    capture_output: bool = True, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    """Use a real process group on production calls; retain injectable runners for isolated tests."""
    if runner is not subprocess.run:
        return runner(command, cwd=cwd, text=text, capture_output=capture_output, timeout=timeout)
    proc = subprocess.Popen(
        command, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True)
    root_start_token = process_start_token(proc.pid)
    known_descendants: dict[int, str] = {}
    monitor_done = threading.Event()

    def monitor() -> None:
        while not monitor_done.is_set():
            for pid in _descendant_pids(proc.pid):
                token = process_start_token(pid)
                if token:
                    known_descendants[pid] = token
            time.sleep(0.005)

    watcher = threading.Thread(target=monitor, name="sensorium-process-tree", daemon=True)
    watcher.start()
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        monitor_done.set()
        watcher.join(timeout=0.2)
        kill_process_tree(
            proc, known_descendants=known_descendants,
            root_start_token=root_start_token,
        )
        raise subprocess.TimeoutExpired(command, timeout, output=exc.output, stderr=exc.stderr) from None
    finally:
        monitor_done.set()
        watcher.join(timeout=0.2)
    # A successful direct child must not leak detached background descendants.
    if any(
        process_start_token(pid) == token and _pid_running(pid)
        for pid, token in known_descendants.items()
    ):
        kill_process_tree(
            proc, known_descendants=known_descendants,
            root_start_token=root_start_token,
        )
    return subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)
