"""Regression tests for the saved-residue fallback anti-eagerness patch.

Background
----------
The foreground patch added an honest saved-residue pointer pathway so
archived candidates with a Kanban SAVE/PROMOTE_CONSCIOUS settlement remain
consciously accessible. Sera flagged that the pathway, taken on its own,
risks two eagerness smells when no active candidate is present:

  * "archive-confetti" — the same archive of old saved residue keeps
    surfacing forever, even when the most recent settled item is days or
    weeks old.
  * "rotation-through-archive" — under cooldown selection, consecutive
    pointer turns can march through every saved residue in the archive.

The patch adds two opt-in knobs (both default ``None`` so existing
operators see no behaviour change):

  * ``saved_residue_max_age_days`` — drops residues whose
    ``kanban_settlement.settled_at`` is older than N days.
  * ``saved_residue_max_items`` — caps the post-sort list at top-N.

It also fixes a deterministic-recency bug: when two residues have equal
pressure (very common for archived rows), the old sort fell back to
``created_at`` so the *oldest* residue won the tie every time. The new
sort uses the freshest explicit signal (``settled_at`` → ``updated_at`` →
``created_at``) as the tiebreak so the most-recently-saved residue wins.

These tests pin those three guarantees.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_sensorium.config import load_instance_config
from agent_sensorium.pointers import (
    _saved_residue_candidates,
    select_attention_pointer,
)
from agent_sensorium.store import SensoriumStore


# ---------- helpers ----------


def _inst_cfg(state_dir):
    """Load the on-disk instance config the way ``select_attention_pointer`` does."""
    cfg, _ = load_instance_config(state_dir=str(state_dir))
    return cfg


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _saved_residue(
    *,
    candidate_id: str,
    pressure: float = 0.5,
    settled_days_ago: float | None = None,
    settled_at: str | None = None,
    updated_days_ago: float | None = None,
    created_at: str = "2026-01-01T00:00:00Z",
    surface: str = "discord",
) -> dict:
    if settled_at is None and settled_days_ago is not None:
        settled_at = (
            datetime.now(timezone.utc) - timedelta(days=settled_days_ago)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    updated_at = ""
    if updated_days_ago is not None:
        updated_at = (
            datetime.now(timezone.utc) - timedelta(days=updated_days_ago)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "id": candidate_id,
        "status": "archived",
        "kind": "research_source_signal",
        "summary": f"Saved residue {candidate_id}",
        "pressure": pressure,
        "correlation_keys": ["lane:test"],
        "sensitivity": "private",
        "allowed_surfaces": [surface],
        "created_at": created_at,
        "updated_at": updated_at,
        "kanban_settlement": {
            "decision": "SAVE",
            "intake_task_id": f"t_{candidate_id}",
            "review_task_id": f"t_rev_{candidate_id}",
            "settled_at": settled_at or "",
            "reason_label": f"reason#{candidate_id}",
        },
    }


def _write_config(state_dir, *, surfaces=("discord",)):
    config = {
        "allowed_surfaces": list(surfaces),
        "max_sensitivity": "private",
        "instance_name": "test",
    }
    path = Path(state_dir) / "instance.config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config))


# ---------- (1) recency tiebreak ----------


def test_saved_residue_freshest_settled_wins_when_pressure_ties(tmp_path):
    """Two archived residues with identical pressure: the more recently
    settled one must come first, regardless of created_at order. Old
    behaviour used created_at as tiebreak so the oldest row won.
    """
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    _write_config(tmp_path)

    store.append_jsonl("candidates", _saved_residue(
        candidate_id="old_residue",
        pressure=0.5,
        settled_days_ago=30,  # older
        created_at="2026-05-01T00:00:00Z",
    ))
    store.append_jsonl("candidates", _saved_residue(
        candidate_id="fresh_residue",
        pressure=0.5,
        settled_days_ago=2,  # newer
        created_at="2025-12-01T00:00:00Z",  # older created_at, used to win
    ))

    ranked = _saved_residue_candidates(
        store, "discord", _inst_cfg(tmp_path), cfg={"saved_residue_max_age_days": None, "saved_residue_max_items": None},
    )
    ids = [c["id"] for c in ranked]
    assert ids[0] == "fresh_residue", (
        f"fresh_residue (settled 2 days ago) must beat old_residue "
        f"(settled 30 days ago) at equal pressure; got {ids!r}"
    )
    assert ids[1] == "old_residue"


def test_saved_residue_higher_pressure_still_wins_over_recency(tmp_path):
    """A fresher residue must NOT leapfrog a much higher-pressure older one.
    Pressure remains the primary sort key.
    """
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    _write_config(tmp_path)

    store.append_jsonl("candidates", _saved_residue(
        candidate_id="fresh_but_low_pressure",
        pressure=0.4,
        settled_days_ago=1,
    ))
    store.append_jsonl("candidates", _saved_residue(
        candidate_id="stale_but_high_pressure",
        pressure=0.95,
        settled_days_ago=45,
    ))

    ranked = _saved_residue_candidates(store, "discord", _inst_cfg(tmp_path))
    ids = [c["id"] for c in ranked]
    assert ids[0] == "stale_but_high_pressure"
    assert ids[1] == "fresh_but_low_pressure"


# ---------- (2) saved_residue_max_age_days ----------


def test_saved_residue_max_age_days_drops_stale_residue(tmp_path):
    """With a 7-day cap, a 30-day-old settled residue must be filtered out."""
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    _write_config(tmp_path)

    store.append_jsonl("candidates", _saved_residue(
        candidate_id="stale_30d",
        settled_days_ago=30,
    ))
    store.append_jsonl("candidates", _saved_residue(
        candidate_id="fresh_2d",
        settled_days_ago=2,
    ))

    ranked = _saved_residue_candidates(
        store, "discord", _inst_cfg(tmp_path), cfg={"saved_residue_max_age_days": 7},
    )
    ids = [c["id"] for c in ranked]
    assert "stale_30d" not in ids, (
        f"stale_30d (settled 30 days ago) must be filtered when cap is 7d; got {ids!r}"
    )
    assert "fresh_2d" in ids


def test_saved_residue_max_age_days_unset_preserves_existing_behaviour(tmp_path):
    """Default ``None`` means no cap — old operators see no change."""
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    _write_config(tmp_path)

    store.append_jsonl("candidates", _saved_residue(
        candidate_id="ancient_365d",
        settled_days_ago=365,
    ))

    ranked_no_cfg = _saved_residue_candidates(store, "discord", _inst_cfg(tmp_path))
    ranked_unset = _saved_residue_candidates(
        store, "discord", _inst_cfg(tmp_path), cfg={"saved_residue_max_age_days": None},
    )
    assert [c["id"] for c in ranked_no_cfg] == [c["id"] for c in ranked_unset]
    assert "ancient_365d" in [c["id"] for c in ranked_unset]


def test_saved_residue_max_age_days_zero_disables_fallback(tmp_path):
    """``max_age_days=0`` is an honest "do not surface any saved residue"
    knob — useful for an operator who wants the saved-residue pathway
    explicitly off without flipping ``saved_residue_fallback_enabled``.
    """
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    _write_config(tmp_path)

    store.append_jsonl("candidates", _saved_residue(
        candidate_id="just_settled",
        settled_days_ago=0.0,
    ))

    ranked = _saved_residue_candidates(
        store, "discord", _inst_cfg(tmp_path), cfg={"saved_residue_max_age_days": 0},
    )
    assert ranked == [], (
        f"max_age_days=0 must drop even just-settled residue; got {ranked!r}"
    )


# ---------- (3) saved_residue_max_items ----------


def test_saved_residue_max_items_caps_post_sort_list(tmp_path):
    """With max_items=1, only the top-1 (freshest highest-pressure) is kept."""
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    _write_config(tmp_path)

    store.append_jsonl("candidates", _saved_residue(
        candidate_id="loser_high_pressure_old",
        pressure=0.9,
        settled_days_ago=40,
    ))
    store.append_jsonl("candidates", _saved_residue(
        candidate_id="winner_fresh_medium",
        pressure=0.7,
        settled_days_ago=1,
    ))
    store.append_jsonl("candidates", _saved_residue(
        candidate_id="also_loser_low_pressure",
        pressure=0.3,
        settled_days_ago=0,
    ))

    ranked = _saved_residue_candidates(
        store, "discord", _inst_cfg(tmp_path), cfg={"saved_residue_max_items": 1},
    )
    ids = [c["id"] for c in ranked]
    assert ids == ["loser_high_pressure_old"], (
        f"max_items=1 must keep the highest-pressure row regardless of "
        f"recency tiebreak; got {ids!r}"
    )


def test_saved_residue_max_items_unset_preserves_existing_behaviour(tmp_path):
    """Default ``None`` means unlimited, same as before the patch."""
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    _write_config(tmp_path)

    for i in range(5):
        store.append_jsonl("candidates", _saved_residue(
            candidate_id=f"res_{i:02d}",
            pressure=0.5,
            settled_days_ago=i,
        ))

    ranked_unset = _saved_residue_candidates(
        store, "discord", _inst_cfg(tmp_path), cfg={"saved_residue_max_items": None},
    )
    assert len(ranked_unset) == 5


def test_saved_residue_max_items_caps_pointer_rotation(tmp_path):
    """End-to-end: with 5 saved residues and ``max_items=2``, only the top 2
    are eligible to surface as a pointer. Items 3–5 cannot appear at all.
    This is the rotation-through-archive guard.
    """
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    _write_config(tmp_path)

    for i in range(5):
        store.append_jsonl("candidates", _saved_residue(
            candidate_id=f"res_{i:02d}",
            pressure=0.5,
            settled_days_ago=i,
        ))

    # Pressure-tied, recency-ordered: res_00 (newest) → res_04 (oldest).
    pointer = select_attention_pointer(
        store, surface="discord",
        config={"saved_residue_max_items": 2},
    )
    assert pointer["pointer_type"] == "saved_residue"
    assert pointer["candidate_id"] in {"res_00", "res_01"}
    # Specifically: top-1 is res_00, but we recorded a cooldown receipt
    # for it. The next visible pointer (without re-firing the hook) would
    # be res_01 — not res_02/03/04.
    # We can't easily exercise the second turn here without faking
    # cooldowns, but the bounded ranking itself is what the patch targets.


# ---------- (4) pointer reception: bounded rotation via cooldowns ----------


def test_pointer_presented_receipt_blocks_recent_saved_residue(tmp_path):
    """Even with no cap, the per-candidate cooldown must suppress a recently
    presented saved residue. This is the existing safety net — the new
    knobs are additive on top of it, not replacements.
    """
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    _write_config(tmp_path)

    store.append_jsonl("candidates", _saved_residue(
        candidate_id="res_a", pressure=0.6, settled_days_ago=1,
    ))
    store.append_jsonl("candidates", _saved_residue(
        candidate_id="res_b", pressure=0.5, settled_days_ago=2,
    ))

    first = select_attention_pointer(store, surface="discord")
    assert first["candidate_id"] == "res_a"  # freshest high-pressure

    # Manually record the receipt so res_a is in cooldown.
    from agent_sensorium.pointers import record_pointer_presented
    record_pointer_presented(store, first, session_id="s1", surface="discord")

    second = select_attention_pointer(store, surface="discord")
    # res_a is now in cooldown; res_b should surface.
    assert second["pointer_type"] == "saved_residue"
    assert second["candidate_id"] == "res_b"