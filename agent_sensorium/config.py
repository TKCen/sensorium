"""Instance config loading, validation, and policy application.

Config is loaded from (in order): explicit config_path argument, then
{state_dir}/instance.config.json, then safe defaults. Missing or corrupt
config falls back to safe defaults — local-only surfaces, private sensitivity,
minimal budgets.

Policy functions only narrow scope; they never broaden an item's sensitivity
or allowed_surfaces beyond what the item already has.
"""

import json
from pathlib import Path

from .schemas import SENSITIVITY_RANK, VALID_SENSITIVITIES

SAFE_DEFAULTS: dict = {
    "instance_name": "default",
    "policy_card_ref": None,
    "allowed_surfaces": ["local"],
    "max_sensitivity": "private",
    "thresholds": {
        "starvation_hours": 72,
        "expiring_window_hours": 24,
    },
    "budgets": {},
}


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
    config: dict = {
        "instance_name": SAFE_DEFAULTS["instance_name"],
        "policy_card_ref": SAFE_DEFAULTS["policy_card_ref"],
        "allowed_surfaces": list(SAFE_DEFAULTS["allowed_surfaces"]),
        "max_sensitivity": SAFE_DEFAULTS["max_sensitivity"],
        "thresholds": dict(SAFE_DEFAULTS["thresholds"]),
        "budgets": dict(SAFE_DEFAULTS["budgets"]),
    }
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
        if isinstance(val, list) and all(isinstance(s, str) for s in val) and val:
            config["allowed_surfaces"] = sorted(set(val))
    if "max_sensitivity" in raw:
        val = raw["max_sensitivity"]
        if val in VALID_SENSITIVITIES:
            config["max_sensitivity"] = val
    if "thresholds" in raw:
        val = raw["thresholds"]
        if isinstance(val, dict):
            for k in ("starvation_hours", "expiring_window_hours"):
                if k in val and isinstance(val[k], (int, float)):
                    config["thresholds"][k] = val[k]
    if "budgets" in raw:
        val = raw["budgets"]
        if isinstance(val, dict):
            config["budgets"] = val
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
