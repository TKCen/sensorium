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
        "sensorium_dispatch_once",
        "sensorium_candidate_update",
        "sensorium_attention_pointer",
        "sensorium_thread_open",
        "sensorium_thread_update",
        "sensorium_compact",
    }
    assert {entry["toolset"] for entry in ctx.tools.values()} == {"agent-sensorium"}
    assert ctx.commands["sensorium"]["handler"]("help").startswith("Usage: /sensorium")
    assert "pre_llm_call" in ctx.hooks
    assert "agent-sensorium" in ctx.skills

    status = json.loads(
        ctx.tools["sensorium_status"]["handler"](
            {"instance": "plugin-test", "state_dir": str(tmp_path)}
        )
    )
    assert status["success"] is True
    assert status["data"]["counts"]["signals"] == 0


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
