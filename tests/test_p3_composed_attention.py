from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_sensorium.conscious_aperture import open_conscious_aperture
from agent_sensorium.conscious_consumer import consume_conscious_advisory
from agent_sensorium.plugin import register
from agent_sensorium.store import SensoriumStore

ROOT = Path(__file__).resolve().parents[1]
HERMES = ROOT.parent / "hermes"
WRAPPER = ROOT.parent / "host-adapters" / "sensorium_native_conscious_surface.py"
RUNNER = ROOT / "scripts" / "sensorium_native_conscious.py"


def require_companion_hermes(*, wrapper: bool = False) -> None:
    missing = [
        path
        for path in (HERMES / "cli.py", HERMES / "hermes_cli")
        if not path.exists()
    ]
    if wrapper and not WRAPPER.is_file():
        missing.append(WRAPPER)
    if missing:
        pytest.skip(
            "requires companion Hermes checkout"
            + (" and host adapter" if wrapper else "")
            + "; missing: "
            + ", ".join(str(path) for path in missing)
        )


class PluginContext:
    def __init__(self):
        self.tools = {}
        self.hooks = []

    def register_tool(self, *, name, handler, **kwargs):
        self.tools[name] = handler

    def register_hook(self, _name, handler):
        self.hooks.append(handler)

    def register_command(self, *args, **kwargs):
        pass

    def register_skill(self, *args, **kwargs):
        pass


def load_script(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def candidate(candidate_id: str, fingerprint: str = "p3-source-v1") -> dict:
    return {
        "id": candidate_id,
        "status": "candidate",
        "kind": "subconscious_advisory",
        "pressure": 0.9,
        "summary": f"Source-specific context for {candidate_id}",
        "fingerprint": f"fp-{candidate_id}",
        "source_candidate_fingerprint": fingerprint,
        "event_ids": [f"evt-{candidate_id}"],
        "source_candidate_ids": [f"src-{candidate_id}"],
        "correlation_keys": ["p3-composed"],
        "sensitivity": "private",
        "allowed_surfaces": ["local"],
        "created_at": "2026-09-09T10:00:00Z",
        "updated_at": "2026-09-09T10:00:00Z",
        "conscious_task": {
            "id": f"task-{candidate_id}",
            "request_type": "THINK",
            "title": "Make one bounded choice",
            "why": "The original source remains meaningfully unresolved.",
            "expected_decision": "Settle, hold, or remain silent.",
        },
        "advisory_meta": {"source_fingerprint": fingerprint},
    }


def configure_store(path: Path) -> SensoriumStore:
    store = SensoriumStore(instance="test-instance", state_dir=str(path))
    store.ensure_dirs()
    (path / "instance.config.json").write_text(json.dumps({
        "instance_name": "test-instance",
        "allowed_surfaces": ["local"],
        "conscious_doorway": {
            "enabled": True,
            "aperture_size": 1,
            "max_active_items": 1,
            "lease_minutes": 30,
            "surfaces": ["local"],
        },
    }), encoding="utf-8")
    return store


def _request_aperture_item(api_kwargs: dict, candidate_id: str) -> tuple[dict, str]:
    sent = next(
        row["content"]
        for row in api_kwargs["messages"]
        if row.get("role") == "user" and candidate_id in str(row.get("content") or "")
    )
    marker = "Leased attention items (1): "
    start = sent.index(marker) + len(marker)
    items, _ = json.JSONDecoder().raw_decode(sent[start:])
    return items[0], sent


def test_foreground_registered_doorway_actual_hermes_request_and_live_tool_settlement(tmp_path, monkeypatch):
    require_companion_hermes()
    state = tmp_path / "foreground"
    store = configure_store(state)
    store.append_jsonl("candidates", candidate("cand_foreground"))
    home = tmp_path / "foreground-home"
    hermes_home = home / ".hermes"
    hermes_home.mkdir(parents=True)
    (hermes_home / "config.yaml").write_text(
        "model:\n  default: stub-model\n  provider: custom\n"
        "display:\n  streaming: false\n  persistent_output: false\n  turn_summary: false\n"
        "agent:\n  environment_probe: false\n  tool_use_enforcement: false\n"
        "memory:\n  memory_enabled: false\n  user_profile_enabled: false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-local-only")
    monkeypatch.setattr(
        "agent_sensorium.conscious_aperture.utc_now_iso",
        lambda: "2026-09-09T11:00:00Z",
    )
    sys.path.insert(0, str(HERMES))

    import cli as cli_mod
    from hermes_cli import plugins as plugins_mod
    from hermes_cli.plugins import PluginContext as HermesPluginContext
    from hermes_cli.plugins import PluginManager, PluginManifest

    manager = PluginManager(scope_key=str(hermes_home.resolve()))
    manager._discovered = True
    monkeypatch.setattr(plugins_mod, "_plugin_manager", manager)
    host_context = HermesPluginContext(
        PluginManifest(name="p3-sensorium-bridge", source="bundled"), manager
    )
    registrations = []

    class RegistrationBridge:
        """Route real host hook/tool lookup to the real Sensorium callbacks."""

        def register_tool(self, *, handler, **kwargs):
            def routed(args, **host_kwargs):
                return handler(
                    args, **host_kwargs, state_dir=str(state), platform="local"
                )

            registrations.append(host_context.register_tool(handler=routed, **kwargs))

        def register_hook(self, name, callback):
            def routed(**host_kwargs):
                payload = dict(host_kwargs)
                payload.update(state_dir=str(state), platform="local")
                return callback(**payload)

            registrations.append(host_context.register_hook(name, routed))

        def register_command(self, *args, **kwargs):
            pass

        def register_skill(self, *args, **kwargs):
            pass

    register(RegistrationBridge())
    captured_requests = []

    def scripted_api(api_kwargs):
        captured_requests.append(api_kwargs)
        if len(captured_requests) == 1:
            item, sent = _request_aperture_item(api_kwargs, "cand_foreground")
            assert "ordinary foreground turn" in sent
            assert item["source_binding"]["source_fingerprint"] == "p3-source-v1"
            call = {
                "action": "update",
                "id": item["candidate_id"],
                "aperture_id": item["aperture_id"],
                "consumer_id": item["consumer_id"],
                "keyword": "settle",
                "text": "The captured exact source was considered.",
            }
            message = SimpleNamespace(
                content=None,
                reasoning_content=None,
                reasoning=None,
                tool_calls=[SimpleNamespace(
                    id="call-p3-foreground",
                    type="function",
                    function=SimpleNamespace(
                        name="tool_call",
                        arguments=json.dumps({
                            "name": "sensorium", "arguments": call
                        }),
                    ),
                )],
            )
            finish_reason = "tool_calls"
        else:
            tool_rows = [row for row in api_kwargs["messages"] if row.get("role") == "tool"]
            assert len(tool_rows) == 1
            assert json.loads(tool_rows[0]["content"])["success"] is True
            message = SimpleNamespace(
                content="foreground choice complete",
                reasoning_content=None,
                reasoning=None,
                tool_calls=None,
            )
            finish_reason = "stop"
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
            model="stub-model",
            usage=SimpleNamespace(
                prompt_tokens=3, completion_tokens=2, total_tokens=5
            ),
        )

    cli = cli_mod._build_cli_from_args(
        "stub-model", ["agent-sensorium-live"], "custom", None,
        "synthetic-local-only",
        "http://127.0.0.1:1/v1", 4, None, False, False, False, False,
        False, False, [], session_purpose="interactive",
    )
    try:
        assert cli._init_agent(runtime_override={
            "api_key": "synthetic-local-only",
            "base_url": "http://127.0.0.1:1/v1",
            "provider": "custom",
            "requested_provider": "custom",
            "api_mode": "chat_completions",
            "command": None,
            "args": None,
            "credential_pool": None,
        }) is True
        cli.agent._disable_streaming = True
        cli.agent._interruptible_api_call = scripted_api
        result = cli.agent.run_conversation(
            "ordinary foreground turn", conversation_history=[], task_id="p3-foreground"
        )
        assert result["final_response"] == "foreground choice complete"
        assert len(captured_requests) == 2
    finally:
        if getattr(cli, "agent", None) is not None:
            cli.agent.close()
        for registration in reversed(registrations):
            if registration is not None:
                registration.dispose()

    final = store.read_jsonl("candidates")[0]
    receipt = final["conscious_aperture"]["settlement_receipt"]
    assert final["status"] == "reviewed"
    assert final["conscious_aperture"]["generation"] == 1
    assert receipt["source_binding"]["source_fingerprint"] == "p3-source-v1"
    assert receipt["candidate_id"] == "cand_foreground"
    attempted = [
        row for row in store.read_jsonl("decisions")
        if row.get("type") == "conscious.aperture.presentation_attempted"
    ]
    assert len(attempted) == 1 and attempted[0]["host_consumption_confirmed"] is False
    assert not [
        row for row in store.read_jsonl("decisions")
        if row.get("type") == "conscious.aperture.consumed"
    ]


class Wire:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        wire = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("content-length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                wire.requests.append({"path": self.path, "body": body})
                if self.path == "/api/show":
                    payload = b'{"error":"not ollama"}'
                    self.send_response(404)
                else:
                    text = wire.responses[0]
                    if body.get("stream") is True:
                        chunks = [
                            {"id": "p3", "object": "chat.completion.chunk", "created": 1,
                             "model": "stub-model", "choices": [{"index": 0,
                             "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]},
                            {"id": "p3", "object": "chat.completion.chunk", "created": 1,
                             "model": "stub-model", "choices": [{"index": 0,
                             "delta": {"content": text}, "finish_reason": None}]},
                            {"id": "p3", "object": "chat.completion.chunk", "created": 1,
                             "model": "stub-model", "choices": [{"index": 0,
                             "delta": {}, "finish_reason": "stop"}],
                             "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}},
                        ]
                        payload = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks).encode() + b"data: [DONE]\n\n"
                        self.send_response(200)
                        self.send_header("content-type", "text/event-stream")
                        self.send_header("content-length", str(len(payload)))
                        self.end_headers()
                        self.wfile.write(payload)
                        return
                    payload = json.dumps({
                        "id": "p3", "object": "chat.completion", "created": 1,
                        "model": "stub-model", "choices": [{"index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
                    }).encode()
                    self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def actual_hermes_runner(wire, observed):
    import cli as cli_mod
    import hermes_cli.main as hermes_main

    def run(command, *, cwd, text, capture_output, timeout):
        args = hermes_main._light_chat_parser().parse_args(command[1:])
        captured = {}
        original_main = cli_mod.main
        cli_mod.main = lambda **kwargs: captured.update(kwargs)
        try:
            assert args.func(args) in (None, 0)
        finally:
            cli_mod.main = original_main
        cli = cli_mod._build_cli_from_args(
            "stub-model", captured.get("toolsets"), "custom",
            captured.get("reasoning"), captured.get("api_key"), captured.get("base_url"),
            captured.get("max_turns"), captured.get("run_budget"), captured.get("verbose"),
            captured.get("compact"), captured.get("resume"), captured.get("checkpoints", False),
            captured.get("pass_session_id", False), captured.get("ignore_rules", False),
            captured.get("skills", []), session_purpose=captured["session_purpose"],
        )
        assert cli._init_agent(runtime_override={
            "api_key": "synthetic-local-only", "base_url": wire.base_url,
            "provider": "custom", "requested_provider": "custom",
            "api_mode": "chat_completions", "command": None, "args": None,
            "credential_pool": None,
        }) is True
        result = cli.agent.run_conversation(captured["query"], conversation_history=[], task_id="p3-unattended")
        observed.append({
            "argv": command, "source": cli.agent.session_source,
            "purpose": cli.agent.session_purpose,
            "final_response": result["final_response"],
        })
        session_id = cli.agent.session_id
        cli.agent.close()
        return subprocess.CompletedProcess(command, 0, stdout=result["final_response"], stderr=f"session_id: {session_id}\n")

    return run


def model_choice(decision: str, candidate_id: str, fingerprint: str, **extra) -> str:
    return json.dumps({
        "decision": decision, "candidate_id": candidate_id,
        "source_candidate_fingerprint": fingerprint,
        "reason": f"Bounded {decision.lower()} over the exact source.", **extra,
    })


def native_args(state_dir: Path) -> argparse.Namespace:
    return argparse.Namespace(
        instance="test-instance", state_dir=str(state_dir), plugin_root=str(ROOT),
        hermes_cli="/synthetic/hermes", provider="custom", model="stub-model",
        timeout_seconds=30, total_timeout_seconds=60, cleanup_reserve_seconds=5,
        failure_cooldown_seconds=1800, stale_after_minutes=180,
        force=False, emit_reachout=False, print_json=False,
    )


def test_wrapper_bound_candidate_runner_actual_hermes_silence_hold_return_and_failure(tmp_path, monkeypatch):
    require_companion_hermes(wrapper=True)
    home = tmp_path / "home"
    hermes_home = home / ".hermes"
    hermes_home.mkdir(parents=True)
    (hermes_home / "config.yaml").write_text(
        "model:\n  default: stub-model\n  provider: custom\n"
        "display:\n  streaming: false\n  persistent_output: false\n  turn_summary: false\n"
        "agent:\n  environment_probe: false\n  tool_use_enforcement: false\n"
        "memory:\n  memory_enabled: false\n  user_profile_enabled: false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-local-only")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost,::1")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost,::1")
    sys.path.insert(0, str(HERMES))

    wrapper = load_script("p3_wrapper", WRAPPER)
    wrapper.PYTHON = Path(sys.executable)
    wrapper.RUNNER = RUNNER
    capture = {}
    monkeypatch.setattr(wrapper.os, "execv", lambda executable, argv: capture.update(executable=executable, argv=argv))
    wrapper.main()
    assert capture["argv"][1:3] == [str(RUNNER), "--instance"]
    configured_instance = capture["argv"][3]
    assert isinstance(configured_instance, str) and configured_instance
    assert capture["argv"][4:] == [
        "--provider", "openai-codex", "--model", "gpt-5.6-sol",
        "--timeout-seconds", "240", "--emit-reachout",
    ]

    runner = load_script("p3_candidate_runner", RUNNER)
    args = runner._parser().parse_args(capture["argv"][2:])
    args.state_dir = str(tmp_path / "unattended")
    args.hermes_cli = str(tmp_path / "bin" / "hermes")
    args.total_timeout_seconds = 270
    args.cleanup_reserve_seconds = 30
    store = SensoriumStore(instance=args.instance, state_dir=args.state_dir)
    store.ensure_dirs()
    store.append_jsonl("candidates", candidate("cand_silence", "silence-v1"))
    observed = []
    wire = Wire([model_choice("SILENCE", "cand_silence", "silence-v1")])
    try:
        silence = runner.run_once(args, run_command=actual_hermes_runner(wire, observed), now="2026-09-09T11:00:00Z")
    finally:
        wire.close()
    assert silence["action"] == "settled_silence", silence.get("reason")
    assert observed[0]["source"] == "sensorium-native-conscious"
    assert observed[0]["purpose"] == "autonomous"
    assert any("Source-specific context for cand_silence" in json.dumps(row["body"].get("messages")) for row in wire.requests)
    assert store.read_jsonl("outbox") == store.read_jsonl("worker_requests") == store.read_jsonl("artifacts") == []

    store.append_jsonl("candidates", candidate("cand_hold", "hold-v1"))
    wire = Wire([model_choice("HOLD", "cand_hold", "hold-v1", return_at="2026-09-09T13:00:00Z")])
    try:
        held = runner.run_once(args, run_command=actual_hermes_runner(wire, observed), now="2026-09-09T12:00:00Z")
    finally:
        wire.close()
    assert held["action"] == "settled_hold"
    before_calls = len(observed)
    early = runner.run_once(args, run_command=lambda *a, **k: (_ for _ in ()).throw(AssertionError("premature model call")), now="2026-09-09T12:30:00Z")
    assert early["action"] == "no_current_advisory" and len(observed) == before_calls
    store.append_jsonl("candidates", candidate("cand_fresh", "fresh-v1"))
    wire = Wire([model_choice("SILENCE", "cand_hold", "hold-v1")])
    try:
        returned = runner.run_once(args, run_command=actual_hermes_runner(wire, observed), now="2026-09-09T13:01:00Z")
    finally:
        wire.close()
    assert returned["candidate_id"] == "cand_hold" and returned["action"] == "settled_silence", returned.get("reason")
    hold_row = next(row for row in store.read_jsonl("candidates") if row["id"] == "cand_hold")
    assert hold_row["summary"] == "Source-specific context for cand_hold"
    assert len([row for row in store.read_jsonl("decisions") if row.get("type") == "conscious.aperture.returned" and row.get("candidate_id") == "cand_hold"]) == 1

    store.append_jsonl("candidates", candidate("cand_failure", "failure-v1"))
    failed = runner.run_once(
        args,
        run_command=lambda command, **kwargs: subprocess.CompletedProcess(command, 7, stdout="", stderr="provider unavailable"),
        now="2026-09-09T14:00:00Z",
    )
    assert failed["action"] == "conscious_session_failed"
    failure_row = next(row for row in store.read_jsonl("candidates") if row["id"] == "cand_failure")
    assert failure_row["status"] == "in_conscious_aperture" and "held_return" not in failure_row
    assert not [row for row in failure_row.get("conscious_settlements", []) if row.get("decision") == "HELD"]


def test_foreground_unattended_race_has_one_canonical_winner_and_loser_cannot_mutate(tmp_path, monkeypatch):
    clock = {"now": "2026-09-09T12:00:00Z"}
    monkeypatch.setattr(
        "agent_sensorium.conscious_aperture.utc_now_iso", lambda: clock["now"]
    )
    runner = load_script("p3_race_runner", RUNNER)

    state = tmp_path / "race"
    store = configure_store(state)
    store.append_jsonl("candidates", candidate("cand_race", "race-v1"))
    ctx = PluginContext()
    register(ctx)
    foreground = ctx.hooks[0](
        platform="local", session_id="race-session", turn_id="race-turn",
        state_dir=str(state),
    )
    assert foreground
    before = store.read_jsonl("candidates")
    clock["now"] = "2026-09-09T12:01:00Z"
    loser = runner.run_once(
        native_args(state),
        run_command=lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("foreground-owned item reached unattended model")
        ),
        now=clock["now"],
    )
    assert loser["action"] == "no_current_advisory"
    assert store.read_jsonl("candidates") == before
    owned = before[0]["conscious_aperture"]
    settled = json.loads(ctx.tools["sensorium"]({
        "action": "update", "instance": "test-instance", "id": "cand_race",
        "aperture_id": owned["id"], "consumer_id": owned["consumer_id"],
        "keyword": "settle", "text": "Foreground won the exact transaction.",
    }, state_dir=str(state), platform="local"))
    assert settled["success"] is True
    final = store.read_jsonl("candidates")[0]
    receipt = final["conscious_aperture"]["settlement_receipt"]
    stale_loser = json.loads(ctx.tools["sensorium"]({
        "action": "update", "instance": "test-instance", "id": "cand_race",
        "aperture_id": "cap-stale", "consumer_id": "conscious-session",
        "keyword": "hold", "return_at": "2026-09-10T12:00:00Z",
        "text": "Loser must not overwrite.",
    }, state_dir=str(state), platform="local"))
    assert stale_loser["success"] is False
    assert store.read_jsonl("candidates")[0]["conscious_aperture"]["settlement_receipt"] == receipt
    assert len([
        row for row in store.read_jsonl("decisions")
        if row.get("type") == "conscious.aperture.settled"
    ]) == 1
    assert store.read_jsonl("outbox") == store.read_jsonl("worker_requests") == []

    reverse_state = tmp_path / "reverse-race"
    reverse = configure_store(reverse_state)
    reverse.append_jsonl("candidates", candidate("cand_reverse", "reverse-v1"))
    clock["now"] = "2026-09-09T13:00:00Z"
    foreground_attempts = []

    def unattended_model(command, **kwargs):
        before_foreground = reverse.read_jsonl("candidates")
        clock["now"] = "2026-09-09T13:01:00Z"
        foreground_loser = ctx.hooks[0](
            platform="local", session_id="reverse-session", turn_id="reverse-turn",
            state_dir=str(reverse_state),
        )
        foreground_attempts.append(foreground_loser)
        assert foreground_loser is None
        assert reverse.read_jsonl("candidates") == before_foreground
        return subprocess.CompletedProcess(
            command, 0,
            stdout=model_choice("SILENCE", "cand_reverse", "reverse-v1"),
            stderr="session_id: reverse-native-session\n",
        )

    unattended = runner.run_once(
        native_args(reverse_state), run_command=unattended_model,
        now="2026-09-09T13:00:00Z",
    )
    assert foreground_attempts == [None]
    assert unattended["candidate_id"] == "cand_reverse"
    assert unattended["action"] == "settled_silence"
    reverse_final = reverse.read_jsonl("candidates")[0]
    reverse_receipt = reverse_final["conscious_aperture"]["settlement_receipt"]
    late_foreground = json.loads(ctx.tools["sensorium"]({
        "action": "update", "instance": "test-instance", "id": "cand_reverse",
        "aperture_id": "cap-stale", "consumer_id": "foreground:reverse-session",
        "keyword": "hold", "return_at": "2026-09-10T13:00:00Z",
        "text": "Late foreground must not overwrite native choice.",
    }, state_dir=str(reverse_state), platform="local"))
    assert late_foreground["success"] is False
    assert reverse.read_jsonl("candidates")[0]["conscious_aperture"]["settlement_receipt"] == reverse_receipt
    assert len([
        row for row in reverse.read_jsonl("decisions")
        if row.get("type") == "conscious.aperture.settled"
    ]) == 1
    assert reverse.read_jsonl("outbox") == reverse.read_jsonl("worker_requests") == []


@pytest.mark.parametrize(
    "case",
    [
        "wrong_consumer",
        "stale_generation",
        "missing_aperture",
        "missing_consumer",
        "expired_lease",
        "changed_source",
    ],
)
def test_foreground_tool_rejects_each_invalid_authority_without_mutation(
    tmp_path, monkeypatch, case
):
    clock = {"now": "2026-09-09T10:00:00Z"}
    monkeypatch.setattr(
        "agent_sensorium.conscious_aperture.utc_now_iso", lambda: clock["now"]
    )
    ctx = PluginContext()
    register(ctx)
    state = tmp_path / f"foreground-{case}"
    store = configure_store(state)
    store.append_jsonl("candidates", candidate("cand_invalid", "foreground-v1"))
    opened = open_conscious_aperture(
        store, aperture_size=1, max_active_items=1,
        consumer_id="foreground-owner", lease_minutes=1, dry_run=False,
        now=clock["now"],
    )
    call = {
        "action": "update", "instance": "test-instance", "id": "cand_invalid",
        "aperture_id": opened["aperture_id"],
        "consumer_id": "foreground-owner",
        "keyword": "settle", "text": "Invalid authority must not settle.",
    }
    expected = {
        "wrong_consumer": "consumer_id_mismatch",
        "stale_generation": "aperture_id_mismatch",
        "missing_aperture": "aperture_id_required",
        "missing_consumer": "consumer_id_required",
        "expired_lease": "aperture_lease_expired",
        "changed_source": "source_binding_mismatch",
    }[case]
    if case == "wrong_consumer":
        call["consumer_id"] = "wrong-owner"
    elif case == "stale_generation":
        rows = store.read_jsonl("candidates")
        rows[0]["conscious_aperture"]["id"] = "cap-new-generation"
        rows[0]["conscious_aperture"]["generation"] += 1
        store.rewrite_jsonl("candidates", rows)
    elif case == "missing_aperture":
        call.pop("aperture_id")
    elif case == "missing_consumer":
        call.pop("consumer_id")
    elif case == "expired_lease":
        clock["now"] = "2026-09-09T10:02:00Z"
    else:
        rows = store.read_jsonl("candidates")
        rows[0]["advisory_meta"]["source_fingerprint"] = "changed-v2"
        store.rewrite_jsonl("candidates", rows)
    before = store.read_jsonl("candidates")
    result = json.loads(
        ctx.tools["sensorium"](call, state_dir=str(state), platform="local")
    )
    assert result["error"] == expected
    assert store.read_jsonl("candidates") == before
    assert not [
        row for row in store.read_jsonl("decisions")
        if row.get("type") == "conscious.aperture.settled"
    ]
    assert store.read_jsonl("outbox") == store.read_jsonl("worker_requests") == []


@pytest.mark.parametrize(
    "case", ["wrong_consumer", "stale_generation", "expired_lease", "changed_source"]
)
def test_unattended_runner_rejects_each_post_open_authority_race(
    tmp_path, case
):
    runner = load_script(f"p3_parity_runner_{case}", RUNNER)
    state = tmp_path / f"unattended-{case}"
    store = configure_store(state)
    store.append_jsonl("candidates", candidate("cand_unattended", "unattended-v1"))
    model_calls = []
    post_tamper = []

    def tamper(command, **kwargs):
        model_calls.append(command)
        rows = store.read_jsonl("candidates")
        aperture = rows[0]["conscious_aperture"]
        if case == "wrong_consumer":
            aperture["consumer_id"] = "other-owner"
        elif case == "stale_generation":
            aperture["id"] = "cap-new-generation"
            aperture["generation"] += 1
        elif case == "expired_lease":
            aperture["lease_expires_at"] = "2026-09-09T11:00:00Z"
        else:
            rows[0]["source_candidate_fingerprint"] = "changed-v2"
        store.rewrite_jsonl("candidates", rows)
        post_tamper[:] = store.read_jsonl("candidates")
        return subprocess.CompletedProcess(
            command, 0,
            stdout=model_choice("SILENCE", "cand_unattended", "unattended-v1"),
            stderr="session_id: parity-session\n",
        )

    result = runner.run_once(
        native_args(state), run_command=tamper, now="2026-09-09T11:00:00Z"
    )
    assert len(model_calls) == 1
    final = store.read_jsonl("candidates")[0]
    if case == "wrong_consumer":
        assert result["action"] == "conscious_session_failed"
        assert result["failure_reason"] == result["reason"] == "source_revision_mismatch"
        assert store.read_jsonl("candidates") == post_tamper
        assert final["conscious_aperture"]["consumer_id"] == "other-owner"
    elif case == "expired_lease":
        assert result["action"] == "conscious_session_failed"
        assert result["failure_reason"] == result["reason"] == "source_revision_mismatch"
        assert result["aperture_id"] == post_tamper[0]["conscious_aperture"]["id"]
        assert store.read_jsonl("candidates") == post_tamper
        assert final["conscious_aperture"]["id"] == post_tamper[0]["conscious_aperture"]["id"]
        assert final["conscious_aperture"]["generation"] == post_tamper[0]["conscious_aperture"]["generation"]
        assert final["source_candidate_fingerprint"] == post_tamper[0]["source_candidate_fingerprint"]
        assert final["advisory_meta"] == post_tamper[0]["advisory_meta"]
        assert final["summary"] == post_tamper[0]["summary"]
        assert final["conscious_task"] == post_tamper[0]["conscious_task"]
    else:
        assert result["action"] == "conscious_session_failed"
        assert store.read_jsonl("candidates") == post_tamper
    assert final["status"] == "in_conscious_aperture"
    assert not final.get("conscious_settlements")
    assert not [
        row for row in store.read_jsonl("decisions")
        if row.get("type") == "conscious.aperture.settled"
    ]
    assert store.read_jsonl("outbox") == store.read_jsonl("worker_requests") == []


@pytest.mark.parametrize("missing", ["aperture_id", "consumer_id"])
def test_unattended_runner_rejects_malformed_resumable_before_model(
    tmp_path, missing
):
    runner = load_script(f"p3_missing_{missing}_runner", RUNNER)
    state = tmp_path / f"unattended-missing-{missing}"
    store = configure_store(state)
    if missing == "consumer_id":
        store.write_conscious_aperture_state({
            "version": 1,
            "fairness_last_served_lane": None,
            "presentation_attempts": [],
        })
    row = candidate("cand_ownerless")
    row["status"] = "in_conscious_aperture"
    row["conscious_aperture"] = {
        "id": "cap-current",
        "state": "open",
        "consumer_id": "conscious-session",
        "lease_expires_at": "2026-09-09T11:30:00Z",
        "generation": 1,
    }
    row["conscious_aperture"].pop("id" if missing == "aperture_id" else "consumer_id")
    store.append_jsonl("candidates", row)
    before = store.read_jsonl("candidates")
    model_calls = []
    result = runner.run_once(
        native_args(state),
        run_command=lambda *a, **k: model_calls.append(a) or (_ for _ in ()).throw(
            AssertionError("malformed authority reached model")
        ),
        now="2026-09-09T11:00:00Z",
    )
    assert result["action"] == "no_current_advisory"
    assert model_calls == []
    assert store.read_jsonl("candidates") == before
    assert not store.read_jsonl("decisions")
    assert store.read_jsonl("outbox") == store.read_jsonl("worker_requests") == []


def test_unattended_runner_valid_same_owner_resume_reaches_one_settlement(tmp_path):
    runner = load_script("p3_valid_resume_runner", RUNNER)
    state = tmp_path / "unattended-valid-resume"
    store = configure_store(state)
    store.append_jsonl("candidates", candidate("cand_resume", "resume-v1"))
    opened = open_conscious_aperture(
        store, aperture_size=1, max_active_items=1,
        consumer_id="conscious-session", dry_run=False,
        now="2026-09-09T11:00:00Z",
    )
    model_calls = []

    def choose(command, **kwargs):
        model_calls.append(command)
        return subprocess.CompletedProcess(
            command, 0,
            stdout=model_choice("SILENCE", "cand_resume", "resume-v1"),
            stderr="session_id: valid-resume-session\n",
        )

    result = runner.run_once(
        native_args(state), run_command=choose, now="2026-09-09T11:01:00Z"
    )
    assert len(model_calls) == 1
    assert result["action"] == "settled_silence"
    final = store.read_jsonl("candidates")[0]
    assert final["conscious_aperture"]["id"] == opened["aperture_id"]
    assert final["conscious_aperture"]["generation"] == 1
    assert len(final["conscious_settlements"]) == 1


def test_runner_settles_selected_item_when_same_clock_callback_adds_due_competitor(
    tmp_path, monkeypatch
):
    runner = load_script("p3_same_clock_selection_settlement", RUNNER)
    state = tmp_path / "same-clock-selection-settlement"
    store = configure_store(state)
    now = "2026-09-09T11:00:00Z"
    store.append_jsonl("candidates", candidate("cand_a", "source-a"))
    selected = {}

    def introduce_due_competitor(command, **kwargs):
        owned = store.read_jsonl("candidates")[0]
        selected.update(owned["conscious_aperture"])
        due = candidate("cand_b", "source-b")
        due["status"] = "held"
        due["held_return"] = {
            "not_before": "2026-09-09T10:00:00Z",
            "reason_code": "time_checkpoint",
        }
        rows = store.read_jsonl("candidates")
        rows.append(due)
        store.rewrite_jsonl("candidates", rows)
        monkeypatch.setattr(
            "agent_sensorium.conscious_consumer.open_conscious_aperture",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("model-result application reopened selection")
            ),
        )
        return subprocess.CompletedProcess(
            command, 0,
            stdout=model_choice("SILENCE", "cand_a", "source-a"),
            stderr="session_id: same-clock-selection-settlement\n",
        )

    result = runner.run_once(
        native_args(state), run_command=introduce_due_competitor, now=now
    )
    assert result["action"] == "settled_silence"
    assert result["candidate_id"] == "cand_a"
    final_a = next(row for row in store.read_jsonl("candidates") if row["id"] == "cand_a")
    assert final_a["status"] == "reviewed"
    assert final_a["conscious_aperture"]["id"] == selected["id"]
    assert final_a["conscious_aperture"]["generation"] == selected["generation"] == 1
    decisions = store.read_jsonl("decisions")
    assert len([row for row in decisions if row.get("type") == "conscious.aperture.settled"]) == 1
    assert not [
        row for row in decisions
        if row.get("type") in {"conscious.aperture.yielded", "conscious.aperture.renewed"}
    ]

    next_activation = open_conscious_aperture(
        store, aperture_size=1, max_active_items=1, consumer_id="conscious-session",
        dry_run=False, now=now,
    )
    assert next_activation["candidate_ids"] == ["cand_b"]
    assert next_activation["returned_candidate_ids"] == ["cand_b"]


@pytest.mark.parametrize(
    "missing",
    [
        "expected_candidate_id",
        "expected_aperture_id",
        "expected_source_candidate_ids",
        "expected_source_candidate_fingerprint",
    ],
)
def test_consumer_incomplete_expected_authority_fails_closed(tmp_path, missing):
    state = tmp_path / f"incomplete-{missing}"
    store = configure_store(state)
    store.append_jsonl("candidates", candidate("cand_exact", "source-exact"))
    opened = open_conscious_aperture(
        store, aperture_size=1, max_active_items=1,
        consumer_id="conscious-session", dry_run=False,
        now="2026-09-09T11:00:00Z",
    )
    expected = {
        "expected_candidate_id": "cand_exact",
        "expected_aperture_id": opened["aperture_id"],
        "expected_source_candidate_ids": ["src-cand_exact"],
        "expected_source_candidate_fingerprint": "source-exact",
    }
    expected.pop(missing)
    tracked = {
        name: store.read_jsonl(name)
        for name in ("candidates", "decisions", "outbox", "worker_requests")
    }
    result = consume_conscious_advisory(
        store,
        decision={"decision": "SILENCE", "reason": "Exact item only."},
        dry_run=False,
        now="2026-09-09T11:00:00Z",
        **expected,
    )
    assert result["success"] is False
    assert result["error"] == "source_revision_mismatch"
    assert {
        name: store.read_jsonl(name)
        for name in ("candidates", "decisions", "outbox", "worker_requests")
    } == tracked
