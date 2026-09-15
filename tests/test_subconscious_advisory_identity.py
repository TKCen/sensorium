from __future__ import annotations

from agent_sensorium.conscious_aperture import (
    open_conscious_aperture,
    settle_conscious_aperture_item,
)
from agent_sensorium.store import SensoriumStore
from agent_sensorium.subconscious import run_subconscious_advisory


def _source(candidate_id: str, *, summary: str = "One unresolved creative pressure") -> dict:
    return {
        "id": candidate_id,
        "status": "candidate",
        "kind": "creative_pull",
        "pressure": 0.9,
        "summary": summary,
        "event_ids": [f"evt_{candidate_id}"],
        "correlation_keys": ["shared-generic-key"],
        "sensitivity": "private",
        "allowed_surfaces": ["local"],
        "created_at": "2026-08-17T00:00:00Z",
        "updated_at": "2026-08-17T00:00:00Z",
        "expires_at": "",
    }


def _output(candidate_ids: list[str], *, title: str = "Choose the next life") -> dict:
    return {
        "action": "CREATE_CONSCIOUS_TASK",
        "rationale": "One coherent unresolved pressure deserves a bounded foreground choice.",
        "event_ids": [],
        "candidate_ids": candidate_ids,
        "pressure": 0.72,
        "conscious_task": {
            "request_type": "THINK",
            "title": title,
            "why": "The source remains unresolved.",
            "expected_decision": "Engage, hold, settle, or remain silent.",
        },
    }


def _store(tmp_path) -> SensoriumStore:
    store = SensoriumStore(instance="test", state_dir=str(tmp_path / "sensorium"))
    store.ensure_dirs()
    return store


def _advisories(store: SensoriumStore) -> list[dict]:
    return [
        row for row in store.read_jsonl("candidates")
        if row.get("kind") == "subconscious_advisory"
    ]


def test_same_source_candidate_dedupes_model_paraphrases(tmp_path):
    store = _store(tmp_path)
    store.append_jsonl("candidates", _source("cand_source"))

    first = run_subconscious_advisory(
        store, advisory_output=_output(["cand_source"], title="Decide whether to revise"),
        enabled=True, dry_run=False,
    )
    second = run_subconscious_advisory(
        store, advisory_output=_output(["cand_source"], title="Choose the next life"),
        enabled=True, dry_run=False,
    )

    assert first["action"] == "created_conscious_task_candidate"
    assert second["action"] == "already_exists"
    advisories = _advisories(store)
    assert len(advisories) == 1
    assert advisories[0]["source_candidate_ids"] == ["cand_source"]
    assert advisories[0]["source_candidate_fingerprint"]
    assert advisories[0]["conscious_task"]["title"] == "Decide whether to revise"


def test_different_sources_with_same_wording_remain_distinct(tmp_path):
    store = _store(tmp_path)
    store.append_jsonl("candidates", _source("cand_a"))
    store.append_jsonl("candidates", _source("cand_b"))

    first = run_subconscious_advisory(
        store, advisory_output=_output(["cand_a"]), enabled=True, dry_run=False,
    )
    second = run_subconscious_advisory(
        store, advisory_output=_output(["cand_b"]), enabled=True, dry_run=False,
    )

    assert first["action"] == "created_conscious_task_candidate"
    assert second["action"] == "created_conscious_task_candidate"
    assert len(_advisories(store)) == 2


def test_settled_unchanged_source_stays_quiet(tmp_path):
    store = _store(tmp_path)
    store.append_jsonl("candidates", _source("cand_source"))
    created = run_subconscious_advisory(
        store, advisory_output=_output(["cand_source"]), enabled=True, dry_run=False,
    )
    advisory_id = created["candidate_id"]

    opened = open_conscious_aperture(
        store, aperture_size=1, dry_run=False, now="2026-08-17T01:00:00Z",
    )
    settled = settle_conscious_aperture_item(
        store,
        candidate_id=advisory_id,
        aperture_id=opened["aperture_id"],
        consumer_id=opened["aperture"][0]["consumer_id"],
        decision="SETTLED",
        reason="Conscious chose to settle this unchanged pressure.",
        dry_run=False,
        now="2026-08-17T01:05:00Z",
    )
    repeated = run_subconscious_advisory(
        store,
        advisory_output=_output(["cand_source"], title="A fresh paraphrase of the same pressure"),
        enabled=True,
        dry_run=False,
    )

    assert settled["new_status"] == "reviewed"
    assert repeated["action"] == "already_exists"
    advisories = _advisories(store)
    assert len(advisories) == 1
    assert advisories[0]["id"] == advisory_id
    assert advisories[0]["status"] == "reviewed"


def test_legacy_prose_change_does_not_invent_material_revision(tmp_path):
    store = _store(tmp_path)
    store.append_jsonl("candidates", _source("cand_source"))
    created = run_subconscious_advisory(
        store, advisory_output=_output(["cand_source"]), enabled=True, dry_run=False,
    )
    advisory_id = created["candidate_id"]
    opened = open_conscious_aperture(
        store, aperture_size=1, dry_run=False, now="2026-08-17T01:00:00Z",
    )
    settle_conscious_aperture_item(
        store,
        candidate_id=advisory_id,
        aperture_id=opened["aperture_id"],
        consumer_id=opened["aperture"][0]["consumer_id"],
        decision="SETTLED",
        reason="Initial revision was settled.",
        dry_run=False,
        now="2026-08-17T01:05:00Z",
    )

    candidates = store.read_jsonl("candidates")
    source = next(row for row in candidates if row.get("id") == "cand_source")
    source["summary"] = "The creative pressure gained materially new evidence"
    source["updated_at"] = "2026-08-17T02:00:00Z"
    store.rewrite_jsonl("candidates", candidates)

    changed = run_subconscious_advisory(
        store,
        advisory_output=_output(["cand_source"], title="Review the materially changed pressure"),
        enabled=True,
        dry_run=False,
    )

    assert changed["action"] == "already_exists"
    assert changed["candidate_id"] == advisory_id
    advisories = _advisories(store)
    assert len(advisories) == 1
    assert advisories[0]["id"] == advisory_id
    assert advisories[0]["status"] == "reviewed"
    assert advisories[0]["conscious_task"]["title"] == "Choose the next life"
    assert len(advisories[0]["conscious_settlements"]) == 1


def test_dangling_event_id_does_not_invent_source_owned_revision(tmp_path):
    store = _store(tmp_path)
    store.append_jsonl("candidates", _source("cand_source"))
    created = run_subconscious_advisory(
        store, advisory_output=_output(["cand_source"]), enabled=True, dry_run=False,
    )

    candidates = store.read_jsonl("candidates")
    source = next(row for row in candidates if row.get("id") == "cand_source")
    source["event_ids"].append("evt_material_revision")
    source["pressure"] = 0.2
    store.rewrite_jsonl("candidates", candidates)

    changed = run_subconscious_advisory(
        store,
        advisory_output=_output(["cand_source"], title="Review the new source evidence"),
        enabled=True,
        dry_run=False,
    )

    advisories = _advisories(store)
    assert changed["action"] == "already_exists"
    assert changed["candidate_id"] == created["candidate_id"]
    assert len(advisories) == 1
    assert advisories[0]["conscious_task"]["title"] == "Choose the next life"


def test_explicitly_suppressed_advisory_never_reopens_automatically(tmp_path):
    store = _store(tmp_path)
    store.append_jsonl("candidates", _source("cand_source"))
    created = run_subconscious_advisory(
        store, advisory_output=_output(["cand_source"]), enabled=True, dry_run=False,
    )
    candidates = store.read_jsonl("candidates")
    advisory = next(row for row in candidates if row.get("id") == created["candidate_id"])
    advisory["status"] = "suppressed"
    source = next(row for row in candidates if row.get("id") == "cand_source")
    source["summary"] = "The source changed after explicit suppression"
    store.rewrite_jsonl("candidates", candidates)

    result = run_subconscious_advisory(
        store,
        advisory_output=_output(["cand_source"], title="Do not reopen me"),
        enabled=True,
        dry_run=False,
    )

    assert result["action"] == "already_exists"
    assert result["reason_code"] == "prior_disposition"
    assert _advisories(store)[0]["status"] == "suppressed"


def test_create_requires_exactly_one_live_source_candidate(tmp_path):
    store = _store(tmp_path)
    store.append_jsonl("candidates", _source("cand_a"))
    store.append_jsonl("candidates", _source("cand_b"))

    missing = run_subconscious_advisory(
        store, advisory_output=_output([]), enabled=True, dry_run=False,
    )
    multiple = run_subconscious_advisory(
        store, advisory_output=_output(["cand_a", "cand_b"]), enabled=True, dry_run=False,
    )
    unknown = run_subconscious_advisory(
        store, advisory_output=_output(["cand_unknown"]), enabled=True, dry_run=False,
    )

    assert missing["action"] == "save"
    assert multiple["action"] == "save"
    assert unknown["action"] == "save"
    assert {missing["reason_code"], multiple["reason_code"], unknown["reason_code"]} == {
        "stable_source_candidate_required"
    }
    assert _advisories(store) == []
