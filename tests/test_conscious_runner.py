from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from agent_sensorium.conscious_aperture import open_conscious_aperture
from agent_sensorium.conscious_consumer import consume_conscious_advisory, parse_conscious_decision
from agent_sensorium.store import SensoriumStore


ROOT = Path(__file__).parents[1]


def _load_runner():
    path = ROOT / "scripts" / "sensorium_native_conscious.py"
    spec = importlib.util.spec_from_file_location("sensorium_native_conscious", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _store(tmp_path: Path) -> SensoriumStore:
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "sensorium"))
    store.ensure_dirs()
    return store


def _advisory(candidate_id: str = "advisory_1", fingerprint: str = "source-revision-1") -> dict:
    return {
        "id": candidate_id,
        "status": "candidate",
        "kind": "subconscious_advisory",
        "pressure": 0.9,
        "summary": "A bounded source deserves one conscious choice.",
        "event_ids": ["event_1"],
        "source_candidate_ids": ["source_1"],
        "source_candidate_fingerprint": fingerprint,
        "sensitivity": "private",
        "allowed_surfaces": ["local"],
        "created_at": "2026-08-26T10:00:00Z",
        "updated_at": "2026-08-26T10:00:00Z",
        "conscious_task": {
            "id": "ctask_1",
            "request_type": "THINK",
            "title": "Choose whether to reach out",
            "why": "The source remains meaningfully unresolved.",
            "expected_decision": "Silence, hold, or author one local message.",
        },
    }


def _args(tmp_path: Path, **overrides) -> argparse.Namespace:
    values = {
        "instance": "test",
        "state_dir": str(tmp_path / "sensorium"),
        "plugin_root": str(ROOT),
        "hermes_cli": "/absolute/hermes",
        "provider": "openai-codex",
        "model": "gpt-5.6-sol",
        "timeout_seconds": 30,
        "failure_cooldown_seconds": 1800,
        "stale_after_minutes": 180,
        "force": False,
        "emit_reachout": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _model_response(decision: str, *, candidate_id="advisory_1", fingerprint="source-revision-1", **fields) -> str:
    payload = {
        "decision": decision,
        "candidate_id": candidate_id,
        "source_candidate_fingerprint": fingerprint,
        "reason": "This exact source deserves a bounded present-tense choice.",
        **fields,
    }
    return json.dumps(payload)


def _assert_opening_without_semantic_settlement(store: SensoriumStore) -> None:
    decisions = store.read_jsonl("decisions")
    assert [row.get("type") for row in decisions] == ["conscious.aperture.opened"]
    assert not any(
        row.get("decision") in {"HELD", "REVIEWED", "SETTLED", "PREPARED_EXTERNAL_WORK"}
        for row in decisions
    )


def test_native_conscious_runner_uses_one_fresh_read_continuity_session_and_silence(tmp_path):
    module = _load_runner()
    store = _store(tmp_path)
    store.append_jsonl("candidates", _advisory())
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        assert command[:2] == ["/absolute/hermes", "chat"]
        assert command[:-1] == [
            "/absolute/hermes", "chat", "-Q",
            "--provider", "openai-codex",
            "-m", "gpt-5.6-sol",
            "-t", "context_engine,memory,session_search",
            "--source", "sensorium-native-conscious",
            "--session-purpose", "autonomous",
            "--max-turns", "6",
            "--run-budget", "210",
            "-q",
        ]
        assert "--ignore-rules" not in command
        assert "--ignore-user-config" not in command
        assert kwargs["timeout"] == 30
        assert "source-revision-1" in command[-1]
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=_model_response("SILENCE"),
            stderr="\nsession_id: hermes-session-123\n",
        )

    result = module.run_once(_args(tmp_path), run_command=fake_run, now="2026-08-26T11:00:00Z")

    assert result["success"] is True
    assert result["action"] == "settled_silence"
    assert result["session"]["session_id"] == "hermes-session-123"
    assert len(calls) == 1
    assert store.read_jsonl("candidates")[0]["status"] == "reviewed"
    assert store.read_jsonl("threads") == []
    assert store.read_jsonl("worker_requests") == []
    assert store.read_jsonl("artifacts") == []
    assert "message" not in json.dumps(result)


def test_native_conscious_prompt_allows_only_one_read_only_continuity_round():
    module = _load_runner()
    prompt = module.build_conscious_prompt({
        "candidate_id": "advisory_1",
        "source_candidate_fingerprint": "source-revision-1",
    })
    payload = json.loads(prompt.split("\n\n", 1)[1])
    authority = payload["authority"]

    assert authority["optional_read_only_continuity"] is True
    assert authority["maximum_retrieval_rounds"] == 1
    assert authority["allowed_tools"] == [
        "hindsight_recall",
        "hindsight_reflect",
        "session_search",
        "lcm_*",
    ]
    assert authority["forbidden_tools"] == ["memory", "hindsight_retain"]
    assert authority["no_memory_or_file_writes"] is True
    assert "Never write memory or files" in prompt


def test_native_conscious_runner_applies_hold_and_reach_out_deterministically(tmp_path):
    module = _load_runner()
    store = _store(tmp_path)
    store.append_jsonl("candidates", _advisory())
    responses = iter([
        _model_response("HOLD", return_at="2026-08-26T12:00:00Z"),
    ])

    first = module.run_once(
        _args(tmp_path),
        run_command=lambda command, **kwargs: subprocess.CompletedProcess(command, 0, stdout=next(responses), stderr=""),
        now="2026-08-26T11:00:00Z",
    )
    assert first["action"] == "settled_hold"
    assert store.read_jsonl("candidates")[0]["status"] == "held"

    # A due held item can be reopened by the next explicit runner cycle.
    store.rewrite_jsonl("candidates", [{
        **store.read_jsonl("candidates")[0],
        "status": "candidate",
        "source_candidate_fingerprint": "source-revision-2",
        "held_return": {},
    }])
    store.append_jsonl("candidates", _advisory("advisory_2", "source-revision-3"))
    # The first item remains the only highest-pressure candidate; the runner must
    # not open two items just because another candidate exists.
    response = _model_response(
        "REACH_OUT",
        candidate_id="advisory_1",
        fingerprint="source-revision-2",
        message="I thought of you while this quiet thread kept opening.",
    )
    second = module.run_once(
        _args(tmp_path, force=True),
        run_command=lambda command, **kwargs: subprocess.CompletedProcess(command, 0, stdout=response, stderr=""),
        now="2026-08-26T13:00:00Z",
    )
    assert second["action"] == "prepared_reach_out"
    assert len(store.read_jsonl("outbox")) == 1
    assert store.read_jsonl("threads") == []
    assert store.read_jsonl("worker_requests") == []
    assert store.read_jsonl("artifacts") == []


def test_model_parser_rejects_non_json_extra_fields_and_wrong_binding(tmp_path):
    module = _load_runner()
    packet = {
        "candidate_id": "advisory_1",
        "source_candidate_fingerprint": "source-revision-1",
    }
    for raw in ("not json", "```json\n{}\n```"):
        with pytest.raises(ValueError):
            module.extract_conscious_response(raw)
    with pytest.raises(ValueError, match="unexpected fields"):
        module.parse_model_decision(
            json.loads(_model_response("SILENCE", extra="no")), packet,
        )
    with pytest.raises(ValueError, match="candidate_id"):
        module.parse_model_decision(
            json.loads(_model_response("SILENCE", candidate_id="other")), packet,
        )
    with pytest.raises(ValueError, match="source_candidate_fingerprint"):
        module.parse_model_decision(
            json.loads(_model_response("SILENCE", fingerprint="source-revision-2")), packet,
        )


def test_source_binding_mismatch_is_rejected_before_canonical_write(tmp_path):
    store = _store(tmp_path)
    store.append_jsonl("candidates", _advisory())
    opened = open_conscious_aperture(store, aperture_size=1, dry_run=False, now="2026-08-26T11:00:00Z")
    before = {name: store.read_jsonl(name) for name in ("candidates", "decisions", "outbox")}
    candidates = store.read_jsonl("candidates")
    candidates[0]["source_candidate_fingerprint"] = "newer-revision"
    store.rewrite_jsonl("candidates", candidates)
    before_mutation = {name: store.read_jsonl(name) for name in ("candidates", "decisions", "outbox")}

    result = consume_conscious_advisory(
        store,
        decision={"decision": "SILENCE", "reason": "The exact source changed before application."},
        dry_run=False,
        now="2026-08-26T11:01:00Z",
        expected_candidate_id="advisory_1",
        expected_aperture_id=opened["aperture_id"],
        expected_source_candidate_fingerprint="source-revision-1",
    )

    assert result["success"] is False
    assert result["error"] == "source_revision_mismatch"
    assert {name: store.read_jsonl(name) for name in ("candidates", "decisions", "outbox")} == before_mutation
    assert before["decisions"]


@pytest.mark.parametrize("same_body", [True, False])
def test_revision_idempotency_survives_partial_settlement_failure(tmp_path, monkeypatch, same_body):
    import agent_sensorium.conscious_consumer as consumer

    store = _store(tmp_path)
    store.append_jsonl("candidates", _advisory())
    original_settle = consumer._settle
    calls = 0

    def fail_final_settlement(*args, **kwargs):
        nonlocal calls
        if kwargs.get("decision") == "SETTLED":
            calls += 1
            return {"success": False, "error": "injected_settlement_failure"}
        return original_settle(*args, **kwargs)

    monkeypatch.setattr(consumer, "_settle", fail_final_settlement)
    first = consume_conscious_advisory(
        store,
        decision={"decision": "REACH_OUT", "reason": "Choose this exact source now.", "message": "I thought of you while this quiet thread kept opening."},
        dry_run=False,
        now="2026-08-26T11:00:00Z",
    )
    assert first["action"] == "prepared_reach_out_unsettled"
    assert len(store.read_jsonl("outbox")) == 1

    monkeypatch.setattr(consumer, "_settle", original_settle)
    message = (
        "I thought of you while this quiet thread kept opening."
        if same_body else "A different wording for the same source revision."
    )
    second = consume_conscious_advisory(
        store,
        decision={"decision": "REACH_OUT", "reason": "The retry must preserve the exact chosen wording.", "message": message},
        dry_run=False,
        now="2026-08-26T11:01:00Z",
    )
    assert second["success"] is True
    rows = store.read_jsonl("outbox")
    assert len(rows) == (1 if same_body else 2)
    assert all(row["source_candidate_fingerprint"] == "source-revision-1" for row in rows)
    assert rows[-1]["message_preview"] == message
    assert second["outbox_id"] == rows[-1]["id"]
    assert calls == 1


def test_500_character_envelope_is_truthful():
    message = "x" * 500
    parsed = parse_conscious_decision({"decision": "REACH_OUT", "reason": "Specific.", "message": message})
    assert len(parsed["message"]) == 500
    with pytest.raises(ValueError, match="500"):
        parse_conscious_decision({"decision": "REACH_OUT", "reason": "Specific.", "message": message + "x"})


def test_dry_run_policy_denial_is_not_reported_as_preparable(tmp_path):
    store = _store(tmp_path)
    store.append_jsonl("candidates", _advisory())
    before = {name: store.read_jsonl(name) for name in ("candidates", "decisions", "outbox")}

    result = consume_conscious_advisory(
        store,
        decision={"decision": "REACH_OUT", "reason": "Policy must decide before preparation.", "message": "I thought of you while this quiet thread kept opening."},
        config={"conscious_reachout": {"enabled": False}},
        dry_run=True,
        now="2026-08-26T11:00:00Z",
    )

    assert result["success"] is False
    assert result["error"] == "reachout_disabled"
    assert "prepared" not in json.dumps(result)
    assert {name: store.read_jsonl(name) for name in ("candidates", "decisions", "outbox")} == before


def test_stale_aperture_is_reclaimed_then_settled_by_exact_owner(tmp_path):
    store = _store(tmp_path)
    candidate = _advisory()
    candidate.update({
        "status": "in_conscious_aperture",
        "conscious_aperture": {"id": "cap_stale", "opened_at": "2026-08-26T00:00:00Z"},
    })
    store.append_jsonl("candidates", candidate)

    result = consume_conscious_advisory(
        store,
        decision={"decision": "SILENCE", "reason": "Do not replace an owner-held stale aperture."},
        dry_run=False,
        now="2026-08-26T11:00:00Z",
    )

    assert result["action"] == "settled_silence"
    assert result["success"] is True
    assert result["settlement"]["success"] is True
    assert store.read_jsonl("candidates")[0]["status"] == "reviewed"


def test_failure_is_bounded_and_cools_down_without_inventing_hold(tmp_path):
    module = _load_runner()
    store = _store(tmp_path)
    store.append_jsonl("candidates", _advisory())
    calls = []

    def fail(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 7, stdout="", stderr="provider unavailable")

    first = module.run_once(_args(tmp_path), run_command=fail, now="2026-08-26T11:00:00Z")
    assert first["success"] is False
    assert first["action"] == "conscious_session_failed"
    assert first["lease_release"] == "canonical_expiry"
    assert store.read_jsonl("candidates")[0]["status"] == "in_conscious_aperture"
    _assert_opening_without_semantic_settlement(store)
    second = module.run_once(_args(tmp_path), run_command=fail, now="2026-08-26T11:01:00Z")
    assert second["action"] == "skipped_retry_cooldown"
    assert len(calls) == 1


def test_runner_preserves_unresolved_aperture_when_model_times_out(tmp_path):
    module = _load_runner()
    store = _store(tmp_path)
    store.append_jsonl("candidates", _advisory())
    calls = []

    def fail(command, **kwargs):
        calls.append(command)
        raise subprocess.TimeoutExpired(command, 30)

    first = module.run_once(
        _args(tmp_path), run_command=fail, now="2026-08-26T11:00:00Z",
    )

    assert first["success"] is False
    assert first["action"] == "conscious_session_failed"
    assert first["opened_by_this_run"] is True
    candidate = store.read_jsonl("candidates")[0]
    assert candidate["status"] == "in_conscious_aperture"
    assert "held_return" not in candidate
    _assert_opening_without_semantic_settlement(store)

    second = module.run_once(
        _args(tmp_path, force=True), run_command=fail, now="2026-08-26T11:30:00Z",
    )
    assert second["action"] == "skipped_retry_cooldown"
    assert len(calls) == 1


def test_runner_does_not_consume_preexisting_ownerless_aperture(tmp_path):
    module = _load_runner()
    store = _store(tmp_path)
    store.write_conscious_aperture_state(store.new_conscious_aperture_state())
    candidate = _advisory()
    candidate.update({
        "status": "in_conscious_aperture",
        "conscious_aperture": {
            "id": "cap_existing",
            "opened_at": "2026-08-26T10:30:00Z",
            "state": "open",
        },
    })
    store.append_jsonl("candidates", candidate)

    result = module.run_once(
        _args(tmp_path),
        run_command=lambda command, **kwargs: subprocess.CompletedProcess(
            command, 7, stdout="", stderr="provider unavailable",
        ),
        now="2026-08-26T11:00:00Z",
    )

    assert result["success"] is True
    assert result["action"] == "no_current_advisory"
    assert store.read_jsonl("candidates")[0]["status"] == "in_conscious_aperture"
    assert store.read_jsonl("decisions") == []


def test_runner_preserves_source_when_binding_is_missing_after_open(tmp_path):
    module = _load_runner()
    store = _store(tmp_path)
    candidate = _advisory(fingerprint="")
    store.append_jsonl("candidates", candidate)

    result = module.run_once(
        _args(tmp_path), run_command=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("source binding must fail before model")
        ), now="2026-08-26T11:00:00Z",
    )

    assert result["success"] is False
    assert result["action"] == "source_binding_missing"
    assert result["opened_by_this_run"] is True
    assert store.read_jsonl("candidates")[0]["status"] == "in_conscious_aperture"
    _assert_opening_without_semantic_settlement(store)


def test_runner_preserves_source_when_deterministic_application_fails(tmp_path, monkeypatch):
    module = _load_runner()
    store = _store(tmp_path)
    store.append_jsonl("candidates", _advisory())

    def fail_apply(*args, **kwargs):
        return {"success": False, "error": "injected_apply_failure"}

    monkeypatch.setattr(module, "consume_conscious_advisory", fail_apply)
    result = module.run_once(
        _args(tmp_path),
        run_command=lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, stdout=_model_response("SILENCE"), stderr="",
        ),
        now="2026-08-26T11:00:00Z",
    )

    assert result["success"] is False
    assert result["action"] == "conscious_session_failed"
    assert "injected_apply_failure" in result["reason"]
    assert store.read_jsonl("candidates")[0]["status"] == "in_conscious_aperture"
    _assert_opening_without_semantic_settlement(store)
    state = json.loads((store.root / "conscious_clock_state.json").read_text())
    assert state["attempt_history"][-1]["failure_class"] == "apply_failure"


@pytest.mark.parametrize("returncode,stdout,stderr,expected", [
    (0, "not json", "", "invalid_disposition"),
    (7, "", "turn iteration exhaustion", "turn_exhaustion"),
    (7, "", "memory unavailable", "memory_unavailable"),
])
def test_runner_records_classified_nonsemantic_failures(
    tmp_path, returncode, stdout, stderr, expected,
):
    module = _load_runner()
    store = _store(tmp_path)
    store.append_jsonl("candidates", _advisory())
    result = module.run_once(
        _args(tmp_path),
        run_command=lambda command, **kwargs: subprocess.CompletedProcess(
            command, returncode, stdout=stdout, stderr=stderr,
        ),
        now="2026-08-26T11:00:00Z",
    )
    assert result["success"] is False
    state = json.loads((store.root / "conscious_clock_state.json").read_text())
    assert state["attempt_history"][-1]["failure_class"] == expected
    _assert_opening_without_semantic_settlement(store)


def test_restart_reconciles_canonical_post_apply_before_retry(tmp_path):
    module = _load_runner()
    store = _store(tmp_path)
    store.append_jsonl("candidates", _advisory())
    first = module.run_once(
        _args(tmp_path),
        run_command=lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, stdout=_model_response("SILENCE"), stderr="",
        ),
        now="2026-08-26T11:00:00Z",
    )
    assert first["action"] == "settled_silence"
    revision = module.source_revision_key(
        candidate_id="advisory_1", source_candidate_ids=["source_1"],
        source_candidate_fingerprint="source-revision-1",
    )
    state_path = store.root / "conscious_clock_state.json"
    state = json.loads(state_path.read_text())
    module.start_attempt(
        state, session_purpose="autonomous", source_revision=revision, ordinal=2,
        stage="applying", deadline_seconds=60, now="2026-08-26T11:01:00Z",
    )
    module._json_write(state_path, state)

    recovered = module.run_once(
        _args(tmp_path),
        run_command=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("canonical post-apply recovery must not reinfer")
        ),
        now="2026-08-26T11:03:00Z",
    )
    assert recovered["action"] == "recovered_canonical_disposition"
    final_state = json.loads(state_path.read_text())
    assert final_state["attempt_history"][-1]["status"] == "succeeded"
    assert final_state["attempt_history"][-1]["failure_class"] is None
    assert final_state["retry_state"]["last_status"] == "succeeded"
    assert len([d for d in store.read_jsonl("decisions") if d.get("type") == "conscious.aperture.settled"]) == 1


def test_runner_never_calls_semantic_settlement_on_model_failure(tmp_path, monkeypatch):
    module = _load_runner()
    store = _store(tmp_path)
    store.append_jsonl("candidates", _advisory())

    def fail_settlement(*args, **kwargs):
        raise RuntimeError("injected settlement outage")

    monkeypatch.setattr(module, "settle_conscious_aperture_item", fail_settlement)
    result = module.run_once(
        _args(tmp_path),
        run_command=lambda command, **kwargs: subprocess.CompletedProcess(
            command, 7, stdout="", stderr="provider unavailable",
        ),
        now="2026-08-26T11:00:00Z",
    )

    assert result["success"] is False
    assert result["action"] == "conscious_session_failed"
    assert "provider unavailable" in result["reason"]
    assert result["lease_release"] == "canonical_expiry"
    assert "settlement" not in result
    assert store.read_jsonl("candidates")[0]["status"] == "in_conscious_aperture"


def test_hermes_transport_envelope_accepts_known_session_footer_and_rejects_wrappers(tmp_path):
    module = _load_runner()
    payload = _model_response("SILENCE")
    assert module.extract_conscious_response(payload)["decision"] == "SILENCE"
    parsed, session = module.extract_conscious_transport(
        payload,
        "\nsession_id: hermes-session-123\n",
    )
    assert parsed["decision"] == "SILENCE"
    assert session == {"session_id": "hermes-session-123"}

    for invalid in (
        "prose before\n" + payload,
        "```json\n" + payload + "\n```",
        payload + "\n" + payload,
    ):
        with pytest.raises(ValueError):
            module.extract_conscious_response(invalid)


def test_hermes_transport_keeps_unexpected_stderr_out_of_decision_content(tmp_path):
    module = _load_runner()
    payload = _model_response("SILENCE")

    parsed, session = module.extract_conscious_transport(payload, "diagnostic only\n")

    assert parsed == json.loads(payload)
    assert session == {}

    for invalid_stderr in (
        "session_id:\n",
        "session_id: contains whitespace\n",
        "session_id: first\nsession_id: second\n",
    ):
        with pytest.raises(ValueError):
            module.extract_conscious_transport(payload, invalid_stderr)


def test_lock_skips_without_subprocess(tmp_path):
    module = _load_runner()
    store = _store(tmp_path)
    held = module.acquire_lock(store.root / "locks" / "conscious_clock.lock")
    assert held is not None
    try:
        result = module.run_once(
            _args(tmp_path),
            run_command=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("locked")),
        )
    finally:
        module.release_lock(held)
    assert result["action"] == "skipped_locked"


def test_emit_mode_is_empty_for_non_reachout_and_body_only_for_prepared_message(tmp_path):
    module = _load_runner()
    assert module.scheduler_output({"action": "settled_silence"}, []) == ""
    assert module.scheduler_output({"action": "settled_hold"}, []) == ""
    assert module.scheduler_output({"action": "prepared_reach_out", "outbox_id": "obx_1"}, [
        {"id": "obx_1", "status": "prepared", "message_preview": "I thought of you."},
    ]) == "I thought of you."
