"""Tests for mediated-presence artifact records."""

import json

import pytest

from agent_sensorium.artifacts import (
    compact_artifacts_for_thread,
    list_artifacts,
    store_artifact,
)
from agent_sensorium.actions import prepare_action
from agent_sensorium.store import SensoriumStore
from agent_sensorium.tools import (
    handle_sensorium_artifact_store,
    handle_sensorium_artifact_status,
    handle_sensorium_thread_open,
)


@pytest.fixture
def state_dir(tmp_path):
    return str(tmp_path / "sensorium")


@pytest.fixture
def store(state_dir):
    s = SensoriumStore(instance="test", state_dir=state_dir)
    s.ensure_dirs()
    return s


def _make_thread(
    thread_id="sth_gift",
    status="dormant",
    allowed_surfaces=None,
    sensitivity="private",
    origin_candidate_id="cand_gift",
):
    if allowed_surfaces is None:
        allowed_surfaces = ["local"]
    return {
        "id": thread_id,
        "status": status,
        "origin": "candidate",
        "conscious_task": {
            "id": "ct_gift",
            "request_type": "PREPARE_ACTION",
            "title": "demo mediated-presence gift",
        },
        "origin_candidate_id": origin_candidate_id,
        "continuity_summary": [],
        "decision_log": [],
        "interaction_refs": [],
        "summary_dirty": False,
        "open_questions": [],
        "next_prompt_to_operator": "review artifact",
        "sensitivity": sensitivity,
        "allowed_surfaces": allowed_surfaces,
        "created_at": "2026-05-30T00:00:00Z",
        "updated_at": "2026-05-30T00:00:00Z",
        "expires_at": "2026-06-06T00:00:00Z",
    }


def _write_config(state_dir, surfaces=None, max_sensitivity="private"):
    from pathlib import Path

    path = Path(state_dir) / "instance.config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "allowed_surfaces": surfaces or ["local"],
        "max_sensitivity": max_sensitivity,
    }))


class TestStoreArtifact:
    def test_video_artifact_defaults_private_local_and_not_delivered(self, store):
        result = store_artifact(
            store,
            kind="video",
            ref_path="/tmp/demo-gift.mp4",
            provenance={"provider": "local-comfy", "prompt_hash": "abc123"},
            why_created="Conscious thread chose to prepare a private presence gift.",
            intended_handoff_mode="pillow_dm",
            source_thread_id="sth_gift",
        )

        assert result["success"] is True
        artifact = result["data"]
        assert artifact["id"].startswith("art_")
        assert artifact["kind"] == "video"
        assert artifact["ref_path"] == "/tmp/demo-gift.mp4"
        assert artifact["sensitivity"] == "private"
        assert artifact["privacy"] == "private"
        assert artifact["allowed_surfaces"] == ["local"]
        assert artifact["delivery_state"] == "not_delivered"
        assert artifact["intended_handoff_mode"] == "pillow_dm"
        assert artifact["source_refs"]["thread_id"] == "sth_gift"

        persisted = store.read_jsonl("artifacts")
        assert persisted == [artifact]
        decisions = store.read_jsonl("decisions")
        assert decisions[-1]["type"] == "artifact.recorded"
        assert decisions[-1]["artifact_id"] == artifact["id"]

    def test_attaches_to_conscious_thread_without_outbound_delivery(self, store):
        store.append_jsonl("threads", _make_thread())

        result = store_artifact(
            store,
            kind="audio",
            ref_path="/tmp/demo-line.opus",
            provenance={"tts": "chatterbox", "voice": "warm-voice-demo"},
            why_created="Prepare voice component for later conscious review.",
            intended_handoff_mode="both_later",
            source_thread_id="sth_gift",
            delivery_state="held_for_review",
        )

        assert result["success"] is True
        artifact_id = result["data"]["id"]
        thread = store.read_jsonl("threads")[0]
        assert thread["summary_dirty"] is True
        assert any(
            ref.get("type") == "artifact_ref" and ref.get("artifact_id") == artifact_id
            for ref in thread["interaction_refs"]
        )
        assert all(d.get("type") != "outbox.dispatched" for d in store.read_jsonl("decisions"))
        assert result["data"]["delivery_state"] == "held_for_review"

    def test_attaches_to_action_using_existing_artifact_ref_primitive(self, store):
        store.append_jsonl("threads", _make_thread())
        action = prepare_action(
            store,
            thread_id="sth_gift",
            intent="prepare_mediated_presence_gift",
            title="Prepare gift artifact",
        )["data"]

        result = store_artifact(
            store,
            kind="text",
            ref_path="/tmp/script.txt",
            provenance={"sha256": "abc"},
            why_created="Draft script chosen by conscious thread.",
            intended_handoff_mode="present_thread",
            source_action_id=action["id"],
        )

        assert result["success"] is True
        artifact_id = result["data"]["id"]
        stored_action = store.read_jsonl("thread_actions")[0]
        assert stored_action["attachments"][-1]["kind"] == "artifact_ref"
        assert stored_action["attachments"][-1]["ref_id"] == artifact_id
        assert result["data"]["source_refs"]["thread_id"] == "sth_gift"
        assert result["data"]["source_refs"]["action_id"] == action["id"]

    def test_rejects_raw_private_prompt_material_in_provenance(self, store):
        result = store_artifact(
            store,
            kind="image",
            ref_path="/tmp/source.png",
            provenance={"raw_prompt": "secret private prompt that must not leak"},
            why_created="source plate",
            intended_handoff_mode="present_thread",
        )

        assert result["success"] is False
        assert result["error"] == "raw_private_material_not_allowed"
        assert store.read_jsonl("artifacts") == []

    def test_rejects_delivery_states_that_imply_outbound_send(self, store):
        result = store_artifact(
            store,
            kind="video",
            ref_path="/tmp/out.mp4",
            why_created="gift",
            intended_handoff_mode="pillow_dm",
            delivery_state="delivered",
        )

        assert result["success"] is False
        assert result["error"] == "outbound_delivery_not_allowed"
        assert store.read_jsonl("artifacts") == []

    def test_persists_across_new_store_instance_like_gateway_restart(self, state_dir):
        first = SensoriumStore(instance="test", state_dir=state_dir)
        first.ensure_dirs()
        created = store_artifact(
            first,
            kind="video",
            ref_path="/tmp/restart-proof.mp4",
            provenance={"sha256": "abc"},
            why_created="restart persistence proof",
            intended_handoff_mode="present_thread",
        )["data"]

        second = SensoriumStore(instance="test", state_dir=state_dir)
        artifacts = list_artifacts(second)
        assert [a["id"] for a in artifacts] == [created["id"]]
        assert artifacts[0]["ref_path"] == "/tmp/restart-proof.mp4"


class TestArtifactSurfacePrivacy:
    def test_compact_artifacts_do_not_include_private_prompt_or_file_path(self, store):
        store.append_jsonl("threads", _make_thread(allowed_surfaces=["local", "discord"]))
        created = store_artifact(
            store,
            kind="video",
            ref_path="/private/demo/source.mp4",
            provenance={"model": "wan", "prompt_hash": "safehash"},
            why_created="Private gift for later review.",
            intended_handoff_mode="pillow_dm",
            source_thread_id="sth_gift",
            allowed_surfaces=["local", "discord"],
        )["data"]

        compact = compact_artifacts_for_thread(
            store,
            "sth_gift",
            surface="discord",
            instance_config={"allowed_surfaces": ["discord"], "max_sensitivity": "private"},
        )

        assert compact == [{
            "id": created["id"],
            "kind": "video",
            "delivery_state": "not_delivered",
            "intended_handoff_mode": "pillow_dm",
            "why_created": "Private gift for later review.",
        }]
        serialized = json.dumps(compact)
        assert "/private/demo/source.mp4" not in serialized
        assert "prompt" not in serialized.lower()
        assert "safehash" not in serialized

    def test_thread_open_includes_only_compact_artifact_refs(self, state_dir):
        store = SensoriumStore(instance="test", state_dir=state_dir)
        store.ensure_dirs()
        _write_config(state_dir, surfaces=["local", "discord"], max_sensitivity="private")
        store.append_jsonl("threads", _make_thread(allowed_surfaces=["local", "discord"]))
        store_artifact(
            store,
            kind="video",
            ref_path="/private/demo/gift.mp4",
            provenance={"model": "wan", "prompt_hash": "abc"},
            why_created="Private gift for review.",
            intended_handoff_mode="both_later",
            source_thread_id="sth_gift",
            allowed_surfaces=["local", "discord"],
        )

        opened = json.loads(handle_sensorium_thread_open(
            instance="test", state_dir=state_dir, thread_id="sth_gift", surface="discord"
        ))

        assert opened["success"] is True
        assert opened["data"]["artifact_count"] == 1
        serialized = json.dumps(opened["data"])
        assert "/private/demo/gift.mp4" not in serialized
        assert "prompt_hash" not in serialized


class TestArtifactTools:
    def test_artifact_store_tool_and_status_tool(self, state_dir):
        result = json.loads(handle_sensorium_artifact_store(
            instance="test",
            state_dir=state_dir,
            kind="text",
            ref_path="/tmp/script.txt",
            provenance={"sha256": "abc"},
            why_created="manual store path",
            intended_handoff_mode="present_thread",
            capacity_requirements={"requires_chatterbox": False, "requires_comfy": False},
            feedback_hooks={"on_review": "record operator evaluation"},
        ))
        assert result["success"] is True
        artifact_id = result["data"]["id"]

        status = json.loads(handle_sensorium_artifact_status(
            instance="test", state_dir=state_dir, limit=5
        ))
        assert status["success"] is True
        assert status["data"][0]["id"] == artifact_id
        assert status["data"][0]["kind"] == "text"
