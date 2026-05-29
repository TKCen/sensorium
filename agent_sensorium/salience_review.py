"""Hermes-style bounded salience review runner.

The automatic runner remains disabled by default, but the safety seams are now
implemented behind the master gate:

- A bounded Hermes AIAgent child can be created without mutating foreground agent
  state.
- Runtime dispatch is guarded by a hard whitelist, not just prompt text.
- Reviewer-visible tools are limited to the two attention-policy authority tools.
- Store writes used by the runner are flushed/atomic where they rewrite files.
- Run counters are serialized to disk under a file lock so stateless gateway
  workers do not depend on process-local counters.

Guardrails that remain permanent: no source-code edits, no SOUL edits, no
outbound delivery, no outbox, no browser/terminal tools, and no recursive review.
"""

from __future__ import annotations

import contextlib
import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

try:  # pragma: no cover - fcntl is present on the Linux/WSL runtime.
    import fcntl
except Exception:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

from .config import manage_attention_policy_config
from .improvement import (
    _SALIENCE_REVIEW_PROMPT,
    apply_salience_review_decision,
    build_salience_review_context,
    parse_salience_review_decision,
    record_attention_policy_decision,
)
from .schemas import truncate_text, utc_now_iso
from .store import SensoriumStore, atomic_write_json

SALIENCE_REVIEW_ENABLED: bool = False
"""Master gate. Keep False until a lead deliberately enables automatic review."""

_MISSING_SEAMS: list[str] = []
"""All known v1 automation seams are implemented; the master gate still defaults off."""

RECORD_DECISION_TOOL = "record_attention_policy_decision"
MANAGE_POLICY_TOOL = "manage_attention_policy_config"
ALLOWED_POLICY_TOOLS = frozenset({RECORD_DECISION_TOOL, MANAGE_POLICY_TOOL})

# Hermes plugin tool names used by an AIAgent child. The local dispatcher below
# exposes the implementation names; the Hermes child exposes registered plugin
# names and pins the same whitelist at the pre-tool-call layer.
HERMES_POLICY_TOOLS = frozenset({
    "sensorium_attention_policy_decide",
    "sensorium_attention_policy_manage",
})

FORBIDDEN_TOOL_NAMES = frozenset({
    "terminal",
    "browser_navigate",
    "browser_click",
    "browser_type",
    "browser_snapshot",
    "send_message",
    "sensorium_outbox_prepare",
    "sensorium_outbox_dispatch",
    "outbox_prepare",
    "outbox_dispatch",
    "run_bounded_salience_review_session",
    "salience_review",
})

_COUNTERS_NAME = "salience_review.counters.json"
_LOCK_NAME = "salience_review.counters.lock"


class ToolDeniedError(PermissionError):
    """Raised when the bounded salience reviewer asks for a non-whitelisted tool."""


def _copy_jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def _counter_path(store: SensoriumStore) -> Path:
    return store.root / _COUNTERS_NAME


def _load_counters(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


@contextlib.contextmanager
def _counter_lock(store: SensoriumStore):
    store.ensure_dirs()
    lock_path = store.root / "locks" / _LOCK_NAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def update_salience_review_counters(store: SensoriumStore, **deltas: int | str | None) -> dict[str, Any]:
    """Serialize salience-review counters to disk for stateless deployments."""
    with _counter_lock(store):
        path = _counter_path(store)
        counters = _load_counters(path)
        counters.setdefault("schema_version", 1)
        counters["updated_at"] = utc_now_iso()
        for key, value in deltas.items():
            if value is None:
                continue
            if isinstance(value, int):
                counters[key] = int(counters.get(key) or 0) + value
            else:
                counters[key] = str(value)
        atomic_write_json(path, counters)
        return counters


class SalienceReviewToolDispatcher:
    """Runtime policy gate for the bounded salience reviewer.

    This is the hard dispatch-layer boundary used by tests and by any local
    runner. It permits only the two typed attention-policy authority tools and
    records denied attempts as counters; unsafe requests never reach tool code.
    """

    def __init__(
        self,
        store: SensoriumStore,
        *,
        candidate_id: str,
        config_path: str | None = None,
    ) -> None:
        self.store = store
        self.candidate_id = candidate_id
        self.config_path = config_path
        self.allowed_tools = ALLOWED_POLICY_TOOLS

    def dispatch(self, tool_name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        name = str(tool_name or "").strip()
        args = dict(args or {})
        if name not in self.allowed_tools:
            update_salience_review_counters(
                self.store,
                denied_tool_calls=1,
                last_denied_tool=name or "<empty>",
            )
            return {
                "status": "denied",
                "tool": name,
                "allowed_tools": sorted(self.allowed_tools),
                "reason": "salience reviewer tool not whitelisted",
            }

        if name == RECORD_DECISION_TOOL:
            payload = {
                "candidate_id": args.get("candidate_id") or self.candidate_id,
                "decision": args.get("decision", ""),
                "reason": args.get("reason", ""),
                "future_tendency_delta": args.get("future_tendency_delta", ""),
                "verification_condition": args.get("verification_condition", ""),
                "rollback_condition": args.get("rollback_condition", ""),
                "decided_by": args.get("decided_by") or "salience-review",
                "decision_ref": args.get("decision_ref") or "",
                "implementation_ref": args.get("implementation_ref") or "",
            }
            result = record_attention_policy_decision(self.store, **payload)
            update_salience_review_counters(self.store, decisions_recorded=1, last_tool=name)
            return {"status": "ok", "tool": name, "result": result}

        if name == MANAGE_POLICY_TOOL:
            result = manage_attention_policy_config(
                action=args.get("action", ""),
                config_path=args.get("config_path") or self.config_path,
                state_dir=str(self.store.root),
                rule=args.get("rule") or "",
                patch=args.get("patch"),
                key=args.get("key") or "",
                value=args.get("value"),
                reason=args.get("reason") or "",
                future_tendency_delta=args.get("future_tendency_delta") or "",
                verification_condition=args.get("verification_condition") or "",
                rollback_condition=args.get("rollback_condition") or "",
                actor=args.get("actor") or "salience-review",
                decision_ref=args.get("decision_ref") or "",
            )
            self.store.append_jsonl("decisions", result["receipt"])
            update_salience_review_counters(self.store, policy_mutations=1, last_tool=name)
            return {"status": "ok", "tool": name, "result": result}

        raise ToolDeniedError(f"unreachable salience review tool path: {name}")


def _build_user_message(candidate_id: str, context: dict[str, Any], state_dir: str) -> str:
    context_json = json.dumps(context, indent=2, sort_keys=True, default=str)
    return (
        f"{_SALIENCE_REVIEW_PROMPT}\n\n"
        "Bounded runtime facts:\n"
        f"- candidate_id: {candidate_id}\n"
        f"- state_dir: {state_dir}\n"
        f"- allowed tools: {', '.join(sorted(HERMES_POLICY_TOOLS))}\n"
        "- any other tool call is denied before execution.\n\n"
        "Review context JSON:\n"
        f"{context_json}\n\n"
        "Call exactly one allowed tool, or return one strict JSON decision payload."
    )


def _safe_tool_names(tools: list[dict[str, Any]] | None) -> list[str]:
    names: list[str] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        func = tool.get("function") or {}
        name = func.get("name")
        if isinstance(name, str):
            names.append(name)
    return names


def _run_hermes_aiaagent_child(
    *,
    parent_agent: Any = None,
    user_message: str,
    max_iterations: int,
    context: dict[str, Any] | None = None,
    dispatcher: SalienceReviewToolDispatcher | None = None,
) -> dict[str, Any]:
    """Run a bounded Hermes child with only Sensorium policy tools exposed."""
    from importlib import import_module

    run_agent_mod = import_module("run_agent")
    model_tools_mod = import_module("model_tools")
    plugins_mod = import_module("hermes_cli.plugins")
    AIAgent = run_agent_mod.AIAgent

    kwargs: dict[str, Any] = {
        "max_iterations": max(1, min(int(max_iterations or 4), 8)),
        "quiet_mode": True,
        "enabled_toolsets": ["agent-sensorium"],
        "skip_memory": True,
        "skip_context_files": True,
        "load_soul_identity": False,
        "platform": "sensorium-salience-review",
    }
    if parent_agent is not None:
        parent_values = {
            "model": getattr(parent_agent, "model", None),
            "provider": getattr(parent_agent, "provider", None),
            "api_mode": getattr(parent_agent, "_api_mode", None) or getattr(parent_agent, "api_mode", None),
            "base_url": getattr(parent_agent, "base_url", None) or getattr(parent_agent, "_base_url", None),
            "api_key": getattr(parent_agent, "api_key", None) or getattr(parent_agent, "_api_key", None),
            "parent_session_id": getattr(parent_agent, "session_id", None),
        }
        for key, value in parent_values.items():
            if value:
                kwargs[key] = value
        pool = getattr(parent_agent, "_credential_pool", None)
        if pool is not None:
            kwargs["credential_pool"] = pool

    child = AIAgent(**kwargs)
    child._memory_nudge_interval = 0
    child._skill_nudge_interval = 0
    child.suppress_status_output = True

    tool_defs = model_tools_mod.get_tool_definitions(
        enabled_toolsets=["agent-sensorium"],
        quiet_mode=True,
    )
    child.tools = [
        tool for tool in tool_defs
        if ((tool.get("function") or {}).get("name") in HERMES_POLICY_TOOLS)
    ]
    actual = set(_safe_tool_names(child.tools))
    if actual != set(HERMES_POLICY_TOOLS):
        missing = sorted(set(HERMES_POLICY_TOOLS) - actual)
        extra = sorted(actual - set(HERMES_POLICY_TOOLS))
        raise RuntimeError(f"salience review tool surface mismatch: missing={missing}, extra={extra}")

    plugins_mod.set_thread_tool_whitelist(
        set(HERMES_POLICY_TOOLS),
        deny_msg_fmt=(
            "Sensorium salience review denied non-whitelisted tool: {tool_name}. "
            "Only attention-policy decision/config tools are allowed."
        ),
    )
    try:
        result = child.run_conversation(user_message=user_message, conversation_history=[])
    finally:
        plugins_mod.clear_thread_tool_whitelist()
        with contextlib.suppress(Exception):
            child.shutdown_memory_provider()
        with contextlib.suppress(Exception):
            child.close()
    return result if isinstance(result, dict) else {"final_response": str(result)}


def _parse_json_from_text(text: str) -> dict[str, Any] | None:
    text = str(text or "").strip()
    if not text:
        return None
    candidates = [text]
    if "```" in text:
        parts = text.split("```")
        candidates.extend(part.strip() for part in parts if part.strip())
    for candidate in candidates:
        cleaned = candidate
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
        try:
            payload = json.loads(cleaned)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _attention_receipt_count(store: SensoriumStore, candidate_id: str) -> int:
    return sum(
        1
        for row in store.read_jsonl("decisions")
        if row.get("type") == "attention_policy_review.decision"
        and row.get("candidate_id") == candidate_id
    )


def run_bounded_salience_review_session(
    store: SensoriumStore,
    candidate_id: str,
    evidence: dict,
    *,
    dry_run: bool = True,
    enabled: bool | None = None,
    agent_runner: Callable[..., dict[str, Any]] | None = None,
    parent_agent: Any = None,
    config_path: str | None = None,
    max_iterations: int = 4,
) -> dict[str, Any]:
    """Run one bounded salience-review session.

    Default behavior remains disabled. Pass ``enabled=True`` only from a guarded
    activation path. ``dry_run=True`` builds the exact bounded prompt/context and
    serializes counters but performs no model/tool call.
    """
    if enabled is None:
        enabled = SALIENCE_REVIEW_ENABLED
    if not enabled:
        return {
            "status": "disabled",
            "reason": "SALIENCE_REVIEW_ENABLED is False; manual harness required",
            "missing_seams": list(_MISSING_SEAMS),
            "candidate_id": candidate_id,
            "use_instead": "scripts/sensorium_salience_review.py --json-only-context",
        }

    store.ensure_dirs()
    evidence_copy = _copy_jsonable(evidence if isinstance(evidence, dict) else {})
    context = build_salience_review_context(store, evidence_copy)
    context = _copy_jsonable(context)
    user_message = _build_user_message(candidate_id, context, str(store.root))
    counters = update_salience_review_counters(
        store,
        attempts=1,
        dry_runs=1 if dry_run else 0,
        last_candidate_id=candidate_id,
    )

    prepared = {
        "status": "dry_run",
        "candidate_id": candidate_id,
        "allowed_tools": sorted(ALLOWED_POLICY_TOOLS),
        "hermes_allowed_tools": sorted(HERMES_POLICY_TOOLS),
        "forbidden_tools": sorted(FORBIDDEN_TOOL_NAMES),
        "context": context,
        "prompt": user_message,
        "counters": counters,
    }
    if dry_run:
        return prepared

    before_count = _attention_receipt_count(store, candidate_id)
    dispatcher = SalienceReviewToolDispatcher(
        store,
        candidate_id=candidate_id,
        config_path=config_path,
    )
    runner = agent_runner or _run_hermes_aiaagent_child
    result = runner(
        parent_agent=parent_agent,
        user_message=user_message,
        context=copy.deepcopy(context),
        dispatcher=dispatcher,
        max_iterations=max_iterations,
    )
    if not isinstance(result, dict):
        result = {"final_response": str(result)}

    after_count = _attention_receipt_count(store, candidate_id)
    if after_count > before_count:
        counters = update_salience_review_counters(store, completed=1)
        return {
            "status": "completed",
            "candidate_id": candidate_id,
            "runner_result": result,
            "counters": counters,
        }

    decision_payload = result.get("decision_payload")
    if not isinstance(decision_payload, dict):
        decision_payload = _parse_json_from_text(str(result.get("final_response") or result.get("text") or ""))
    if isinstance(decision_payload, dict):
        parsed = parse_salience_review_decision(decision_payload)
        applied = apply_salience_review_decision(store, candidate_id, parsed)
        counters = update_salience_review_counters(store, completed=1, decisions_recorded=1)
        return {
            "status": "completed",
            "candidate_id": candidate_id,
            "applied": applied,
            "runner_result": result,
            "counters": counters,
        }

    counters = update_salience_review_counters(store, no_decision=1)
    return {
        "status": "no_decision",
        "candidate_id": candidate_id,
        "runner_result": result,
        "counters": counters,
        "reason": truncate_text("reviewer returned no tool call or parseable JSON decision", 160),
    }
