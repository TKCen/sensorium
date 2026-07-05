"""JSONL-backed local state store for Agent Sensorium."""

import collections
import json
import os
import tempfile
from pathlib import Path

_STATE_NAMES = {
    "signals": "signals/inbox.jsonl",
    "events": "events.jsonl",
    "candidates": "candidates.jsonl",
    "threads": "threads.jsonl",
    "decisions": "decisions.jsonl",
    "outbox": "outbox.jsonl",
    "worker_requests": "worker_requests.jsonl",
    "thread_actions": "thread_actions.jsonl",
    "artifacts": "artifacts.jsonl",
}

_DEFAULT_BASE = os.path.expanduser("~/.hermes/agent-sensorium")


def _fsync_parent(path: Path) -> None:
    """Best-effort fsync of a directory entry after replace/create."""
    try:
        fd = os.open(str(path.parent), os.O_DIRECTORY)
    except (AttributeError, OSError):
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_json(path: Path, payload: dict) -> None:
    """Atomically write JSON and fsync before replacing the target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        _fsync_parent(path)
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def atomic_rewrite_jsonl(path: Path, rows: list[dict]) -> None:
    """Atomically replace a JSONL file and fsync before making it visible."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as f:
            for row in rows:
                f.write(json.dumps(row, separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        _fsync_parent(path)
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


class SensoriumStore:
    def __init__(self, instance: str = "default", state_dir: str | None = None):
        self.instance = instance
        if state_dir:
            self._root = Path(state_dir)
        else:
            self._root = Path(_DEFAULT_BASE) / instance

    @property
    def root(self) -> Path:
        return self._root

    @property
    def paths(self) -> dict[str, Path]:
        return {name: self._root / rel for name, rel in _STATE_NAMES.items()}

    def ensure_dirs(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        (self._root / "signals").mkdir(exist_ok=True)
        (self._root / "archive").mkdir(exist_ok=True)
        (self._root / "locks").mkdir(exist_ok=True)
        (self._root / "sensors").mkdir(exist_ok=True)
        (self._root / "actuators").mkdir(exist_ok=True)
        (self._root / "inner_life").mkdir(exist_ok=True)

    def read_sensor_registry(self) -> dict:
        path = self._root / "sensors" / "registry.json"
        if not path.exists():
            return {}
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def write_sensor_registry(self, registry: dict) -> None:
        self.ensure_dirs()
        atomic_write_json(self._root / "sensors" / "registry.json", registry)

    def read_sensor_edges(self) -> dict:
        path = self._root / "sensors" / "edges.json"
        if not path.exists():
            return {}
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def write_sensor_edges(self, edges: dict) -> None:
        self.ensure_dirs()
        atomic_write_json(self._root / "sensors" / "edges.json", edges)

    def read_block_run_state(self, block_id: str) -> dict:
        path = self._root / "sensors" / "run_state" / f"{block_id}.json"
        if not path.exists():
            return {}
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def write_block_run_state(self, block_id: str, state: dict) -> None:
        path = self._root / "sensors" / "run_state" / f"{block_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, state)

    def append_dampener_effect(self, effect: dict) -> None:
        """Append-only audit trace of a dampener_effect record (inner_life/dampeners.jsonl).

        Reversible/derived telemetry only -- never the durable signal/event/
        candidate/decision spine.
        """
        self.ensure_dirs()
        path = self._root / "inner_life" / "dampeners.jsonl"
        with open(path, "a") as f:
            f.write(json.dumps(effect, separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())
        _fsync_parent(path)

    def read_dampener_effects(self, limit: int | None = None) -> list[dict]:
        path = self._root / "inner_life" / "dampeners.jsonl"
        if not path.exists():
            return []
        results: list[dict] = []
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    results.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        if limit is not None:
            results = results[-limit:]
        return results

    def append_blocker_effect(self, effect: dict) -> None:
        """Append-only audit trace of a blocker_effect record (inner_life/blockers.jsonl)."""
        self.ensure_dirs()
        path = self._root / "inner_life" / "blockers.jsonl"
        with open(path, "a") as f:
            f.write(json.dumps(effect, separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())
        _fsync_parent(path)

    def read_blocker_effects(self, limit: int | None = None) -> list[dict]:
        path = self._root / "inner_life" / "blockers.jsonl"
        if not path.exists():
            return []
        results: list[dict] = []
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    results.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        if limit is not None:
            results = results[-limit:]
        return results

    def read_sensor_policy(self) -> dict:
        path = self._root / "sensors" / "policy.json"
        if not path.exists():
            return {}
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def write_sensor_policy(self, policy: dict) -> None:
        self.ensure_dirs()
        atomic_write_json(self._root / "sensors" / "policy.json", policy)

    def _resolve(self, name: str) -> Path:
        rel = _STATE_NAMES.get(name)
        if not rel:
            raise ValueError(f"Unknown state name: {name}")
        return self._root / rel

    def append_jsonl(self, name: str, obj: dict) -> None:
        path = self._resolve(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(obj, separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())
        _fsync_parent(path)

    def rewrite_jsonl(self, name: str, rows: list[dict]) -> None:
        atomic_rewrite_jsonl(self._resolve(name), rows)

    def count_jsonl(self, name: str) -> int:
        """Fast O(N) line count using binary chunked reading (no JSON parsing)."""
        path = self._resolve(name)
        if not path.exists():
            return 0
        count = 0
        last_char = b""
        with open(path, "rb") as f:
            # Use 1MB buffer for efficient counting of newlines
            buf_size = 1024 * 1024
            read_f = f.read
            buf = read_f(buf_size)
            while buf:
                count += buf.count(b"\n")
                last_char = buf[-1:]
                buf = read_f(buf_size)
        # Robust against missing trailing newline
        if last_char and last_char != b"\n":
            count += 1
        return count

    def read_jsonl(self, name: str, limit: int | None = None) -> list[dict]:
        path = self._resolve(name)
        if not path.exists():
            return []
        results: list[dict] = []
        with open(path) as f:
            if limit is not None and limit > 0:
                # Optimized tail read: only parse the last N lines
                for line in collections.deque(f, maxlen=limit):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        results.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            else:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        results.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue  # skip corrupted lines
        return results

    def write_state(self, obj: dict) -> None:
        self.ensure_dirs()
        atomic_write_json(self._root / "state.latest.json", obj)

    def read_state(self) -> dict:
        path = self._root / "state.latest.json"
        if not path.exists():
            return {}
        with open(path) as f:
            return json.load(f)
