import importlib.util
import json
from pathlib import Path


class FakePluginContext:
    def __init__(self):
        self.tools = {}
        self.commands = {}
        self.skills = {}
        self.hooks = {}

    def register_tool(self, name, toolset, schema, handler, **kwargs):
        self.tools[name] = {
            "toolset": toolset,
            "schema": schema,
            "handler": handler,
            **kwargs,
        }

    def register_command(self, name, handler, **kwargs):
        self.commands[name] = {"handler": handler, **kwargs}

    def register_skill(self, name, path, **kwargs):
        self.skills[name] = {"path": path, **kwargs}

    def register_hook(self, name, handler, **kwargs):
        key = name
        if key in self.hooks:
            idx = 2
            while f"{name}#{idx}" in self.hooks:
                idx += 1
            key = f"{name}#{idx}"
        self.hooks[key] = {"handler": handler, **kwargs}


def test_plugin_registers_with_real_plugin_context_shape(tmp_path):
    from agent_sensorium.plugin import register

    ctx = FakePluginContext()
    register(ctx)

    expected_admin_tools = {
        "sensorium_status",
        "sensorium_ingest_signal",
        "sensorium_sensor_config",
        "sensorium_profile",
        "sensorium_ingest_event",
        "sensorium_candidate_update",
        "sensorium_attention_pointer",
        "sensorium_attention_inbox",
        "sensorium_thread_open",
        "sensorium_thread_update",
        "sensorium_compact",
        "sensorium_service_threads",
        "sensorium_subconscious_advisory",
        "sensorium_improvement_collect",
        "sensorium_attention_policy_decide",
        "sensorium_attention_policy_manage",
        "sensorium_improvement_status",
        "sensorium_artifact_store",
        "sensorium_artifact_status",
        "sensorium_media_gift_decide",
    }
    assert set(ctx.tools) == {"sensorium", *expected_admin_tools}
    assert ctx.tools["sensorium"]["toolset"] == "agent-sensorium-live"
    assert {
        entry["toolset"] for name, entry in ctx.tools.items() if name != "sensorium"
    } == {"agent-sensorium-admin"}
    help_output = ctx.commands["sensorium"]["handler"]("help")
    assert help_output.startswith("Usage: /sensorium")
    assert "outbox" not in help_output
    assert "dispatch" not in help_output
    assert "workers" not in help_output
    assert "pre_llm_call" in ctx.hooks
    assert "agent-sensorium" in ctx.skills

    status = json.loads(
        ctx.tools["sensorium"]["handler"](
            {"action": "status", "instance": "plugin-test", "state_dir": str(tmp_path)}
        )
    )
    assert status["success"] is True
    assert status["data"]["counts"]["signals"] == 0
    assert "pointer" in status["data"]

    live_schema = ctx.tools["sensorium"]["schema"]["parameters"]["properties"]
    assert "foreground_action_taken" in live_schema
    assert "foreground_resolution" in live_schema
    assert "residue" in live_schema
    assert "durable_capture" in live_schema
    assert "background_action_allowed" in live_schema


def test_live_ingest_receipt_suppresses_foreground_owned_no_residue(tmp_path):
    from agent_sensorium.plugin import register
    from agent_sensorium.store import SensoriumStore

    ctx = FakePluginContext()
    register(ctx)

    result = json.loads(ctx.tools["sensorium"]["handler"]({
        "action": "ingest",
        "instance": "plugin-test",
        "state_dir": str(tmp_path),
        "text": "Documented foreground issue",
        "kind": "design_insight",
        "foreground_action_taken": True,
        "foreground_resolution": "full",
        "residue": "none",
        "durable_capture": "docs",
    }))

    store = SensoriumStore(instance="plugin-test", state_dir=str(tmp_path))
    assert result["success"] is True
    assert result["data"]["ingested"] is False
    assert result["data"]["reason"] == "foreground_owned_no_residue"
    assert store.read_jsonl("signals") == []
    receipts = store.read_jsonl("decisions")
    assert receipts[-1]["type"] == "live_turn.ingest_decision"
    assert receipts[-1]["ingested"] is False
    assert receipts[-1]["residue"] == "none"


def test_live_ingest_records_residue_intent_on_signal_and_receipt(tmp_path):
    from agent_sensorium.plugin import register
    from agent_sensorium.store import SensoriumStore

    ctx = FakePluginContext()
    register(ctx)

    result = json.loads(ctx.tools["sensorium"]["handler"]({
        "action": "ingest",
        "instance": "plugin-test",
        "state_dir": str(tmp_path),
        "text": "Watch this repeated pattern",
        "kind": "design_insight",
        "foreground_action_taken": True,
        "foreground_resolution": "partial",
        "residue": "watch",
        "durable_capture": "docs",
    }))

    store = SensoriumStore(instance="plugin-test", state_dir=str(tmp_path))
    signals = store.read_jsonl("signals")
    receipts = store.read_jsonl("decisions")
    assert result["success"] is True
    assert signals[-1]["live_turn_intent"]["residue"] == "watch"
    assert "live-residue:watch" in signals[-1]["correlation_keys"]
    assert result["data"]["live_turn_receipt"]["ingested"] is True
    assert receipts[-1]["signal_id"] == result["data"]["signal_id"]


def test_thread_update_plugin_schema_and_handler_forward_resume_trigger(tmp_path):
    from agent_sensorium.plugin import register
    from agent_sensorium.store import SensoriumStore

    store = SensoriumStore(instance="plugin-test", state_dir=str(tmp_path))
    store.ensure_dirs()
    store.append_jsonl("threads", {
        "id": "sth_holdtest",
        "status": "dormant",
        "origin": "candidate",
        "conscious_task": {"id": "ct_1", "request_type": "THINK", "title": "Hold test"},
        "origin_candidate_id": "cand_1",
        "continuity_summary": [],
        "decision_log": [],
        "interaction_refs": [],
        "summary_dirty": False,
        "open_questions": [],
        "next_prompt_to_operator": "test",
        "sensitivity": "private",
        "allowed_surfaces": ["local"],
        "created_at": "2026-05-24T10:00:00Z",
        "updated_at": "2026-05-24T10:00:00Z",
        "expires_at": "2099-05-31T10:00:00Z",
    })

    ctx = FakePluginContext()
    register(ctx)

    thread_update_schema = ctx.tools["sensorium_thread_update"]["schema"]
    assert "resume_trigger" in thread_update_schema["parameters"]["properties"]

    result = json.loads(ctx.tools["sensorium_thread_update"]["handler"]({
        "instance": "plugin-test",
        "state_dir": str(tmp_path),
        "thread_id": "sth_holdtest",
        "action": "hold",
        "reason": "waiting",
        "resume_trigger": "new_evidence",
    }))
    assert result["success"] is True

    thread = store.read_jsonl("threads")[0]
    assert thread["hold_reason"] == "waiting"
    assert thread["resume_trigger"] == "new_evidence"


def test_pre_llm_hook_forwards_state_dir(tmp_path):
    from agent_sensorium.plugin import register
    from agent_sensorium.store import SensoriumStore

    store = SensoriumStore(instance="default", state_dir=str(tmp_path))
    store.ensure_dirs()
    store.append_jsonl("threads", {
        "id": "sth_hooktest",
        "status": "dormant",
        "origin": "candidate",
        "conscious_task": {"id": "ct_1", "request_type": "THINK", "title": "Hook test"},
        "origin_candidate_id": "cand_1",
        "continuity_summary": [],
        "decision_log": [],
        "interaction_refs": [],
        "summary_dirty": False,
        "open_questions": [],
        "next_prompt_to_operator": "test",
        "sensitivity": "private",
        "allowed_surfaces": ["local"],
        "created_at": "2026-05-24T10:00:00Z",
        "updated_at": "2026-05-24T10:00:00Z",
        "expires_at": "2099-05-31T10:00:00Z",
    })

    ctx = FakePluginContext()
    register(ctx)
    hook = ctx.hooks["pre_llm_call"]["handler"]

    result = hook(platform="local", session_id="s1", state_dir=str(tmp_path))
    assert result is not None
    assert "[Sensorium Pointer 🧵]" in result["context"]
    assert "sensorium(action=\"open\"" in result["context"]
    assert "id=\"sth_hooktest\"" in result["context"]

    result2 = hook(platform="local", session_id="s1", state_dir=str(tmp_path))
    assert result2 is not None
    assert "id=\"sth_hooktest\"" in result2["context"]


def test_root_plugin_entrypoint_reexports_register():
    root_init = Path(__file__).resolve().parents[1] / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        "hermes_plugins.agent_sensorium_test",
        root_init,
        submodule_search_locations=[str(root_init.parent)],
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert callable(module.register)


def test_memory_reflection_not_in_live_tool_schema(tmp_path):
    """Memory reflection must never appear as a model-visible tool."""
    from agent_sensorium.plugin import register

    ctx = FakePluginContext()
    register(ctx)

    # The registered surface is one live Sensorium tool plus admin-only granular tools.
    # This is a snapshot guard: adding a memory-probe tool will cause this to fail.
    _EXPECTED_TOOL_COUNT = 21

    # No tool name may reference memory reflection concepts.
    _FORBIDDEN_NAME_FRAGMENTS = {"memory_reflection", "reflect", "recall", "probe"}
    for name in ctx.tools:
        for fragment in _FORBIDDEN_NAME_FRAGMENTS:
            assert fragment not in name, (
                f"Tool name '{name}' contains forbidden fragment '{fragment}'"
            )

    # No tool schema JSON may mention memory_reflection concepts.
    _FORBIDDEN_SCHEMA_SUBSTRINGS = {"memory_reflection", "memory reflection"}
    for name, entry in ctx.tools.items():
        schema_json = json.dumps(entry["schema"])
        for substr in _FORBIDDEN_SCHEMA_SUBSTRINGS:
            assert substr not in schema_json, (
                f"Tool '{name}' schema contains forbidden substring '{substr}'"
            )

    # Snapshot guard: tool count must not grow due to memory reflection additions.
    assert len(ctx.tools) == _EXPECTED_TOOL_COUNT, (
        f"Tool count changed: expected {_EXPECTED_TOOL_COUNT}, got {len(ctx.tools)}. "
        "If a memory reflection tool was added, remove it — reflection is out-of-band only."
    )
