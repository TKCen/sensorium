import json
import os
import time
import tempfile
from pathlib import Path
from collections import deque

def _read_jsonl_original(path: Path, limit: int | None = None):
    if not path.exists():
        return [], 0
    try:
        # Simulate dashboard/plugin_api.py implementation
        lines = path.read_text(errors="ignore").splitlines()
    except Exception:
        return [], 0
    if limit is not None:
        lines = lines[-limit:]
    rows = []
    bad = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except Exception:
            bad += 1
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows, bad

def _read_jsonl_optimized(path: Path, limit: int | None = None):
    if not path.exists():
        return [], 0
    rows = []
    bad = 0
    try:
        with open(path, 'r', errors='ignore') as f:
            if limit is not None:
                lines = deque(f, maxlen=limit)
            else:
                lines = f

            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    value = json.loads(line)
                    if isinstance(value, dict):
                        rows.append(value)
                except Exception:
                    bad += 1
                    continue
    except Exception:
        return [], 0
    return rows, bad

def benchmark():
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.jsonl') as tmp:
        tmp_path = Path(tmp.name)
        print(f"Creating large JSONL file at {tmp_path}...")
        # 100k lines, each about 1KB
        payload = "s" * 1024
        for i in range(100000):
            tmp.write(json.dumps({"id": i, "data": payload}) + "\n")

    try:
        limit = 5000

        print(f"Benchmarking original _read_jsonl with limit={limit}...")
        start = time.perf_counter()
        rows_orig, bad_orig = _read_jsonl_original(tmp_path, limit=limit)
        end = time.perf_counter()
        orig_time = end - start
        print(f"Original took: {orig_time:.4f}s, rows: {len(rows_orig)}")

        print(f"Benchmarking optimized _read_jsonl with limit={limit}...")
        start = time.perf_counter()
        rows_opt, bad_opt = _read_jsonl_optimized(tmp_path, limit=limit)
        end = time.perf_counter()
        opt_time = end - start
        print(f"Optimized took: {opt_time:.4f}s, rows: {len(rows_opt)}")

        speedup = orig_time / opt_time
        print(f"Speedup: {speedup:.2f}x")

        assert rows_orig == rows_opt
        assert bad_orig == bad_opt

    finally:
        os.unlink(tmp_path)

if __name__ == "__main__":
    benchmark()
