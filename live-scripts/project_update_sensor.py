#!/usr/bin/env python3
"""Generic cheap upstream update sensor for Sensorium.

Modes:
- git: compare a local checkout HEAD to a remote branch via ls-remote.
- github-release: compare a local configured version to GitHub latest release.

The script is script-sensor compatible: emit one compact JSON signal on update
salience, otherwise print nothing and exit 0. It never pulls, merges, edits a
checkout, or updates containers.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

HOME = Path.home()
DEFAULT_INSTANCE = os.environ.get("SENSORIUM_INSTANCE") or os.environ.get("AGENT_SENSORIUM_DEFAULT_INSTANCE") or "sera"
DEFAULT_INTERVAL_SECONDS = int(os.environ.get("PROJECT_UPDATE_SENSOR_INTERVAL_SECONDS", str(6 * 60 * 60)))
TIMEOUT_SECONDS = float(os.environ.get("PROJECT_UPDATE_SENSOR_TIMEOUT_SECONDS", "8"))
DEFAULT_COMMIT_CAP = int(os.environ.get("PROJECT_UPDATE_SENSOR_COMMIT_CAP", "8"))
DEFAULT_FILE_CAP = int(os.environ.get("PROJECT_UPDATE_SENSOR_FILE_CAP", "12"))
DEFAULT_RELEASE_BODY_CAP = int(os.environ.get("PROJECT_UPDATE_SENSOR_RELEASE_BODY_CAP", "900"))


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _safe_sensor_name(name: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", name.strip()).strip("._-")
    return safe or "project_update"


def _state_path(instance: str, sensor: str) -> Path:
    return HOME / ".hermes" / "agent-sensorium" / instance / f"{_safe_sensor_name(sensor)}_state.json"


def _read_state(path: Path) -> dict:
    try:
        if path.exists():
            data = json.loads(path.read_text(errors="ignore"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def _run_git(repo: Path, args: list[str], timeout: float = TIMEOUT_SECONDS) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except OSError as exc:
        return 127, "", f"exec failed: {exc}"


def _github_request(path_or_url: str) -> dict | None:
    url = path_or_url if path_or_url.startswith("http") else f"https://api.github.com{path_or_url}"
    req = Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "Sera-Sensorium-Project-Update-Sensor",
    })
    try:
        with urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


def _github_repo_slug(remote_url: str) -> str:
    if not remote_url:
        return ""
    url = remote_url.strip()
    if url.endswith(".git"):
        url = url[:-4]
    if url.startswith("git@github.com:"):
        return url.split(":", 1)[1].strip("/")
    parsed = urlparse(url)
    if parsed.netloc.endswith("github.com"):
        return parsed.path.strip("/")
    return ""


def _remote_to_github_commit_url(remote_url: str, sha: str) -> str:
    slug = _github_repo_slug(remote_url)
    return f"https://github.com/{slug}/commit/{sha}" if slug and sha else ""


def _safe_short(sha: str) -> str:
    sha = (sha or "").strip()
    return sha[:12] if sha else "unknown"


def _parse_git_log(text: str) -> list[dict]:
    commits: list[dict] = []
    for line in text.splitlines():
        parts = line.split("\x1f")
        if len(parts) < 4:
            continue
        sha, subject, author, ts = parts[:4]
        commits.append({
            "sha": sha,
            "short_sha": _safe_short(sha),
            "subject": subject[:180],
            "author": author[:80],
            "ts": ts,
        })
    return commits


def _change_digest_from_local_refs(repo: Path, *, base: str, remote_ref: str, commit_cap: int, file_cap: int) -> dict | None:
    rc, ahead_count, _ = _run_git(repo, ["rev-list", "--count", f"{base}..{remote_ref}"])
    if rc != 0 or not ahead_count.strip().isdigit():
        return None
    rc, log_out, _ = _run_git(repo, [
        "log",
        f"--max-count={max(1, commit_cap)}",
        "--format=%H%x1f%s%x1f%an%x1f%ct",
        f"{base}..{remote_ref}",
    ])
    commits = _parse_git_log(log_out) if rc == 0 else []
    rc, files_out, _ = _run_git(repo, ["diff", "--name-only", f"{base}..{remote_ref}"])
    files = [line[:220] for line in files_out.splitlines() if line.strip()] if rc == 0 else []
    count = int(ahead_count.strip())
    return {
        "source": "local_remote_ref",
        "ahead_by": count,
        "commit_count_reported": count,
        "commits": commits[:commit_cap],
        "commits_truncated": count > len(commits[:commit_cap]),
        "files_sample": files[:file_cap],
        "files_truncated": len(files) > file_cap,
    }


def _change_digest_from_github_compare(repo_slug: str, *, base: str, head: str, commit_cap: int, file_cap: int) -> dict | None:
    if not repo_slug or not base or not head:
        return None
    data = _github_request(f"/repos/{repo_slug}/compare/{base}...{head}?per_page={max(1, commit_cap)}")
    if not isinstance(data, dict):
        return None
    commits = []
    for item in data.get("commits") or []:
        commit = item.get("commit") or {}
        author = commit.get("author") or {}
        message = str(commit.get("message") or "").splitlines()[0]
        sha = str(item.get("sha") or "")
        commits.append({
            "sha": sha,
            "short_sha": _safe_short(sha),
            "subject": message[:180],
            "author": str(author.get("name") or "")[:80],
            "date": str(author.get("date") or ""),
        })
    files = [str((f or {}).get("filename") or "")[:220] for f in data.get("files") or []]
    files = [f for f in files if f]
    try:
        ahead_by = int(data.get("ahead_by"))
    except Exception:
        ahead_by = len(commits)
    return {
        "source": "github_compare_api",
        "compare_url": data.get("html_url") or f"https://github.com/{repo_slug}/compare/{base}...{head}",
        "ahead_by": ahead_by,
        "commit_count_reported": data.get("total_commits", ahead_by),
        "commits": commits[:commit_cap],
        "commits_truncated": ahead_by > len(commits[:commit_cap]),
        "files_sample": files[:file_cap],
        "files_truncated": len(files) > file_cap,
    }


def _digest_phrase(digest: dict) -> str:
    commits = digest.get("commits") or []
    subjects = [str(c.get("subject") or "").strip() for c in commits[:3]]
    subjects = [s for s in subjects if s]
    if subjects:
        joined = "; ".join(subjects)
        if digest.get("commits_truncated"):
            joined += "; …"
        return joined[:260]
    body = str(digest.get("body_excerpt") or "").strip().replace("\r", " ").replace("\n", " ")
    return body[:260] if body else "change details unavailable within cap"


def _compact_digest_for_signal(digest: dict) -> dict:
    """Return a compact, script-sensor-safe digest for emitted signals.

    The full digest is useful in local state, but Sensorium script sensors have a
    deliberately small stdout cap. Emitted signals should carry enough evidence
    to decide whether to inspect the upstream URL, not the whole changelog or
    compare payload.
    """
    if not isinstance(digest, dict):
        return {}
    out: dict = {}
    for key in (
        "source",
        "compare_url",
        "html_url",
        "published_at",
        "release_name",
        "ahead_by",
        "commit_count_reported",
        "commits_truncated",
        "files_truncated",
        "body_truncated",
    ):
        if key in digest:
            out[key] = digest[key]
    if digest.get("body_excerpt"):
        out["body_excerpt"] = str(digest.get("body_excerpt") or "")[:300]
    commits = digest.get("commits") or []
    if isinstance(commits, list) and commits:
        out["commits"] = [
            {
                "short_sha": str(c.get("short_sha") or _safe_short(str(c.get("sha") or "")))[:12],
                "subject": str(c.get("subject") or "")[:140],
                "author": str(c.get("author") or "")[:60],
                "date": str(c.get("date") or c.get("ts") or "")[:40],
            }
            for c in commits[:3]
            if isinstance(c, dict)
        ]
    files = digest.get("files_sample") or []
    if isinstance(files, list) and files:
        out["files_sample"] = [str(f)[:160] for f in files[:5]]
    compare_digest = digest.get("compare_digest")
    if isinstance(compare_digest, dict):
        out["compare_digest"] = _compact_digest_for_signal(compare_digest)
    return out


def collect_git(args: argparse.Namespace) -> tuple[dict, dict]:
    repo = Path(args.repo).expanduser()
    if not repo.exists():
        return {"ok": False, "error": f"repo missing: {repo}"}, {}
    rc, local_head, local_err = _run_git(repo, ["rev-parse", "HEAD"])
    if rc != 0 or not local_head:
        return {"ok": False, "error": f"local HEAD unavailable: {local_err or rc}"}, {}
    rc, remote_url, _ = _run_git(repo, ["remote", "get-url", args.remote])
    if rc != 0:
        remote_url = ""
    rc, out, err = _run_git(repo, ["ls-remote", args.remote, f"refs/heads/{args.branch}"])
    if rc != 0 or not out:
        return {"ok": False, "error": f"remote head unavailable: {err or rc}"}, {}
    remote_head = out.split()[0]
    rc, status_out, _ = _run_git(repo, ["status", "--short", "--branch"])
    branch_status = status_out.splitlines()[0].replace("## ", "", 1)[:160] if status_out else ""
    sample = {
        "ok": True,
        "mode": "git",
        "repo": str(repo),
        "remote": args.remote,
        "branch": args.branch,
        "remote_url": remote_url,
        "local_head": local_head.strip(),
        "remote_head": remote_head.strip(),
        "branch_status": branch_status,
        "checked_at": _now_iso(),
    }
    digest = None
    remote_ref = f"refs/remotes/{args.remote}/{args.branch}"
    rc, tracking_head, _ = _run_git(repo, ["rev-parse", "--verify", remote_ref])
    if rc == 0 and tracking_head.strip() == remote_head:
        digest = _change_digest_from_local_refs(
            repo,
            base=sample["local_head"],
            remote_ref=remote_ref,
            commit_cap=args.commit_cap,
            file_cap=args.file_cap,
        )
    if digest is None:
        digest = _change_digest_from_github_compare(
            _github_repo_slug(remote_url),
            base=sample["local_head"],
            head=sample["remote_head"],
            commit_cap=args.commit_cap,
            file_cap=args.file_cap,
        )
    return sample, digest or {"source": "unavailable", "ahead_by": None, "commits": [], "files_sample": []}


def _read_env_value(path: str, key: str) -> str:
    if not path or not key:
        return ""
    p = Path(path).expanduser()
    try:
        for raw in p.read_text(errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == key:
                return v.strip().strip('"').strip("'")
    except Exception:
        return ""
    return ""


def collect_github_release(args: argparse.Namespace) -> tuple[dict, dict]:
    data = _github_request(f"/repos/{args.github_repo}/releases/latest")
    if not isinstance(data, dict) or not data.get("tag_name"):
        return {"ok": False, "error": f"latest release unavailable for {args.github_repo}"}, {}
    latest = str(data.get("tag_name") or "")
    current = args.current_version or _read_env_value(args.current_version_file, args.current_version_key)
    normalized_current = current if current.startswith("v") else (f"v{current}" if current else "")
    body = str(data.get("body") or "")
    digest = {
        "source": "github_release_api",
        "release_name": str(data.get("name") or latest)[:160],
        "published_at": data.get("published_at") or "",
        "html_url": data.get("html_url") or f"https://github.com/{args.github_repo}/releases/tag/{latest}",
        "body_excerpt": body[:args.release_body_cap],
        "body_truncated": len(body) > args.release_body_cap,
        "compare_url": f"https://github.com/{args.github_repo}/compare/{normalized_current}...{latest}" if normalized_current and normalized_current != latest else "",
    }
    if normalized_current and normalized_current != latest:
        compare = _change_digest_from_github_compare(
            args.github_repo,
            base=normalized_current,
            head=latest,
            commit_cap=args.commit_cap,
            file_cap=args.file_cap,
        )
        if compare:
            digest["compare_digest"] = compare
    sample = {
        "ok": True,
        "mode": "github-release",
        "github_repo": args.github_repo,
        "current_version": current,
        "current_version_normalized": normalized_current,
        "latest_tag": latest,
        "latest_name": str(data.get("name") or latest)[:160],
        "published_at": data.get("published_at") or "",
        "html_url": digest["html_url"],
        "checked_at": _now_iso(),
    }
    return sample, digest


def build_signal(args: argparse.Namespace, sample: dict, digest: dict, state: dict) -> dict | None:
    if not sample.get("ok"):
        return {
            "sensor": args.sensor,
            "source": "machine",
            "kind": f"{args.kind_prefix}_sensor_error",
            "summary": f"{args.label} update sensor could not check upstream: {sample.get('error', 'unknown error')}",
            "actor": "tool",
            "strength_hint": 0.72,
            "sensitivity": "private",
            "allowed_surfaces": ["local", "discord"],
            "correlation_keys": [args.sensor, f"{args.kind_prefix}-sensor-error"],
            "scope": "global",
        }

    if sample.get("mode") == "git":
        local_head = sample["local_head"]
        remote_head = sample["remote_head"]
        previous_remote_head = state.get("last_remote_head")
        first_observation = not bool(previous_remote_head)
        remote_changed = bool(previous_remote_head and previous_remote_head != remote_head)
        local_differs = local_head != remote_head
        if not remote_changed and not (first_observation and local_differs):
            return None
        ahead_by = digest.get("ahead_by")
        ahead_phrase = f"{ahead_by} commits" if isinstance(ahead_by, int) else "new commits"
        if remote_changed:
            summary = (
                f"{args.label} upstream {sample.get('branch', 'main')} moved: "
                f"{_safe_short(previous_remote_head or '')} → {_safe_short(remote_head)}; "
                f"local is {_safe_short(local_head)}; {ahead_phrase} ahead. "
                f"Sample: {_digest_phrase(digest)}. Choose whether this warrants an update."
            )
        else:
            summary = (
                f"{args.label} upstream differs from local checkout: remote {_safe_short(remote_head)}, "
                f"local {_safe_short(local_head)}; {ahead_phrase} ahead; "
                f"branch status {sample.get('branch_status') or 'status unavailable'}. "
                f"Sample: {_digest_phrase(digest)}. Choose whether this warrants an update."
            )
        return {
            "sensor": args.sensor,
            "source": "machine",
            "kind": f"{args.kind_prefix}_available",
            "summary": summary[:500],
            "actor": "tool",
            "strength_hint": 0.78 if first_observation else 0.84,
            "sensitivity": "private",
            "allowed_surfaces": ["local", "discord"],
            "correlation_keys": [args.sensor, f"repo:{_github_repo_slug(sample.get('remote_url', '')) or sample.get('repo')}", f"branch:{sample.get('branch')}", f"remote:{sample.get('remote')}"],
            "scope": "global",
            "repo": sample.get("repo"),
            "remote": sample.get("remote"),
            "branch": sample.get("branch"),
            "local_head": local_head,
            "remote_head": remote_head,
            "branch_status": sample.get("branch_status") or "",
            "source_ref": _remote_to_github_commit_url(sample.get("remote_url", ""), remote_head),
            "change_digest": _compact_digest_for_signal(digest),
        }

    latest = sample.get("latest_tag") or ""
    current = sample.get("current_version_normalized") or sample.get("current_version") or "unknown"
    previous_latest = state.get("last_latest_tag")
    latest_changed = bool(previous_latest and previous_latest != latest)
    first_observation = not bool(previous_latest)
    local_differs = bool(current and current != latest)
    if not latest_changed and not (first_observation and local_differs):
        return None
    summary = (
        f"{args.label} release available: local {current or 'unknown'} → latest {latest}. "
        f"Sample: {_digest_phrase(digest)}. Choose whether this warrants an update."
    )
    return {
        "sensor": args.sensor,
        "source": "machine",
        "kind": f"{args.kind_prefix}_available",
        "summary": summary[:500],
        "actor": "tool",
        "strength_hint": 0.78 if first_observation else 0.84,
        "sensitivity": "private",
        "allowed_surfaces": ["local", "discord"],
        "correlation_keys": [args.sensor, f"repo:{sample.get('github_repo')}", f"release:{latest}"],
        "scope": "global",
        "github_repo": sample.get("github_repo"),
        "current_version": sample.get("current_version"),
        "latest_tag": latest,
        "source_ref": sample.get("html_url") or "",
        "change_digest": _compact_digest_for_signal(digest),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Generic project update Sensorium sensor")
    parser.add_argument("--mode", choices=["git", "github-release"], required=True)
    parser.add_argument("--sensor", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--kind-prefix", required=True)
    parser.add_argument("--instance", default=DEFAULT_INSTANCE)
    parser.add_argument("--repo", default="")
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--github-repo", default="")
    parser.add_argument("--current-version", default="")
    parser.add_argument("--current-version-file", default="")
    parser.add_argument("--current-version-key", default="")
    parser.add_argument("--commit-cap", type=int, default=DEFAULT_COMMIT_CAP)
    parser.add_argument("--file-cap", type=int, default=DEFAULT_FILE_CAP)
    parser.add_argument("--release-body-cap", type=int, default=DEFAULT_RELEASE_BODY_CAP)
    parser.add_argument("--force", action="store_true", help="Ignore min check interval")
    parser.add_argument("--debug-json", action="store_true", help="Print full receipt instead of script-sensor signal/empty")
    args = parser.parse_args()
    args.sensor = _safe_sensor_name(args.sensor)
    args.commit_cap = max(1, args.commit_cap)
    args.file_cap = max(1, args.file_cap)
    args.release_body_cap = max(120, args.release_body_cap)

    state_file = _state_path(args.instance, args.sensor)
    state = _read_state(state_file)
    now = time.time()
    last_checked = float(state.get("last_checked_epoch") or 0.0)
    if not args.force and not args.debug_json and last_checked and now - last_checked < DEFAULT_INTERVAL_SECONDS:
        return 0

    if args.mode == "git":
        sample, digest = collect_git(args)
    else:
        sample, digest = collect_github_release(args)

    signal = build_signal(args, sample, digest, state)
    next_state = dict(state)
    next_state.update({
        "last_checked_at": _now_iso(),
        "last_checked_epoch": now,
        "last_ok": bool(sample.get("ok")),
        "last_error": sample.get("error", ""),
        "last_sample": sample,
        "last_change_digest": digest,
    })
    if sample.get("ok"):
        if sample.get("mode") == "git":
            next_state["last_local_head"] = sample.get("local_head")
            next_state["last_remote_head"] = sample.get("remote_head")
            next_state["last_branch_status"] = sample.get("branch_status")
        else:
            next_state["last_current_version"] = sample.get("current_version")
            next_state["last_latest_tag"] = sample.get("latest_tag")
            next_state["last_latest_name"] = sample.get("latest_name")
    if args.debug_json:
        print(json.dumps({"sample": sample, "change_digest": digest, "signal": signal, "state_file": str(state_file)}, indent=2, sort_keys=True))
    elif signal:
        print(json.dumps(signal, separators=(",", ":")))
    sys.stdout.flush()
    if signal:
        next_state["last_signal_at"] = _now_iso()
        next_state["last_signal_kind"] = signal.get("kind")
        next_state["last_signal_summary"] = signal.get("summary")
    _write_state(state_file, next_state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
