"""Tests for agent_sensorium.memory_reflection.

Covers: config hot-load, validation/clamping, reducer stripping, fake client
run, require_delta/liveness, due detection, ingestion path, dry_run.
"""

from __future__ import annotations

import json
from pathlib import Path


import agent_sensorium.memory_reflection as mr
from agent_sensorium.tools import handle_sensorium_ingest_signal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_CADENCE = {"type": "interval", "hours": 24}


def _write_config(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data))


def _minimal_config(probes: list[dict], *, enabled: bool = True) -> dict:
    return {"enabled": enabled, "probes": probes}


def _minimal_probe(probe_id: str = "p1", query: str = "What happened?") -> dict:
    return {"id": probe_id, "query": query, "cadence": _VALID_CADENCE}


# ---------------------------------------------------------------------------
# 1. Config hot-load
# ---------------------------------------------------------------------------


class TestConfigHotLoad:
    def test_initial_load(self, tmp_path):
        cfg_file = tmp_path / "memory_reflection.json"
        _write_config(cfg_file, _minimal_config([_minimal_probe("p1")]))

        cfg = mr.load_config(state_dir=str(tmp_path))
        assert cfg.enabled is True
        assert len(cfg.probes) == 1
        assert cfg.probes[0].id == "p1"

    def test_hot_reload_adds_probe(self, tmp_path):
        cfg_file = tmp_path / "memory_reflection.json"
        _write_config(cfg_file, _minimal_config([_minimal_probe("p1")]))

        cfg1 = mr.load_config(state_dir=str(tmp_path))
        assert len(cfg1.probes) == 1

        # Add a second probe without any restart
        _write_config(cfg_file, _minimal_config([_minimal_probe("p1"), _minimal_probe("p2", "Anything else?")]))

        cfg2 = mr.load_config(state_dir=str(tmp_path))
        assert len(cfg2.probes) == 2
        assert {p.id for p in cfg2.probes} == {"p1", "p2"}

    def test_hot_reload_removes_probe(self, tmp_path):
        cfg_file = tmp_path / "memory_reflection.json"
        _write_config(cfg_file, _minimal_config([_minimal_probe("p1"), _minimal_probe("p2", "Other?")]))

        mr.load_config(state_dir=str(tmp_path))  # prime first call

        _write_config(cfg_file, _minimal_config([_minimal_probe("p1")]))
        cfg2 = mr.load_config(state_dir=str(tmp_path))
        assert len(cfg2.probes) == 1
        assert cfg2.probes[0].id == "p1"

    def test_hot_reload_changes_query(self, tmp_path):
        cfg_file = tmp_path / "memory_reflection.json"
        _write_config(cfg_file, _minimal_config([_minimal_probe("p1", "Old query")]))
        mr.load_config(state_dir=str(tmp_path))

        _write_config(cfg_file, _minimal_config([_minimal_probe("p1", "New query")]))
        cfg2 = mr.load_config(state_dir=str(tmp_path))
        assert cfg2.probes[0].query == "New query"

    def test_missing_file_returns_disabled(self, tmp_path):
        cfg = mr.load_config(state_dir=str(tmp_path))
        assert cfg.enabled is False
        assert cfg.probes == []
        assert cfg.source == "missing"

    def test_explicit_config_path_overrides_state_dir(self, tmp_path):
        elsewhere = tmp_path / "custom.json"
        _write_config(elsewhere, _minimal_config([_minimal_probe("custom")]))

        cfg = mr.load_config(state_dir=str(tmp_path), config_path=str(elsewhere))
        assert len(cfg.probes) == 1
        assert cfg.probes[0].id == "custom"


# ---------------------------------------------------------------------------
# 2. Config validation — errors and clamping
# ---------------------------------------------------------------------------


class TestConfigValidation:
    def test_invalid_mode_disables_probe_and_records_error(self, tmp_path):
        cfg_file = tmp_path / "memory_reflection.json"
        _write_config(cfg_file, _minimal_config([
            {**_minimal_probe("bad"), "mode": "dream"},
            _minimal_probe("good"),
        ]))

        cfg = mr.load_config(state_dir=str(tmp_path))
        ids = [p.id for p in cfg.probes]
        assert "bad" not in ids
        assert "good" in ids
        assert any("bad" in str(e) or "invalid mode" in str(e).lower() for e in cfg.errors)

    def test_missing_id_records_error(self, tmp_path):
        cfg_file = tmp_path / "memory_reflection.json"
        _write_config(cfg_file, _minimal_config([
            {"query": "no id probe", "cadence": _VALID_CADENCE},
            _minimal_probe("ok"),
        ]))

        cfg = mr.load_config(state_dir=str(tmp_path))
        assert len(cfg.probes) == 1
        assert cfg.probes[0].id == "ok"
        assert len(cfg.errors) == 1

    def test_missing_query_records_error(self, tmp_path):
        cfg_file = tmp_path / "memory_reflection.json"
        _write_config(cfg_file, _minimal_config([
            {"id": "no_query", "cadence": _VALID_CADENCE},
            _minimal_probe("ok"),
        ]))

        cfg = mr.load_config(state_dir=str(tmp_path))
        assert len(cfg.probes) == 1
        assert cfg.probes[0].id == "ok"
        assert len(cfg.errors) == 1

    def test_bad_cadence_disables_probe(self, tmp_path):
        cfg_file = tmp_path / "memory_reflection.json"
        _write_config(cfg_file, _minimal_config([
            {"id": "bad_cadence", "query": "q", "cadence": {"type": "cron", "expr": "* * * * *"}},
            _minimal_probe("ok"),
        ]))

        cfg = mr.load_config(state_dir=str(tmp_path))
        ids = [p.id for p in cfg.probes]
        assert "bad_cadence" not in ids
        assert "ok" in ids
        assert len(cfg.errors) >= 1

    def test_duplicate_ids_records_error(self, tmp_path):
        cfg_file = tmp_path / "memory_reflection.json"
        _write_config(cfg_file, _minimal_config([
            _minimal_probe("dup"),
            _minimal_probe("dup"),
            _minimal_probe("unique"),
        ]))

        cfg = mr.load_config(state_dir=str(tmp_path))
        ids = [p.id for p in cfg.probes]
        assert ids.count("dup") == 1  # first survives
        assert "unique" in ids
        assert any("duplicate" in str(e).lower() for e in cfg.errors)

    def test_out_of_range_timeout_clamped(self, tmp_path):
        cfg_file = tmp_path / "memory_reflection.json"
        _write_config(cfg_file, _minimal_config([
            {**_minimal_probe("p1"), "timeout_s": 99999},
        ]))
        cfg = mr.load_config(state_dir=str(tmp_path))
        assert cfg.probes[0].timeout_s <= mr.MAX_TIMEOUT_S

    def test_below_minimum_timeout_clamped(self, tmp_path):
        cfg_file = tmp_path / "memory_reflection.json"
        _write_config(cfg_file, _minimal_config([
            {**_minimal_probe("p1"), "timeout_s": -5},
        ]))
        cfg = mr.load_config(state_dir=str(tmp_path))
        assert cfg.probes[0].timeout_s >= 1.0

    def test_max_signals_clamped_to_ceiling(self, tmp_path):
        cfg_file = tmp_path / "memory_reflection.json"
        _write_config(cfg_file, _minimal_config([
            {**_minimal_probe("p1"), "max_signals": 1000},
        ]))
        cfg = mr.load_config(state_dir=str(tmp_path))
        assert cfg.probes[0].max_signals <= mr.MAX_SIGNALS_CEIL

    def test_max_summary_chars_clamped(self, tmp_path):
        cfg_file = tmp_path / "memory_reflection.json"
        _write_config(cfg_file, _minimal_config([
            {**_minimal_probe("p1"), "max_summary_chars": 9999},
        ]))
        cfg = mr.load_config(state_dir=str(tmp_path))
        assert cfg.probes[0].max_summary_chars <= mr.MAX_SUMMARY_CHARS_CEIL

    def test_valid_probes_still_load_alongside_invalid(self, tmp_path):
        cfg_file = tmp_path / "memory_reflection.json"
        _write_config(cfg_file, _minimal_config([
            {"id": "bad1", "cadence": _VALID_CADENCE},  # missing query
            _minimal_probe("good1"),
            {"id": "bad2", "query": "q", "cadence": {"type": "bad"}},  # bad cadence
            _minimal_probe("good2", "Other question?"),
        ]))

        cfg = mr.load_config(state_dir=str(tmp_path))
        ids = {p.id for p in cfg.probes}
        assert ids == {"good1", "good2"}
        assert len(cfg.errors) == 2

    def test_unreadable_json_returns_error(self, tmp_path):
        cfg_file = tmp_path / "memory_reflection.json"
        cfg_file.write_text("{not valid json{{")

        cfg = mr.load_config(state_dir=str(tmp_path))
        assert cfg.enabled is False
        assert cfg.source == "unreadable"
        assert len(cfg.errors) >= 1


# ---------------------------------------------------------------------------
# 3. Reducer strips raw fields
# ---------------------------------------------------------------------------


def _make_probe(
    probe_id: str = "p1",
    max_signals: int = 5,
    max_summary_chars: int = 400,
) -> mr.ProbeConfig:
    return mr.ProbeConfig(
        id=probe_id,
        query="test query",
        cadence_hours=24.0,
        max_signals=max_signals,
        max_summary_chars=max_summary_chars,
    )


def _fake_raw_ref() -> dict:
    return {"raw_ref": "/tmp/fake.json", "raw_sha256": "abc123", "raw_bytes": 100, "truncated": False}


class TestReducer:
    def test_no_forbidden_fields_on_emitted_signals(self):
        raw_output = {
            "synthesis": "This is a good summary.",
            "insights": ["Point A", "Point B"],
            # Forbidden fields that must be stripped:
            "transcript": "raw log content",
            "raw_text": "raw text here",
            "memories": ["mem1", "mem2"],
            "full_text": "full text body",
            "documents": [{"doc": "content"}],
        }
        probe = _make_probe()
        signals, fp = mr.reduce_reflection(
            raw_output=raw_output, probe=probe, raw_ref=_fake_raw_ref(), now="2026-01-01T00:00:00Z"
        )
        assert len(signals) > 0
        for signal in signals:
            for forbidden in mr.RAW_FORBIDDEN_SIGNAL_FIELDS:
                assert forbidden not in signal, f"Forbidden field '{forbidden}' found in signal"

    def test_summary_truncated_to_max_summary_chars(self):
        long_summary = "x" * 1000
        raw_output = {"synthesis": long_summary}
        probe = _make_probe(max_summary_chars=100)
        signals, _ = mr.reduce_reflection(
            raw_output=raw_output, probe=probe, raw_ref=_fake_raw_ref(), now="2026-01-01T00:00:00Z"
        )
        assert len(signals) == 1
        # truncate_text adds ellipsis, so actual len may exceed max_chars by 1 (the "…")
        assert len(signals[0]["summary"]) <= 102

    def test_signals_capped_to_max_signals(self):
        raw_output = {
            "insights": ["point 1", "point 2", "point 3", "point 4", "point 5", "point 6"],
        }
        probe = _make_probe(max_signals=3)
        signals, _ = mr.reduce_reflection(
            raw_output=raw_output, probe=probe, raw_ref=_fake_raw_ref(), now="2026-01-01T00:00:00Z"
        )
        assert len(signals) <= 3

    def test_each_signal_has_correct_kind_sensor_unverified(self):
        raw_output = {"insights": ["A", "B"]}
        probe = _make_probe()
        signals, _ = mr.reduce_reflection(
            raw_output=raw_output, probe=probe, raw_ref=_fake_raw_ref(), now="2026-01-01T00:00:00Z"
        )
        for sig in signals:
            assert sig["kind"] == mr.MEMORY_REFLECTION_KIND
            assert sig["sensor"] == mr.MEMORY_REFLECTION_SENSOR
            assert sig["unverified"] is True

    def test_each_signal_has_raw_ref_string(self):
        raw_output = {"synthesis": "Some insight"}
        probe = _make_probe()
        raw_ref = _fake_raw_ref()
        signals, _ = mr.reduce_reflection(
            raw_output=raw_output, probe=probe, raw_ref=raw_ref, now="2026-01-01T00:00:00Z"
        )
        assert len(signals) == 1
        assert isinstance(signals[0]["raw_ref"], str)
        assert signals[0]["raw_ref"] == raw_ref["raw_ref"]

    def test_raw_ref_present_but_no_raw_body_in_signals(self):
        raw_output = {
            "synthesis": "Compact insight",
            "raw": "this must not pass",
            "body": "nor this",
            "content": "nor this either",
        }
        probe = _make_probe()
        signals, _ = mr.reduce_reflection(
            raw_output=raw_output, probe=probe, raw_ref=_fake_raw_ref(), now="2026-01-01T00:00:00Z"
        )
        assert len(signals) > 0
        for sig in signals:
            assert "raw" not in sig
            assert "body" not in sig
            assert "content" not in sig

    def test_empty_raw_output_yields_no_real_signals(self):
        probe = _make_probe(max_signals=3)
        probe.low_significance_liveness = False
        signals, _ = mr.reduce_reflection(
            raw_output={}, probe=probe, raw_ref=_fake_raw_ref(), now="2026-01-01T00:00:00Z"
        )
        assert signals == []


# ---------------------------------------------------------------------------
# 4. Fake client: raw file written, emitted signals compact only
# ---------------------------------------------------------------------------


class TestFakeClientRun:
    def _make_config(self, tmp_path: Path, probe_id: str = "p1") -> None:
        cfg_file = tmp_path / "memory_reflection.json"
        _write_config(cfg_file, {
            "enabled": True,
            "probes": [{
                "id": probe_id,
                "query": "What is new?",
                "cadence": _VALID_CADENCE,
                "require_delta": False,
                "low_significance_liveness": False,
            }],
        })

    def test_raw_file_written_under_raw_dir(self, tmp_path):
        self._make_config(tmp_path)
        fake = mr.FakeHindsightMemoryClient(reflect_result={
            "synthesis": "Interesting finding",
            "insights": ["Point A"],
        })

        result = mr.run_due_probes(
            state_dir=str(tmp_path),
            client=fake,
            now="2026-01-01T00:00:00Z",
        )
        assert len(result["runs"]) == 1
        run = result["runs"][0]

        raw_path = run["raw_ref"]
        assert raw_path, "raw_ref should be a non-empty path"
        assert Path(raw_path).exists(), "raw file should exist on disk"

        # The raw file should contain the raw output
        raw_on_disk = json.loads(Path(raw_path).read_text())
        assert "synthesis" in raw_on_disk or "insights" in raw_on_disk

    def test_run_record_has_no_raw_body(self, tmp_path):
        self._make_config(tmp_path)
        fake = mr.FakeHindsightMemoryClient(reflect_result={
            "synthesis": "Finding",
            "transcript": "secret log",
            "memories": ["mem1"],
        })

        result = mr.run_due_probes(
            state_dir=str(tmp_path),
            client=fake,
            now="2026-01-01T00:00:00Z",
        )
        run = result["runs"][0]
        run_str = json.dumps(run)
        for forbidden in mr.RAW_FORBIDDEN_SIGNAL_FIELDS:
            assert f'"{forbidden}"' not in run_str, (
                f"Run record contains forbidden field '{forbidden}'"
            )

    def test_emitted_summaries_present_but_no_raw_body(self, tmp_path):
        self._make_config(tmp_path)
        fake = mr.FakeHindsightMemoryClient(reflect_result={
            "synthesis": "Summary text",
        })

        result = mr.run_due_probes(
            state_dir=str(tmp_path),
            client=fake,
            now="2026-01-01T00:00:00Z",
        )
        run = result["runs"][0]
        assert isinstance(run.get("emitted_summaries"), list)
        assert len(run["emitted_summaries"]) >= 1
        assert run["emitted_summaries"][0]  # non-empty summary

    def test_raw_dir_structure(self, tmp_path):
        self._make_config(tmp_path)
        fake = mr.FakeHindsightMemoryClient(reflect_result={"synthesis": "x"})
        mr.run_due_probes(state_dir=str(tmp_path), client=fake, now="2026-01-01T00:00:00Z")

        raw_dir = tmp_path / "memory_reflection" / "raw"
        assert raw_dir.is_dir()
        raw_files = list(raw_dir.glob("*.json"))
        assert len(raw_files) == 1


# ---------------------------------------------------------------------------
# 5. require_delta / liveness
# ---------------------------------------------------------------------------


class TestRequireDeltaLiveness:
    def _make_config_with_delta(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "memory_reflection.json"
        _write_config(cfg_file, {
            "enabled": True,
            "probes": [{
                "id": "delta_probe",
                "query": "Anything new?",
                "cadence": {"type": "interval", "hours": 1},
                "cooldown_hours": 0.0,
                "require_delta": True,
                "low_significance_liveness": True,
            }],
        })

    def test_first_run_emits_real_signals_with_delta(self, tmp_path):
        self._make_config_with_delta(tmp_path)
        fake = mr.FakeHindsightMemoryClient(reflect_result={"synthesis": "Something new"})

        result = mr.run_due_probes(
            state_dir=str(tmp_path),
            client=fake,
            now="2026-01-01T00:00:00Z",
        )
        run = result["runs"][0]
        assert run["delta"] is True
        assert run["emit_reason"] in ("delta", "liveness_empty")
        # If delta=True and signals exist, emit_reason should be "delta"

    def test_second_run_same_fingerprint_emits_liveness_signal(self, tmp_path):
        self._make_config_with_delta(tmp_path)
        fake = mr.FakeHindsightMemoryClient(reflect_result={"synthesis": "Same content unchanged"})

        # First run
        mr.run_due_probes(
            state_dir=str(tmp_path),
            client=fake,
            now="2026-01-01T00:00:00Z",
            force=True,
        )
        # Second run with same result
        result2 = mr.run_due_probes(
            state_dir=str(tmp_path),
            client=fake,
            now="2026-01-01T01:30:00Z",
            force=True,
        )
        run2 = result2["runs"][0]
        assert run2["delta"] is False
        assert run2["emit_reason"] == "liveness_no_delta"
        # Liveness signal has low strength
        assert run2["emitted_count"] == 1

    def test_liveness_signal_strength_is_liveness_strength(self, tmp_path):
        """Liveness signal must carry LIVENESS_STRENGTH."""
        self._make_config_with_delta(tmp_path)

        # Build probe to call liveness_signal directly
        probe = mr.ProbeConfig(
            id="p1",
            query="q",
            cadence_hours=1.0,
            require_delta=True,
            low_significance_liveness=True,
        )
        fp = mr.reflection_fingerprint("p1", ["Stable content"])
        sig = mr.liveness_signal(probe=probe, fingerprint=fp, raw_ref=_fake_raw_ref())
        assert sig["strength_hint"] == mr.LIVENESS_STRENGTH
        assert sig.get("liveness") is True

    def test_is_probe_due_returns_false_during_cooldown(self, tmp_path):
        probe = mr.ProbeConfig(
            id="cooldown_probe",
            query="q",
            cadence_hours=24.0,
            cooldown_hours=20.0,
        )
        history = [{"probe_id": "cooldown_probe", "completed_at": "2026-01-01T00:00:00Z"}]

        due, reason = mr.is_probe_due(probe, history, now="2026-01-01T01:00:00Z")
        assert due is False
        assert "cooldown" in reason

    def test_is_probe_due_returns_true_after_cooldown(self, tmp_path):
        probe = mr.ProbeConfig(
            id="cooldown_probe",
            query="q",
            cadence_hours=24.0,
            cooldown_hours=20.0,
        )
        history = [{"probe_id": "cooldown_probe", "completed_at": "2026-01-01T00:00:00Z"}]

        due, reason = mr.is_probe_due(probe, history, now="2026-01-02T06:00:00Z")
        assert due is True


# ---------------------------------------------------------------------------
# 6. Due detection / cadence + cooldown
# ---------------------------------------------------------------------------


class TestDueDetection:
    def test_no_history_probe_is_due(self):
        probe = mr.ProbeConfig(id="p1", query="q", cadence_hours=24.0)
        due, reason = mr.is_probe_due(probe, [], now="2026-01-01T12:00:00Z")
        assert due is True
        assert reason == "no_prior_run"

    def test_just_ran_probe_is_not_due(self):
        probe = mr.ProbeConfig(id="p1", query="q", cadence_hours=24.0, cooldown_hours=20.0)
        history = [{"probe_id": "p1", "completed_at": "2026-01-01T00:00:00Z"}]

        due, _ = mr.is_probe_due(probe, history, now="2026-01-01T02:00:00Z")
        assert due is False

    def test_far_future_probe_is_due(self):
        probe = mr.ProbeConfig(id="p1", query="q", cadence_hours=24.0, cooldown_hours=20.0)
        history = [{"probe_id": "p1", "completed_at": "2026-01-01T00:00:00Z"}]

        due, _ = mr.is_probe_due(probe, history, now="2026-01-05T00:00:00Z")
        assert due is True

    def test_different_probe_id_history_does_not_affect_due(self):
        probe = mr.ProbeConfig(id="p1", query="q", cadence_hours=24.0)
        history = [{"probe_id": "p2", "completed_at": "2026-01-01T00:00:00Z"}]

        due, reason = mr.is_probe_due(probe, history, now="2026-01-01T01:00:00Z")
        assert due is True
        assert reason == "no_prior_run"

    def test_uses_max_of_cadence_and_cooldown(self):
        # cadence=6h, cooldown=20h => gap_needed=20h
        probe = mr.ProbeConfig(id="p1", query="q", cadence_hours=6.0, cooldown_hours=20.0)
        history = [{"probe_id": "p1", "completed_at": "2026-01-01T00:00:00Z"}]

        # 10 hours later — past cadence but within cooldown
        due, _ = mr.is_probe_due(probe, history, now="2026-01-01T10:00:00Z")
        assert due is False

        # 21 hours later — past both
        due, _ = mr.is_probe_due(probe, history, now="2026-01-01T21:00:00Z")
        assert due is True


# ---------------------------------------------------------------------------
# 7. Ingestion path
# ---------------------------------------------------------------------------


class TestIngestionPath:
    def _make_config(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "memory_reflection.json"
        _write_config(cfg_file, {
            "enabled": True,
            "probes": [{
                "id": "ingest_probe",
                "query": "What should I know?",
                "cadence": _VALID_CADENCE,
                "require_delta": False,
                "low_significance_liveness": False,
            }],
        })

    def test_signal_lands_in_store_signals_jsonl(self, tmp_path):
        self._make_config(tmp_path)
        fake = mr.FakeHindsightMemoryClient(reflect_result={
            "synthesis": "Something important happened",
        })

        def ingest_fn(sig: dict) -> dict:
            raw = handle_sensorium_ingest_signal(
                signal=sig, instance="t", state_dir=str(tmp_path)
            )
            return json.loads(raw)

        mr.run_due_probes(
            state_dir=str(tmp_path),
            client=fake,
            now="2026-01-01T00:00:00Z",
            ingest_fn=ingest_fn,
        )

        signals_file = tmp_path / "signals" / "inbox.jsonl"
        assert signals_file.exists(), "signals inbox should exist"
        lines = [line for line in signals_file.read_text().splitlines() if line.strip()]
        assert len(lines) >= 1
        sig = json.loads(lines[0])
        assert sig["kind"] == mr.MEMORY_REFLECTION_KIND

    def test_ingested_signal_kind_not_in_direct_conscious_kinds(self, tmp_path):
        from agent_sensorium.subconscious import DIRECT_CONSCIOUS_KINDS

        self._make_config(tmp_path)
        fake = mr.FakeHindsightMemoryClient(reflect_result={"synthesis": "Notice this"})

        captured: list[dict] = []

        def ingest_fn(sig: dict) -> dict:
            captured.append(sig)
            raw = handle_sensorium_ingest_signal(
                signal=sig, instance="t", state_dir=str(tmp_path)
            )
            return json.loads(raw)

        mr.run_due_probes(
            state_dir=str(tmp_path),
            client=fake,
            now="2026-01-01T00:00:00Z",
            ingest_fn=ingest_fn,
        )

        assert len(captured) >= 1
        for sig in captured:
            assert sig["kind"] not in DIRECT_CONSCIOUS_KINDS, (
                f"Signal kind '{sig['kind']}' is in DIRECT_CONSCIOUS_KINDS — "
                "memory reflection must not bypass Subconscious review"
            )

    def test_run_record_ingested_list(self, tmp_path):
        self._make_config(tmp_path)
        fake = mr.FakeHindsightMemoryClient(reflect_result={"synthesis": "Test"})

        def ingest_fn(sig: dict) -> dict:
            raw = handle_sensorium_ingest_signal(
                signal=sig, instance="t", state_dir=str(tmp_path)
            )
            return json.loads(raw)

        result = mr.run_due_probes(
            state_dir=str(tmp_path),
            client=fake,
            now="2026-01-01T00:00:00Z",
            ingest_fn=ingest_fn,
        )
        run = result["runs"][0]
        assert "ingested" in run
        assert isinstance(run["ingested"], list)
        assert len(run["ingested"]) >= 1


# ---------------------------------------------------------------------------
# 8. dry_run writes nothing
# ---------------------------------------------------------------------------


class TestDryRun:
    def _make_config(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "memory_reflection.json"
        _write_config(cfg_file, {
            "enabled": True,
            "probes": [{
                "id": "dry_probe",
                "query": "Dry run test",
                "cadence": _VALID_CADENCE,
                "require_delta": False,
                "low_significance_liveness": False,
            }],
        })

    def test_dry_run_writes_no_raw_file(self, tmp_path):
        self._make_config(tmp_path)
        fake = mr.FakeHindsightMemoryClient(reflect_result={"synthesis": "Some data"})

        mr.run_due_probes(
            state_dir=str(tmp_path),
            client=fake,
            now="2026-01-01T00:00:00Z",
            dry_run=True,
        )

        raw_dir = tmp_path / "memory_reflection" / "raw"
        if raw_dir.exists():
            raw_files = list(raw_dir.glob("*.json"))
            assert len(raw_files) == 0, "dry_run must not write raw files"

    def test_dry_run_writes_no_history(self, tmp_path):
        self._make_config(tmp_path)
        fake = mr.FakeHindsightMemoryClient(reflect_result={"synthesis": "Some data"})

        mr.run_due_probes(
            state_dir=str(tmp_path),
            client=fake,
            now="2026-01-01T00:00:00Z",
            dry_run=True,
        )

        history_path = tmp_path / "memory_reflection" / "history.jsonl"
        assert not history_path.exists(), "dry_run must not write history.jsonl"

    def test_dry_run_run_record_has_dry_run_true(self, tmp_path):
        self._make_config(tmp_path)
        fake = mr.FakeHindsightMemoryClient(reflect_result={"synthesis": "Data"})

        result = mr.run_due_probes(
            state_dir=str(tmp_path),
            client=fake,
            now="2026-01-01T00:00:00Z",
            dry_run=True,
        )
        run = result["runs"][0]
        assert run["dry_run"] is True

    def test_dry_run_raw_ref_is_empty_string(self, tmp_path):
        self._make_config(tmp_path)
        fake = mr.FakeHindsightMemoryClient(reflect_result={"synthesis": "Data"})

        result = mr.run_due_probes(
            state_dir=str(tmp_path),
            client=fake,
            now="2026-01-01T00:00:00Z",
            dry_run=True,
        )
        run = result["runs"][0]
        assert run["raw_ref"] == "", "dry_run raw_ref must be empty string (no file path)"


# ---------------------------------------------------------------------------
# Additional: store_raw_output isolation
# ---------------------------------------------------------------------------


class TestStoreRawOutput:
    def test_returns_compact_ref_not_body(self, tmp_path):
        ref = mr.store_raw_output(
            state_dir=str(tmp_path),
            probe_id="p1",
            raw_output={"synthesis": "secret", "transcript": "very secret"},
            now="2026-01-01T00:00:00Z",
            max_raw_chars=6000,
        )
        assert "raw_ref" in ref
        assert "raw_sha256" in ref
        assert "raw_bytes" in ref
        # ref dict itself must not contain the actual raw content
        assert "synthesis" not in ref
        assert "transcript" not in ref

    def test_raw_file_contains_expected_content(self, tmp_path):
        ref = mr.store_raw_output(
            state_dir=str(tmp_path),
            probe_id="p1",
            raw_output={"synthesis": "My secret finding"},
            now="2026-01-01T00:00:00Z",
            max_raw_chars=6000,
        )
        raw_content = json.loads(Path(ref["raw_ref"]).read_text())
        assert raw_content.get("synthesis") == "My secret finding"
