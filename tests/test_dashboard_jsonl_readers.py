"""Regression coverage for bounded dashboard JSONL readers."""

import importlib.util
import json
from pathlib import Path


def _load_dashboard():
    path = Path(__file__).parents[1] / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("dashboard_jsonl_reader_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_physical_tail_fixture(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            (
                json.dumps({"id": "old"}),
                "not-json-before-tail",
                json.dumps({"id": "middle"}),
                "   ",
                "not-json-in-tail",
                json.dumps({"id": "tail"}),
            )
        )
        + "\n",
        encoding="utf-8",
    )


def test_dashboard_jsonl_readers_bound_the_physical_tail_before_parsing(tmp_path):
    api = _load_dashboard()
    plain_path = tmp_path / "timeseries.jsonl"
    root = tmp_path / "state"
    state_path = root / "signals" / "inbox.jsonl"
    inner_path = root / "inner_life" / "dampeners.jsonl"
    for path in (plain_path, state_path, inner_path):
        _write_physical_tail_fixture(path)

    assert api._read_plain_jsonl(plain_path, limit=3) == [{"id": "tail"}]
    assert api._read_jsonl(root, "signals", limit=3) == ([{"id": "tail"}], 1)
    assert api._read_inner_life_rows(root, "dampeners.jsonl", limit=3) == [{"id": "tail"}]


def test_dashboard_jsonl_readers_preserve_unbounded_and_zero_limit_behavior(tmp_path):
    api = _load_dashboard()
    plain_path = tmp_path / "timeseries.jsonl"
    root = tmp_path / "state"
    state_path = root / "signals" / "inbox.jsonl"
    inner_path = root / "inner_life" / "dampeners.jsonl"
    for path in (plain_path, state_path, inner_path):
        _write_physical_tail_fixture(path)

    expected_rows = [{"id": "old"}, {"id": "middle"}, {"id": "tail"}]
    assert api._read_plain_jsonl(plain_path) == expected_rows
    assert api._read_jsonl(root, "signals") == (expected_rows, 2)
    # Existing list slicing treats -0 as zero, so [-0:] is intentionally all lines.
    assert api._read_plain_jsonl(plain_path, limit=0) == expected_rows
    assert api._read_jsonl(root, "signals", limit=0) == (expected_rows, 2)
    assert api._read_inner_life_rows(root, "dampeners.jsonl", limit=0) == expected_rows


def test_dashboard_jsonl_readers_return_empty_for_missing_and_empty_files(tmp_path):
    api = _load_dashboard()
    root = tmp_path / "state"
    plain_path = tmp_path / "missing-timeseries.jsonl"

    assert api._read_plain_jsonl(plain_path, limit=2) == []
    assert api._read_jsonl(root, "signals", limit=2) == ([], 0)
    assert api._read_inner_life_rows(root, "dampeners.jsonl", limit=2) == []

    state_path = root / "signals" / "inbox.jsonl"
    inner_path = root / "inner_life" / "dampeners.jsonl"
    for path in (plain_path, state_path, inner_path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")

    assert api._read_plain_jsonl(plain_path, limit=2) == []
    assert api._read_jsonl(root, "signals", limit=2) == ([], 0)
    assert api._read_inner_life_rows(root, "dampeners.jsonl", limit=2) == []
