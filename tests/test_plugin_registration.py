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
        self.hooks[name] = {"handler": handler, **kwargs}


def test_plugin_registers_with_real_plugin_context_shape(tmp_path):
    from agent_sensorium.plugin import register

    ctx = FakePluginContext()
    register(ctx)

    assert set(ctx.tools) == {
        "sensorium_status",
        "sensorium_ingest_signal",
        "sensorium_ingest_event",
        "sensorium_dispatch_once",
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
        "sensorium_outbox_prepare",
        "sensorium_outbox_dispatch",
        "sensorium_worker_prepare",
        "sensorium_worker_dispatch",
        "sensorium_worker_result",
        "sensorium_worker_status",
        "sensorium_artifact_store",
        "sensorium_artifact_status",
        "sensorium_action_prepare",
        "sensorium_action_attach",
        "sensorium_action_result",
        "sensorium_action_status",
        "sensorium_conscious_claim",
        "sensorium_conscious_complete",
    }
    assert {entry["toolset"] for entry in ctx.tools.values()} == {"agent-sensorium"}
    help_output = ctx.commands["sensorium"]["handler"]("help")
    assert help_output.startswith("Usage: /sensorium")
    assert "outbox" in help_output
    assert "pre_llm_call" in ctx.hooks
    assert "agent-sensorium" in ctx.skills

    status = json.loads(
        ctx.tools["sensorium_status"]["handler"](
            {"instance": "plugin-test", "state_dir": str(tmp_path)}
        )
    )
    assert status["success"] is True
    assert status["data"]["counts"]["signals"] == 0


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
        "expires_at": "2026-05-31T10:00:00Z",
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
        "expires_at": "2026-05-31T10:00:00Z",
    })

    ctx = FakePluginContext()
    register(ctx)
    hook = ctx.hooks["pre_llm_call"]["handler"]

    result = hook(platform="local", session_id="s1", state_dir=str(tmp_path))
    assert result is not None
    assert "[Sensorium Pointer]" in result["context"]
    assert "sensorium_thread_open" in result["context"]
    assert "thread_id=\"sth_hooktest\"" in result["context"]

    result2 = hook(platform="local", session_id="s1", state_dir=str(tmp_path))
    assert result2 is not None
    assert "thread_id=\"sth_hooktest\"" in result2["context"]


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
