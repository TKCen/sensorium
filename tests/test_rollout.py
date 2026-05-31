"""Tests for scripts/sensorium_plugin_rollout.py.

The rollout script syncs the agent-sensorium plugin tree to a target Hermes
plugin installation directory. These tests use tmp_path exclusively and never
touch ~/.hermes or any real git repo.
"""

import subprocess
import sys
from pathlib import Path

import pytest

# Add scripts/ dir to path so we can import the rollout script
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import sensorium_plugin_rollout as rollout


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_source(source_root: Path) -> None:
    """Populate a minimal fake plugin source tree."""
    (source_root / "agent_sensorium").mkdir(parents=True)
    (source_root / "agent_sensorium" / "__init__.py").write_text("# agent_sensorium\n")
    (source_root / "agent_sensorium" / "gate.py").write_text("# gate\n")
    (source_root / "dashboard").mkdir()
    (source_root / "dashboard" / "plugin_api.py").write_text("# dashboard\n")
    (source_root / "scripts").mkdir()
    (source_root / "scripts" / "sensorium_tick.py").write_text("# tick\n")
    (source_root / "skills").mkdir()
    (source_root / "skills" / "example.yaml").write_text("name: example\n")
    (source_root / "plugin.yaml").write_text("name: agent-sensorium\n")
    (source_root / "__init__.py").write_text("# root init\n")
    (source_root / "README.md").write_text("# README\n")
    (source_root / "pyproject.toml").write_text("[project]\nname = 'agent-sensorium'\n")
    # live-script that gets mapped separately
    (source_root / "live-scripts").mkdir()
    (source_root / "live-scripts" / "sensorium_kanban_sensor_tick.py").write_text(
        "# kanban sensor tick\n"
    )


# ---------------------------------------------------------------------------
# Test 1: dry_run=True does not create any files on disk
# ---------------------------------------------------------------------------

def test_dry_run_no_mutation(tmp_path):
    source_root = tmp_path / "source"
    source_root.mkdir()
    _make_fake_source(source_root)

    target_root = tmp_path / "hermes" / "plugins" / "agent-sensorium"
    scripts_target = tmp_path / "hermes" / "scripts"

    items = rollout.get_managed_items(source_root, target_root, scripts_target)
    assert len(items) > 0

    rollout.do_sync(items, dry_run=True)

    # Target directory must not have been created at all (or must be empty)
    if target_root.exists():
        all_files = list(target_root.rglob("*"))
        assert all_files == [], f"Expected no files after dry run, found: {all_files}"
    if scripts_target.exists():
        all_scripts = list(scripts_target.rglob("*"))
        assert all_scripts == [], f"Expected no scripts after dry run, found: {all_scripts}"


# ---------------------------------------------------------------------------
# Test 2: do_check returns True (clean) immediately after do_sync
# ---------------------------------------------------------------------------

def test_check_no_drift_after_sync(tmp_path):
    source_root = tmp_path / "source"
    source_root.mkdir()
    _make_fake_source(source_root)

    target_root = tmp_path / "hermes" / "plugins" / "agent-sensorium"
    scripts_target = tmp_path / "hermes" / "scripts"

    items = rollout.get_managed_items(source_root, target_root, scripts_target)
    rollout.do_sync(items, dry_run=False)

    is_clean = rollout.do_check(items, target_root=target_root, scripts_target=scripts_target)
    assert is_clean is True


# ---------------------------------------------------------------------------
# Test 3: check_dirty raises SystemExit on dirty git tree
# ---------------------------------------------------------------------------

def test_dirty_source_refusal(tmp_path, monkeypatch):
    source_root = tmp_path / "source"
    source_root.mkdir()

    def fake_run(cmd, **kwargs):
        result = subprocess.CompletedProcess(cmd, returncode=0)
        result.stdout = "M file.py\n"
        return result

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(SystemExit):
        rollout.check_dirty(source_root, allow_dirty=False)


# ---------------------------------------------------------------------------
# Test 4: check_dirty does NOT raise when allow_dirty=True
# ---------------------------------------------------------------------------

def test_dirty_source_allowed(tmp_path, monkeypatch):
    source_root = tmp_path / "source"
    source_root.mkdir()

    def fake_run(cmd, **kwargs):
        result = subprocess.CompletedProcess(cmd, returncode=0)
        result.stdout = "M file.py\n"
        return result

    monkeypatch.setattr(subprocess, "run", fake_run)

    # Should not raise
    rollout.check_dirty(source_root, allow_dirty=True)


# ---------------------------------------------------------------------------
# Test 5: live-scripts tick file appears in managed items and is synced
# ---------------------------------------------------------------------------

def test_live_script_tick_sync(tmp_path):
    source_root = tmp_path / "source"
    source_root.mkdir()
    _make_fake_source(source_root)

    target_root = tmp_path / "hermes" / "plugins" / "agent-sensorium"
    scripts_target = tmp_path / "hermes" / "scripts"

    items = rollout.get_managed_items(source_root, target_root, scripts_target)

    # Verify the tick script appears in items with correct destination
    tick_items = [
        (src, dst) for src, dst in items
        if src.name == "sensorium_kanban_sensor_tick.py"
    ]
    assert len(tick_items) == 1, f"Expected exactly one tick item, got: {tick_items}"
    tick_src, tick_dst = tick_items[0]
    assert tick_src == source_root / "live-scripts" / "sensorium_kanban_sensor_tick.py"
    assert tick_dst == scripts_target / "sensorium_kanban_sensor_tick.py"

    # Sync and verify the file was actually copied
    rollout.do_sync(items, dry_run=False)
    assert tick_dst.exists(), "Tick script was not copied to scripts_target"
    assert tick_dst.read_text() == "# kanban sensor tick\n"


# ---------------------------------------------------------------------------
# Test 6: do_check returns False after a file is modified in target
# ---------------------------------------------------------------------------

def test_check_detects_drift(tmp_path):
    source_root = tmp_path / "source"
    source_root.mkdir()
    _make_fake_source(source_root)

    target_root = tmp_path / "hermes" / "plugins" / "agent-sensorium"
    scripts_target = tmp_path / "hermes" / "scripts"

    items = rollout.get_managed_items(source_root, target_root, scripts_target)
    rollout.do_sync(items, dry_run=False)

    # Modify one file in target to introduce drift
    target_plugin_yaml = target_root / "plugin.yaml"
    assert target_plugin_yaml.exists(), "plugin.yaml should exist in target after sync"
    target_plugin_yaml.write_text("name: MODIFIED\n")

    is_clean = rollout.do_check(items, target_root=target_root, scripts_target=scripts_target)
    assert is_clean is False


# ---------------------------------------------------------------------------
# Test 7: do_backup with dry_run=True does not create a backup directory
# ---------------------------------------------------------------------------

def test_no_backup_flag(tmp_path):
    target_root = tmp_path / "hermes" / "plugins" / "agent-sensorium"
    target_root.mkdir(parents=True)
    (target_root / "plugin.yaml").write_text("name: agent-sensorium\n")

    result = rollout.do_backup(target_root, dry_run=True)

    # dry_run backup should return None or a path that does not exist
    if result is not None:
        assert not Path(result).exists(), (
            f"dry_run=True backup must not create a directory, but {result} exists"
        )


# ---------------------------------------------------------------------------
# Test 8: do_backup with dry_run=False creates a sibling backup directory
# ---------------------------------------------------------------------------

def test_backup_creates_dir(tmp_path):
    target_root = tmp_path / "hermes" / "plugins" / "agent-sensorium"
    target_root.mkdir(parents=True)
    (target_root / "plugin.yaml").write_text("name: agent-sensorium\n")
    (target_root / "README.md").write_text("# README\n")

    backup_path = rollout.do_backup(target_root, dry_run=False)

    assert backup_path is not None, "do_backup should return the backup path"
    backup_dir = Path(backup_path)
    assert backup_dir.exists(), f"Backup directory {backup_dir} was not created"
    # Backup should be a sibling of target_root (or a descendant of the parent)
    assert backup_dir.parent == target_root.parent or backup_dir.is_relative_to(target_root.parent)
    # Backup should contain at least one of the files
    all_backed_up = list(backup_dir.rglob("*"))
    assert len(all_backed_up) > 0, "Backup directory is empty"


# ---------------------------------------------------------------------------
# Test 9: cache/build residue is ignored during managed item expansion
# ---------------------------------------------------------------------------

def test_ignored_cache_files_are_not_managed(tmp_path):
    source_root = tmp_path / "source"
    source_root.mkdir()
    _make_fake_source(source_root)
    cache_dir = source_root / "agent_sensorium" / "__pycache__"
    cache_dir.mkdir()
    (cache_dir / "store.cpython-311.pyc").write_bytes(b"pyc")

    target_root = tmp_path / "hermes" / "plugins" / "agent-sensorium"
    scripts_target = tmp_path / "hermes" / "scripts"

    items = rollout.get_managed_items(source_root, target_root, scripts_target)

    assert all("__pycache__" not in src.parts for src, _ in items)
    assert all(src.suffix != ".pyc" for src, _ in items)


# ---------------------------------------------------------------------------
# Test 10: check reports stale extra files in managed target paths
# ---------------------------------------------------------------------------

def test_check_detects_extra_managed_target_file(tmp_path):
    source_root = tmp_path / "source"
    source_root.mkdir()
    _make_fake_source(source_root)

    target_root = tmp_path / "hermes" / "plugins" / "agent-sensorium"
    scripts_target = tmp_path / "hermes" / "scripts"
    items = rollout.get_managed_items(source_root, target_root, scripts_target)
    rollout.do_sync(items, dry_run=False)

    extra = target_root / "agent_sensorium" / "removed_module.py"
    extra.write_text("# stale\n")

    assert rollout.do_check(items, target_root=target_root, scripts_target=scripts_target) is False


# ---------------------------------------------------------------------------
# Test 11: prune removes stale managed target files
# ---------------------------------------------------------------------------

def test_prune_removes_extra_managed_target_file(tmp_path):
    source_root = tmp_path / "source"
    source_root.mkdir()
    _make_fake_source(source_root)

    target_root = tmp_path / "hermes" / "plugins" / "agent-sensorium"
    scripts_target = tmp_path / "hermes" / "scripts"
    items = rollout.get_managed_items(source_root, target_root, scripts_target)
    rollout.do_sync(items, dry_run=False)

    extra = target_root / "agent_sensorium" / "removed_module.py"
    extra.write_text("# stale\n")
    managed_dst = {dst for _, dst in items}
    extra_targets = [
        dst for dst in rollout.get_managed_targets(target_root, scripts_target)
        if dst not in managed_dst
    ]

    assert extra in extra_targets
    assert rollout.do_prune(extra_targets, dry_run=False) is True
    assert not extra.exists()
    assert rollout.do_check(items, target_root=target_root, scripts_target=scripts_target) is True
