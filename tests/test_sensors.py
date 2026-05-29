"""Tests for Phase 7 — deterministic compact sensor helpers."""

import json
import tempfile

import pytest

from agent_sensorium.sensors import (
    MAX_REF_CHARS,
    MAX_SUMMARY_CHARS,
    artifact_signal,
    classify_media_capacity,
    classify_hindsight_pressure,
    classify_kanban_pressure,
    classify_machine_body_pressure,
    classify_machine_network_pressure,
    classify_machine_process_pressure,
    classify_tts_sidecar_pressure,
    file_content_hash,
    hindsight_pressure_sample,
    kanban_pressure_sample,
    machine_body_pressure_sample,
    machine_network_pressure_sample,
    machine_process_pressure_sample,
    media_capacity_sample,
    operator_signal,
    replay_machine_body_pressure,
    replay_media_capacity,
    session_event_signal,
    tts_sidecar_pressure_sample,
    wsl_disk_paths,
)


class TestSessionEventSignal:
    """Session-event sensor emits compact signals only."""

    def test_emits_compact_dict(self):
        sig = session_event_signal(kind="design_decision", summary="User discussed architecture")
        assert isinstance(sig, dict)
        assert sig["sensor"] == "sensorium.session_event"
        assert sig["source"] == "hermes_session"
        assert sig["kind"] == "design_decision"
        assert "User discussed" in sig["summary"]

    def test_truncates_long_summary(self):
        long_text = "x " * 300
        sig = session_event_signal(kind="note", summary=long_text)
        assert len(sig["summary"]) <= MAX_SUMMARY_CHARS

    def test_defaults_sensitivity_private(self):
        sig = session_event_signal(kind="note", summary="test")
        assert sig["sensitivity"] == "private"
        assert sig["allowed_surfaces"] == ["local"]

    def test_rejects_invalid_sensitivity_to_private(self):
        sig = session_event_signal(kind="note", summary="test", sensitivity="world_readable")
        assert sig["sensitivity"] == "private"

    def test_clamps_strength_high(self):
        sig = session_event_signal(kind="note", summary="test", strength_hint=5.0)
        assert sig["strength_hint"] == 1.0

    def test_clamps_strength_low(self):
        sig = session_event_signal(kind="note", summary="test", strength_hint=-1.0)
        assert sig["strength_hint"] == 0.0

    def test_no_raw_transcript_fields(self):
        sig = session_event_signal(kind="note", summary="test")
        for key in ("transcript", "raw", "content", "messages", "full_text"):
            assert key not in sig

    def test_session_ref_truncated(self):
        sig = session_event_signal(kind="note", summary="test", session_ref="r" * 500)
        assert len(sig["session_ref"]) <= MAX_REF_CHARS

    def test_sanitizes_optional_lists(self):
        sig = session_event_signal(
            kind="note",
            summary="test",
            allowed_surfaces=["local", "", "  ", "discord"],
            correlation_keys=["k" * 400, "", "  "],
        )
        assert sig["allowed_surfaces"] == ["local", "discord"]
        assert len(sig["correlation_keys"][0]) <= MAX_REF_CHARS
        assert sig["correlation_keys"][0].startswith("k")

    def test_non_numeric_strength_defaults_safely(self):
        strength = "high"
        sig = session_event_signal(kind="note", summary="test", strength_hint=strength)  # type: ignore[arg-type]
        assert sig["strength_hint"] == 0.5


class TestArtifactSignal:
    """Artifact sensor records metadata/hash/ref, not raw contents."""

    def test_emits_compact_dict_with_metadata(self):
        sig = artifact_signal(
            path="/tmp/file.py", summary="New module", size=1234, content_hash="abc123"
        )
        assert sig["sensor"] == "sensorium.artifact"
        assert sig["source"] == "artifact"
        assert sig["path"] == "/tmp/file.py"
        assert sig["size"] == 1234
        assert sig["content_hash"] == "abc123"

    def test_no_file_content_in_signal(self):
        sig = artifact_signal(path="/tmp/file.py", summary="New module")
        for key in ("content", "raw", "body", "file_content", "data", "text"):
            assert key not in sig

    def test_truncates_long_summary(self):
        sig = artifact_signal(path="/tmp/f.py", summary="x " * 300)
        assert len(sig["summary"]) <= MAX_SUMMARY_CHARS

    def test_truncates_long_hash(self):
        sig = artifact_signal(path="/tmp/f.py", summary="test", content_hash="a" * 100)
        assert len(sig["content_hash"]) <= 64

    def test_defaults_sensitivity_private(self):
        sig = artifact_signal(path="/tmp/f.py", summary="test")
        assert sig["sensitivity"] == "private"
        assert sig["allowed_surfaces"] == ["local"]

    def test_default_kind_artifact_created(self):
        sig = artifact_signal(path="/tmp/f.py", summary="test")
        assert sig["kind"] == "artifact_created"

    def test_empty_hash_stays_empty(self):
        sig = artifact_signal(path="/tmp/f.py", summary="test", content_hash="")
        assert sig["content_hash"] == ""

    def test_path_truncated(self):
        sig = artifact_signal(path="/" + "a" * 500, summary="test")
        assert len(sig["path"]) <= MAX_REF_CHARS


class TestOperatorSignal:
    """Explicit operator sensor sets appropriate source/kind/sensitivity/surfaces."""

    def test_emits_compact_dict(self):
        sig = operator_signal(summary="Please prioritize X")
        assert sig["sensor"] == "sensorium.explicit_operator"
        assert sig["source"] == "manual"
        assert sig["actor"] == "operator"
        assert sig["kind"] == "user_correction"

    def test_truncates_long_summary(self):
        sig = operator_signal(summary="x " * 300)
        assert len(sig["summary"]) <= MAX_SUMMARY_CHARS

    def test_defaults_sensitivity_private(self):
        sig = operator_signal(summary="test")
        assert sig["sensitivity"] == "private"
        assert sig["allowed_surfaces"] == ["local"]

    def test_rejects_invalid_sensitivity(self):
        sig = operator_signal(summary="test", sensitivity="public_danger")
        assert sig["sensitivity"] == "private"

    def test_source_ref_truncated(self):
        sig = operator_signal(summary="test", source_ref="r" * 500)
        assert len(sig["source_ref"]) <= MAX_REF_CHARS

    def test_high_default_strength(self):
        sig = operator_signal(summary="test")
        assert sig["strength_hint"] >= 0.7

    def test_custom_kind(self):
        sig = operator_signal(summary="New design requirement", kind="design_decision")
        assert sig["kind"] == "design_decision"


class TestMachineBodyPressure:
    """Body sensor emits only compact global transition signals from cheap samples."""

    def test_healthy_samples_emit_nothing(self):
        state = {}
        emitted = []
        for sample in [
            {"mem_available_pct": 80.0, "load_per_cpu": 0.2, "swap_used_pct": 0.0},
            {"mem_available_pct": 75.0, "load_per_cpu": 0.3, "swap_used_pct": 0.0},
            {"mem_available_pct": 70.0, "load_per_cpu": 0.4, "swap_used_pct": 0.0},
        ]:
            sig, state = classify_machine_body_pressure(sample, state=state)
            emitted.append(sig)
        assert emitted == [None, None, None]
        assert state["level"] == "healthy"

    def test_degraded_memory_requires_debounce_and_emits_compact_signal(self):
        state = {}
        sample = {"mem_available_pct": 8.0, "load_per_cpu": 0.2, "swap_used_pct": 0.0}
        first, state = classify_machine_body_pressure(sample, state=state)
        second, state = classify_machine_body_pressure(sample, state=state)
        third, state = classify_machine_body_pressure(sample, state=state)

        assert first is None
        assert second is None
        assert third is not None
        assert third["sensor"] == "sensorium.machine_body_pressure"
        assert third["source"] == "machine"
        assert third["kind"] == "body_pressure"
        assert third["scope"] == "global"
        assert third["pressure_level"] == "degraded"
        assert third["transition"] == "healthy_to_degraded"
        assert third["metric_family"] == "memory"
        assert third["window"]["samples"] == 3
        assert third["sensitivity"] == "local_only"
        assert third["allowed_surfaces"] == ["local"]
        assert third["strength_hint"] >= 0.75
        for forbidden in ("processes", "cmdline", "stdout", "stderr", "transcript", "raw"):
            assert forbidden not in third

    def test_critical_memory_emits_faster_than_degraded(self):
        state = {}
        sample = {"mem_available_pct": 3.0, "load_per_cpu": 0.2, "swap_used_pct": 0.0}
        first, state = classify_machine_body_pressure(sample, state=state)
        second, state = classify_machine_body_pressure(sample, state=state)

        assert first is None
        assert second is not None
        assert second["pressure_level"] == "critical"
        assert second["transition"] == "healthy_to_critical"
        assert second["strength_hint"] >= 0.9

    def test_recovery_requires_healthy_window(self):
        state = {}
        bad = {"mem_available_pct": 8.0, "load_per_cpu": 0.2, "swap_used_pct": 0.0}
        for _ in range(3):
            sig, state = classify_machine_body_pressure(bad, state=state)
        assert sig is not None
        healthy = {"mem_available_pct": 70.0, "load_per_cpu": 0.2, "swap_used_pct": 0.0}
        emitted = []
        for _ in range(5):
            sig, state = classify_machine_body_pressure(healthy, state=state)
            emitted.append(sig)

        assert emitted[:4] == [None, None, None, None]
        assert emitted[4] is not None
        assert emitted[4]["transition"] == "degraded_to_recovered"
        assert emitted[4]["pressure_level"] == "healthy"

    def test_sustained_pressure_heartbeat_is_rate_limited(self):
        config = {"degraded_samples": 1, "sustained_samples": 3}
        state = {}
        bad = {"mem_available_pct": 8.0, "load_per_cpu": 0.2, "swap_used_pct": 0.0}
        transition, state = classify_machine_body_pressure(bad, state=state, config=config)
        assert transition is not None

        one, state = classify_machine_body_pressure(bad, state=state, config=config)
        two, state = classify_machine_body_pressure(bad, state=state, config=config)
        three, state = classify_machine_body_pressure(bad, state=state, config=config)
        assert one is None
        assert two is None
        assert three is not None
        assert three["transition"] == "sustained_degraded"

    def test_replay_uses_same_online_classifier_without_runtime_history_scan(self):
        samples = [
            {"mem_available_pct": 80.0, "load_per_cpu": 0.2, "swap_used_pct": 0.0},
            {"mem_available_pct": 8.0, "load_per_cpu": 0.2, "swap_used_pct": 0.0},
            {"mem_available_pct": 8.0, "load_per_cpu": 0.2, "swap_used_pct": 0.0},
            {"mem_available_pct": 8.0, "load_per_cpu": 0.2, "swap_used_pct": 0.0},
        ]
        signals = replay_machine_body_pressure(samples)
        assert len(signals) == 1
        assert signals[0]["transition"] == "healthy_to_degraded"

    def test_procfs_sample_parses_fixture_without_process_lists(self, tmp_path):
        proc = tmp_path / "proc"
        proc.mkdir()
        (proc / "loadavg").write_text("0.50 0.40 0.30 1/100 123\n")
        (proc / "meminfo").write_text(
            "MemTotal:       1000000 kB\n"
            "MemAvailable:    250000 kB\n"
            "SwapTotal:        500000 kB\n"
            "SwapFree:         400000 kB\n"
        )
        pressure = proc / "pressure"
        pressure.mkdir()
        (pressure / "cpu").write_text("some avg10=1.00 avg60=0.50 avg300=0.10 total=10\n")
        (pressure / "memory").write_text("some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
        (pressure / "io").write_text("some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")

        sample = machine_body_pressure_sample(proc_root=str(proc), disk_paths=[str(tmp_path)])
        assert sample["mem_available_pct"] == 25.0
        assert sample["swap_used_pct"] == 20.0
        assert sample["psi_cpu_some_avg10"] == 1.0
        assert "processes" not in sample
        assert "cmdline" not in sample

    def test_default_disk_paths_include_wsl_root_and_windows_mounts(self, tmp_path):
        mnt = tmp_path / "mnt"
        (mnt / "c").mkdir(parents=True)
        (mnt / "d").mkdir()
        (mnt / "not-a-drive").mkdir()

        paths = wsl_disk_paths(mount_root=str(mnt))

        assert paths[0] == "/"
        assert str(mnt / "c") in paths
        assert str(mnt / "d") in paths
        assert str(mnt / "not-a-drive") not in paths

    def test_body_pressure_signal_validates_and_ingests(self):
        from agent_sensorium.schemas import validate_signal
        from agent_sensorium.tools import handle_sensorium_ingest_signal

        state = {}
        sample = {"mem_available_pct": 8.0, "load_per_cpu": 0.2, "swap_used_pct": 0.0}
        sig = None
        for _ in range(3):
            sig, state = classify_machine_body_pressure(sample, state=state)
        assert sig is not None
        validate_signal(sig)
        with tempfile.TemporaryDirectory() as td:
            raw = handle_sensorium_ingest_signal(signal=sig, instance="test", state_dir=td)
            result = json.loads(raw)
            assert result["success"] is True


class TestMachineNetworkPressure:
    def test_network_sample_parses_proc_without_addresses(self, tmp_path):
        proc = tmp_path / "proc"
        (proc / "net").mkdir(parents=True)
        (proc / "net" / "dev").write_text(
            "Inter-| Receive | Transmit\n"
            " face |bytes packets errs drop fifo frame compressed multicast|bytes packets errs drop fifo colls carrier compressed\n"
            "  eth0: 100 1 2 3 0 0 0 0 200 2 4 5 0 0 0 0\n"
        )
        (proc / "net" / "tcp").write_text("sl local_address rem_address st tx_queue rx_queue tr tm->when retrnsmt uid timeout inode\n0: 00000000:0000 00000000:0000 08 0 0 0 0 0 0 0\n")
        (proc / "net" / "tcp6").write_text("sl local_address rem_address st tx_queue rx_queue tr tm->when retrnsmt uid timeout inode\n")

        sample = machine_network_pressure_sample(proc_root=str(proc))

        assert sample["interfaces"] == ["eth0"]
        assert sample["non_loopback_interfaces"] == 1
        assert sample["rx_errors"] == 2
        assert sample["tx_errors"] == 4
        assert sample["rx_drops"] == 3
        assert sample["tx_drops"] == 5
        assert sample["tcp_states"]["CLOSE_WAIT"] == 1
        assert "local_address" not in sample
        assert "remote_address" not in sample

    def test_network_transition_emits_compact_signal(self):
        state = {"level": "healthy", "last_error_total": 0, "last_drop_total": 0}
        sample = {
            "interfaces": ["eth0"], "non_loopback_interfaces": 1,
            "rx_errors": 1, "tx_errors": 0, "rx_drops": 0, "tx_drops": 0,
            "tcp_states": {},
        }
        sig, state = classify_machine_network_pressure(sample, state=state)

        assert sig is not None
        assert sig["sensor"] == "sensorium.machine_network_pressure"
        assert sig["source"] == "machine"
        assert sig["kind"] == "network_pressure"
        assert sig["pressure_level"] == "degraded"
        assert sig["transition"] == "healthy_to_degraded"
        assert sig["sensitivity"] == "local_only"
        assert "remote_address" not in sig
        assert "packets" not in sig


class TestMachineProcessPressure:
    def test_process_sample_counts_states_without_cmdlines(self, tmp_path):
        proc = tmp_path / "proc"
        for pid, state in [("1", "S"), ("2", "Z"), ("3", "D")]:
            d = proc / pid
            d.mkdir(parents=True)
            (d / "stat").write_text(f"{pid} (test) {state} 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0")

        sample = machine_process_pressure_sample(proc_root=str(proc))

        assert sample["process_count"] == 3
        assert sample["zombie_count"] == 1
        assert sample["uninterruptible_count"] == 1
        assert "cmdline" not in sample
        assert "processes" not in sample

    def test_process_pressure_emits_for_zombies(self):
        sig, state = classify_machine_process_pressure({"process_count": 20, "zombie_count": 2, "uninterruptible_count": 0}, state={})

        assert sig is not None
        assert sig["sensor"] == "sensorium.machine_process_pressure"
        assert sig["source"] == "machine"
        assert sig["kind"] == "process_pressure"
        assert sig["metric_family"] == "zombie"
        assert sig["pressure_level"] == "degraded"
        assert "cmdline" not in sig


class TestHindsightPressure:
    def test_hindsight_unavailable_sample_is_compact(self):
        sample = hindsight_pressure_sample(base_url="http://127.0.0.1:1", timeout_seconds=0.01)
        assert sample["api_available"] is False
        assert "error" in sample
        assert "content" not in sample

    def test_hindsight_queue_pressure_emits_signal(self):
        sample = {"api_available": True, "pending_total": 250, "processing_total": 1, "failed_total": 0}
        sig, state = classify_hindsight_pressure(sample, state={})

        assert sig is not None
        assert sig["sensor"] == "sensorium.hindsight_pressure"
        assert sig["source"] == "memory"
        assert sig["kind"] == "hindsight_pressure"
        assert sig["pressure_level"] == "critical"
        assert "memory_text" not in sig
        assert "raw" not in sig


class TestKanbanPressure:
    def test_kanban_sample_reads_counts_only(self, tmp_path):
        import sqlite3, time

        db = tmp_path / "kanban.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE tasks (id TEXT, status TEXT, priority INTEGER, consecutive_failures INTEGER, last_heartbeat_at INTEGER)")
        con.executemany(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, ?)",
            [("t1", "ready", 100, 0, None), ("t2", "running", 50, 0, int(time.time()) - 9999), ("t3", "failed", 10, 3, None)],
        )
        con.commit(); con.close()

        sample = kanban_pressure_sample(board_paths=[str(db)], now_epoch=int(time.time()))

        assert sample["board_count"] == 1
        assert sample["status_counts"]["ready"] == 1
        assert sample["status_counts"]["running"] == 1
        assert sample["failed_tasks"] == 1
        assert sample["stale_running_tasks"] == 1
        assert "title" not in sample
        assert "body" not in sample

    def test_kanban_pressure_emits_for_stale_running(self):
        sample = {"board_count": 1, "status_counts": {"running": 2}, "failed_tasks": 0, "blocked_tasks": 0, "stale_running_tasks": 2}
        sig, state = classify_kanban_pressure(sample, state={})

        assert sig is not None
        assert sig["sensor"] == "sensorium.kanban_pressure"
        assert sig["source"] == "kanban"
        assert sig["kind"] == "kanban_pressure"
        assert sig["metric_family"] == "stale_running"
        assert sig["sensitivity"] == "local_only"
        assert "body" not in sig


class TestTtsSidecarPressure:
    def test_log_sample_hashes_timeout_lines_without_raw_content(self, tmp_path):
        log = tmp_path / "gateway.log"
        log.write_text(
            "ordinary line\n"
            "Auto voice reply TTS failed: openai.APITimeoutError talking to http://127.0.0.1:8892/v1\n"
        )
        pid = tmp_path / "missing.pid"

        sample = tts_sidecar_pressure_sample(
            log_paths=[str(log)],
            health_url=None,
            pid_file=str(pid),
        )

        assert sample["timeout_match_count"] == 1
        assert sample["timeout_fingerprint"]
        assert sample["pattern_counts"]["auto_voice_tts_failed"] == 1
        assert sample["sidecar_running"] is False
        rendered = json.dumps(sample)
        assert "Auto voice reply" not in rendered
        assert "APITimeoutError talking" not in rendered

    def test_new_timeout_emits_bounded_cue_signal_once(self, tmp_path):
        log = tmp_path / "errors.log"
        log.write_text("text_to_speech failed with ConnectTimeout on chatterbox port 8892\n")
        sample = tts_sidecar_pressure_sample(log_paths=[str(log)], health_url=None, pid_file=str(tmp_path / "none.pid"))

        sig, state = classify_tts_sidecar_pressure(sample, state={})
        duplicate, state = classify_tts_sidecar_pressure(sample, state=state)

        assert sig is not None
        assert duplicate is None
        assert sig["sensor"] == "sensorium.tts_sidecar_pressure"
        assert sig["source"] == "machine"
        assert sig["kind"] == "tts_sidecar_pressure"
        assert sig["policy"] == "cue_only_no_auto_restart"
        assert "stop || true" in "\n".join(sig["recovery_checklist"])
        assert "health returns JSON" in "\n".join(sig["verification_criteria"])
        assert "text_to_speech failed" not in json.dumps(sig)

    def test_running_but_unhealthy_sidecar_emits_even_without_log_timeout(self):
        sample = {
            "timeout_match_count": 0,
            "timeout_fingerprint": "",
            "pattern_counts": {},
            "sidecar_running": True,
            "health_available": False,
            "health_error": "URLError",
        }

        sig, state = classify_tts_sidecar_pressure(sample, state={})
        repeat, state = classify_tts_sidecar_pressure(sample, state=state)

        assert sig is not None
        assert repeat is None
        assert sig["pressure_level"] == "degraded"
        assert "sidecar_running" in sig["values"]
        assert state["level"] == "degraded"

    def test_stopped_sidecar_without_timeout_is_healthy_and_silent(self):
        sig, state = classify_tts_sidecar_pressure(
            {
                "timeout_match_count": 0,
                "timeout_fingerprint": "",
                "pattern_counts": {},
                "sidecar_running": False,
                "health_available": False,
                "health_error": "URLError",
            },
            state={},
        )

        assert sig is None
        assert state["level"] == "healthy"

    def test_health_probe_default_timeout_is_not_hair_trigger(self):
        import inspect

        default_timeout = inspect.signature(tts_sidecar_pressure_sample).parameters["timeout_seconds"].default
        assert default_timeout >= 2.0


class TestMediaCapacity:
    def _idle_sample(self):
        return {
            "comfy_available": True,
            "comfy_queue_running": 0,
            "comfy_queue_pending": 0,
            "gpu_sample_available": True,
            "gpu_count": 1,
            "gpu_util_pct": 3,
            "vram_used_pct": 10.0,
            "mem_available_pct": 70.0,
            "swap_used_pct": 0.0,
            "tts_health_available": True,
            "tts_health_error": "",
        }

    def test_almost_idle_emits_bounded_capacity_signal_once(self):
        sample = self._idle_sample()

        sig, state = classify_media_capacity(sample, state={})
        duplicate, state = classify_media_capacity(sample, state=state)

        assert sig is not None
        assert duplicate is None
        assert sig["sensor"] == "sensorium.media_capacity"
        assert sig["kind"] == "media_capacity"
        assert sig["capacity_status"] == "almost_idle"
        assert sig["policy"] == "capacity_record_only_no_generation"
        assert sig["sensitivity"] == "local_only"
        assert sig["allowed_surfaces"] == ["local"]
        assert sig["strength_hint"] < 0.7
        assert state["capacity_record"]["status"] == "almost_idle"
        rendered = json.dumps(sig)
        for forbidden in ("cmdline", "processes", "prompt", "generated", "stdout", "stderr"):
            assert forbidden not in rendered

    def test_busy_queue_records_busy_without_positive_signal(self):
        sample = self._idle_sample()
        sample["comfy_queue_running"] = 1

        sig, state = classify_media_capacity(sample, state={})

        assert sig is None
        assert state["status"] == "busy"
        assert "comfy_queue_active" in state["capacity_record"]["reasons"]
        assert state["capacity_record"]["policy"] == "capacity_record_only_no_generation"

    def test_unknown_dependencies_record_unknown_without_restart_instruction(self):
        sample = self._idle_sample()
        sample.update(
            {
                "comfy_available": False,
                "comfy_error": "URLError",
                "tts_health_available": False,
                "tts_health_error": "TimeoutError",
            }
        )

        sig, state = classify_media_capacity(sample, state={})

        assert sig is None
        assert state["status"] == "unknown"
        assert "comfy_unavailable" in state["capacity_record"]["unknown"]
        assert "tts_health_unavailable" in state["capacity_record"]["unknown"]
        assert "restart" not in json.dumps(state["capacity_record"]).lower()

    def test_replay_fixtures_use_online_classifier(self):
        busy = self._idle_sample()
        busy["gpu_util_pct"] = 90
        idle = self._idle_sample()

        signals = replay_media_capacity([busy, idle, idle])

        assert len(signals) == 1
        assert signals[0]["capacity_status"] == "almost_idle"

    def test_sample_reads_proc_and_sysfs_fixtures_without_process_lists(self, tmp_path):
        proc = tmp_path / "proc"
        proc.mkdir()
        (proc / "meminfo").write_text(
            "MemTotal:       1000000 kB\n"
            "MemAvailable:    600000 kB\n"
            "SwapTotal:        500000 kB\n"
            "SwapFree:         500000 kB\n"
        )
        gpu = tmp_path / "sys" / "class" / "drm" / "card0" / "device"
        gpu.mkdir(parents=True)
        (gpu / "gpu_busy_percent").write_text("4\n")
        (gpu / "mem_info_vram_used").write_text("100\n")
        (gpu / "mem_info_vram_total").write_text("1000\n")

        sample = media_capacity_sample(
            comfy_base_url="http://127.0.0.1:1",
            tts_health_url=None,
            proc_root=str(proc),
            gpu_drm_root=str(tmp_path / "sys" / "class" / "drm"),
            timeout_seconds=0.01,
        )

        assert sample["mem_available_pct"] == 60.0
        assert sample["swap_used_pct"] == 0.0
        assert sample["gpu_sample_available"] is True
        assert sample["gpu_util_pct"] == 4
        assert sample["vram_used_pct"] == 10.0
        assert sample["comfy_available"] is False
        assert "cmdline" not in sample
        assert "processes" not in sample

    def test_media_capacity_signal_validates_and_ingests(self):
        from agent_sensorium.schemas import validate_signal
        from agent_sensorium.tools import handle_sensorium_ingest_signal

        sig, _ = classify_media_capacity(self._idle_sample(), state={})
        assert sig is not None
        validate_signal(sig)
        with tempfile.TemporaryDirectory() as td:
            raw = handle_sensorium_ingest_signal(signal=sig, instance="test", state_dir=td)
            result = json.loads(raw)
            assert result["success"] is True


class TestFileContentHash:
    def test_hashes_real_file(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("hello")
        h = file_content_hash(str(f))
        assert len(h) == 64
        assert h == file_content_hash(str(f))

    def test_nonexistent_file_returns_empty(self, tmp_path):
        h = file_content_hash(str(tmp_path / "nope.txt"))
        assert h == ""


class TestSensorSignalsAreIngestable:
    """All sensor signals pass validate_signal and are suitable for ingest."""

    def test_session_event_validates(self):
        from agent_sensorium.schemas import validate_signal

        sig = session_event_signal(kind="design_decision", summary="Test")
        validate_signal(sig)

    def test_artifact_validates(self):
        from agent_sensorium.schemas import validate_signal

        sig = artifact_signal(path="/tmp/f.py", summary="Test")
        validate_signal(sig)

    def test_operator_validates(self):
        from agent_sensorium.schemas import validate_signal

        sig = operator_signal(summary="Test")
        validate_signal(sig)

    def test_tts_sidecar_pressure_validates(self):
        from agent_sensorium.schemas import validate_signal

        sig, _ = classify_tts_sidecar_pressure(
            {
                "timeout_match_count": 1,
                "timeout_fingerprint": "abc123",
                "pattern_counts": {"openai_timeout": 1},
                "sidecar_running": False,
                "health_available": False,
                "health_error": "URLError",
            },
            state={},
        )
        assert sig is not None
        validate_signal(sig)

    def test_session_event_ingestable(self):
        from agent_sensorium.tools import handle_sensorium_ingest_signal

        with tempfile.TemporaryDirectory() as td:
            sig = session_event_signal(
                kind="design_decision", summary="Test event", strength_hint=0.9,
            )
            raw = handle_sensorium_ingest_signal(signal=sig, instance="test", state_dir=td)
            result = json.loads(raw)
            assert result["success"] is True

    def test_artifact_ingestable(self):
        from agent_sensorium.tools import handle_sensorium_ingest_signal

        with tempfile.TemporaryDirectory() as td:
            sig = artifact_signal(
                path="/tmp/f.py", summary="New file", strength_hint=0.9,
            )
            raw = handle_sensorium_ingest_signal(signal=sig, instance="test", state_dir=td)
            result = json.loads(raw)
            assert result["success"] is True

    def test_operator_ingestable(self):
        from agent_sensorium.tools import handle_sensorium_ingest_signal

        with tempfile.TemporaryDirectory() as td:
            sig = operator_signal(summary="Correction", strength_hint=0.9)
            raw = handle_sensorium_ingest_signal(signal=sig, instance="test", state_dir=td)
            result = json.loads(raw)
            assert result["success"] is True
