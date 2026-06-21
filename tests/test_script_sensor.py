"""Tests for the trusted local script-sensor execution contract."""

import json
import sys

from agent_sensorium.script_sensor import parse_script_output, run_script_sensor

PY = sys.executable


def _py(code: str) -> list[str]:
    return [PY, "-c", code]


class TestArgvOnly:
    def test_rejects_non_list_command(self):
        result = run_script_sensor("echo hi")
        assert result["ok"] is False
        assert "argv list" in result["error"]

    def test_rejects_empty_command(self):
        result = run_script_sensor([])
        assert result["ok"] is False

    def test_rejects_non_string_items(self):
        result = run_script_sensor([PY, 1])
        assert result["ok"] is False


class TestEmptyStdout:
    def test_empty_stdout_is_quiet_success(self):
        result = run_script_sensor(_py("pass"))
        assert result["ok"] is True
        assert result["signals"] == []
        assert result["error"] is None
        assert result["exit_code"] == 0

    def test_whitespace_only_stdout_is_quiet_success(self):
        result = run_script_sensor(_py("print('   ')"))
        assert result["ok"] is True
        assert result["signals"] == []


class TestJsonAndJsonl:
    def test_single_json_object(self):
        result = run_script_sensor(_py("import json; print(json.dumps({'a': 1}))"))
        assert result["ok"] is True
        assert result["signals"] == [{"a": 1}]

    def test_json_array_of_objects(self):
        code = "import json; print(json.dumps([{'a': 1}, {'b': 2}]))"
        result = run_script_sensor(_py(code))
        assert result["ok"] is True
        assert result["signals"] == [{"a": 1}, {"b": 2}]

    def test_json_array_with_non_object_is_error(self):
        code = "import json; print(json.dumps([{'a': 1}, 'not-an-object']))"
        result = run_script_sensor(_py(code))
        assert result["ok"] is False
        assert "array" in result["error"]

    def test_jsonl_two_lines(self):
        code = "print('{\"a\": 1}'); print('{\"b\": 2}')"
        result = run_script_sensor(_py(code))
        assert result["ok"] is True
        assert result["signals"] == [{"a": 1}, {"b": 2}]

    def test_invalid_json_records_error_not_exception(self):
        result = run_script_sensor(_py("print('not json at all {{{')"))
        assert result["ok"] is False
        assert result["signals"] == []
        assert result["error"]

    def test_jsonl_with_bad_line_records_error(self):
        code = "print('{\"a\": 1}'); print('not json')"
        result = run_script_sensor(_py(code))
        assert result["ok"] is False
        assert "line 2" in result["error"]


class TestExitCodeAndTimeout:
    def test_nonzero_exit_records_error_not_exception(self):
        result = run_script_sensor(_py("import sys; sys.exit(3)"))
        assert result["ok"] is False
        assert result["exit_code"] == 3
        assert "nonzero exit 3" in result["error"]

    def test_nonzero_exit_with_stdout_is_still_an_error(self):
        code = "import sys, json; print(json.dumps({'a': 1})); sys.exit(1)"
        result = run_script_sensor(_py(code))
        assert result["ok"] is False
        assert result["signals"] == []

    def test_timeout_records_error_not_exception(self):
        result = run_script_sensor(_py("import time; time.sleep(5)"), timeout_seconds=0.2)
        assert result["ok"] is False
        assert "timeout" in result["error"]
        assert result["exit_code"] is None

    def test_exec_failure_records_error_not_exception(self):
        result = run_script_sensor(["/nonexistent/path/to/nowhere-xyz"])
        assert result["ok"] is False
        assert result["exit_code"] is None


class TestStdoutCap:
    def test_large_stdout_is_truncated_and_capped(self):
        code = "print('x' * 5000)"
        result = run_script_sensor(_py(code), max_stdout_bytes=100)
        assert result["stdout_truncated"] is True
        # Truncated output is not valid JSON/JSONL -> recorded as error, not a crash.
        assert result["ok"] is False

    def test_stdout_within_cap_is_not_truncated(self):
        code = "import json; print(json.dumps({'a': 1}))"
        result = run_script_sensor(_py(code), max_stdout_bytes=65536)
        assert result["stdout_truncated"] is False
        assert result["ok"] is True

    def test_infinite_stdout_is_killed_without_full_buffering(self):
        # A script that floods stdout forever would hang for the full
        # timeout if the runner buffered the whole stream before applying
        # the cap (the old `subprocess.run(capture_output=True)` behavior).
        # A real streaming bound kills it almost immediately instead.
        code = (
            "import sys\n"
            "while True:\n"
            "    sys.stdout.write('x' * 65536)\n"
            "    sys.stdout.flush()\n"
        )
        result = run_script_sensor(_py(code), max_stdout_bytes=1024, timeout_seconds=10)
        assert result["ok"] is False
        assert result["exit_code"] is None
        assert "stdout" in result["error"]
        assert "byte cap" in result["error"]
        assert result["duration_seconds"] < 5.0

    def test_infinite_stderr_is_killed_without_full_buffering(self):
        code = (
            "import sys\n"
            "while True:\n"
            "    sys.stderr.write('y' * 65536)\n"
            "    sys.stderr.flush()\n"
        )
        result = run_script_sensor(_py(code), timeout_seconds=10)
        assert result["ok"] is False
        assert result["exit_code"] is None
        assert "stderr" in result["error"]
        assert "byte cap" in result["error"]
        assert result["duration_seconds"] < 5.0


class TestStderrSanitization:
    def test_stderr_content_never_appears_in_nonzero_exit_error(self):
        secret = "sk-secret-token-do-not-leak"
        code = f"import sys; sys.stderr.write({secret!r}); sys.exit(1)"
        result = run_script_sensor(_py(code))
        assert result["ok"] is False
        assert secret not in result["error"]
        assert "nonzero exit 1" in result["error"]

    def test_stdout_content_never_appears_in_bounds_exceeded_error(self):
        secret = "sk-secret-token-do-not-leak"
        code = f"import sys; sys.stdout.write({secret!r} * 50)"
        result = run_script_sensor(_py(code), max_stdout_bytes=10)
        assert result["ok"] is False
        assert secret not in result["error"]

    def test_stderr_content_never_appears_in_bounds_exceeded_error(self):
        secret = "sk-secret-token-do-not-leak"
        code = f"import sys; sys.stderr.write({secret!r} * 5000)"
        result = run_script_sensor(_py(code))
        assert result["ok"] is False
        assert secret not in json.dumps(result)


class TestParseScriptOutputHelper:
    def test_parses_object(self):
        signals, error = parse_script_output('{"a": 1}')
        assert error is None
        assert signals == [{"a": 1}]

    def test_parses_array(self):
        signals, error = parse_script_output('[{"a": 1}, {"b": 2}]')
        assert error is None
        assert signals == [{"a": 1}, {"b": 2}]

    def test_rejects_non_object_non_array(self):
        signals, error = parse_script_output("42")
        assert signals == []
        assert error is not None
