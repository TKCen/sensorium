"""Reusable paid-provider subscription budget threshold helpers.

The helpers in this module are deliberately compact and stdlib-only so both the
Sensorium runtime and external no-agent watchdog scripts can consume the exact
same threshold and debounce semantics. Payloads must already be sanitized: no
raw provider responses, tokens, account identifiers, or emails belong here.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

BAND_RANKS = {"healthy": 0, "watch": 1, "degraded": 2, "critical": 3, "exhausted": 4}
RANK_BANDS = {rank: band for band, rank in BAND_RANKS.items()}
DEFAULT_PROVIDER_BUDGET_CONFIG = {
    "watch_percent": 70.0,
    "degraded_percent": 85.0,
    "critical_percent": 95.0,
    "exhausted_percent": 100.0,
    "pace_degraded_pp": 10.0,
    "pace_critical_pp": 20.0,
}
WINDOW_SECONDS = {"5h": 5 * 60 * 60, "weekly": 7 * 24 * 60 * 60}
PROVIDER_BUDGET_CORRELATION_KEYS = [
    "minimax-energy",
    "provider-budget",
    "paid-stack-utilization",
]


def _truncate(value: Any, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _parse_iso(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _seconds_until(reset_at: Any, generated_at: Any) -> int | None:
    reset = _parse_iso(reset_at)
    generated = _parse_iso(generated_at)
    if reset is None or generated is None:
        return None
    return max(0, int((reset - generated).total_seconds()))


def _window_pace_points(*, used_percent: Any, reset_after_seconds: Any, window_seconds: Any) -> tuple[float, float, float] | None:
    window = _safe_float(window_seconds, -1.0)
    if window <= 0:
        return None
    reset_after = max(0.0, min(window, _safe_float(reset_after_seconds)))
    used = max(0.0, min(100.0, _safe_float(used_percent)))
    elapsed_percent = ((window - reset_after) / window) * 100.0
    over_expected_pp = used - elapsed_percent
    return round(used, 3), round(elapsed_percent, 3), round(over_expected_pp, 3)


def reset_identity(window: dict[str, Any]) -> str:
    reset_at = str(window.get("reset_at") or "").strip()
    if reset_at:
        return reset_at[:80]
    reset_after = window.get("reset_after_seconds")
    if reset_after is not None:
        return f"reset_after:{_safe_int(reset_after)}"
    return "reset:unknown"


def state_key(provider: str, window_name: str, reset_id: str, band: str) -> str:
    return f"{provider}:{window_name}:{reset_id}:{band}"


def _status_exhausted(status: Any) -> bool:
    if isinstance(status, bool):
        return False
    if _safe_int(status, -9999) == 2:
        return True
    return str(status or "").strip().lower() in {"2", "exhausted", "blocked", "limit_reached", "forbidden"}


def _classify_window(provider: dict[str, Any], window: dict[str, Any], cfg: dict[str, float]) -> tuple[str, str, dict[str, Any]]:
    used = max(0.0, min(100.0, _safe_float(window.get("used_percent"))))
    status = window.get("status")
    allowed = provider.get("allowed")
    limit_reached = provider.get("limit_reached")
    pace = _window_pace_points(
        used_percent=used,
        reset_after_seconds=window.get("reset_after_seconds"),
        window_seconds=window.get("window_seconds"),
    )
    elapsed = pace[1] if pace else 0.0
    over = pace[2] if pace else 0.0
    if used >= float(cfg["exhausted_percent"]) or _status_exhausted(status):
        band = "exhausted"
        reset = window.get("reset_at") or "unknown"
        reason = f"{window.get('window')} exhausted at {used:.0f}% used; status={status}; resets {reset}"
    elif allowed is False or bool(limit_reached):
        band = "critical"
        reason = f"{window.get('window')} provider blocked or limit reached"
    elif used >= float(cfg["critical_percent"]):
        band = "critical"
        reason = f"{window.get('window')} critical at {used:.0f}% used"
    elif over >= float(cfg["pace_critical_pp"]):
        band = "critical"
        reason = f"{window.get('window')} pace critical: {over:.0f}pp ahead of elapsed-window pace"
    elif used >= float(cfg["degraded_percent"]):
        band = "degraded"
        reason = f"{window.get('window')} degraded at {used:.0f}% used"
    elif over >= float(cfg["pace_degraded_pp"]):
        band = "degraded"
        reason = f"{window.get('window')} pace degraded: {over:.0f}pp ahead of elapsed-window pace"
    elif used >= float(cfg["watch_percent"]):
        band = "watch"
        reason = f"{window.get('window')} watch at {used:.0f}% used"
    else:
        band = "healthy"
        reason = f"{window.get('window')} healthy at {used:.0f}% used"
    values = {
        "provider": provider.get("provider") or "unknown",
        "window": window.get("window") or "unknown",
        "used_percent": round(used, 3),
        "status": status,
        "reset_at": _truncate(window.get("reset_at") or "", 80),
        "reset_after_seconds": _safe_int(window.get("reset_after_seconds")) if window.get("reset_after_seconds") is not None else None,
        "window_seconds": _safe_int(window.get("window_seconds")) if window.get("window_seconds") is not None else None,
        "elapsed_percent": elapsed,
        "over_expected_pp": over,
        "selected_model": _truncate(provider.get("selected_model") or "", 80),
    }
    return band, reason, values


def _missed_bands_for_first_observation(band: str) -> list[str]:
    rank = BAND_RANKS.get(band, 0)
    return [candidate for candidate in ("watch", "degraded", "critical") if BAND_RANKS[candidate] < rank]


def evaluate_provider_budget_sample(
    sample: dict[str, Any],
    *,
    state: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return newly-emittable provider budget threshold events plus next state.

    Debounce identity is provider + window + reset identity + threshold band. A
    repeated observation in the same band is quiet, but upward crossings, reset
    recoveries, and downward recoveries emit explicit events.
    """
    cfg = dict(DEFAULT_PROVIDER_BUDGET_CONFIG)
    if config:
        cfg.update({k: v for k, v in config.items() if k in cfg and isinstance(v, (int, float)) and not isinstance(v, bool)})

    st: dict[str, Any] = dict(state or {})
    windows_state: dict[str, dict[str, Any]] = dict(st.get("windows") or {})
    emitted_keys = set(st.get("emitted_keys") or [])
    missed_warnings = list(st.get("missed_warnings") or [])
    events: list[dict[str, Any]] = []
    observed_level = "healthy"
    observed_rank = 0

    if not sample.get("available", True):
        observed_level = "critical"
        observed_rank = BAND_RANKS["critical"]
        provider = str(sample.get("provider") or "provider")
        reason = _truncate(sample.get("error") or "provider budget probe unavailable", 160)
        key = state_key(provider, "availability", "availability", "critical")
        if key not in emitted_keys:
            emitted_keys.add(key)
            events.append({
                "provider": provider,
                "window": "availability",
                "band": "critical",
                "previous_band": "healthy",
                "transition": "healthy_to_critical",
                "metric_family": "availability",
                "reason": reason,
                "state_key": key,
                "reset_identity": "availability",
                "values": {"provider": provider, "available": False, "error": reason},
            })

    for provider in sample.get("providers") or []:
        if not isinstance(provider, dict):
            continue
        provider_name = _truncate(provider.get("provider") or "unknown", 80)
        if provider.get("available") is False:
            if BAND_RANKS["critical"] > observed_rank:
                observed_level = "critical"
                observed_rank = BAND_RANKS["critical"]
            reason = _truncate(provider.get("error") or "provider budget unavailable", 160)
            key = state_key(provider_name, "availability", "availability", "critical")
            if key not in emitted_keys:
                emitted_keys.add(key)
                events.append({
                    "provider": provider_name,
                    "window": "availability",
                    "band": "critical",
                    "previous_band": "healthy",
                    "transition": "healthy_to_critical",
                    "metric_family": "availability",
                    "reason": reason,
                    "state_key": key,
                    "reset_identity": "availability",
                    "values": {"provider": provider_name, "available": False, "error": reason},
                })
        for raw_window in provider.get("windows") or []:
            if not isinstance(raw_window, dict):
                continue
            window_name = _truncate(raw_window.get("window") or "unknown", 40)
            window = dict(raw_window)
            window["window"] = window_name
            if window.get("reset_after_seconds") is None and window.get("reset_at") and sample.get("generated_at"):
                reset_after = _seconds_until(window.get("reset_at"), sample.get("generated_at"))
                if reset_after is not None:
                    window["reset_after_seconds"] = reset_after
            window.setdefault("window_seconds", WINDOW_SECONDS.get(window_name))
            band, reason, values = _classify_window(provider, window, cfg)
            current_rank = BAND_RANKS.get(band, 0)
            if current_rank > observed_rank:
                observed_level = band
                observed_rank = current_rank
            reset_id = reset_identity(window)
            slot = f"{provider_name}:{window_name}"
            previous = dict(windows_state.get(slot) or {})
            previous_band = previous.get("band", "healthy")
            previous_reset = previous.get("reset_identity")
            previous_rank = BAND_RANKS.get(previous_band, 0)
            emit_reason = ""

            if previous_reset and previous_reset != reset_id and previous_rank > 0:
                reset_key = state_key(provider_name, window_name, previous_reset, "recovered")
                if reset_key not in emitted_keys:
                    emitted_keys.add(reset_key)
                    events.append({
                        "provider": provider_name,
                        "window": window_name,
                        "band": "healthy",
                        "previous_band": previous_band,
                        "transition": f"{previous_band}_to_recovered",
                        "metric_family": f"{window_name}_reset",
                        "reason": f"{window_name} reset/recovered from {previous_band}; previous reset {previous_reset}",
                        "state_key": reset_key,
                        "reset_identity": previous_reset,
                        "values": {**values, "reset_identity": previous_reset},
                    })
                previous_band = "healthy"
                previous_rank = 0

            key = state_key(provider_name, window_name, reset_id, band)
            if current_rank > previous_rank:
                emit_reason = "upward_crossing"
            elif current_rank < previous_rank:
                emit_reason = "recovery"
            elif current_rank > 0 and key not in emitted_keys:
                emit_reason = "first_observation"

            missed = []
            if previous_rank == 0 and current_rank >= BAND_RANKS["critical"]:
                missed = _missed_bands_for_first_observation(band)
                if missed:
                    missed_record = {
                        "provider": provider_name,
                        "window": window_name,
                        "reset_identity": reset_id,
                        "observed_band": band,
                        "missed_bands": missed,
                    }
                    if missed_record not in missed_warnings:
                        missed_warnings.append(missed_record)

            if emit_reason and key not in emitted_keys:
                emitted_keys.add(key)
                transition = f"{previous_band}_to_{band}" if band != "healthy" else f"{previous_band}_to_recovered"
                metric_family = f"{window_name}_{band}" if band != "healthy" else f"{window_name}_recovery"
                events.append({
                    "provider": provider_name,
                    "window": window_name,
                    "band": band,
                    "previous_band": previous_band,
                    "transition": transition,
                    "metric_family": metric_family,
                    "reason": reason,
                    "state_key": key,
                    "reset_identity": reset_id,
                    "missed_warning": bool(missed),
                    "missed_bands": missed,
                    "emit_reason": emit_reason,
                    "values": {**values, "reset_identity": reset_id},
                })

            windows_state[slot] = {
                "provider": provider_name,
                "window": window_name,
                "band": band,
                "reset_identity": reset_id,
                "last_values": values,
            }

    events.sort(
        key=lambda event: (
            BAND_RANKS.get(str(event.get("band") or "healthy"), 0),
            event.get("window") == "weekly",
        ),
        reverse=True,
    )
    st.update({
        "windows": windows_state,
        "emitted_keys": sorted(emitted_keys)[-500:],
        "missed_warnings": missed_warnings[-100:],
        "last_generated_at": sample.get("generated_at") or "",
        "last_events": events,
        "level": observed_level,
    })
    return events, st


def minimax_budget_provider(minimax: dict[str, Any], *, generated_at: str | None = None) -> dict[str, Any]:
    """Sanitize the MiniMax probe payload into a generic provider budget row."""
    raw_rate_limits = minimax.get("rateLimits")
    rl: dict[str, Any] = raw_rate_limits if isinstance(raw_rate_limits, dict) else {}
    return {
        "provider": "minimax",
        "available": bool(minimax.get("available")),
        "selected_model": _truncate(minimax.get("selected_model") or "", 80),
        "source": _truncate(minimax.get("source") or "", 80),
        "error": _truncate(minimax.get("error") or "", 120),
        "windows": [
            {
                "window": "5h",
                "used_percent": rl.get("fiveHourPercent"),
                "status": rl.get("fiveHourStatus"),
                "reset_at": _truncate(rl.get("fiveHourResetsAt") or "", 80),
                "window_seconds": WINDOW_SECONDS["5h"],
                "reset_after_seconds": _seconds_until(rl.get("fiveHourResetsAt"), generated_at),
            },
            {
                "window": "weekly",
                "used_percent": rl.get("weeklyPercent"),
                "status": rl.get("weeklyStatus"),
                "reset_at": _truncate(rl.get("weeklyResetsAt") or "", 80),
                "window_seconds": WINDOW_SECONDS["weekly"],
                "reset_after_seconds": _seconds_until(rl.get("weeklyResetsAt"), generated_at),
            },
        ],
    }


def provider_budget_sample_from_usage_payload(data: dict[str, Any]) -> dict[str, Any]:
    minimax = data.get("minimax") if isinstance(data.get("minimax"), dict) else {}
    generated_at = _truncate(data.get("generated_at") or "", 80)
    providers = []
    if minimax:
        providers.append(minimax_budget_provider(minimax, generated_at=generated_at))
    return {
        "available": True,
        "generated_at": generated_at,
        "providers": providers,
    }


def collect_provider_budget_sample(*, probe_path: str | None = None, timeout_seconds: int = 45) -> dict[str, Any]:
    """Run the read-only subscription usage probe and return compact budget sample."""
    path = Path(probe_path or (Path.home() / ".hermes" / "scripts" / "subscription_usage_probe.py"))
    if not path.exists():
        return {"available": False, "provider": "provider_budget", "error": "usage_probe_missing", "probe_path": str(path)}
    try:
        proc = subprocess.run(
            [sys.executable, str(path), "--json"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return {"available": False, "provider": "provider_budget", "error": "usage_probe_timeout", "probe_path": str(path)}
    except Exception as exc:
        return {"available": False, "provider": "provider_budget", "error": f"usage_probe_error:{type(exc).__name__}", "probe_path": str(path)}
    if proc.returncode != 0:
        return {"available": False, "provider": "provider_budget", "error": "usage_probe_failed", "probe_path": str(path)}
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"available": False, "provider": "provider_budget", "error": "usage_probe_bad_json", "probe_path": str(path)}
    if not isinstance(payload, dict):
        return {"available": False, "provider": "provider_budget", "error": "usage_probe_bad_shape", "probe_path": str(path)}
    return provider_budget_sample_from_usage_payload(payload)


def format_budget_event(event: dict[str, Any]) -> str:
    raw_values = event.get("values")
    values: dict[str, Any] = raw_values if isinstance(raw_values, dict) else {}
    reset = values.get("reset_at") or "unknown"
    status = values.get("status")
    used = values.get("used_percent")
    provider = event.get("provider") or "provider"
    window = event.get("window") or "window"
    band = event.get("band") or "pressure"
    message = f"{provider} {window} {band}: {used:.0f}% used" if isinstance(used, (int, float)) else f"{provider} {window} {band}"
    if status not in (None, ""):
        message += f"; status={status}"
    if band == "exhausted" or reset != "unknown":
        message += f"; resets {reset}"
    if event.get("missed_warning"):
        message += f"; first observation already {band}, missed earlier bands={','.join(event.get('missed_bands') or [])}"
    return message
