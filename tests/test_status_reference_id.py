"""RED tests for handle_sensorium_status reference_id branch (P0-2 audit fix).

Acceptance criteria for the exact-subject status branch:

  (a) handle_sensorium_status MUST accept an optional ``reference_id`` kwarg
      that, when present, returns an ``exact_subject`` block pinning to that
      specific thread or candidate id (whichever is valid AND surface-allowed).
  (b) ``exact_subject`` MUST carry: kind ("thread"|"candidate"|"saved_residue"),
      id, title, status (when applicable), and kanban_settlement (when applicable).
  (c) When the ``reference_id`` matches no surface-allowed subject, the
      ``exact_subject`` block MUST report ``not_found == True`` so the
      conscious layer can tell the difference between "the pointer presented
      this id" and "the id is not actually visible to me right now".
  (d) Backwards compatibility: when ``reference_id`` is omitted, the existing
      top_threads/top_candidates shape is unchanged. (Test 6 below locks this
      down — it MUST pass today and continue passing after the fix.)

These tests reproduce the live P0-2 audit mismatch where the conscious layer
saw only a rotating top-N and had no way to recover "the subject the prior
pointer presented" without guessing. The fix is a single optional parameter;
the tests below pin the contract.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_sensorium.store import SensoriumStore
from agent_sensorium.settlement import apply_kanban_settlement
from agent_sensorium.tools import handle_sensorium_status


# ---------- helpers (mirror test_pointer_doorway_saved_residue.py) ----------


def _arxiv_candidate(**overrides):
    """Archived research_source_signal candidate with a kanban SAVE settlement."""
    base = {
        "id": "cand_arxiv_status_ref",
        "status": "archived",
        "kind": "research_source_signal",
        "pressure": 0.783,
        "summary": (
            "Research source feed (arXiv cs.AI: agent collaboration / governance): "
            "From Signals to Structure: How Memory Architecture Drives Language "
            "Emergence in LLM Agents. Authors: Talebirad et al. Abstract: arXiv:2607.00233."
        ),
        "correlation_keys": [
            "lane:agent-society",
            "source:arxiv-cs-ai-agent-collaboration-governance",
            "topic:memory",
            "topic:multi-agent",
        ],
        "sensitivity": "private",
        "allowed_surfaces": ["local", "discord"],
        "created_at": "2026-07-02T04:18:00Z",
        "updated_at": "2026-07-02T04:39:17Z",
        "kanban_settlement": {
            "decision": "SAVE",
            "intake_task_id": "t_status_intake",
            "review_task_id": "t_status_review",
            "settled_at": "2026-07-02T04:29:29Z",
            "reason_label": "reason#status_reference_id",
        },
    }
    base.update(overrides)
    return base


def _active_candidate(**overrides):
    """Active (status='candidate') candidate, surface-allowed everywhere."""
    base = {
        "id": "cand_active_status_ref",
        "status": "candidate",
        "kind": "research_source_signal",
        "pressure": 0.91,
        "summary": (
            "Active pointer candidate for status reference_id test: arXiv "
            "agent-collaboration governance follow-up."
        ),
        "correlation_keys": [
            "lane:agent-society",
            "topic:memory",
            "topic:multi-agent",
        ],
        "sensitivity": "private",
        "allowed_surfaces": ["local", "discord"],
        "created_at": "2026-07-04T08:00:00Z",
        "updated_at": "2026-07-04T08:00:00Z",
    }
    base.update(overrides)
    return base


def _dormant_thread():
    """Dormant thread seeded against the active candidate above."""
    return {
        "id": "sth_status_ref_dormant",
        "status": "dormant",
        "origin": "candidate",
        "origin_candidate_id": "cand_active_status_ref",
        "conscious_task": {
            "id": "ctask_status_ref",
            "request_type": "THINK",
            "title": "Review arXiv agent collaboration governance thread",
            "why": "Pin exact-subject status branch (P0-2 audit)",
            "expected_decision": "decide",
        },
        "created_at": "2026-07-04T08:00:00Z",
        "updated_at": "2026-07-04T08:00:00Z",
        "sensitivity": "private",
        "allowed_surfaces": ["local", "discord"],
    }


def _write_config(state_dir, *, surfaces=("discord", "local")):
    config = {
        "allowed_surfaces": list(surfaces),
        "max_sensitivity": "private",
        "instance_name": "test",
    }
    path = Path(state_dir) / "instance.config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config))


def _seed_state(tmp_path, *, candidates=None, threads=None, surfaces=("discord", "local")):
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    _write_config(tmp_path, surfaces=surfaces)
    for c in (candidates or []):
        store.append_jsonl("candidates", c)
    for t in (threads or []):
        store.append_jsonl("threads", t)
    return store


# ---------- 1. dormant thread pin ----------


def test_status_reference_id_pins_thread(tmp_path):
    """A dormant thread's reference_id must surface as exact_subject.kind='thread'."""
    _seed_state(
        tmp_path,
        candidates=[_active_candidate(), _arxiv_candidate()],
        threads=[_dormant_thread()],
    )

    raw = handle_sensorium_status(
        instance="test",
        state_dir=str(tmp_path),
        reference_id="sth_status_ref_dormant",
    )
    payload = json.loads(raw)
    assert payload["success"] is True, payload
    exact = payload["data"]["exact_subject"]
    assert exact["kind"] == "thread"
    assert exact["id"] == "sth_status_ref_dormant"
    assert exact["status"] == "dormant"
    # Title must come from conscious_task.title (the conscious-layer-visible
    # title), so the pointer can hand the agent back the same words the
    # pointer context presented.
    assert exact["title"] == "Review arXiv agent collaboration governance thread"
    # Thread exact_subject does not need kanban_settlement; if present it
    # must not be populated from a candidate record.
    assert exact.get("kanban_settlement") in (None, {}, "", [])
    assert not exact.get("not_found")


# ---------- 2. active candidate pin ----------


def test_status_reference_id_pins_candidate(tmp_path):
    """An active candidate's reference_id must surface with kind='candidate' + pressure."""
    _seed_state(
        tmp_path,
        candidates=[_active_candidate(), _arxiv_candidate()],
        threads=[_dormant_thread()],
    )

    raw = handle_sensorium_status(
        instance="test",
        state_dir=str(tmp_path),
        reference_id="cand_active_status_ref",
    )
    payload = json.loads(raw)
    assert payload["success"] is True, payload
    exact = payload["data"]["exact_subject"]
    assert exact["kind"] == "candidate"
    assert exact["id"] == "cand_active_status_ref"
    assert exact["status"] == "candidate"
    assert exact["pressure"] == pytest.approx(0.91)
    # The active candidate has no kanban settlement yet; if the field is
    # returned, it must be empty rather than borrowing one from the archived
    # arXiv candidate in the same store.
    settlement = exact.get("kanban_settlement") or {}
    assert not settlement.get("intake_task_id")
    assert not exact.get("not_found")


# ---------- 3. saved-residue (archived + SAVE) pin ----------


def test_status_reference_id_pins_saved_residue(tmp_path):
    """Archived candidate with kanban SAVE must surface as kind='saved_residue'
    and carry the intake/review task ids — the conscious layer's only durable
    handle on a saved residue it never saw promoted."""
    _seed_state(
        tmp_path,
        candidates=[_active_candidate(), _arxiv_candidate()],
        threads=[_dormant_thread()],
    )

    raw = handle_sensorium_status(
        instance="test",
        state_dir=str(tmp_path),
        reference_id="cand_arxiv_status_ref",
    )
    payload = json.loads(raw)
    assert payload["success"] is True, payload
    exact = payload["data"]["exact_subject"]
    assert exact["kind"] == "saved_residue"
    assert exact["id"] == "cand_arxiv_status_ref"
    # Saved residue is archived; kind is the active-status twin.
    assert exact["status"] == "archived"
    # kanban_settlement block is the whole point of this branch.
    settlement = exact["kanban_settlement"]
    assert settlement["decision"] == "SAVE"
    assert settlement["intake_task_id"] == "t_status_intake"
    assert settlement["review_task_id"] == "t_status_review"
    assert not exact.get("not_found")


@pytest.mark.parametrize("decision", ["SAVE", "PROMOTE_CONSCIOUS"])
def test_status_reference_id_classifies_reviewed_kanban_settlement_as_saved_residue(tmp_path, decision):
    """Kanban SAVE/PROMOTE settlements leave reviewed candidates as residue."""
    store = _seed_state(tmp_path, candidates=[_active_candidate()])
    result = apply_kanban_settlement(
        store,
        decision=decision,
        candidate_id="cand_active_status_ref",
        intake_task_id="t_settlement_intake",
        review_task_id="t_settlement_review",
    )
    assert result["action"] == "settled"

    raw = handle_sensorium_status(
        instance="test",
        state_dir=str(tmp_path),
        reference_id="cand_active_status_ref",
    )
    payload = json.loads(raw)
    exact = payload["data"]["exact_subject"]
    assert exact["status"] == "reviewed"
    assert exact["kind"] == "saved_residue"
    assert exact["kanban_settlement"]["decision"] == decision
    assert exact["kanban_settlement"]["intake_task_id"] == "t_settlement_intake"


# ---------- 4. unknown id → not_found ----------


def test_status_reference_id_unknown_marks_not_found(tmp_path):
    """An id that matches no thread and no candidate MUST be reported as
    not_found, not silently coerced into the top-N or omitted entirely."""
    _seed_state(
        tmp_path,
        candidates=[_active_candidate(), _arxiv_candidate()],
        threads=[_dormant_thread()],
    )

    raw = handle_sensorium_status(
        instance="test",
        state_dir=str(tmp_path),
        reference_id="cand_doesnotexist",
    )
    payload = json.loads(raw)
    assert payload["success"] is True, payload
    exact = payload["data"]["exact_subject"]
    assert exact["not_found"] is True
    # The id the caller asked about must be echoed back, so the conscious
    # layer can log the mismatch against the pointer that presented it.
    assert exact.get("id") == "cand_doesnotexist"
    # An unknown id is not a thread, not a candidate, not a saved residue.
    assert exact.get("kind") in (None, "", "unknown")


# ---------- 5. surface gating ----------


def test_status_reference_id_respects_surface(tmp_path):
    """A candidate whose allowed_surfaces does not include the caller's
    surface MUST be reported as not_found (or otherwise surface-disallowed),
    never silently surfaced onto a forbidden surface."""
    # Config permits BOTH surfaces, but the candidate is local-only.
    _seed_state(
        tmp_path,
        candidates=[
            _active_candidate(allowed_surfaces=["local"]),  # local only!
            _arxiv_candidate(),
        ],
        threads=[],
        surfaces=("discord", "local"),
    )

    raw = handle_sensorium_status(
        instance="test",
        state_dir=str(tmp_path),
        surface="discord",
        reference_id="cand_active_status_ref",
    )
    payload = json.loads(raw)
    assert payload["success"] is True, payload
    exact = payload["data"]["exact_subject"]
    # The exact subject must NOT be presented as visible on this surface.
    assert exact["not_found"] is True
    # A surface=disallowed marker is acceptable per the spec; not_found=True
    # alone is the minimum requirement.


# ---------- 6. backwards compatibility (MUST pass today) ----------


def test_status_without_reference_id_keeps_existing_top_n(tmp_path):
    """When reference_id is omitted, the top_threads/top_candidates shape is
    unchanged from the current contract. This locks down backwards compat so
    adding reference_id cannot silently alter the legacy caller path.
    """
    _seed_state(
        tmp_path,
        candidates=[_active_candidate(), _arxiv_candidate()],
        threads=[_dormant_thread()],
    )

    # No reference_id kwarg at all.
    raw = handle_sensorium_status(instance="test", state_dir=str(tmp_path))
    payload = json.loads(raw)
    assert payload["success"] is True, payload
    data = payload["data"]

    # Legacy shape: top_threads / top_candidates lists are present and shaped
    # exactly the way the existing pointer tests expect.
    assert isinstance(data["top_threads"], list)
    assert isinstance(data["top_candidates"], list)
    assert len(data["top_threads"]) == 1
    assert data["top_threads"][0]["id"] == "sth_status_ref_dormant"
    assert data["top_threads"][0]["status"] == "dormant"
    # Only status=='candidate' rows appear in top_candidates; archived rows
    # with a kanban SAVE are NOT in top_candidates (they live in saved_residue).
    assert len(data["top_candidates"]) == 1
    assert data["top_candidates"][0]["id"] == "cand_active_status_ref"
    assert data["top_candidates"][0]["pressure"] == pytest.approx(0.91)

    # Counts remain consistent with the top-N.
    assert data["counts"]["active_candidates"] == 1
    assert data["counts"]["dormant_threads"] == 1

    # exact_subject MUST NOT appear when reference_id was not requested —
    # otherwise a surface caller can leak the old shape into a regression.
    assert "exact_subject" not in data
