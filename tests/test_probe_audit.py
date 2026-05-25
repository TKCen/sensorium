"""Tests for Phase 7.5 — probe coverage audit and smoke validation."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent_sensorium.probe_audit import audit_store, probe_inventory, run_smoke_probes
from agent_sensorium.sensors import operator_signal, session_event_signal
from agent_sensorium.store import SensoriumStore
from agent_sensorium.tools import handle_sensorium_ingest_signal

AUDIT_SCRIPT = str(Path(__file__).resolve().parent.parent / "scripts" / "sensorium_probe_audit.py")


@pytest.fixture
def state_dir(tmp_path):
    return str(tmp_path / "sensorium")


# --- Probe Inventory ---


class TestProbeInventory:
    def test_lists_all_helpers(self):
        inv = probe_inventory()
        names = [h["function"] for h in inv["implemented_helpers"]]
        assert "session_event_signal" in names
        assert "artifact_signal" in names
        assert "operator_signal" in names

    def test_no_wired_live_probes(self):
        inv = probe_inventory()
        assert inv["wired_live_probes"] == []

    def test_helpers_marked_not_wired(self):
        inv = probe_inventory()
        for h in inv["implemented_helpers"]:
            assert h["wired_live"] is False

    def test_blind_spots_listed(self):
        inv = probe_inventory()
        names = [b["name"] for b in inv["blind_spots"]]
        assert "hindsight_echoes" in names
        assert "rss_feed_items" in names
        assert "file_crawl" in names
        assert "task_results" in names
        assert "active_session_summary" in names

    def test_configured_sources_present(self):
        inv = probe_inventory()
        assert "manual" in inv["configured_sources"]
        assert "hermes_session" in inv["configured_sources"]
        assert "artifact" in inv["configured_sources"]


# --- Empty Store ---


class TestEmptyStoreAudit:
    def test_counts_zero(self, state_dir):
        report = audit_store(state_dir=state_dir, instance="test")
        assert report["counts"]["signals"] == 0
        assert report["counts"]["events"] == 0
        assert report["counts"]["candidates"] == 0
        assert report["counts"]["active_candidates"] == 0

    def test_promotion_yield_zero(self, state_dir):
        report = audit_store(state_dir=state_dir, instance="test")
        assert report["promotion_yield"]["yield_pct"] == 0.0

    def test_lists_silent_sources(self, state_dir):
        report = audit_store(state_dir=state_dir, instance="test")
        assert len(report["silent_sources"]) > 0
        assert "manual" in report["silent_sources"]

    def test_lists_blind_spots(self, state_dir):
        report = audit_store(state_dir=state_dir, instance="test")
        assert len(report["blind_spots"]) > 0
        assert "hindsight_echoes" in report["blind_spots"]

    def test_audit_does_not_create_missing_store_dirs(self, tmp_path):
        missing = tmp_path / "missing_sensorium_state"
        assert not missing.exists()
        report = audit_store(state_dir=str(missing), instance="test")
        assert report["counts"]["signals"] == 0
        assert not missing.exists()


# --- Smoke Probes ---


class TestSmokeProbes:
    def test_exercises_three_source_classes(self, state_dir):
        result = run_smoke_probes(state_dir=state_dir)
        classes = {p["source_class"] for p in result["probes"]}
        assert classes == {"session_event", "artifact", "operator"}

    def test_all_probes_promoted(self, state_dir):
        result = run_smoke_probes(state_dir=state_dir)
        assert result["all_promoted"] is True
        for p in result["probes"]:
            assert p["result"]["promoted"] is True

    def test_signals_written_to_inbox(self, state_dir):
        run_smoke_probes(state_dir=state_dir)
        store = SensoriumStore(instance="probe_smoke", state_dir=state_dir)
        signals = store.read_jsonl("signals")
        assert len(signals) == 3

    def test_events_created(self, state_dir):
        run_smoke_probes(state_dir=state_dir)
        store = SensoriumStore(instance="probe_smoke", state_dir=state_dir)
        assert len(store.read_jsonl("events")) == 3

    def test_candidates_created(self, state_dir):
        run_smoke_probes(state_dir=state_dir)
        store = SensoriumStore(instance="probe_smoke", state_dir=state_dir)
        assert len(store.read_jsonl("candidates")) == 3

    def test_counts_match(self, state_dir):
        result = run_smoke_probes(state_dir=state_dir)
        assert result["counts"] == {"signals": 3, "events": 3, "candidates": 3}


# --- Threshold Promotion / Correlation ---


class TestThresholdPromotion:
    def test_high_strength_promotes(self, state_dir):
        result = run_smoke_probes(state_dir=state_dir)
        for p in result["probes"]:
            assert p["result"].get("event_id") is not None
            assert p["result"].get("candidate_id") is not None

    def test_low_strength_does_not_promote(self, state_dir):
        sig = session_event_signal(kind="note", summary="Low strength", strength_hint=0.3)
        raw = handle_sensorium_ingest_signal(signal=sig, instance="test", state_dir=state_dir)
        assert json.loads(raw)["data"]["promoted"] is False

    def test_correlation_coalesces_candidates(self, state_dir):
        sig1 = operator_signal(
            summary="First correction", kind="user_correction",
            strength_hint=0.9, correlation_keys=["shared_key"],
        )
        sig2 = operator_signal(
            summary="Second correction same topic", kind="user_correction",
            strength_hint=0.9, correlation_keys=["shared_key"],
        )
        handle_sensorium_ingest_signal(signal=sig1, instance="test", state_dir=state_dir)
        handle_sensorium_ingest_signal(signal=sig2, instance="test", state_dir=state_dir)

        store = SensoriumStore(instance="test", state_dir=state_dir)
        active = [c for c in store.read_jsonl("candidates") if c.get("status") == "candidate"]
        assert len(active) == 1
        assert len(active[0].get("event_ids", [])) == 2

    def test_coalesced_candidate_pressure_increases(self, state_dir):
        sig1 = operator_signal(
            summary="Pressure test A", kind="user_correction",
            strength_hint=0.9, correlation_keys=["pressure_key"],
        )
        raw1 = handle_sensorium_ingest_signal(signal=sig1, instance="test", state_dir=state_dir)
        cand_id = json.loads(raw1)["data"]["candidate_id"]

        store = SensoriumStore(instance="test", state_dir=state_dir)
        before = [c for c in store.read_jsonl("candidates") if c["id"] == cand_id][0]

        sig2 = operator_signal(
            summary="Pressure test B", kind="user_correction",
            strength_hint=0.9, correlation_keys=["pressure_key"],
        )
        handle_sensorium_ingest_signal(signal=sig2, instance="test", state_dir=state_dir)

        after = [c for c in store.read_jsonl("candidates") if c["id"] == cand_id][0]
        assert after["pressure"] > before["pressure"]


# --- Privacy / Surface Bounds ---


class TestPrivacySurfaceBounds:
    def test_default_sensitivity_private(self, state_dir):
        run_smoke_probes(state_dir=state_dir)
        store = SensoriumStore(instance="probe_smoke", state_dir=state_dir)
        for sig in store.read_jsonl("signals"):
            assert sig.get("sensitivity") == "private"

    def test_default_surfaces_local(self, state_dir):
        run_smoke_probes(state_dir=state_dir)
        store = SensoriumStore(instance="probe_smoke", state_dir=state_dir)
        for sig in store.read_jsonl("signals"):
            assert sig.get("allowed_surfaces") == ["local"]

    def test_candidates_inherit_privacy(self, state_dir):
        run_smoke_probes(state_dir=state_dir)
        store = SensoriumStore(instance="probe_smoke", state_dir=state_dir)
        for cand in store.read_jsonl("candidates"):
            assert cand.get("sensitivity") == "private"
            assert cand.get("allowed_surfaces") == ["local"]

    def test_events_inherit_privacy(self, state_dir):
        run_smoke_probes(state_dir=state_dir)
        store = SensoriumStore(instance="probe_smoke", state_dir=state_dir)
        for evt in store.read_jsonl("events"):
            assert evt.get("sensitivity") == "private"
            assert evt.get("allowed_surfaces") == ["local"]


# --- No Raw Content ---


class TestNoRawContent:
    _FORBIDDEN = frozenset({
        "transcript", "raw", "content", "messages", "full_text",
        "file_content", "body", "data", "text",
    })

    def test_signals_no_raw_fields(self, state_dir):
        run_smoke_probes(state_dir=state_dir)
        store = SensoriumStore(instance="probe_smoke", state_dir=state_dir)
        for sig in store.read_jsonl("signals"):
            assert not (self._FORBIDDEN & sig.keys()), f"raw field in signal: {self._FORBIDDEN & sig.keys()}"

    def test_events_no_raw_fields(self, state_dir):
        run_smoke_probes(state_dir=state_dir)
        store = SensoriumStore(instance="probe_smoke", state_dir=state_dir)
        for evt in store.read_jsonl("events"):
            assert not (self._FORBIDDEN & evt.keys()), f"raw field in event: {self._FORBIDDEN & evt.keys()}"

    def test_candidates_no_raw_fields(self, state_dir):
        run_smoke_probes(state_dir=state_dir)
        store = SensoriumStore(instance="probe_smoke", state_dir=state_dir)
        for cand in store.read_jsonl("candidates"):
            assert not (self._FORBIDDEN & cand.keys()), f"raw field in candidate: {self._FORBIDDEN & cand.keys()}"


# --- No Outbound Records ---


class TestNoOutboundRecords:
    _OUTBOUND_TYPES = frozenset({
        "message.sent", "task.created", "thread.created",
        "delivery.dispatched", "outbox.submitted",
    })

    def test_smoke_creates_no_threads(self, state_dir):
        run_smoke_probes(state_dir=state_dir)
        store = SensoriumStore(instance="probe_smoke", state_dir=state_dir)
        assert len(store.read_jsonl("threads")) == 0

    def test_smoke_no_outbound_decisions(self, state_dir):
        run_smoke_probes(state_dir=state_dir)
        store = SensoriumStore(instance="probe_smoke", state_dir=state_dir)
        for d in store.read_jsonl("decisions"):
            assert d.get("type") not in self._OUTBOUND_TYPES, f"outbound decision: {d['type']}"


# --- Seeded Audit ---


class TestSeededAudit:
    def test_counts_after_smoke(self, state_dir):
        run_smoke_probes(state_dir=state_dir)
        report = audit_store(state_dir=state_dir, instance="probe_smoke")
        assert report["counts"]["signals"] == 3
        assert report["counts"]["events"] == 3
        assert report["counts"]["candidates"] == 3

    def test_by_source(self, state_dir):
        run_smoke_probes(state_dir=state_dir)
        report = audit_store(state_dir=state_dir, instance="probe_smoke")
        assert "hermes_session" in report["by_source"]
        assert "artifact" in report["by_source"]
        assert "manual" in report["by_source"]

    def test_promotion_yield_100(self, state_dir):
        run_smoke_probes(state_dir=state_dir)
        report = audit_store(state_dir=state_dir, instance="probe_smoke")
        assert report["promotion_yield"]["yield_pct"] == 100.0

    def test_freshness_per_source(self, state_dir):
        run_smoke_probes(state_dir=state_dir)
        report = audit_store(state_dir=state_dir, instance="probe_smoke")
        for source in ("hermes_session", "artifact", "manual"):
            assert report["freshness"].get(source), f"No freshness for {source}"

    def test_by_kind(self, state_dir):
        run_smoke_probes(state_dir=state_dir)
        report = audit_store(state_dir=state_dir, instance="probe_smoke")
        assert "design_decision" in report["by_kind"]
        assert "artifact_created" in report["by_kind"]
        assert "user_correction" in report["by_kind"]


# --- Script Stdout Behavior ---


class TestScriptStdout:
    def _run(self, *extra_args, state_dir=None):
        cmd = [sys.executable, AUDIT_SCRIPT]
        if state_dir:
            cmd.extend(["--state-dir", state_dir])
        cmd.extend(extra_args)
        return subprocess.run(cmd, capture_output=True, text=True, timeout=15)

    def test_inventory_json(self):
        proc = self._run("inventory", "--json")
        assert proc.returncode == 0
        out = json.loads(proc.stdout)
        assert "implemented_helpers" in out

    def test_inventory_human(self):
        proc = self._run("inventory")
        assert proc.returncode == 0
        assert "Probe Inventory" in proc.stdout
        assert "session_event_signal" in proc.stdout

    def test_smoke_json(self):
        proc = self._run("smoke", "--json")
        assert proc.returncode == 0
        out = json.loads(proc.stdout)
        assert out["all_promoted"] is True
        assert out["counts"]["signals"] == 3

    def test_smoke_human(self):
        proc = self._run("smoke")
        assert proc.returncode == 0
        assert "Probe Smoke" in proc.stdout
        assert "PROMOTED" in proc.stdout

    def test_audit_json_empty_state(self, state_dir):
        proc = self._run("audit", "--json", state_dir=state_dir)
        assert proc.returncode == 0
        out = json.loads(proc.stdout)
        assert out["counts"]["signals"] == 0

    def test_audit_human_empty_state(self, state_dir):
        proc = self._run("audit", state_dir=state_dir)
        assert proc.returncode == 0
        assert "Probe Audit" in proc.stdout

    def test_no_subcommand_shows_help(self):
        proc = self._run()
        assert proc.returncode == 0
        assert "usage" in proc.stdout.lower() or "probe audit" in proc.stdout.lower()

    def test_smoke_stderr_clean(self):
        proc = self._run("smoke", "--json")
        assert proc.returncode == 0
        assert proc.stderr.strip() == ""


# --- Default State Not Mutated ---


class TestDefaultStateNotMutated:
    def test_smoke_uses_temp_not_default(self):
        default_inbox = Path.home() / ".hermes" / "agent-sensorium" / "default" / "signals" / "inbox.jsonl"
        before = default_inbox.read_text().splitlines() if default_inbox.exists() else []

        proc = subprocess.run(
            [sys.executable, AUDIT_SCRIPT, "smoke", "--json"],
            capture_output=True, text=True, timeout=15,
        )
        assert proc.returncode == 0

        after = default_inbox.read_text().splitlines() if default_inbox.exists() else []
        assert before == after

    def test_smoke_json_reports_temp_state_dir(self):
        proc = subprocess.run(
            [sys.executable, AUDIT_SCRIPT, "smoke", "--json"],
            capture_output=True, text=True, timeout=15,
        )
        out = json.loads(proc.stdout)
        assert "sensorium_smoke_" in out["state_dir"]
