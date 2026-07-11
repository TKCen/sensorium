"""Static contract smoke checks for the World Model dashboard view."""

import json
import shutil
import subprocess
from pathlib import Path


DASHBOARD = Path(__file__).resolve().parents[1] / "dashboard"


def test_world_model_view_is_lazy_keyboard_accessible_and_get_only():
    manifest = json.loads((DASHBOARD / "manifest.json").read_text())
    source = (DASHBOARD / manifest["entry"]).read_text()

    assert "function WorldModelView" in source
    assert 'id: "world_model"' in source
    assert "/api/plugins/agent-sensorium/world-model/search?query=" in source
    assert "/api/plugins/agent-sensorium/world-model/pages/" in source
    assert "/relations?hops=1" in source
    assert "/trace" in source
    assert "role: \"listbox\"" in source
    assert "role: \"option\"" in source
    assert 'type: "button"' in source
    assert "Canonical Markdown is lazy-loaded only after selection" in source
    assert "source_current_digest" in source
    assert "function renderCanonicalMarkdown" in source
    assert "Raw canonical Markdown" in source
    assert "Verification details" in source
    assert "sx-world-model-layout" in source
    assert 'h(Pill, { band: "green" }, "read-only")' in source
    view_source = source.split("function WorldModelView", 1)[1].split("function SensoriumPage", 1)[0]
    for forbidden in ("postJSON", "putJSON", "deleteJSON", "fetch(", "/triage", "approve_delivery", "promote"):
        assert forbidden not in view_source


def test_world_model_bundle_remains_valid_javascript():
    manifest = json.loads((DASHBOARD / "manifest.json").read_text())
    node = shutil.which("node")
    assert node is not None
    result = subprocess.run([node, "--check", str(DASHBOARD / manifest["entry"])], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
