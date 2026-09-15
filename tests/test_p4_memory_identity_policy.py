from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from agent_sensorium import admission
from agent_sensorium import memory_reflection as mr
from agent_sensorium.admission import binding_for_candidate, build_admission_plan
from agent_sensorium.store import SensoriumStore
from agent_sensorium.tools import handle_sensorium_ingest_signal

ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-09-10T09:00:00Z"
SCOPE = {"provider": "hindsight", "bank_id": "synthetic-bank"}


def _probe(**overrides) -> mr.ProbeConfig:
    values = {
        "id": "policy", "query": "bounded synthetic query", "cadence_hours": 1.0,
        "cooldown_hours": 0.0, "max_signals": 1, "require_delta": False,
        "low_significance_liveness": True, "strength_hint": 1.0,
    }
    values.update(overrides)
    return mr.ProbeConfig(**values)


def _config(path: Path, *, mode: str = "reflect") -> None:
    (path / "memory_reflection.json").write_text(json.dumps({
        "enabled": True,
        "probes": [{
            "id": "policy", "query": "bounded synthetic query", "mode": mode,
            "cadence": {"type": "interval", "hours": 1}, "cooldown_hours": 0,
            "max_signals": 1, "require_delta": False,
            "low_significance_liveness": True, "strength_hint": 1.0,
        }],
    }))


def _complete(text: str, *ids: str) -> dict:
    return {"text": text, "based_on": {"memories": [{"id": value} for value in ids]}}


def _raw_ref() -> dict:
    return {"raw_ref": "/synthetic/raw", "raw_sha256": "abc"}


def _unsupported_signal(signal_id: str = "unsupported") -> dict:
    return {
        "id": signal_id, "sensor": mr.MEMORY_REFLECTION_SENSOR,
        "source": mr.MEMORY_REFLECTION_SOURCE, "kind": mr.MEMORY_REFLECTION_KIND,
        "summary": "unsupported native synthesis", "actor": "tool",
        "strength_hint": 1.0, "sensitivity": "private", "allowed_surfaces": ["local"],
        "correlation_keys": ["memory-reflection:policy"], "unverified": True,
    }


def _ingest(store: SensoriumStore, signal: dict) -> dict:
    return json.loads(handle_sensorium_ingest_signal(
        signal=signal, instance=store.instance, state_dir=str(store.root),
    ))["data"]


def test_reducer_validates_complete_response_before_max_signals_truncation():
    signals, _ = mr.reduce_reflection(
        raw_output={"results": [
            {"id": "visible", "text": "identified prefix"},
            {"text": "unidentified tail"},
        ]}, probe=_probe(max_signals=1), raw_ref=_raw_ref(), now=NOW,
        source_scope=SCOPE,
    )
    assert len(signals) == 1
    assert signals[0]["source_identity_status"] == "unsupported"
    assert "memory_provenance" not in signals[0]


@pytest.mark.parametrize("raw", [
    {"text": "missing ids"},
    {"text": "partial", "based_on": {"memories": [{"id": "A"}, {}]}},
    {"results": [{"id": "dup", "text": "A"}, {"id": "dup", "text": "B"}]},
    {"results": [{"id": "x" * 513, "text": "too long"}]},
])
def test_unsupported_nonempty_run_is_visible_and_never_ingested(tmp_path, raw):
    _config(tmp_path)
    fake = mr.FakeHindsightMemoryClient(reflect_result=raw)
    ingested: list[dict] = []
    result = mr.run_due_probes(
        state_dir=str(tmp_path), client=fake, now=NOW, force=True,
        ingest_fn=lambda signal: ingested.append(signal) or {},
    )
    run = result["runs"][0]
    assert len(fake.calls) == 1
    assert ingested == []
    assert run["status"] == run["source_identity_status"] == "unsupported"
    assert run["emit_reason"] == "unsupported_source_identity"
    assert run["delta"] is False
    assert run["emitted_count"] == 0 and run["emitted_summaries"] == []
    assert run["raw_ref"]
    assert mr.read_history(str(tmp_path))[-1] == run


def test_invalid_or_absent_adapter_scope_is_unsupported(tmp_path):
    class InvalidScope(mr.FakeHindsightMemoryClient):
        def source_scope(self):
            return {"provider": "hindsight"}

    class AbsentScope(mr.FakeHindsightMemoryClient):
        pass

    absent = AbsentScope(reflect_result=_complete("identified", "A"))
    absent.source_scope = None  # type: ignore[method-assign]
    for index, client in enumerate((
        InvalidScope(reflect_result=_complete("identified", "A")), absent,
    )):
        state = tmp_path / str(index)
        state.mkdir()
        _config(state)
        captured: list[dict] = []
        run = mr.run_due_probes(
            state_dir=str(state), client=client, now=NOW,
            ingest_fn=lambda signal: captured.append(signal) or {},
        )["runs"][0]
        assert run["status"] == "unsupported" and captured == []


def test_rewording_delta_disabled_liveness_and_force_are_not_escape_hatches(tmp_path):
    _config(tmp_path)
    first = mr.FakeHindsightMemoryClient(reflect_result={"text": "wording one"})
    second = mr.FakeHindsightMemoryClient(reflect_result={"text": "wording two"})
    captured: list[dict] = []
    for when, client in ((NOW, first), ("2026-09-10T10:00:00Z", second)):
        run = mr.run_due_probes(
            state_dir=str(tmp_path), client=client, now=when, force=True,
            ingest_fn=lambda signal: captured.append(signal) or {},
        )["runs"][0]
        assert run["status"] == "unsupported"
        assert run["delta"] is False and run["emitted_count"] == 0
    assert captured == []


def test_dry_run_reports_unsupported_but_writes_no_state(tmp_path):
    _config(tmp_path)
    fake = mr.FakeHindsightMemoryClient(reflect_result={"text": "unsupported"})
    run = mr.run_due_probes(
        state_dir=str(tmp_path), client=fake, now=NOW, force=True, dry_run=True,
        ingest_fn=lambda signal: pytest.fail("dry run must not ingest"),
    )["runs"][0]
    assert run["status"] == "unsupported" and run["raw_ref"] == ""
    assert not (tmp_path / "memory_reflection" / "history.jsonl").exists()
    assert not (tmp_path / "memory_reflection" / "raw").exists()


def test_empty_error_and_unsupported_to_complete_recovery(tmp_path):
    _config(tmp_path)
    empty = mr.run_due_probes(
        state_dir=str(tmp_path), client=mr.FakeHindsightMemoryClient(reflect_result={}),
        now=NOW, force=True,
    )["runs"][0]
    assert empty["status"] == "ok" and empty["source_identity_status"] == "empty"
    error = mr.run_due_probes(
        state_dir=str(tmp_path), client=mr.FakeHindsightMemoryClient(error=TimeoutError()),
        now="2026-09-10T10:00:00Z", force=True,
    )["runs"][0]
    assert error["status"] == "error" and "source_identity_status" not in error
    captured: list[dict] = []
    complete = mr.run_due_probes(
        state_dir=str(tmp_path),
        client=mr.FakeHindsightMemoryClient(reflect_result=_complete("supported", "A", "B")),
        now="2026-09-10T11:00:00Z", force=True,
        ingest_fn=lambda signal: captured.append(signal) or {},
    )["runs"][0]
    assert complete["status"] == "ok" and complete["source_identity_status"] == "complete"
    assert complete["emitted_count"] == 1 and len(captured) == 1
    assert captured[0]["memory_provenance"]["item_ids"] == ["A", "B"]


def test_ordinary_recall_with_complete_ids_remains_available(tmp_path):
    _config(tmp_path, mode="recall")
    fake = mr.FakeHindsightMemoryClient(recall_result={
        "results": [{"id": "A", "text": "ordinary bounded recall"}],
    })
    captured: list[dict] = []
    run = mr.run_due_probes(
        state_dir=str(tmp_path), client=fake, now=NOW,
        ingest_fn=lambda signal: captured.append(signal) or {},
    )["runs"][0]
    assert fake.calls[0]["op"] == "recall"
    assert run["source_identity_status"] == "complete" and len(captured) == 1


def test_retained_unsupported_native_candidate_suppresses_before_model_and_preserves_ledgers(tmp_path):
    from tests.test_p2_admission import _clock_args, _load_clock
    store = SensoriumStore(instance="unsupported", state_dir=str(tmp_path / "state"))
    store.ensure_dirs()
    result = _ingest(store, _unsupported_signal())
    before = {name: store.read_jsonl(name) for name in ("signals", "events", "candidates", "decisions")}
    plan = build_admission_plan(store)
    assert plan["selection"] is None
    assert plan["suppressed_counts"] == {"unsupported_memory_identity": 1}
    calls = 0
    def forbidden(command, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("unsupported native candidate must not launch")
    tick = _load_clock().run_once(_clock_args(store), run_command=forbidden)
    assert tick["action"] == "skipped_no_eligible_source" and calls == 0
    assert {name: store.read_jsonl(name) for name in before} == before
    assert result["promoted"] is True


def test_malformed_explicit_memory_provenance_keeps_existing_reason(tmp_path):
    store = SensoriumStore(instance="malformed", state_dir=str(tmp_path / "state"))
    store.ensure_dirs()
    signal = _unsupported_signal("malformed")
    signal["memory_provenance"] = {"provider": "hindsight", "bank_id": "b", "item_ids": ["A", "A"]}
    _ingest(store, signal)
    assert build_admission_plan(store)["suppressed_counts"] == {"malformed_memory_identity": 1}


def test_unrelated_unknown_first_consideration_remains_eligible(tmp_path):
    store = SensoriumStore(instance="unknown", state_dir=str(tmp_path / "state"))
    store.ensure_dirs()
    signal = _unsupported_signal("other")
    signal.update(sensor="other.sensor", source="other", kind="creative_pull")
    result = _ingest(store, signal)
    binding, error = binding_for_candidate(store, result["candidate_id"])
    assert error is None and binding and binding["identity_mode"] == "legacy"
    assert build_admission_plan(store)["selection"] == binding


def test_tick_projection_exposes_unsupported_fields(tmp_path, monkeypatch, capsys):
    path = ROOT / "scripts" / "sensorium_tick.py"
    spec = importlib.util.spec_from_file_location("p4_tick", path)
    assert spec and spec.loader
    tick = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tick)
    monkeypatch.setattr(tick.memory_reflection_mod, "run_due_probes", lambda **kwargs: {
        "enabled": True, "config_source": "synthetic", "due": [{"probe_id": "policy"}],
        "skipped": [], "runs": [{
            "probe_id": "policy", "status": "unsupported",
            "emit_reason": "unsupported_source_identity", "emitted_count": 0, "delta": False,
        }],
    })
    assert tick.main(["--instance", "tick-policy", "--state-dir", str(tmp_path),
                      "--memory-reflection", "--dry-run", "--json"]) == 0
    projected = json.loads(capsys.readouterr().out)["memory_reflection"]["runs"][0]
    assert projected == {
        "probe_id": "policy", "status": "unsupported",
        "emit_reason": "unsupported_source_identity", "emitted_count": 0, "delta": False,
    }


def test_imports_bind_to_successor():
    assert Path(mr.__file__).resolve() == ROOT / "agent_sensorium" / "memory_reflection.py"
    assert Path(admission.__file__).resolve() == ROOT / "agent_sensorium" / "admission.py"
