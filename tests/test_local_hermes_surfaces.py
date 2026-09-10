from __future__ import annotations

import json

import pytest

from agent_sensorium.conscious_doorway import handle_conscious_doorway_pre_llm
from agent_sensorium.config import resolve_hermes_surface
from agent_sensorium.plugin import register
from agent_sensorium.pointers import handle_pointer_pre_llm
from agent_sensorium.pre_llm_salience import handle_salience_pre_llm
from agent_sensorium.store import SensoriumStore


LOCAL_HERMES_PLATFORMS = ("desktop", "tui", "cli", "local")
NONLOCAL_PLATFORMS = ("discord", "telegram", "subagent", "future-surface")


def test_surface_resolution_preserves_platform_metadata_and_fails_closed():
    assert resolve_hermes_surface("Desktop") == ("Desktop", "local")
    assert resolve_hermes_surface("future-surface") == (
        "future-surface",
        "future-surface",
    )


def _candidate(candidate_id: str, *, sensitivity: str = "private", pressure: float = 0.96) -> dict:
    return {
        "id": candidate_id,
        "status": "candidate",
        "kind": "subconscious_advisory",
        "pressure": pressure,
        "summary": f"Synthetic local advisory {candidate_id}",
        "fingerprint": f"fp-{candidate_id}",
        "event_ids": [f"evt_{candidate_id}"],
        "source_candidate_ids": [],
        "correlation_keys": ["surface-compatibility-test"],
        "sensitivity": sensitivity,
        "allowed_surfaces": ["local"],
        "created_at": "2026-06-07T10:00:00Z",
        "updated_at": "2026-06-07T10:00:00Z",
        "conscious_task": {
            "id": f"ctask_{candidate_id}",
            "request_type": "THINK",
            "title": f"Review {candidate_id}",
            "why": "Verify the local foreground policy domain.",
            "expected_decision": "Settle or hold with exact ownership.",
        },
        "advisory_meta": {
            "rationale": "synthetic test advisory",
            "source_fingerprint": f"source-{candidate_id}",
        },
    }


def _write_local_config(state_dir, *, max_sensitivity: str = "private") -> None:
    (state_dir / "instance.config.json").write_text(json.dumps({
        "instance_name": "test",
        "allowed_surfaces": ["local"],
        "max_sensitivity": max_sensitivity,
        "conscious_doorway": {"enabled": True, "surfaces": ["local"]},
    }))


@pytest.mark.parametrize("platform", LOCAL_HERMES_PLATFORMS)
def test_local_hermes_platforms_reach_local_doorway_with_exact_lease_and_no_outbound(
    tmp_path, platform
):
    state_dir = tmp_path / platform
    store = SensoriumStore(instance="test", state_dir=str(state_dir))
    store.ensure_dirs()
    _write_local_config(state_dir)
    store.append_jsonl("candidates", _candidate(f"cand_{platform}"))

    result = handle_conscious_doorway_pre_llm(
        instance="test",
        platform=platform,
        session_id=f"session-{platform}",
        turn_id=f"turn-{platform}",
        state_dir=str(state_dir),
    )

    assert result is not None
    candidate = store.read_jsonl("candidates")[0]
    ownership = candidate["conscious_aperture"]
    assert candidate["status"] == "in_conscious_aperture"
    assert ownership["id"] in result["context"]
    assert ownership["consumer_id"] in result["context"]
    assert ownership["lease_expires_at"] in result["context"]
    attempt = next(
        row for row in store.read_jsonl("decisions")
        if row.get("type") == "conscious.aperture.presentation_attempted"
    )
    assert attempt["surface"] == "local"
    assert attempt["platform"] == platform
    assert attempt["consumer_id"] == ownership["consumer_id"]
    assert attempt["aperture_ids"] == [ownership["id"]]
    assert attempt["host_consumption_confirmed"] is False
    assert store.read_jsonl("outbox") == store.read_jsonl("worker_requests") == []


@pytest.mark.parametrize("platform", NONLOCAL_PLATFORMS)
def test_remote_and_unknown_platforms_do_not_acquire_local_doorway_access(tmp_path, platform):
    state_dir = tmp_path / platform
    store = SensoriumStore(instance="test", state_dir=str(state_dir))
    store.ensure_dirs()
    _write_local_config(state_dir)
    store.append_jsonl("candidates", _candidate(f"cand_blocked_{platform}"))

    result = handle_conscious_doorway_pre_llm(
        instance="test",
        platform=platform,
        session_id=f"session-{platform}",
        turn_id=f"turn-{platform}",
        state_dir=str(state_dir),
    )

    assert result is None
    assert store.read_jsonl("candidates")[0]["status"] == "candidate"
    assert store.read_jsonl("decisions") == []
    assert store.read_jsonl("outbox") == store.read_jsonl("worker_requests") == []


def test_desktop_alias_does_not_bypass_private_candidate_policy(tmp_path):
    state_dir = tmp_path / "private-policy"
    store = SensoriumStore(instance="test", state_dir=str(state_dir))
    store.ensure_dirs()
    _write_local_config(state_dir, max_sensitivity="local_only")
    store.append_jsonl("candidates", _candidate("cand_private"))

    result = handle_conscious_doorway_pre_llm(
        instance="test",
        platform="desktop",
        session_id="session-private",
        turn_id="turn-private",
        state_dir=str(state_dir),
    )

    assert result is None
    assert store.read_jsonl("candidates")[0]["status"] == "candidate"
    assert store.read_jsonl("decisions") == []


@pytest.mark.parametrize("platform", LOCAL_HERMES_PLATFORMS)
def test_pointer_uses_local_policy_surface_and_preserves_platform(tmp_path, platform):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    _write_local_config(tmp_path)
    store.append_jsonl("candidates", _candidate("cand_pointer"))

    result = handle_pointer_pre_llm(
        instance="test",
        platform=platform,
        session_id=f"session-{platform}",
        state_dir=str(tmp_path),
    )

    assert result is not None
    assert 'surface="local"' in result["context"]
    receipt = store.read_jsonl("decisions")[-1]
    assert receipt["type"] == "pointer.presented"
    assert receipt["surface"] == "local"
    assert receipt["platform"] == platform


@pytest.mark.parametrize("platform", NONLOCAL_PLATFORMS)
def test_pointer_and_salience_fail_closed_for_nonlocal_platforms(tmp_path, platform):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    _write_local_config(tmp_path)
    store.append_jsonl("candidates", _candidate("cand_pointer"))

    pointer = handle_pointer_pre_llm(
        instance="test",
        platform=platform,
        session_id=f"session-{platform}",
        state_dir=str(tmp_path),
    )
    salience = handle_salience_pre_llm(
        instance="test",
        platform=platform,
        state_dir=str(tmp_path),
    )

    assert pointer is None
    assert salience is None
    assert store.read_jsonl("decisions") == []


@pytest.mark.parametrize("platform", LOCAL_HERMES_PLATFORMS)
def test_known_local_platforms_receive_salience_and_ingest_into_local_policy_domain(
    tmp_path, monkeypatch, platform
):
    import agent_sensorium.store as store_module

    root = tmp_path / "state"
    monkeypatch.setattr(store_module, "_DEFAULT_BASE", str(root))
    salience = handle_salience_pre_llm(
        instance="test",
        platform=platform,
        state_dir=str(root / "direct"),
    )
    assert salience is not None
    assert "Sensorium Salience Hook" in salience["context"]

    ctx = _PluginContext()
    register(ctx)
    result = json.loads(ctx.tools["sensorium"](
        {
            "action": "ingest",
            "instance": "test",
            "text": "Keep one compact local advisory.",
            "kind": "design_insight",
            "surface": platform,
        },
        platform=platform,
    ))

    store = SensoriumStore(instance="test")
    signal = store.read_jsonl("signals")[-1]
    receipt = store.read_jsonl("decisions")[-1]
    assert result["success"] is True
    assert signal["allowed_surfaces"] == ["local"]
    assert signal["platform"] == platform
    assert "surface:local" in signal["correlation_keys"]
    assert receipt["surface"] == "local"
    assert receipt["platform"] == platform


class _PluginContext:
    def __init__(self):
        self.tools = {}

    def register_tool(self, *, name, handler, **kwargs):
        self.tools[name] = handler

    def register_hook(self, *args, **kwargs):
        pass

    def register_command(self, *args, **kwargs):
        pass

    def register_skill(self, *args, **kwargs):
        pass
