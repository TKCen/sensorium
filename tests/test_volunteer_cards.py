from __future__ import annotations

import json
from pathlib import Path

from agent_sensorium.store import SensoriumStore
from agent_sensorium.tools import handle_sensorium_status
from agent_sensorium.volunteer_cards import build_volunteer_cards


SECRET = "sk-live-secret-123456"


def _write_config(state_dir, *, surfaces=("local", "discord"), max_sensitivity="private"):
    path = Path(state_dir) / "instance.config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "instance_name": "test",
        "allowed_surfaces": list(surfaces),
        "max_sensitivity": max_sensitivity,
    }))


def _thread(thread_id: str, **overrides):
    base = {
        "id": thread_id,
        "status": "dormant",
        "origin": "candidate",
        "conscious_task": {
            "id": f"ctask_{thread_id}",
            "request_type": "THINK",
            "title": f"Thread {thread_id} title",
            "why": "compact why",
            "expected_decision": "decide",
        },
        "created_at": "2026-07-04T08:00:00Z",
        "updated_at": "2026-07-04T09:00:00Z",
        "sensitivity": "private",
        "allowed_surfaces": ["local", "discord"],
    }
    base.update(overrides)
    return base


def _candidate(candidate_id: str, *, pressure: float = 0.5, summary: str | None = None, **overrides):
    base = {
        "id": candidate_id,
        "status": "candidate",
        "kind": "explicit_correction",
        "pressure": pressure,
        "summary": summary or f"Candidate {candidate_id} summary",
        "created_at": "2026-07-04T08:00:00Z",
        "updated_at": "2026-07-04T09:00:00Z",
        "sensitivity": "private",
        "allowed_surfaces": ["local", "discord"],
    }
    base.update(overrides)
    return base


def _saved_residue(candidate_id: str, *, pressure: float = 0.4, summary: str | None = None, **overrides):
    base = {
        "id": candidate_id,
        "status": "archived",
        "kind": "research_source_signal",
        "pressure": pressure,
        "summary": summary or f"Saved residue {candidate_id}",
        "created_at": "2026-07-03T08:00:00Z",
        "updated_at": "2026-07-03T09:00:00Z",
        "sensitivity": "private",
        "allowed_surfaces": ["local", "discord"],
        "kanban_settlement": {
            "decision": "SAVE",
            "intake_task_id": f"kt_intake_{candidate_id}",
            "review_task_id": f"kt_review_{candidate_id}",
            "settled_at": "2026-07-03T09:30:00Z",
            "reason_label": "reason#saved",
        },
    }
    base.update(overrides)
    return base


def _artifact(artifact_id: str, **overrides):
    base = {
        "id": artifact_id,
        "kind": "text",
        "status": "recorded",
        "delivery_state": "held_for_review",
        "intended_handoff_mode": "present_thread",
        "why_created": f"Artifact {artifact_id} review note",
        "ref_path": f"/tmp/{artifact_id}.txt",
        "source_refs": {"thread_id": "sth_parent"},
        "created_at": "2026-07-04T10:00:00Z",
        "updated_at": "2026-07-04T10:00:00Z",
        "sensitivity": "private",
        "allowed_surfaces": ["local", "discord"],
    }
    base.update(overrides)
    return base


def _store(tmp_path) -> SensoriumStore:
    store = SensoriumStore(instance="test", state_dir=str(tmp_path))
    store.ensure_dirs()
    _write_config(tmp_path)
    return store


def _status_cards(tmp_path, *, surface="local") -> list[dict]:
    payload = json.loads(handle_sensorium_status(instance="test", state_dir=str(tmp_path), surface=surface))
    assert payload["success"] is True, payload
    return payload["data"]["volunteer_cards"]


def test_status_returns_zero_volunteer_cards_for_empty_state(tmp_path):
    _store(tmp_path)
    cards = _status_cards(tmp_path)
    assert cards == []


def test_status_volunteer_cards_cap_at_three_and_rank_thread_candidate_saved_residue(tmp_path):
    store = _store(tmp_path)
    store.append_jsonl("threads", _thread("sth_first"))
    store.append_jsonl("threads", _thread("sth_second", updated_at="2026-07-04T08:30:00Z"))
    store.append_jsonl("candidates", _candidate("cand_high", pressure=0.95))
    store.append_jsonl("candidates", _candidate("cand_low", pressure=0.15))
    store.append_jsonl("candidates", _saved_residue("cand_saved", pressure=0.7))
    store.append_jsonl("artifacts", _artifact("art_extra", source_refs={"thread_id": "sth_parent_extra"}))

    cards = _status_cards(tmp_path, surface="discord")

    assert len(cards) == 3
    assert [card["card_type"] for card in cards] == ["thread", "thread", "candidate"]
    assert cards[0]["reference_id"] == "sth_first"
    assert cards[1]["reference_id"] == "sth_second"
    assert cards[2]["reference_id"] == "cand_high"


def test_status_volunteer_cards_respect_surface_and_sensitivity_filtering(tmp_path):
    store = _store(tmp_path)
    store.append_jsonl("threads", _thread("sth_local_only", allowed_surfaces=["local"]))
    store.append_jsonl("threads", _thread("sth_public_safe", sensitivity="public_safe"))
    store.append_jsonl("candidates", _candidate("cand_visible", pressure=0.8))
    store.append_jsonl("candidates", _saved_residue("cand_saved_visible", pressure=0.7))

    cards = _status_cards(tmp_path, surface="discord")

    assert [card["reference_id"] for card in cards] == ["cand_visible", "cand_saved_visible"]
    assert all("discord" in card["allowed_surfaces"] for card in cards)
    assert all(card["privacy_scope"] == "private" for card in cards)


def test_volunteer_cards_are_compact_and_do_not_leak_secret_or_body_fields(tmp_path):
    store = _store(tmp_path)
    store.append_jsonl("candidates", _candidate(
        "cand_secret",
        pressure=0.9,
        summary=f"raw transcript {SECRET} should not surface",
    ))
    store.append_jsonl("artifacts", _artifact(
        "art_secret",
        why_created=f"Prompt draft with {SECRET} and raw transcript body",
        source_refs={"thread_id": "sth_secret_parent"},
    ))

    cards = _status_cards(tmp_path, surface="local")
    serialized = json.dumps(cards, sort_keys=True)

    assert SECRET not in serialized
    assert "raw transcript" not in serialized.lower()
    assert "ref_path" not in serialized
    assert "body" not in serialized
    assert cards[0]["reference_id"] == "cand_secret"
    assert cards[0]["openable_ref"]["reference_id"] == "cand_secret"


def test_artifact_card_has_parent_reference_id_when_selected(tmp_path):
    store = _store(tmp_path)
    store.append_jsonl("artifacts", _artifact("art_only", source_refs={"thread_id": "sth_parent"}))

    cards = build_volunteer_cards(store, surface="local", limit=3)

    assert len(cards) == 1
    assert cards[0]["card_type"] == "artifact"
    assert cards[0]["subject_ref"]["kind"] == "artifact"
    assert cards[0]["reference_id"] == "sth_parent"
    assert cards[0]["openable_ref"]["reference_id"] == "sth_parent"
