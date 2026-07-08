"""Deterministic, DOM-free smoke checks for the flow-DAG frontend rewire (sera-ck9.3).

A real browser smoke (load the bundle in Chrome, click a node, watch /trace
fire) is not available from this worker environment -- there is no display
and no Chrome/Playwright runtime wired into the sandbox. These checks
substitute the next-best executable evidence: the bundle is syntactically
valid JS, it actually references the corrected `/topology` + `/runtime-status`
+ `/trace` endpoints (not just the legacy `/snapshot`), and the manifest
points at files that exist on disk so there is no stale-asset-cache
ambiguity.
"""

import json
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_DIR = REPO_ROOT / "dashboard"


def _manifest() -> dict:
    return json.loads((DASHBOARD_DIR / "manifest.json").read_text())


def test_manifest_points_at_existing_bundle_files():
    manifest = _manifest()
    entry_path = DASHBOARD_DIR / manifest["entry"]
    css_path = DASHBOARD_DIR / manifest["css"]
    assert entry_path.exists(), f"manifest entry {manifest['entry']} is missing"
    assert css_path.exists(), f"manifest css {manifest['css']} is missing"


def test_bundle_is_syntactically_valid_js():
    manifest = _manifest()
    entry_path = DASHBOARD_DIR / manifest["entry"]
    node = shutil.which("node")
    assert node is not None, "node is required for this static smoke check"
    result = subprocess.run([node, "--check", str(entry_path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_bundle_references_corrected_flow_dag_endpoints_not_just_snapshot():
    manifest = _manifest()
    entry_path = DASHBOARD_DIR / manifest["entry"]
    source = entry_path.read_text()

    assert "/api/plugins/agent-sensorium/topology" in source
    assert "/api/plugins/agent-sensorium/runtime-status" in source
    assert "/api/plugins/agent-sensorium/trace" in source
    # Snapshot must remain available as fallback/debug, not be removed outright.
    assert "/api/plugins/agent-sensorium/snapshot" in source


def test_bundle_merges_topology_and_runtime_status_client_side():
    manifest = _manifest()
    entry_path = DASHBOARD_DIR / manifest["entry"]
    source = entry_path.read_text()

    assert "function buildFlowDagGraph" in source
    assert "function FlowDagView" in source
    # Selection must call /trace, not re-derive detail from /snapshot.
    assert "function useTrace" in source
    assert "selected.selType" in source


def test_bundle_labels_runtime_edges_as_bounded_not_complete_traversal():
    manifest = _manifest()
    entry_path = DASHBOARD_DIR / manifest["entry"]
    source = entry_path.read_text()

    assert "observed: false" in source
    assert "observed: true" in source
    assert "not a complete traversal log" in source.lower()


def test_flow_dag_is_the_default_primary_view():
    manifest = _manifest()
    entry_path = DASHBOARD_DIR / manifest["entry"]
    source = entry_path.read_text()

    assert 'useState("flow_dag")' in source


def test_flow_dag_distinguishes_structure_forms_from_live_items():
    manifest = _manifest()
    entry_path = DASHBOARD_DIR / manifest["entry"]
    css_path = DASHBOARD_DIR / manifest["css"]
    source = entry_path.read_text()
    css = css_path.read_text()

    assert "function flowNodeForm" in source
    assert "sx-flow-node-form-" in source
    assert "persistent structure" in source
    assert "visual form" in source
    assert ".sx-flow-node-form-sensor" in css
    assert ".sx-flow-node-form-persistent" in css
    assert ".sx-flow-node-form-live" in css
