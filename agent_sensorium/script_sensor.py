"""Trusted local script-sensor execution contract.

Script sensors are local, trusted, argv-list commands — never shell strings,
never remote URLs. They emit compact signals as a single JSON object, a JSON
array of objects, or JSONL (one JSON object per line) on stdout.

The runner enforces a timeout and a stdout/stderr byte cap and never lets a
bad script crash the tick: a timeout, nonzero exit, or invalid output each
become a recorded error and execution continues. Empty stdout means quiet
success.

Stdout/stderr are read from the child's pipes in bounded chunks as the
process runs (never buffered fully in memory) and the process is killed the
moment either cap is exceeded, so a misbehaving script cannot force the
runner to hold an unbounded amount of data. Error strings returned to
callers are sanitized categories (exit code, cap name, timeout) — raw
stdout/stderr content is never included, since those streams are untrusted
script output that may contain secrets, tokens, or paths.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_STDOUT_BYTES = 65536
DEFAULT_MAX_STDERR_BYTES = 65536
_READ_CHUNK_SIZE = 65536
_POLL_INTERVAL_SECONDS = 0.02
_KILL_DRAIN_TIMEOUT_SECONDS = 2.0


def _read_capped(stream, cap: int, result: dict) -> None:
    """Read a child pipe in bounded chunks, stopping as soon as `cap` is passed.

    Runs in its own thread so stdout and stderr can be drained concurrently
    without risking a classic pipe deadlock. Never accumulates more than
    `cap` bytes (plus the last chunk read) before flagging `exceeded`.
    """
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            chunk = stream.read(_READ_CHUNK_SIZE)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > cap:
                result["exceeded"] = True
                break
    except (OSError, ValueError):
        pass
    finally:
        result["data"] = b"".join(chunks)
        try:
            stream.close()
        except OSError:
            pass


def _write_stdin(stream, data: bytes, result: dict) -> None:
    """Write stdin in a helper thread so timeout polling can still kill the child."""
    try:
        stream.write(data)
        stream.flush()
        result["ok"] = True
    except (BrokenPipeError, OSError, ValueError):
        result["ok"] = False
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _parse_jsonl(text: str) -> tuple[list[dict], str | None]:
    signals: list[dict] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return [], f"invalid JSON on line {lineno}"
        if not isinstance(obj, dict):
            return [], f"line {lineno} is not a JSON object"
        signals.append(obj)
    if not signals:
        return [], "script output is not valid JSON or JSONL"
    return signals, None


def parse_script_output(text: str) -> tuple[list[dict], str | None]:
    """Parse script stdout as compact JSON (object/array) or JSONL.

    Returns (signals, error). Empty/whitespace-only text is not handled here;
    callers should treat empty stdout as quiet success before calling this.
    """
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return _parse_jsonl(text)
    if isinstance(parsed, dict):
        return [parsed], None
    if isinstance(parsed, list):
        if all(isinstance(item, dict) for item in parsed):
            return parsed, None
        return [], "script output JSON array must contain only objects"
    return [], "script output JSON must be an object or array of objects"


def run_script_sensor(
    command: list[str],
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_stdout_bytes: int = DEFAULT_MAX_STDOUT_BYTES,
    max_stderr_bytes: int = DEFAULT_MAX_STDERR_BYTES,
    cwd: str | None = None,
    env: dict | None = None,
    stdin_text: str | None = None,
) -> dict:
    """Run a trusted local script sensor; never raises on script misbehavior.

    `command` must be a non-empty argv list of strings — never a shell string.

    Returns a normalized result dict:
      {"ok": bool, "signals": [dict, ...], "error": str | None,
       "exit_code": int | None, "stdout_truncated": bool,
       "duration_seconds": float}
    """
    if not isinstance(command, list) or not command or not all(isinstance(c, str) for c in command):
        return {
            "ok": False,
            "signals": [],
            "error": "script command must be a non-empty argv list of strings",
            "exit_code": None,
            "stdout_truncated": False,
            "duration_seconds": 0.0,
        }

    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE if stdin_text is not None else None,
            cwd=cwd,
            env=env,
            shell=False,
        )
    except OSError as exc:
        return {
            "ok": False,
            "signals": [],
            "error": f"exec failed: {exc}",
            "exit_code": None,
            "stdout_truncated": False,
            "duration_seconds": round(time.monotonic() - started, 3),
        }

    stdout_result: dict = {}
    stderr_result: dict = {}
    stdin_result: dict = {}
    stdout_thread = threading.Thread(
        target=_read_capped, args=(proc.stdout, max_stdout_bytes, stdout_result), daemon=True
    )
    stderr_thread = threading.Thread(
        target=_read_capped, args=(proc.stderr, max_stderr_bytes, stderr_result), daemon=True
    )
    stdin_thread: threading.Thread | None = None
    if stdin_text is not None and proc.stdin is not None:
        stdin_thread = threading.Thread(
            target=_write_stdin,
            args=(proc.stdin, stdin_text.encode("utf-8"), stdin_result),
            daemon=True,
        )
    stdout_thread.start()
    stderr_thread.start()
    if stdin_thread is not None:
        stdin_thread.start()

    deadline = started + timeout_seconds
    timed_out = False
    bounds_exceeded = False
    while True:
        stdout_thread.join(timeout=_POLL_INTERVAL_SECONDS)
        if stdout_result.get("exceeded") or stderr_result.get("exceeded"):
            bounds_exceeded = True
            break
        stdin_done = stdin_thread is None or not stdin_thread.is_alive()
        if (
            stdin_done
            and not stdout_thread.is_alive()
            and not stderr_thread.is_alive()
            and proc.poll() is not None
        ):
            break
        if time.monotonic() > deadline:
            timed_out = True
            break

    if timed_out or bounds_exceeded:
        proc.kill()

    if stdin_thread is not None:
        stdin_thread.join(timeout=_KILL_DRAIN_TIMEOUT_SECONDS)
    stdout_thread.join(timeout=_KILL_DRAIN_TIMEOUT_SECONDS)
    stderr_thread.join(timeout=_KILL_DRAIN_TIMEOUT_SECONDS)
    try:
        proc.wait(timeout=_KILL_DRAIN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=_KILL_DRAIN_TIMEOUT_SECONDS)

    duration = round(time.monotonic() - started, 3)

    if timed_out:
        return {
            "ok": False,
            "signals": [],
            "error": f"timeout after {timeout_seconds}s",
            "exit_code": None,
            "stdout_truncated": bool(stdout_result.get("exceeded")),
            "duration_seconds": duration,
        }

    if bounds_exceeded:
        if stdout_result.get("exceeded"):
            cap_name, cap_value = "stdout", max_stdout_bytes
        else:
            cap_name, cap_value = "stderr", max_stderr_bytes
        return {
            "ok": False,
            "signals": [],
            "error": f"{cap_name} exceeded {cap_value} byte cap; process terminated",
            "exit_code": None,
            "stdout_truncated": cap_name == "stdout",
            "duration_seconds": duration,
        }

    # `bounds_exceeded` above already caught any read past `max_stdout_bytes`,
    # so reaching here means the full read fit within the cap.
    raw_stdout = stdout_result.get("data") or b""
    truncated = False
    stdout_text = raw_stdout.decode("utf-8", errors="replace").strip()

    if proc.returncode != 0:
        return {
            "ok": False,
            "signals": [],
            "error": f"nonzero exit {proc.returncode}",
            "exit_code": proc.returncode,
            "stdout_truncated": truncated,
            "duration_seconds": duration,
        }

    if not stdout_text:
        return {
            "ok": True,
            "signals": [],
            "error": None,
            "exit_code": 0,
            "stdout_truncated": truncated,
            "duration_seconds": duration,
        }

    signals, parse_error = parse_script_output(stdout_text)
    if parse_error:
        return {
            "ok": False,
            "signals": [],
            "error": parse_error,
            "exit_code": 0,
            "stdout_truncated": truncated,
            "duration_seconds": duration,
        }
    return {
        "ok": True,
        "signals": signals,
        "error": None,
        "exit_code": 0,
        "stdout_truncated": truncated,
        "duration_seconds": duration,
    }
