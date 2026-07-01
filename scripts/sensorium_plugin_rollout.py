#!/usr/bin/env python3
"""Sync agent-sensorium repo snapshot into Hermes plugin install.

Usage:
  sensorium_plugin_rollout.py [--dry-run] [--allow-dirty] [--no-backup] [--check] [--prune]
  sensorium_plugin_rollout.py --help

Managed paths (relative to repo root → plugin install root):
  agent_sensorium/   dashboard/   scripts/   skills/   live-scripts/
  plugin.yaml   __init__.py   README.md   pyproject.toml
  live-scripts/*.py → {scripts_target}/*.py
"""

import argparse
import datetime
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def resolve_paths(args) -> tuple[Path, Path, Path]:
    """Return (source_root, target_root, scripts_target) as Path objects."""
    # Source: repo root = parent of the scripts/ dir this script lives in
    source_root = Path(__file__).resolve().parent.parent

    # Hermes home: env var takes priority, fall back to ~/.hermes
    hermes_home_env = os.environ.get("HERMES_HOME")
    if hermes_home_env:
        hermes_home = Path(hermes_home_env)
    else:
        hermes_home = Path.home() / ".hermes"

    target_root = hermes_home / "plugins" / "agent-sensorium"
    scripts_target = hermes_home / "scripts"

    return source_root, target_root, scripts_target


# ---------------------------------------------------------------------------
# Dirty-tree check
# ---------------------------------------------------------------------------

def check_dirty(source_root: Path, allow_dirty: bool) -> None:
    """Raise SystemExit if working tree is dirty and allow_dirty is False."""
    if allow_dirty:
        return
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            cwd=source_root,
        )
        if result.returncode != 0:
            print("WARNING: git returned non-zero; cannot check dirty state. Continuing.")
            return
        if result.stdout.strip():
            print("ERROR: Working tree has uncommitted changes.")
            print("       Run with --allow-dirty to bypass this check.")
            print("       Dirty files:")
            for line in result.stdout.strip().splitlines():
                print(f"         {line}")
            sys.exit(1)
    except FileNotFoundError:
        print("WARNING: git not found; cannot check dirty state. Continuing.")


# ---------------------------------------------------------------------------
# Managed item enumeration
# ---------------------------------------------------------------------------

# Directories to copy recursively (src relative → dst relative, same name)
_MANAGED_DIRS = [
    "agent_sensorium",
    "dashboard",
    "scripts",
    "skills",
    "live-scripts",
]

# Individual files to copy (src relative → dst relative, same name)
_MANAGED_FILES = [
    "plugin.yaml",
    "__init__.py",
    "README.md",
    "pyproject.toml",
]

# Special mappings: live scripts are kept in the plugin tree for source parity
# and also copied into ~/.hermes/scripts for cron/runtime use.
_LIVE_SCRIPT_GLOB = "*.py"

_IGNORED_NAMES = {
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".git",
    ".DS_Store",
}


def _is_ignored(path: Path) -> bool:
    """Return True when path belongs to local/cache/build residue."""
    return any(part in _IGNORED_NAMES for part in path.parts)


def _expand_dir(src_dir: Path, dst_dir: Path) -> list[tuple[Path, Path]]:
    """Recursively expand a source directory into (src_file, dst_file) pairs."""
    pairs: list[tuple[Path, Path]] = []
    for src_file in sorted(src_dir.rglob("*")):
        rel = src_file.relative_to(src_dir)
        if src_file.is_file() and not _is_ignored(rel):
            dst_file = dst_dir / rel
            pairs.append((src_file, dst_file))
    return pairs


def get_managed_targets(target_root: Path, scripts_target: Path) -> list[Path]:
    """Return existing files currently inside managed destination paths."""
    targets: list[Path] = []
    for name in _MANAGED_DIRS:
        dst_dir = target_root / name
        if dst_dir.exists():
            for dst_file in sorted(dst_dir.rglob("*")):
                rel = dst_file.relative_to(dst_dir)
                if dst_file.is_file() and not _is_ignored(rel):
                    targets.append(dst_file)
    for name in _MANAGED_FILES:
        dst_file = target_root / name
        if dst_file.exists() and dst_file.is_file():
            targets.append(dst_file)
    live_scripts_dir = target_root / "live-scripts"
    if live_scripts_dir.exists():
        for live_src in sorted(live_scripts_dir.glob(_LIVE_SCRIPT_GLOB)):
            live_dst = scripts_target / live_src.name
            if live_dst.exists() and live_dst.is_file():
                targets.append(live_dst)
    return targets


def get_managed_items(
    source_root: Path,
    target_root: Path,
    scripts_target: Path,
) -> list[tuple[Path, Path]]:
    """Return list of (src_path, dst_path) for all managed paths that exist in source."""
    items: list[tuple[Path, Path]] = []

    # Directories
    for name in _MANAGED_DIRS:
        src = source_root / name
        if src.exists() and src.is_dir():
            dst = target_root / name
            items.extend(_expand_dir(src, dst))

    # Individual files
    for name in _MANAGED_FILES:
        src = source_root / name
        if src.exists() and src.is_file():
            dst = target_root / name
            items.append((src, dst))

    # Live scripts: also install directly into ~/.hermes/scripts for cron/runtime use.
    live_scripts_dir = source_root / "live-scripts"
    if live_scripts_dir.exists() and live_scripts_dir.is_dir():
        for live_src in sorted(live_scripts_dir.glob(_LIVE_SCRIPT_GLOB)):
            if live_src.is_file() and not _is_ignored(live_src.relative_to(live_scripts_dir)):
                live_dst = scripts_target / live_src.name
                items.append((live_src, live_dst))

    return items


# ---------------------------------------------------------------------------
# File hashing
# ---------------------------------------------------------------------------

def file_hash(path: Path) -> str:
    """Return sha256 hex of file content, or '' if file is missing."""
    if not path.exists():
        return ""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

def do_backup(target_root: Path, dry_run: bool) -> Path | None:
    """Create a timestamped backup outside the live plugin search tree.

    Dashboard discovery scans every directory under ``~/.hermes/plugins`` for
    ``dashboard/manifest.json``. Storing backup copies there makes old backups
    shadow the real plugin because hidden ``.sensorium-backup-*`` directories
    sort before ``agent-sensorium``. Keep backups under ``~/.hermes/backups``.
    """
    if not target_root.exists():
        return None

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_root = target_root.parent.parent / "backups" / "agent-sensorium-rollout"
    backup_path = backup_root / f"sensorium-backup-{timestamp}"

    print(f"[BACKUP] {target_root} → {backup_path}")

    if not dry_run:
        backup_root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(target_root, backup_path)

    return backup_path


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def do_sync(items: list[tuple[Path, Path]], dry_run: bool) -> bool:
    """Copy files from src to dst, creating directories as needed.

    Returns True on full success, False if any copy failed.
    """
    success = True
    for src, dst in items:
        # Ensure parent directory exists
        if not dst.parent.exists():
            print(f"[{'DRY-RUN ' if dry_run else ''}MKDIR] {dst.parent}")
            if not dry_run:
                dst.parent.mkdir(parents=True, exist_ok=True)

        print(f"[{'DRY-RUN ' if dry_run else 'SYNC'} COPY] {src} → {dst}")

        if not dry_run:
            try:
                shutil.copy2(src, dst)
            except Exception as exc:
                print(f"ERROR: Failed to copy {src} → {dst}: {exc}", file=sys.stderr)
                success = False

    return success


def do_prune(extra_targets: list[Path], dry_run: bool) -> bool:
    """Delete stale managed target files that no longer exist in source."""
    success = True
    for dst in sorted(extra_targets):
        print(f"[{'DRY-RUN ' if dry_run else ''}PRUNE] {dst}")
        if not dry_run:
            try:
                dst.unlink()
            except Exception as exc:
                print(f"ERROR: Failed to remove stale file {dst}: {exc}", file=sys.stderr)
                success = False
    return success


# ---------------------------------------------------------------------------
# Check / verify
# ---------------------------------------------------------------------------

def do_check(
    items: list[tuple[Path, Path]],
    *,
    target_root: Path | None = None,
    scripts_target: Path | None = None,
) -> bool:
    """Compare repo vs installed plugin for all managed paths.

    Prints MATCH / DRIFT / MISSING for each source-managed item and EXTRA for
    stale installed files inside managed target paths.
    Returns True if fully clean (no drift, no missing, no extras).
    """
    clean = True
    seen_dsts: set[Path] = set()

    for src, dst in items:
        seen_dsts.add(dst)
        src_hash = file_hash(src)
        dst_hash = file_hash(dst)

        if not dst.exists():
            print(f"[CHECK] MISSING  {dst}")
            clean = False
        elif src_hash == dst_hash:
            print(f"[CHECK] MATCH    {dst}")
        else:
            print(f"[CHECK] DRIFT    {dst}")
            clean = False

    if target_root is not None and scripts_target is not None:
        for dst in get_managed_targets(target_root, scripts_target):
            if dst not in seen_dsts:
                print(f"[CHECK] EXTRA    {dst}")
                clean = False

    return clean


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync agent-sensorium repo into Hermes plugin install.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned operations without making any changes.",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Skip dirty working-tree check.",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Skip backup of existing target directory.",
    )
    parser.add_argument(
        "--check",
        "--verify",
        action="store_true",
        dest="check",
        help="Compare repo vs installed plugin; exit 0 if clean, 1 if drift.",
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help="Remove stale files from managed target paths after backup.",
    )
    args = parser.parse_args()

    source_root, target_root, scripts_target = resolve_paths(args)

    print(f"Source : {source_root}")
    print(f"Target : {target_root}")
    print(f"Scripts: {scripts_target}")
    print()

    # Dirty check (skip in check/verify mode — no mutation anyway)
    if not args.check:
        check_dirty(source_root, args.allow_dirty)

    # Build managed item list
    items = get_managed_items(source_root, target_root, scripts_target)

    if not items:
        print("No managed items found in source. Nothing to do.")
        sys.exit(0)

    # --check mode: compare only, no mutation
    if args.check:
        clean = do_check(items, target_root=target_root, scripts_target=scripts_target)
        sys.exit(0 if clean else 1)

    # Sync mode
    # Backup before first mutation
    if not args.no_backup:
        do_backup(target_root, dry_run=args.dry_run)
        print()

    ok = do_sync(items, dry_run=args.dry_run)
    if args.prune:
        managed_dst = {dst for _, dst in items}
        extra_targets = [
            dst for dst in get_managed_targets(target_root, scripts_target)
            if dst not in managed_dst
        ]
        ok = do_prune(extra_targets, dry_run=args.dry_run) and ok

    if args.dry_run:
        print()
        print("[DRY-RUN] No files were modified.")
        sys.exit(0)

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
