"""Sensorium Memory Reflection Layer (Subconscious-owned, slow, not a live sensor).

This module runs configurable, hot-loaded recurring Hindsight reflect/recall
probes as small internal-thinking sessions. Each probe looks at bounded
historical Sensorium pressure/context, calls Hindsight through an injectable
client seam, and reduces the raw output into compact Sensorium signals that flow
through the ordinary ingest path.

Hard constraints honored here:
  * Out-of-band only. Nothing in this module is registered as a model-visible
    tool, and it must never be added to the compact live `sensorium` tool schema.
    Admin/config is local config + CLI/script/dashboard surfaces.
  * Reflection notices; it does not act. The layer emits compact signals only —
    no outbound delivery, media, worker dispatch, or autonomous messages.
  * Raw reflect output never appears in live status/open/tool responses. Raw is
    stored locally under a raw artifact ref; emitted signals carry only a
    reduced summary plus the ref.
  * Operational claims from reflect output stay flagged unverified until live
    tools verify them later.
  * This is Subconscious-tier, not a cheap deterministic sensor. It is distinct
    from `hindsight_pressure_sample`, which is a quantitative queue-pressure
    sensor that never calls reflect/recall.
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

from .http_urls import validate_http_endpoint_url
from .schemas import VALID_SENSITIVITIES, truncate_text, utc_now_iso

# --- Stable identifiers for the emitted compact signal --------------------------

MEMORY_REFLECTION_SENSOR = "sensorium.memory_reflection"
MEMORY_REFLECTION_SOURCE = "memory"
# Deliberately NOT in subconscious.DIRECT_CONSCIOUS_KINDS: semantic reflection
# must enter the candidate queue for Subconscious review, never promote blind.
MEMORY_REFLECTION_KIND = "memory_reflection"

CONFIG_FILENAME = "memory_reflection.json"
_RAW_SUBDIR = Path("memory_reflection") / "raw"
_HISTORY_REL = Path("memory_reflection") / "history.jsonl"

VALID_MODES = {"reflect", "recall", "recall_then_reflect"}

# Bounds (fail-closed ceilings; per-probe values are clamped into these ranges).
MAX_TIMEOUT_S = 180.0
MAX_RAW_CHARS = 200_000
MAX_SUMMARY_CHARS_CEIL = 700
MAX_SIGNALS_CEIL = 5
MIN_COOLDOWN_HOURS = 0.0

# Fields that must never survive reduction into an emitted/persisted signal.
RAW_FORBIDDEN_SIGNAL_FIELDS = frozenset({
    "raw",
    "raw_text",
    "raw_output",
    "transcript",
    "session_log",
    "memory_dump",
    "full_text",
    "fulltext",
    "documents",
    "memories",
    "messages",
    "body",
    "content",
    "text",
})

# Strength used for a require_delta "nothing new" liveness marker. Stays well
# below promotion thresholds so it proves the channel is alive without creating
# pressure.
LIVENESS_STRENGTH = 0.15

_DEFAULT_PROBE_DEFAULTS = {
    "mode": "reflect",
    "timeout_s": 90.0,
    "max_raw_chars": 6000,
    "max_summary_chars": 400,
    "max_signals": 3,
    "strength_hint": 0.55,
    "sensitivity": "private",
    "allowed_surfaces": ["local"],
    "sensitivity_floor": "private",
    "cooldown_hours": 20.0,
    "require_delta": True,
    "event_gated": False,
    "low_significance_liveness": True,
}


def _now_dt(now: str | None) -> datetime:
    if now:
        try:
            return datetime.fromisoformat(now.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            pass
    return datetime.now(timezone.utc)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _clamp(value, lo: float, hi: float, default: float) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, numeric))


def _safe_sensitivity(value, default: str = "private") -> str:
    return value if value in VALID_SENSITIVITIES else default


def _safe_surfaces(value, default: list[str]) -> list[str]:
    if not isinstance(value, list):
        return list(default)
    cleaned = sorted({s.strip() for s in value if isinstance(s, str) and s.strip()})
    return cleaned or list(default)


# --- Config model ---------------------------------------------------------------


@dataclass
class ProbeConfig:
    """One validated, hot-loadable memory-reflection probe."""

    id: str
    query: str
    enabled: bool = True
    mode: str = "reflect"
    cadence_hours: float = 24.0
    cooldown_hours: float = 20.0
    timeout_s: float = 90.0
    max_raw_chars: int = 6000
    max_summary_chars: int = 400
    max_signals: int = 3
    strength_hint: float = 0.55
    sensitivity: str = "private"
    allowed_surfaces: list[str] = field(default_factory=lambda: ["local"])
    correlation_keys: list[str] = field(default_factory=list)
    event_gated: bool = False
    require_delta: bool = True
    low_significance_liveness: bool = True
    recall_limit: int = 10

    def primary_correlation_key(self) -> str:
        return f"memory-reflection:{self.id}"

    def signal_correlation_keys(self) -> list[str]:
        keys = [self.primary_correlation_key()]
        for key in self.correlation_keys:
            if key and key not in keys:
                keys.append(key)
        return keys


@dataclass
class MemoryReflectionConfig:
    enabled: bool
    probes: list[ProbeConfig]
    errors: list[dict]
    source: str
    path: str | None

    def enabled_probes(self) -> list[ProbeConfig]:
        if not self.enabled:
            return []
        return [p for p in self.probes if p.enabled]

    def probe(self, probe_id: str) -> ProbeConfig | None:
        return next((p for p in self.probes if p.id == probe_id), None)


def config_path_for(state_dir: str | None, config_path: str | None = None) -> Path | None:
    """Resolve the memory-reflection config path.

    Order: explicit config_path, then {state_dir}/memory_reflection.json.
    """
    if config_path:
        return Path(config_path)
    if state_dir:
        return Path(state_dir) / CONFIG_FILENAME
    return None


def _validate_probe(raw: dict, defaults: dict, index: int) -> tuple[ProbeConfig | None, dict | None]:
    if not isinstance(raw, dict):
        return None, {"index": index, "error": "probe must be an object"}

    probe_id = raw.get("id")
    if not isinstance(probe_id, str) or not probe_id.strip():
        return None, {"index": index, "error": "probe missing non-empty 'id'"}
    probe_id = probe_id.strip()

    query = raw.get("query")
    if not isinstance(query, str) or not query.strip():
        return None, {"id": probe_id, "error": "probe missing non-empty 'query'"}

    mode = raw.get("mode", defaults.get("mode", "reflect"))
    if mode not in VALID_MODES:
        return None, {"id": probe_id, "error": f"invalid mode '{mode}', must be one of {sorted(VALID_MODES)}"}

    cadence_hours = _cadence_hours(raw.get("cadence"))
    if cadence_hours is None:
        return None, {"id": probe_id, "error": "invalid or missing cadence (need {'type':'interval','hours':N})"}

    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        enabled = True

    probe = ProbeConfig(
        id=probe_id,
        query=query.strip(),
        enabled=enabled,
        mode=mode,
        cadence_hours=cadence_hours,
        cooldown_hours=_clamp(
            raw.get("cooldown_hours", defaults.get("cooldown_hours", 20.0)),
            MIN_COOLDOWN_HOURS, 24.0 * 14, float(defaults.get("cooldown_hours", 20.0)),
        ),
        timeout_s=_clamp(
            raw.get("timeout_s", defaults.get("timeout_s", 90.0)),
            1.0, MAX_TIMEOUT_S, float(defaults.get("timeout_s", 90.0)),
        ),
        max_raw_chars=int(_clamp(
            raw.get("max_raw_chars", defaults.get("max_raw_chars", 6000)),
            256, MAX_RAW_CHARS, float(defaults.get("max_raw_chars", 6000)),
        )),
        max_summary_chars=int(_clamp(
            raw.get("max_summary_chars", defaults.get("max_summary_chars", 400)),
            40, MAX_SUMMARY_CHARS_CEIL, float(defaults.get("max_summary_chars", 400)),
        )),
        max_signals=int(_clamp(
            raw.get("max_signals", defaults.get("max_signals", 3)),
            1, MAX_SIGNALS_CEIL, float(defaults.get("max_signals", 3)),
        )),
        strength_hint=_clamp(
            raw.get("strength_hint", defaults.get("strength_hint", 0.55)),
            0.0, 1.0, float(defaults.get("strength_hint", 0.55)),
        ),
        sensitivity=_safe_sensitivity(
            raw.get("sensitivity", defaults.get("sensitivity", "private")),
            default=_safe_sensitivity(defaults.get("sensitivity_floor", "private")),
        ),
        allowed_surfaces=_safe_surfaces(
            raw.get("allowed_surfaces", defaults.get("allowed_surfaces", ["local"])),
            default=defaults.get("allowed_surfaces", ["local"]),
        ),
        correlation_keys=_safe_surfaces(raw.get("correlation_keys", []), default=[]),
        event_gated=bool(raw.get("event_gated", defaults.get("event_gated", False))),
        require_delta=bool(raw.get("require_delta", defaults.get("require_delta", True))),
        low_significance_liveness=bool(
            raw.get("low_significance_liveness", defaults.get("low_significance_liveness", True))
        ),
        recall_limit=int(_clamp(raw.get("recall_limit", 10), 1, 50, 10)),
    )
    return probe, None


def _cadence_hours(cadence) -> float | None:
    if not isinstance(cadence, dict):
        return None
    if cadence.get("type") != "interval":
        return None
    hours = cadence.get("hours")
    try:
        value = float(hours)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return min(value, 24.0 * 30)


def load_config(*, state_dir: str | None, config_path: str | None = None) -> MemoryReflectionConfig:
    """Load and validate the probe config FRESH from disk (hot-load).

    This intentionally never caches across calls: each run re-reads the file so
    add/remove/update of probes takes effect without any gateway/plugin restart.
    A single invalid probe disables only that probe and is recorded in errors;
    the rest still load.
    """
    path = config_path_for(state_dir, config_path)
    if path is None or not path.exists():
        return MemoryReflectionConfig(
            enabled=False, probes=[], errors=[], source="missing", path=str(path) if path else None
        )

    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return MemoryReflectionConfig(
            enabled=False,
            probes=[],
            errors=[{"error": f"config_unreadable:{type(exc).__name__}"}],
            source="unreadable",
            path=str(path),
        )

    if not isinstance(raw, dict):
        return MemoryReflectionConfig(
            enabled=False, probes=[], errors=[{"error": "config root must be an object"}],
            source="invalid", path=str(path),
        )

    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        enabled = False

    defaults = dict(_DEFAULT_PROBE_DEFAULTS)
    raw_defaults = raw.get("defaults")
    if isinstance(raw_defaults, dict):
        for key, value in raw_defaults.items():
            if key in defaults:
                defaults[key] = value

    probes: list[ProbeConfig] = []
    errors: list[dict] = []
    seen_ids: set[str] = set()
    raw_probes = raw.get("probes")
    if not isinstance(raw_probes, list):
        raw_probes = []
    for index, raw_probe in enumerate(raw_probes):
        probe, err = _validate_probe(raw_probe, defaults, index)
        if err is not None:
            errors.append(err)
            continue
        if probe.id in seen_ids:
            errors.append({"id": probe.id, "error": "duplicate probe id"})
            continue
        seen_ids.add(probe.id)
        probes.append(probe)

    return MemoryReflectionConfig(
        enabled=enabled, probes=probes, errors=errors, source="file", path=str(path)
    )


# --- Hindsight client seam -------------------------------------------------------


class HindsightMemoryClient(Protocol):
    """Injectable Hindsight memory client.

    Implementations must return a plain dict of raw output. The reducer is the
    only thing that decides what survives into a compact signal, so clients are
    free to return rich payloads — none of it reaches live context directly.
    """

    def reflect(self, *, query: str, timeout_s: float) -> dict: ...

    def recall(self, *, query: str, limit: int, timeout_s: float) -> dict: ...

    def source_scope(self) -> dict: ...


class HttpHindsightMemoryClient:
    """Dependency-free HTTP client for a local Hindsight instance.

    Only used for out-of-band/live runs; tests use a fake. Endpoints are best
    effort and configurable; this module never asserts a live Hindsight exists.
    """

    def __init__(self, *, base_url: str = "http://localhost:8888", bank_id: str = "hermes"):
        self.base = base_url.rstrip("/")
        self.bank_id = bank_id.strip()

    def source_scope(self) -> dict:
        """Trusted adapter-owned scope; response bodies cannot override it."""
        return {"provider": "hindsight", "bank_id": self.bank_id}

    def _post_json(self, path: str, payload: dict, *, timeout_s: float) -> dict:
        data = json.dumps(payload).encode("utf-8")
        url = f"{self.base}{path}"
        validate_http_endpoint_url(url)
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json", "Accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 - localhost admin API by config
            body = resp.read(MAX_RAW_CHARS).decode("utf-8", errors="ignore") or "{}"
        parsed = json.loads(body)
        return parsed if isinstance(parsed, dict) else {"result": parsed}

    def reflect(self, *, query: str, timeout_s: float) -> dict:
        bank = urllib.parse.quote(self.bank_id)
        return self._post_json(
            f"/v1/default/banks/{bank}/reflect",
            {"query": query, "include": {"facts": {}}},
            timeout_s=timeout_s,
        )

    def recall(self, *, query: str, limit: int, timeout_s: float) -> dict:
        bank = urllib.parse.quote(self.bank_id)
        return self._post_json(
            f"/v1/default/banks/{bank}/recall", {"query": query, "limit": limit}, timeout_s=timeout_s
        )


class FakeHindsightMemoryClient:
    """Deterministic in-memory client for tests/dry-run smoke."""

    def __init__(self, *, reflect_result: dict | None = None, recall_result: dict | None = None,
                 error: Exception | None = None, bank_id: str = "synthetic-hindsight"):
        self.reflect_result = reflect_result or {}
        self.recall_result = recall_result or {}
        self.error = error
        self.bank_id = bank_id.strip()
        self.calls: list[dict] = []

    def source_scope(self) -> dict:
        return {"provider": "hindsight", "bank_id": self.bank_id}

    def reflect(self, *, query: str, timeout_s: float) -> dict:
        self.calls.append({"op": "reflect", "query": query, "timeout_s": timeout_s})
        if self.error is not None:
            raise self.error
        return dict(self.reflect_result)

    def recall(self, *, query: str, limit: int, timeout_s: float) -> dict:
        self.calls.append({"op": "recall", "query": query, "limit": limit, "timeout_s": timeout_s})
        if self.error is not None:
            raise self.error
        return dict(self.recall_result)


# --- Raw output local-ref storage ------------------------------------------------


def _raw_dir(state_dir: str) -> Path:
    return Path(state_dir) / _RAW_SUBDIR


def store_raw_output(
    *, state_dir: str, probe_id: str, raw_output: dict, now: str, max_raw_chars: int
) -> dict:
    """Persist bounded raw reflect output locally and return only a compact ref.

    The returned ref carries a path, byte size, and content hash — never the raw
    body. Callers attach this ref to emitted signals so raw is recoverable
    locally by an operator without ever entering live context.
    """
    raw_dir = _raw_dir(state_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(raw_output, ensure_ascii=False, sort_keys=True)[:max_raw_chars]
    digest = hashlib.sha256(serialized.encode("utf-8", errors="ignore")).hexdigest()
    safe_id = "".join(c if c.isalnum() or c in "-_." else "_" for c in probe_id)[:60]
    stamp = now.replace(":", "").replace("-", "")
    filename = f"{safe_id}.{stamp}.{digest[:12]}.json"
    path = raw_dir / filename
    path.write_text(serialized)
    return {
        "raw_ref": str(path),
        "raw_bytes": len(serialized),
        "raw_sha256": digest[:32],
        "truncated": len(json.dumps(raw_output, ensure_ascii=False)) > max_raw_chars,
    }


# --- Reducer ---------------------------------------------------------------------


_SUMMARY_KEYS = ("synthesis", "summary", "answer", "reflection", "result", "text")
_POINTS_KEYS = ("insights", "points", "items", "highlights", "findings", "memories")


def _extract_points(raw_output: dict) -> list[str]:
    points: list[str] = []
    for key in _POINTS_KEYS:
        value = raw_output.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item.strip():
                    points.append(item.strip())
                elif isinstance(item, dict):
                    for candidate_key in ("summary", "text", "point", "title", "fact"):
                        candidate = item.get(candidate_key)
                        if isinstance(candidate, str) and candidate.strip():
                            points.append(candidate.strip())
                            break
        if points:
            break
    return points


def _identity_component(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value and len(value) <= 512 else None


def _validated_source_scope(value: object) -> dict | None:
    if not isinstance(value, dict) or set(value) != {"provider", "bank_id"}:
        return None
    provider = _identity_component(value.get("provider"))
    bank_id = _identity_component(value.get("bank_id"))
    if provider is None or bank_id is None:
        return None
    return {"provider": provider, "bank_id": bank_id}


def _native_records(raw_output: dict) -> list[tuple[str, list[str] | None]]:
    """Extract text plus only exact provider-issued Hindsight item IDs."""
    results = raw_output.get("results")
    if isinstance(results, list) and results:
        records: list[tuple[str, list[str] | None]] = []
        ids: list[str] = []
        valid = True
        for item in results:
            if not isinstance(item, dict):
                valid = False
                continue
            text = item.get("text")
            if not isinstance(text, str) or not text.strip():
                valid = False
                continue
            item_id = _identity_component(item.get("id"))
            if item_id is None:
                valid = False
            else:
                ids.append(item_id)
            records.append((text.strip(), [item_id] if item_id is not None else None))
        if len(ids) != len(set(ids)):
            valid = False
        return records if valid else [(text, None) for text, _ in records]

    summary = _extract_summary(raw_output)
    based_on = raw_output.get("based_on")
    if summary and isinstance(based_on, dict) and "memories" in based_on:
        memories = based_on.get("memories")
        if isinstance(memories, list) and memories:
            ids = [
                _identity_component(item.get("id")) if isinstance(item, dict) else None
                for item in memories
            ]
            valid_ids = [item_id for item_id in ids if item_id is not None]
            if len(valid_ids) == len(ids) and len(valid_ids) == len(set(valid_ids)):
                return [(summary, sorted(valid_ids))]
        return [(summary, None)]
    points = _extract_points(raw_output)
    if points:
        return [(point, None) for point in points]
    return [(summary, None)] if summary else []


def _extract_summary(raw_output: dict) -> str:
    for key in _SUMMARY_KEYS:
        value = raw_output.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def reflection_fingerprint(
    probe_id: str,
    summaries: list[str],
    provenance_units: list[tuple[str, str, tuple[str, ...]]] | None = None,
) -> str:
    material = json.dumps(
        {"probe": probe_id, **(
            {"native_items": sorted(provenance_units)} if provenance_units is not None
            else {"summaries": summaries}
        )},
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8", errors="ignore")).hexdigest()[:16]


def _strip_forbidden(signal: dict) -> dict:
    """Defense in depth: guarantee no raw field can ride along on a signal."""
    return {k: v for k, v in signal.items() if k not in RAW_FORBIDDEN_SIGNAL_FIELDS}


def reduce_reflection(
    *,
    raw_output: dict,
    probe: ProbeConfig,
    raw_ref: dict,
    now: str,
    source_scope: dict | None = None,
) -> tuple[list[dict], str]:
    """Reduce raw reflect/recall output into <= max_signals compact signals.

    Returns (signals, fingerprint). Each signal carries only a truncated summary,
    correlation keys, sensitivity/surfaces, the raw ref, and an unverified flag.
    No raw transcript/memory text is ever included.
    """
    del now
    # Validate identity over the complete relevant provider response before the
    # output bound is applied.  Otherwise an unidentified tail could be hidden
    # behind an apparently complete prefix.
    all_records = _native_records(raw_output)
    records = all_records[: probe.max_signals]
    chosen = [text for text, _ in records]
    scope = _validated_source_scope(source_scope)
    provenance_units = None
    identity_complete = bool(
        scope is not None
        and all_records
        and all(item_ids is not None for _, item_ids in all_records)
    )
    if identity_complete:
        assert scope is not None
        provenance_units = [
            (scope["provider"], scope["bank_id"], tuple(sorted(item_ids or [])))
            for _, item_ids in records
        ]
    fingerprint = reflection_fingerprint(probe.id, chosen, provenance_units)

    signals: list[dict] = []
    for text, item_ids in records:
        signal = {
            "sensor": MEMORY_REFLECTION_SENSOR,
            "source": MEMORY_REFLECTION_SOURCE,
            "kind": MEMORY_REFLECTION_KIND,
            "summary": truncate_text(text, probe.max_summary_chars),
            "actor": "tool",
            "strength_hint": probe.strength_hint,
            "sensitivity": probe.sensitivity,
            "allowed_surfaces": list(probe.allowed_surfaces),
            "correlation_keys": probe.signal_correlation_keys(),
            "scope": "memory_reflection",
            "probe_id": probe.id,
            "probe_mode": probe.mode,
            "raw_ref": raw_ref.get("raw_ref", ""),
            "raw_sha256": raw_ref.get("raw_sha256", ""),
            "reflection_fingerprint": fingerprint,
            "source_identity_status": "complete" if identity_complete else "unsupported",
            "unverified": True,
        }
        if identity_complete and scope is not None and item_ids is not None:
            canonical_ids = sorted(item_ids)
            signal["memory_provenance"] = {
                "provider": scope["provider"],
                "bank_id": scope["bank_id"],
                "item_ids": canonical_ids,
            }
            equality_material = json.dumps(
                [scope["provider"], scope["bank_id"], *canonical_ids],
                ensure_ascii=False, separators=(",", ":"),
            )
            equality_key = hashlib.sha256(equality_material.encode("utf-8")).hexdigest()[:32]
            signal["correlation_keys"] = [
                *signal["correlation_keys"], f"memory-source-item:{equality_key}",
            ]
        signals.append(_strip_forbidden(signal))
    return signals, fingerprint


def liveness_signal(*, probe: ProbeConfig, fingerprint: str, raw_ref: dict) -> dict:
    """Compact 'nothing new' liveness marker (low strength, won't promote)."""
    return _strip_forbidden({
        "sensor": MEMORY_REFLECTION_SENSOR,
        "source": MEMORY_REFLECTION_SOURCE,
        "kind": MEMORY_REFLECTION_KIND,
        "summary": truncate_text(f"Memory reflection probe '{probe.id}': nothing new", probe.max_summary_chars),
        "actor": "tool",
        "strength_hint": LIVENESS_STRENGTH,
        "sensitivity": probe.sensitivity,
        "allowed_surfaces": list(probe.allowed_surfaces),
        "correlation_keys": probe.signal_correlation_keys(),
        "scope": "memory_reflection",
        "probe_id": probe.id,
        "probe_mode": probe.mode,
        "raw_ref": raw_ref.get("raw_ref", ""),
        "reflection_fingerprint": fingerprint,
        "liveness": True,
        "unverified": True,
    })


# --- Run history / due detection -------------------------------------------------


def _history_path(state_dir: str) -> Path:
    return Path(state_dir) / _HISTORY_REL


def read_history(state_dir: str) -> list[dict]:
    path = _history_path(state_dir)
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def append_history(state_dir: str, record: dict) -> None:
    path = _history_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(record, separators=(",", ":")) + "\n")


def last_run_for(history: list[dict], probe_id: str) -> dict | None:
    runs = [r for r in history if r.get("probe_id") == probe_id and r.get("completed_at")]
    if not runs:
        return None
    return max(runs, key=lambda r: r.get("completed_at", ""))


def is_probe_due(probe: ProbeConfig, history: list[dict], *, now: str) -> tuple[bool, str]:
    """Cadence + cooldown gate. Hot-reloaded cadence applies from now forward.

    A probe is due only after both the cadence interval and the cooldown have
    elapsed since its last completed run.
    """
    last = last_run_for(history, probe.id)
    if last is None:
        return True, "no_prior_run"
    last_dt = _parse_iso(last.get("completed_at"))
    if last_dt is None:
        return True, "unparseable_last_run"
    elapsed_hours = (_now_dt(now) - last_dt).total_seconds() / 3600.0
    gap_needed = max(probe.cadence_hours, probe.cooldown_hours)
    if elapsed_hours >= gap_needed:
        return True, f"elapsed {elapsed_hours:.2f}h >= {gap_needed:.2f}h"
    return False, f"cooldown: elapsed {elapsed_hours:.2f}h < {gap_needed:.2f}h"


# --- Orchestration ---------------------------------------------------------------


def _call_client(client: HindsightMemoryClient, probe: ProbeConfig) -> dict:
    if probe.mode == "recall":
        return client.recall(query=probe.query, limit=probe.recall_limit, timeout_s=probe.timeout_s)
    if probe.mode == "recall_then_reflect":
        recalled = client.recall(query=probe.query, limit=probe.recall_limit, timeout_s=probe.timeout_s)
        reflected = client.reflect(query=probe.query, timeout_s=probe.timeout_s)
        return {"recall": recalled, **reflected}
    return client.reflect(query=probe.query, timeout_s=probe.timeout_s)


def run_probe(
    *,
    probe: ProbeConfig,
    client: HindsightMemoryClient,
    state_dir: str,
    history: list[dict],
    now: str,
    dry_run: bool,
    ingest_fn: Callable[[dict], dict] | None = None,
) -> dict:
    """Run one probe: call Hindsight, reduce, store raw, optionally ingest.

    Returns a compact run record (also the shape persisted to history). On
    dry_run, no raw file, history, or ingest is written. Emitted signal summaries
    are included so an out-of-band caller can inspect them, but raw is never
    returned inline.
    """
    started_at = now
    record: dict = {
        "probe_id": probe.id,
        "mode": probe.mode,
        "started_at": started_at,
        "dry_run": dry_run,
    }
    # One probe's failure (Hindsight unreachable, disk write, ingest, reduce) must
    # never escape to abort the whole tick step. Any failure is isolated into a
    # per-probe error record; sibling probes and later tick steps proceed.
    try:
        raw_output = _call_client(client, probe)
        if not isinstance(raw_output, dict):
            raw_output = {"result": raw_output}

        if dry_run:
            # Hold raw only in-memory; compute a ref-shaped placeholder without write.
            serialized = json.dumps(raw_output, ensure_ascii=False, sort_keys=True)[: probe.max_raw_chars]
            raw_ref = {
                "raw_ref": "",
                "raw_bytes": len(serialized),
                "raw_sha256": hashlib.sha256(serialized.encode("utf-8", errors="ignore")).hexdigest()[:32],
                "truncated": False,
            }
        else:
            raw_ref = store_raw_output(
                state_dir=state_dir,
                probe_id=probe.id,
                raw_output=raw_output,
                now=now,
                max_raw_chars=probe.max_raw_chars,
            )

        scope_fn = getattr(client, "source_scope", None)
        maybe_scope = scope_fn() if callable(scope_fn) else None
        source_scope: dict | None = maybe_scope if isinstance(maybe_scope, dict) else None
        signals, fingerprint = reduce_reflection(
            raw_output=raw_output, probe=probe, raw_ref=raw_ref, now=now,
            source_scope=source_scope,
        )

        source_identity_status = (
            "empty" if not signals
            else "complete" if all(
                signal.get("source_identity_status") == "complete" for signal in signals
            )
            else "unsupported"
        )
        last = last_run_for(history, probe.id)
        prior_fp = (last or {}).get("fingerprint")
        delta = fingerprint != prior_fp

        emitted = signals
        emit_reason = "delta" if delta else "no_delta"
        # Unsupported native output is visible but creates no Signal/Event/
        # candidate pressure.  This precedes delta and liveness handling so
        # force, rewording, require_delta=False, and liveness cannot bypass it.
        if source_identity_status == "unsupported":
            delta = False
            emitted = []
            emit_reason = "unsupported_source_identity"
        elif signals and probe.require_delta and not delta:
            if probe.low_significance_liveness:
                emitted = [liveness_signal(probe=probe, fingerprint=fingerprint, raw_ref=raw_ref)]
                emit_reason = "liveness_no_delta"
            else:
                emitted = []
                emit_reason = "suppressed_no_delta"
        elif not signals:
            if probe.low_significance_liveness:
                emitted = [liveness_signal(probe=probe, fingerprint=fingerprint, raw_ref=raw_ref)]
                emit_reason = "liveness_empty"
            else:
                emitted = []
                emit_reason = "empty"

        ingest_results: list[dict] = []
        if ingest_fn is not None and not dry_run:
            for signal in emitted:
                ingest_results.append(ingest_fn(signal))

        record.update({
            "status": "unsupported" if source_identity_status == "unsupported" else "ok",
            "source_identity_status": source_identity_status,
            "completed_at": utc_now_iso(),
            "fingerprint": fingerprint,
            "delta": delta,
            "emit_reason": emit_reason,
            "raw_ref": raw_ref.get("raw_ref", ""),
            "raw_sha256": raw_ref.get("raw_sha256", ""),
            "reduced_count": len(signals),
            "emitted_count": len(emitted),
            "emitted_summaries": [s.get("summary", "") for s in emitted],
        })
        if ingest_results:
            record["ingested"] = [
                {"promoted": r.get("data", {}).get("promoted"), "duplicate": r.get("data", {}).get("duplicate")}
                if isinstance(r, dict) else {}
                for r in ingest_results
            ]
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        record["status"] = "error"
        record["error"] = f"{type(exc).__name__}"
        record["completed_at"] = utc_now_iso()
    except Exception as exc:  # noqa: BLE001 - last-resort per-probe isolation
        record["status"] = "error"
        record["error"] = f"unexpected:{type(exc).__name__}"
        record["completed_at"] = utc_now_iso()

    if not dry_run:
        append_history(state_dir, record)
    return record


def run_due_probes(
    *,
    state_dir: str,
    config_path: str | None = None,
    client: HindsightMemoryClient | None = None,
    now: str | None = None,
    dry_run: bool = False,
    only_probe: str | None = None,
    force: bool = False,
    ingest_fn: Callable[[dict], dict] | None = None,
) -> dict:
    """Load fresh config, run due/enabled probes, and return a compact summary.

    Quiet, structured result suitable for a tick step. Never returns raw reflect
    bodies. If `client` is None and any probe would run, a real HTTP client is
    constructed lazily — but callers/tests should inject a client.
    """
    now = now or utc_now_iso()
    config = load_config(state_dir=state_dir, config_path=config_path)
    history = read_history(state_dir)

    result: dict = {
        "enabled": config.enabled,
        "config_source": config.source,
        "config_path": config.path,
        "config_errors": config.errors,
        "due": [],
        "skipped": [],
        "runs": [],
    }
    if not config.enabled:
        result["reason"] = "disabled"
        return result

    candidates = config.enabled_probes()
    if only_probe:
        candidates = [p for p in candidates if p.id == only_probe]

    to_run: list[ProbeConfig] = []
    for probe in candidates:
        if force:
            to_run.append(probe)
            result["due"].append({"probe_id": probe.id, "reason": "forced"})
            continue
        due, reason = is_probe_due(probe, history, now=now)
        if due:
            to_run.append(probe)
            result["due"].append({"probe_id": probe.id, "reason": reason})
        else:
            result["skipped"].append({"probe_id": probe.id, "reason": reason})

    if to_run and client is None:
        client = HttpHindsightMemoryClient()

    for probe in to_run:
        record = run_probe(
            probe=probe,
            client=client,
            state_dir=state_dir,
            history=history,
            now=now,
            dry_run=dry_run,
            ingest_fn=ingest_fn,
        )
        result["runs"].append(record)
        # Make same-run dedupe coherent if two probes share an id-space.
        history = history + [record]

    return result


# --- Probe-audit seam ------------------------------------------------------------

# Inventory descriptor consumed by probe_audit.py. Memory reflection is a
# configured/hot-loadable internal probe family — Subconscious-owned and slow —
# NOT a wired live deterministic sensor and NOT a model-visible tool.
MEMORY_REFLECTION_PROBE_FAMILY = {
    "family": "memory_reflection",
    "sensor": MEMORY_REFLECTION_SENSOR,
    "source": MEMORY_REFLECTION_SOURCE,
    "kind": MEMORY_REFLECTION_KIND,
    "tier": "subconscious",
    "wired_live": False,
    "model_visible_tool": False,
    "hot_loadable": True,
    "config_file": CONFIG_FILENAME,
    "calls_hindsight_reflect_recall": True,
    "notes": (
        "Subconscious-owned recurring Hindsight reflect/recall probe. Hot-loaded "
        "from local config; emits compact reduced signals only; raw stored by "
        "local ref; operational claims unverified until live tools confirm."
    ),
}
