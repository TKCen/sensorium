"""Instance config loading, validation, and policy application.

Config is loaded from (in order): explicit config_path argument, then
{state_dir}/instance.config.json, then safe defaults. Missing or corrupt
config falls back to safe defaults — local-only surfaces, private sensitivity,
minimal budgets.

Policy functions only narrow scope; they never broaden an item's sensitivity
or allowed_surfaces beyond what the item already has.
"""

import json
import os
import re
import tempfile
from pathlib import Path

from .gate import DEFAULT_CONFIG as GATE_DEFAULT_CONFIG
from .schemas import SENSITIVITY_RANK, VALID_SENSITIVITIES, utc_now_iso

DEFAULT_ATTENTION_POLICY: dict = {
    "evidence_rules": {
        "repeated_suppression_or_drop": {
            "min_count": 3,
            "require_actionable_key": True,
            "ignore_correlation_keys": [
                "attention-policy-harness-canary",
                "sensorium-self-improvement-proof",
            ],
        },
        "settlement_gap": {"min_count": 1},
        "manual_intervention": {"min_count": 1},
        "completed_task_outcomes": {"min_count": 2},
    },
    "review_budget": {},
    "priority_weights": {},
}

DEFAULT_MEDIA_GIFT_POLICY: dict = {
    "enabled": True,
    # Subconscious can shape pressure into a proposal; only Conscious chooses.
    "subconscious_may": ["propose"],
    "conscious_may": [
        "propose",
        "prepare_thread_artifact",
        "offer_choice",
        "choose_silence",
        "decline",
        "approve_delivery",
        "block_delivery",
    ],
    "direct_prompt_mode": "choice_required",
    "silence_allowed": True,
    "artifact_first_required": True,
    "scheduler_delivery_enabled": False,
    "require_why_now_for": [
        "propose",
        "prepare_thread_artifact",
        "approve_delivery",
        "block_delivery",
    ],
    "delivery": {
        "enabled": False,
        "allowed_surfaces": [],
        "allowed_targets": [],
        "cooldown_hours": 24,
    },
}

_KNOWN_ATTENTION_RULES = set(DEFAULT_ATTENTION_POLICY["evidence_rules"])
_ALLOWED_RULE_FIELDS = {
    "min_count",
    "require_actionable_key",
    "ignore_correlation_keys",
    "cooldown_hours",
    "coalescing_window_hours",
    "source_weight",
}

# Generic local TTS / talking-head sidecar defaults. base_url/model are safe
# loopback/engine names; the sidecar is dormant until an operator configures
# sidecar_base/control_command/pid_file for their own local deployment. No
# private checkout path is baked in here.
DEFAULT_TTS_CONFIG: dict = {
    "base_url": "http://127.0.0.1:8892/v1",
    "model": "chatterbox-turbo",
    "voice": "warm-voice-demo",
    "sidecar_base": None,
    "control_command": None,
    "pid_file": None,
}

SAFE_DEFAULTS: dict = {
    "instance_name": "default",
    "policy_card_ref": None,
    "allowed_surfaces": ["local"],
    "max_sensitivity": "private",
    "thresholds": {
        "single_signal_strength": GATE_DEFAULT_CONFIG["thresholds"]["single_signal_strength"],
        "important_kind_strength": GATE_DEFAULT_CONFIG["thresholds"]["important_kind_strength"],
        "candidate_pressure": GATE_DEFAULT_CONFIG["thresholds"]["candidate_pressure"],
        "starvation_hours": 72,
        "expiring_window_hours": 24,
    },
    "promote_kinds": list(GATE_DEFAULT_CONFIG["promote_kinds"]),
    "budgets": {},
    "thread_ttl_hours": 168,
    # Generic default actor for the deprecated background-conscious lease lane.
    "default_actor": "background_conscious",
    # Generic cheap-reviewer profile the Kanban bridge assigns intake to.
    "subconscious_profile": "subconscious_worker",
    # Dashboard quiet-tick freshness filename (configurable, generic default).
    "tick_quiet_filename": "sensorium_tick_quiet.latest.json",
    "tts": DEFAULT_TTS_CONFIG,
    "operational_pointer": {
        "enabled": False,
        "kinds": [],
        "surfaces": [],
        "sensitivity": "private",
    },
    "pointer": {},
    "outbox": {},
    "attention_policy": DEFAULT_ATTENTION_POLICY,
    "media_gift_policy": DEFAULT_MEDIA_GIFT_POLICY,
}


def _deepcopy_jsonable(value):
    return json.loads(json.dumps(value))


def _sanitize_string_list(value) -> list[str] | None:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return None
    return sorted({v.strip() for v in value if v.strip()})


def _sanitize_attention_rule(raw: dict, default: dict | None = None) -> dict:
    if not isinstance(raw, dict):
        raw = {}
    rule = dict(default or {})
    for key, value in raw.items():
        if key not in _ALLOWED_RULE_FIELDS:
            continue
        if key == "min_count":
            if isinstance(value, int) and value > 0:
                rule[key] = value
        elif key == "require_actionable_key":
            if isinstance(value, bool):
                rule[key] = value
        elif key == "ignore_correlation_keys":
            keys = _sanitize_string_list(value)
            if keys is not None:
                rule[key] = keys
        elif key in {"cooldown_hours", "coalescing_window_hours", "source_weight"}:
            if isinstance(value, (int, float)) and value > 0:
                rule[key] = value
    return rule


def sanitize_media_gift_policy(raw: dict | None = None) -> dict:
    """Return bounded conscious-choice policy for mediated-presence gifts.

    The policy controls authorization receipts only. It cannot dispatch outbound
    messages, create schedulers, or broaden an artifact/thread's surfaces.
    """
    raw = raw if isinstance(raw, dict) else {}
    policy = _deepcopy_jsonable(DEFAULT_MEDIA_GIFT_POLICY)
    for key in (
        "enabled",
        "silence_allowed",
        "artifact_first_required",
        "scheduler_delivery_enabled",
    ):
        if isinstance(raw.get(key), bool):
            policy[key] = raw[key]
    for key in ("subconscious_may", "conscious_may", "require_why_now_for"):
        values = _sanitize_string_list(raw.get(key))
        if values is not None:
            policy[key] = values
    if raw.get("direct_prompt_mode") in {"choice_required", "disabled"}:
        policy["direct_prompt_mode"] = raw["direct_prompt_mode"]

    delivery = dict(policy["delivery"])
    maybe_delivery = raw.get("delivery")
    raw_delivery: dict = maybe_delivery if isinstance(maybe_delivery, dict) else {}
    if isinstance(raw_delivery.get("enabled"), bool):
        delivery["enabled"] = raw_delivery["enabled"]
    for key in ("allowed_surfaces", "allowed_targets"):
        values = _sanitize_string_list(raw_delivery.get(key))
        if values is not None:
            delivery[key] = values
    cooldown = raw_delivery.get("cooldown_hours")
    if isinstance(cooldown, (int, float)) and cooldown >= 0:
        delivery["cooldown_hours"] = cooldown
    policy["delivery"] = delivery
    return policy


def sanitize_attention_policy(raw: dict | None = None) -> dict:
    """Return the schema-bounded Sensorium attention policy.

    This is the declarative reflex-tuning surface. It can tune thresholds,
    coalescing/cooldown hints, ignored keys, source weights, review budgets,
    and priority weights. It cannot broaden privacy surfaces or mutate code.
    """
    raw = raw if isinstance(raw, dict) else {}
    policy = _deepcopy_jsonable(DEFAULT_ATTENTION_POLICY)
    raw_rules_obj = raw.get("evidence_rules")
    raw_rules = raw_rules_obj if isinstance(raw_rules_obj, dict) else {}
    for name in _KNOWN_ATTENTION_RULES:
        policy["evidence_rules"][name] = _sanitize_attention_rule(
            raw_rules.get(name) or {},
            default=policy["evidence_rules"].get(name) or {},
        )

    for key in ("review_budget", "priority_weights"):
        raw_section = raw.get(key)
        if isinstance(raw_section, dict):
            section = {}
            for name, value in raw_section.items():
                if not isinstance(name, str) or not name.strip():
                    continue
                if isinstance(value, (int, float)) and value > 0:
                    section[name.strip()] = value
            policy[key] = section
    return policy


def _fsync_parent(path: Path) -> None:
    try:
        fd = os.open(str(path.parent), os.O_DIRECTORY)
    except (AttributeError, OSError):
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
        _fsync_parent(path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# A Sensorium profile is a named runtime namespace (config + state) living in a
# directory under the Sensorium state root. Internally the mechanism is the
# "instance"; "profile" is the operator/agent-facing term. Profile names must be
# safe directory names so they cannot traverse outside the state root.
PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
ACTIVE_PROFILE_MARKER = "active_profile.json"
DEFAULT_STATE_ROOT = "~/.hermes/agent-sensorium"


def sanitize_profile_name(name: str) -> str:
    """Validate and return a safe profile (instance) name, else raise ValueError.

    Rejects blank names, path traversal, separators, and leading dots so a
    profile name can only ever address a direct child of the state root.
    """
    candidate = (name or "").strip()
    if not candidate:
        raise ValueError("profile name must not be blank")
    if len(candidate) > 64:
        raise ValueError("profile name must be at most 64 characters")
    if candidate.startswith(".") or candidate in {".", ".."}:
        raise ValueError(f"invalid profile name: {name!r}")
    if "/" in candidate or "\\" in candidate or "\x00" in candidate:
        raise ValueError(f"invalid profile name: {name!r}")
    if not PROFILE_NAME_RE.match(candidate):
        raise ValueError(
            "profile name may only contain letters, digits, '.', '_', '-': "
            f"{name!r}"
        )
    return candidate


def sensorium_state_root(base_dir: str | None = None) -> Path:
    """Resolve the Sensorium state root that holds per-profile namespaces."""
    if base_dir and base_dir.strip():
        return Path(base_dir).expanduser()
    env = os.environ.get("AGENT_SENSORIUM_ROOT")
    if env and env.strip():
        return Path(env.strip()).expanduser()
    return Path(os.path.expanduser(DEFAULT_STATE_ROOT))


def read_active_profile(base_dir: str | None = None) -> str | None:
    """Return the operator-selected active profile from the root marker, if any."""
    path = sensorium_state_root(base_dir) / ACTIVE_PROFILE_MARKER
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    value = raw.get("profile") if isinstance(raw, dict) else None
    if isinstance(value, str) and value.strip():
        try:
            return sanitize_profile_name(value)
        except ValueError:
            return None
    return None


def write_active_profile(name: str, base_dir: str | None = None) -> Path:
    """Persist the active/default profile selection to the root marker file."""
    safe = sanitize_profile_name(name)
    path = sensorium_state_root(base_dir) / ACTIVE_PROFILE_MARKER
    _atomic_write_json(path, {"profile": safe, "updated_at": utc_now_iso()})
    return path


def profile_state_dir(name: str, base_dir: str | None = None) -> Path:
    """Return the state directory for a named profile under the state root."""
    return sensorium_state_root(base_dir) / sanitize_profile_name(name)


def list_profiles(base_dir: str | None = None) -> list[str]:
    """List profile namespaces (safe-named child directories) under the root."""
    root = sensorium_state_root(base_dir)
    if not root.is_dir():
        return []
    names: set[str] = set()
    for child in root.iterdir():
        if not child.is_dir():
            continue
        try:
            names.add(sanitize_profile_name(child.name))
        except ValueError:
            continue
    return sorted(names)


def init_profile_config(
    name: str,
    *,
    base_dir: str | None = None,
    overwrite: bool = False,
) -> dict:
    """Create a profile namespace and write a default instance.config.json.

    Idempotent: an existing config is left untouched unless overwrite is set.
    """
    safe = sanitize_profile_name(name)
    state_dir = profile_state_dir(safe, base_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "instance.config.json"
    created = False
    if overwrite or not path.exists():
        _atomic_write_json(path, _validate_config({"instance_name": safe}))
        created = True
    config, diagnostics = load_instance_config(state_dir=str(state_dir))
    return {
        "profile": safe,
        "state_dir": str(state_dir),
        "config_path": str(path),
        "created": created,
        "config": config,
        "diagnostics": diagnostics,
    }


def default_instance_name(default: str = "default") -> str:
    """Resolve the active/default Sensorium profile outside the tool wrapper.

    Tool calls get the Hermes runtime context through the plugin wrapper, but
    cron scripts and dashboard plugin APIs can run as plain Python modules.
    Resolution order: environment override, operator-set active-profile marker,
    Hermes config, then a safe generic fallback.
    """
    for env_name in ("AGENT_SENSORIUM_DEFAULT_INSTANCE", "SENSORIUM_INSTANCE"):
        value = os.environ.get(env_name)
        if isinstance(value, str) and value.strip():
            return value.strip()

    marker = read_active_profile()
    if marker:
        return marker

    try:
        from importlib import import_module

        config_mod = import_module("hermes_cli.config")
        value = config_mod.cfg_get(
            config_mod.load_config(), "agent_sensorium", "default_instance", default=default
        )
        if isinstance(value, str) and value.strip():
            return value.strip()
    except Exception:
        pass

    return default


def resolve_config_path(
    config_path: str | None = None,
    state_dir: str | None = None,
) -> Path | None:
    if config_path:
        p = Path(config_path)
        if p.is_file():
            return p
    if state_dir:
        p = Path(state_dir) / "instance.config.json"
        if p.is_file():
            return p
    return None


def _validate_config(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raw = {}

    config: dict = {
        "instance_name": SAFE_DEFAULTS["instance_name"],
        "policy_card_ref": SAFE_DEFAULTS["policy_card_ref"],
        "allowed_surfaces": list(SAFE_DEFAULTS["allowed_surfaces"]),
        "max_sensitivity": SAFE_DEFAULTS["max_sensitivity"],
        "thresholds": dict(SAFE_DEFAULTS["thresholds"]),
        "promote_kinds": list(SAFE_DEFAULTS["promote_kinds"]),
        "budgets": dict(SAFE_DEFAULTS["budgets"]),
        "thread_ttl_hours": SAFE_DEFAULTS["thread_ttl_hours"],
        "default_actor": SAFE_DEFAULTS["default_actor"],
        "subconscious_profile": SAFE_DEFAULTS["subconscious_profile"],
        "tick_quiet_filename": SAFE_DEFAULTS["tick_quiet_filename"],
        "tts": dict(SAFE_DEFAULTS["tts"]),
        "operational_pointer": dict(SAFE_DEFAULTS["operational_pointer"]),
        "pointer": dict(SAFE_DEFAULTS["pointer"]),
        "outbox": dict(SAFE_DEFAULTS["outbox"]),
        "attention_policy": sanitize_attention_policy(SAFE_DEFAULTS["attention_policy"]),
        "media_gift_policy": sanitize_media_gift_policy(SAFE_DEFAULTS["media_gift_policy"]),
    }
    if isinstance(raw.get("conscious_reachout"), dict):
        # Preserve the raw subtree for the conscious reach-out policy gate; the
        # reach-out module owns field-level sanitization. Without this, the
        # validated instance config silently drops operator allowlists and falls
        # back to defaults at the live-tool seam.
        config["conscious_reachout"] = dict(raw["conscious_reachout"])
    if "instance_name" in raw:
        val = raw["instance_name"]
        if isinstance(val, str) and val.strip():
            config["instance_name"] = val.strip()
    if "policy_card_ref" in raw:
        val = raw["policy_card_ref"]
        if isinstance(val, str) and val.strip():
            config["policy_card_ref"] = val.strip()
        else:
            config["policy_card_ref"] = None
    if "allowed_surfaces" in raw:
        val = raw["allowed_surfaces"]
        if isinstance(val, list) and all(isinstance(s, str) for s in val):
            surfaces = sorted({s.strip() for s in val if s.strip()})
            if surfaces:
                config["allowed_surfaces"] = surfaces
    if "max_sensitivity" in raw:
        val = raw["max_sensitivity"]
        if val in VALID_SENSITIVITIES:
            config["max_sensitivity"] = val
    if "thresholds" in raw:
        val = raw["thresholds"]
        if isinstance(val, dict):
            for k in (
                "single_signal_strength",
                "important_kind_strength",
                "candidate_pressure",
                "starvation_hours",
                "expiring_window_hours",
                "dispatch_pressure",
            ):
                if k in val and isinstance(val[k], (int, float)) and val[k] > 0:
                    config["thresholds"][k] = val[k]
    if "promote_kinds" in raw:
        val = raw["promote_kinds"]
        if isinstance(val, list) and all(isinstance(k, str) for k in val):
            config["promote_kinds"] = sorted({k.strip() for k in val if k.strip()})
    if "budgets" in raw:
        val = raw["budgets"]
        if isinstance(val, dict):
            config["budgets"] = val
    if "thread_ttl_hours" in raw:
        val = raw["thread_ttl_hours"]
        if isinstance(val, (int, float)) and val > 0:
            config["thread_ttl_hours"] = val
    for key in ("default_actor", "subconscious_profile"):
        if key in raw and isinstance(raw[key], str) and raw[key].strip():
            config[key] = raw[key].strip()
    if "tick_quiet_filename" in raw and isinstance(raw["tick_quiet_filename"], str):
        val = raw["tick_quiet_filename"].strip()
        if val and "/" not in val and "\\" not in val and val not in {".", ".."}:
            config["tick_quiet_filename"] = val
    if "tts" in raw and isinstance(raw["tts"], dict):
        tts = dict(config["tts"])
        raw_tts = raw["tts"]
        for k in ("base_url", "model", "voice", "sidecar_base", "control_command", "pid_file"):
            if k not in raw_tts:
                continue
            v = raw_tts[k]
            if isinstance(v, str) and v.strip():
                tts[k] = v.strip()
            elif v is None:
                tts[k] = None
        config["tts"] = tts
    if "operational_pointer" in raw and isinstance(raw["operational_pointer"], dict):
        val = raw["operational_pointer"]
        op = dict(config["operational_pointer"])
        if isinstance(val.get("enabled"), bool):
            op["enabled"] = val["enabled"]
        if isinstance(val.get("kinds"), list) and all(isinstance(k, str) for k in val["kinds"]):
            op["kinds"] = sorted({k.strip() for k in val["kinds"] if k.strip()})
        if isinstance(val.get("surfaces"), list) and all(isinstance(s, str) for s in val["surfaces"]):
            op["surfaces"] = sorted({s.strip() for s in val["surfaces"] if s.strip()})
        if val.get("sensitivity") in VALID_SENSITIVITIES:
            op["sensitivity"] = val["sensitivity"]
        config["operational_pointer"] = op
    for key in ("pointer", "outbox"):
        if key in raw and isinstance(raw[key], dict):
            config[key] = raw[key]
    if "attention_policy" in raw:
        config["attention_policy"] = sanitize_attention_policy(raw.get("attention_policy"))
    if "media_gift_policy" in raw:
        config["media_gift_policy"] = sanitize_media_gift_policy(raw.get("media_gift_policy"))
    return config


def _config_diagnostics(config: dict, source: str, path: str | None) -> dict:
    return {
        "source": source,
        "path": path,
        "policy_card_ref": config.get("policy_card_ref"),
        "instance_name": config.get("instance_name", "default"),
        "allowed_surfaces": config.get("allowed_surfaces", ["local"]),
        "max_sensitivity": config.get("max_sensitivity", "private"),
    }


def load_instance_config(
    config_path: str | None = None,
    state_dir: str | None = None,
) -> tuple[dict, dict]:
    path = resolve_config_path(config_path=config_path, state_dir=state_dir)
    if path is None:
        config = _validate_config({})
        return config, _config_diagnostics(config, source="defaults", path=None)

    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        config = _validate_config({})
        diag = _config_diagnostics(config, source="defaults", path=str(path))
        diag["error"] = "config_unreadable"
        return config, diag

    config = _validate_config(raw)
    return config, _config_diagnostics(config, source="file", path=str(path))


def _load_raw_config_for_policy(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return raw if isinstance(raw, dict) else {}


def resolve_policy_write_path(
    *, config_path: str | None = None, state_dir: str | None = None
) -> Path:
    if config_path:
        return Path(config_path)
    if state_dir:
        return Path(state_dir) / "instance.config.json"
    raise ValueError("state_dir or config_path is required for attention policy mutation")


def manage_attention_policy_config(
    *,
    action: str,
    config_path: str | None = None,
    state_dir: str | None = None,
    rule: str = "",
    patch: dict | None = None,
    key: str = "",
    value: object | None = None,
    reason: str = "",
    future_tendency_delta: str = "",
    verification_condition: str = "",
    rollback_condition: str = "",
    actor: str = "conscious",
    decision_ref: str = "",
) -> dict:
    """Apply a narrow declarative Sensorium attention-policy mutation.

    This is intentionally not a generic config/file writer. It only changes the
    `attention_policy` subtree and only through typed actions with rollback and
    verification metadata.
    """
    action = str(action or "").strip()
    valid_actions = {
        "patch_evidence_rule",
        "add_ignored_key",
        "patch_review_budget",
        "patch_priority_weight",
    }
    if action not in valid_actions:
        raise ValueError(f"action must be one of {sorted(valid_actions)}")
    required = {
        "reason": reason,
        "future_tendency_delta": future_tendency_delta,
        "verification_condition": verification_condition,
        "rollback_condition": rollback_condition,
    }
    missing = [name for name, text in required.items() if not isinstance(text, str) or not text.strip()]
    if missing:
        raise ValueError(f"attention policy mutation missing required fields: {missing}")

    path = resolve_policy_write_path(config_path=config_path, state_dir=state_dir)
    raw = _load_raw_config_for_policy(path)
    before = sanitize_attention_policy(raw.get("attention_policy"))
    after = _deepcopy_jsonable(before)

    if action in {"patch_evidence_rule", "add_ignored_key"}:
        if rule not in _KNOWN_ATTENTION_RULES:
            raise ValueError(f"rule must be one of {sorted(_KNOWN_ATTENTION_RULES)}")
        current_rule = after["evidence_rules"].get(rule) or {}
        if action == "patch_evidence_rule":
            sanitized = _sanitize_attention_rule(patch or {}, default=current_rule)
            if sanitized == current_rule:
                raise ValueError("patch_evidence_rule did not contain any valid policy fields")
            after["evidence_rules"][rule] = sanitized
        else:
            if not isinstance(key, str) or not key.strip():
                raise ValueError("add_ignored_key requires non-empty key")
            ignored = sorted(set(current_rule.get("ignore_correlation_keys") or []) | {key.strip()})
            current_rule["ignore_correlation_keys"] = ignored
            after["evidence_rules"][rule] = _sanitize_attention_rule(current_rule)
    elif action == "patch_review_budget":
        if not isinstance(patch, dict) or not patch:
            raise ValueError("patch_review_budget requires non-empty patch")
        budget = dict(after.get("review_budget") or {})
        for name, number in patch.items():
            if isinstance(name, str) and name.strip() and isinstance(number, (int, float)) and number > 0:
                budget[name.strip()] = number
        if budget == (after.get("review_budget") or {}):
            raise ValueError("patch_review_budget did not contain any valid positive numeric fields")
        after["review_budget"] = budget
    elif action == "patch_priority_weight":
        if not isinstance(key, str) or not key.strip():
            raise ValueError("patch_priority_weight requires non-empty key")
        if not isinstance(value, (int, float)) or value <= 0:
            raise ValueError("patch_priority_weight requires positive numeric value")
        weights = dict(after.get("priority_weights") or {})
        weights[key.strip()] = value
        after["priority_weights"] = weights

    after = sanitize_attention_policy(after)
    if after == before:
        raise ValueError("attention policy mutation produced no change")

    raw["attention_policy"] = after
    _atomic_write_json(path, raw)
    verified, _ = load_instance_config(config_path=str(path))
    if verified.get("attention_policy") != after:
        raise ValueError("attention policy mutation failed verification after write")

    return {
        "action": action,
        "path": str(path),
        "changed": True,
        "before": before,
        "after": after,
        "receipt": {
            "ts": utc_now_iso(),
            "type": "attention_policy.config_mutation",
            "action": action,
            "rule": rule,
            "key": key,
            "actor": actor,
            "decision_ref": decision_ref,
            "reason": reason.strip(),
            "future_tendency_delta": future_tendency_delta.strip(),
            "verification_condition": verification_condition.strip(),
            "rollback_condition": rollback_condition.strip(),
            "config_path": str(path),
            "before": before,
            "after": after,
        },
    }


def visible_on_surface(item: dict, surface: str, config: dict) -> bool:
    """Unified visibility gate: True only if item is allowed on surface per policy.

    Checks BOTH item allowed_surfaces AND config allowed_surfaces, plus
    sensitivity. Fails closed: missing surfaces or sensitivity data hides item.
    """
    if not surface:
        return False
    item_surfaces = set(item.get("allowed_surfaces") or [])
    config_surfaces = set(config.get("allowed_surfaces") or [])
    if surface not in item_surfaces or surface not in config_surfaces:
        return False
    item_rank = SENSITIVITY_RANK.get(item.get("sensitivity", "private"), 1)
    max_rank = SENSITIVITY_RANK.get(config.get("max_sensitivity", "private"), 1)
    return item_rank <= max_rank


def apply_surface_policy(
    item_surfaces: list[str] | None,
    config_surfaces: list[str] | None,
) -> list[str]:
    if not item_surfaces:
        return []
    if not config_surfaces:
        return sorted(item_surfaces)
    return sorted(set(item_surfaces) & set(config_surfaces))


def apply_sensitivity_policy(
    item_sensitivity: str,
    config_max_sensitivity: str,
) -> str:
    item_rank = SENSITIVITY_RANK.get(item_sensitivity, 1)
    config_rank = SENSITIVITY_RANK.get(config_max_sensitivity, 0)
    result_rank = min(item_rank, config_rank)
    for name, rank in SENSITIVITY_RANK.items():
        if rank == result_rank:
            return name
    return "local_only"
