import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace


def _load_sensor_module():
    path = Path(__file__).resolve().parents[1] / "live-scripts" / "project_update_sensor.py"
    spec = importlib.util.spec_from_file_location("project_update_sensor_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_github_release_signal_digest_stays_under_script_sensor_cap():
    sensor = _load_sensor_module()
    args = SimpleNamespace(
        sensor="hindsight_updates",
        label="Hindsight",
        kind_prefix="hindsight_update",
    )
    sample = {
        "ok": True,
        "mode": "github-release",
        "github_repo": "vectorize-io/hindsight",
        "current_version": "0.8.3",
        "current_version_normalized": "v0.8.3",
        "latest_tag": "v0.8.4",
        "html_url": "https://github.com/vectorize-io/hindsight/releases/tag/v0.8.4",
    }
    digest = {
        "source": "github_release_api",
        "release_name": "v0.8.4",
        "published_at": "2026-07-01T12:04:39Z",
        "html_url": sample["html_url"],
        "compare_url": "https://github.com/vectorize-io/hindsight/compare/v0.8.3...v0.8.4",
        "body_excerpt": "x" * 12_000,
        "body_truncated": True,
        "compare_digest": {
            "source": "github_compare_api",
            "ahead_by": 131,
            "commit_count_reported": 131,
            "compare_url": "https://github.com/vectorize-io/hindsight/compare/v0.8.3...v0.8.4",
            "commits_truncated": True,
            "files_truncated": True,
            "commits": [
                {
                    "sha": f"{i:040x}",
                    "short_sha": f"{i:012x}",
                    "subject": "very long commit subject " + ("y" * 500),
                    "author": "author " + ("z" * 100),
                    "date": "2026-07-01T12:04:39Z",
                }
                for i in range(40)
            ],
            "files_sample": [f"very/long/path/{i}/" + ("name" * 80) for i in range(80)],
        },
    }

    signal = sensor.build_signal(args, sample, digest, state={})
    assert signal is not None
    encoded = json.dumps(signal, separators=(",", ":")).encode("utf-8")

    assert len(encoded) < 4096
    assert signal["change_digest"]["body_excerpt"] == "x" * 300
    assert len(signal["change_digest"]["compare_digest"]["commits"]) == 3
    assert len(signal["change_digest"]["compare_digest"]["files_sample"]) == 5


def test_state_is_not_advanced_when_stdout_flush_fails(monkeypatch, tmp_path):
    sensor = _load_sensor_module()
    monkeypatch.setattr(sensor, "HOME", tmp_path)

    def fake_collect(_args):
        return (
            {
                "ok": True,
                "mode": "github-release",
                "github_repo": "example/project",
                "current_version": "1.0.0",
                "current_version_normalized": "v1.0.0",
                "latest_tag": "v1.1.0",
                "latest_name": "v1.1.0",
                "html_url": "https://github.com/example/project/releases/tag/v1.1.0",
            },
            {
                "source": "github_release_api",
                "release_name": "v1.1.0",
                "html_url": "https://github.com/example/project/releases/tag/v1.1.0",
                "body_excerpt": "release notes",
            },
        )

    class FlushFailingStdout:
        def __init__(self):
            self.buffer = ""

        def write(self, text):
            self.buffer += text
            return len(text)

        def flush(self):
            raise BrokenPipeError("simulated cap/pipe failure")

    monkeypatch.setattr(sensor, "collect_github_release", fake_collect)
    monkeypatch.setattr(sys, "argv", [
        "project_update_sensor.py",
        "--mode", "github-release",
        "--sensor", "example_updates",
        "--label", "Example",
        "--kind-prefix", "example_update",
        "--github-repo", "example/project",
        "--current-version", "1.0.0",
        "--force",
    ])
    stdout = FlushFailingStdout()
    monkeypatch.setattr(sys, "stdout", stdout)

    try:
        sensor.main()
    except BrokenPipeError:
        pass
    else:
        raise AssertionError("main should surface the stdout failure")

    state_file = tmp_path / ".hermes" / "agent-sensorium" / "sera" / "example_updates_state.json"
    assert not state_file.exists()
    assert "example_update_available" in stdout.buffer
