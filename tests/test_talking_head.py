"""Tests for manual talking-head artifact worker wrapper."""

import json
from pathlib import Path

import pytest

from agent_sensorium.store import SensoriumStore
from agent_sensorium.talking_head import TalkingHeadRequest, run_talking_head_worker


def _make_thread(thread_id="sth_talk"):
    return {
        "id": thread_id,
        "status": "dormant",
        "origin": "candidate",
        "conscious_task": {
            "id": "ct_talk",
            "request_type": "PREPARE_ACTION",
            "title": "Manual talking-head artifact",
        },
        "origin_candidate_id": "cand_talk",
        "continuity_summary": [],
        "decision_log": [],
        "interaction_refs": [],
        "summary_dirty": False,
        "open_questions": [],
        "next_prompt_to_operator": "review talking-head artifact",
        "sensitivity": "private",
        "allowed_surfaces": ["local"],
        "created_at": "2026-05-30T00:00:00Z",
        "updated_at": "2026-05-30T00:00:00Z",
    }


def _inputs(tmp_path: Path):
    script = tmp_path / "script.txt"
    script.write_text("Sebastian, this is a private selected line.\n")
    source = tmp_path / "source.png"
    source.write_bytes(b"not-a-real-png-but-dry-and-stub-tests-only")
    return script, source


def _store_with_thread(state_dir: str):
    store = SensoriumStore(instance="test", state_dir=state_dir)
    store.ensure_dirs()
    store.append_jsonl("threads", _make_thread())
    return store


class TestTalkingHeadDryRun:
    def test_dry_run_writes_planned_artifact_records_only(self, tmp_path):
        state_dir = str(tmp_path / "state")
        store = _store_with_thread(state_dir)
        script, source = _inputs(tmp_path)
        root = tmp_path / "artifacts"

        req = TalkingHeadRequest(
            script_file=script,
            source_still=source,
            slug="demo",
            thread_id="sth_talk",
            instance="test",
            state_dir=state_dir,
            artifact_root=root,
            real_run=False,
            source_prompt_hash="abc123",
        )

        result = run_talking_head_worker(req)

        assert result["success"] is True
        assert result["mode"] == "dry_run"
        assert result["outbound_delivery"] is False
        assert result["artifact_count"] == 7
        assert not (root / "demo").exists()

        artifacts = store.read_jsonl("artifacts")
        assert len(artifacts) == 7
        assert {a["kind"] for a in artifacts} == {"text", "audio", "image", "video"}
        assert all(a["delivery_state"] == "held_for_review" for a in artifacts)
        assert all(a["allowed_surfaces"] == ["local"] for a in artifacts)
        assert all(a["source_refs"]["thread_id"] == "sth_talk" for a in artifacts)
        assert all(a["provenance"]["manual_only_no_autonomous_trigger"] is True for a in artifacts)
        assert all(a["provenance"]["planned_only"] is True for a in artifacts)

        serialized = json.dumps(artifacts)
        assert "Sebastian, this is a private selected line" not in serialized
        assert "outbox.dispatched" not in json.dumps(store.read_jsonl("decisions"))

        thread = store.read_jsonl("threads")[0]
        assert thread["summary_dirty"] is True
        assert len([r for r in thread["interaction_refs"] if r.get("type") == "artifact_ref"]) == 7

    def test_requires_thread_or_action_context(self, tmp_path):
        script, source = _inputs(tmp_path)
        req = TalkingHeadRequest(
            script_file=script,
            source_still=source,
            slug="no-context",
            instance="test",
            state_dir=str(tmp_path / "state"),
            artifact_root=tmp_path / "artifacts",
        )

        with pytest.raises(ValueError, match="thread_id or action_id is required"):
            run_talking_head_worker(req)


class TestTalkingHeadRealRun:
    def test_real_run_requires_almost_idle_capacity_before_side_effects(self, tmp_path):
        state_dir = str(tmp_path / "state")
        store = _store_with_thread(state_dir)
        script, source = _inputs(tmp_path)
        root = tmp_path / "artifacts"
        req = TalkingHeadRequest(
            script_file=script,
            source_still=source,
            slug="busy",
            thread_id="sth_talk",
            instance="test",
            state_dir=state_dir,
            artifact_root=root,
            real_run=True,
            lipsync_command="unused {video}",
        )

        with pytest.raises(RuntimeError, match="media_capacity_not_ok"):
            run_talking_head_worker(
                req,
                capacity_probe=lambda: {"status": "busy", "reasons": ["gpu_busy"], "unknown": []},
            )

        assert not (root / "busy").exists()
        assert store.read_jsonl("artifacts") == []

    def test_real_run_generates_files_manifest_and_prepared_refs(self, tmp_path):
        state_dir = str(tmp_path / "state")
        store = _store_with_thread(state_dir)
        script, source = _inputs(tmp_path)
        root = tmp_path / "artifacts"

        def crop_stub(_req, paths):
            paths.face_crop.write_bytes(b"crop")

        def tts_stub(_req, _script_text, output_path):
            output_path.write_bytes(b"audio")

        def lipsync_stub(_req, paths):
            paths.video.write_bytes(b"video")

        def qc_stub(paths):
            paths.ffprobe.write_text('{"format":{"duration":"1.0"}}\n')
            paths.contact_sheet.write_bytes(b"jpg")

        req = TalkingHeadRequest(
            script_file=script,
            source_still=source,
            slug="real",
            thread_id="sth_talk",
            instance="test",
            state_dir=state_dir,
            artifact_root=root,
            real_run=True,
            lipsync_command="stub {audio} {crop} {video}",
        )

        result = run_talking_head_worker(
            req,
            capacity_probe=lambda: {"status": "almost_idle", "reasons": [], "unknown": []},
            crop_generator=crop_stub,
            tts_generator=tts_stub,
            lipsync_runner=lipsync_stub,
            qc_generator=qc_stub,
        )

        run_dir = root / "real"
        assert result["success"] is True
        assert result["mode"] == "real"
        assert result["capacity_status"] == "almost_idle"
        assert (run_dir / "script.txt").exists()
        assert (run_dir / "source-fullframe.png").exists()
        assert (run_dir / "source-face-crop.png").exists()
        assert (run_dir / "chatterbox-audio.wav").exists()
        assert (run_dir / "talking-head.mp4").exists()
        assert (run_dir / "ffprobe.json").exists()
        assert (run_dir / "contact-sheet.jpg").exists()
        assert (run_dir / "manifest.json").exists()

        manifest = json.loads((run_dir / "manifest.json").read_text())
        assert manifest["manual_only_no_autonomous_trigger"] is True
        assert manifest["outbound_delivery"] is False
        assert len(manifest["artifact_ids"]) == 7

        artifacts = store.read_jsonl("artifacts")
        assert len(artifacts) == 7
        assert all(a["delivery_state"] == "prepared" for a in artifacts)
        assert all(a["provenance"].get("capacity_status") == "almost_idle" for a in artifacts)
        assert all(a["provenance"].get("exists") is True for a in artifacts)
        assert all("sha256" in a["provenance"] for a in artifacts)
