"""JSONL-backed local state store for Agent Sensorium."""

import json
import os
from pathlib import Path

_STATE_NAMES = {
    "signals": "signals/inbox.jsonl",
    "events": "events.jsonl",
    "candidates": "candidates.jsonl",
    "threads": "threads.jsonl",
    "decisions": "decisions.jsonl",
}

_DEFAULT_BASE = os.path.expanduser("~/.hermes/agent-sensorium")


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

    def read_jsonl(self, name: str, limit: int | None = None) -> list[dict]:
        path = self._resolve(name)
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
                    continue  # skip corrupted lines
        if limit is not None:
            results = results[-limit:]
        return results

    def write_state(self, obj: dict) -> None:
        self.ensure_dirs()
        path = self._root / "state.latest.json"
        with open(path, "w") as f:
            json.dump(obj, f, indent=2)

    def read_state(self) -> dict:
        path = self._root / "state.latest.json"
        if not path.exists():
            return {}
        with open(path) as f:
            return json.load(f)
