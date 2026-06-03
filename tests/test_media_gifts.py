"""Tests for mediated-presence gift conscious-choice policy."""

import json
from pathlib import Path

import pytest

from agent_sensorium.artifacts import store_artifact
from agent_sensorium.config import load_instance_config
from agent_sensorium.media_gifts import apply_media_gift_choice
from agent_sensorium.store import SensoriumStore
from agent_sensorium.tools import handle_sensorium_media_gift_decide


@pytest.fixture
def state_dir(tmp_path):
    return str(tmp_path / "sensorium")


@pytest.fixture
def store(state_dir):
    s = SensoriumStore(instance="test", state_dir=state_dir)
    s.ensure_dirs()
    return s


def _thread(allowed_surfaces=None, sensitivity="private"):
    return {
        "id": "sth_gift",
        "status": "dormant",
        "origin": "candidate",
        "conscious_task": {
            "id": "ct_gift",
            "request_type": "PRIVATE_EXPRESSION",
            "title": "demo mediated-presence gift",
        },
        "origin_candidate_id": "cand_gift",
        "continuity_summary": [],
        "decision_log": [],
        "interaction_refs": [],
        "summary_dirty": False,
        "open_questions": [],
        "next_prompt_to_operator": "review gift choice",
        "sensitivity": sensitivity,
        "allowed_surfaces": allowed_surfaces or ["local"],
        "created_at": "2026-05-30T00:00:00Z",
        "updated_at": "2026-05-30T00:00:00Z",
        "expires_at": "2026-06-06T00:00:00Z",
    }


def _config(delivery_enabled=False):
    return {
        "allowed_surfaces": ["local", "discord"],
        "max_sensitivity": "private",
        "media_gift_policy": {
            "delivery": {
                "enabled": delivery_enabled,
                "allowed_surfaces": ["discord"],
                "allowed_targets": ["discord:#general"],
                "cooldown_hours": 24,
            }
        },
    }


def _artifact(store):
    store.append_jsonl("threads", _thread(allowed_surfaces=["local", "discord"]))
    return store_artifact(
        store,
        kind="video",
        ref_path="/private/demo/gift.mp4",
        provenance={"sha256": "abc"},
        why_created="The agent prepared a private mediated-presence gift.",
        intended_handoff_mode="both_later",
        delivery_state="held_for_review",
        source_thread_id="sth_gift",
        allowed_surfaces=["local", "discord"],
    )["data"]


class TestMediaGiftConfig:
    def test_safe_defaults_keep_delivery_disabled_and_silence_valid(self):
        config, _ = load_instance_config()
        policy = config["media_gift_policy"]
        assert policy["enabled"] is True
        assert policy["silence_allowed"] is True
        assert policy["artifact_first_required"] is True
        assert policy["scheduler_delivery_enabled"] is False
        assert policy["delivery"]["enabled"] is False
        assert policy["delivery"]["allowed_surfaces"] == []

    def test_custom_media_gift_policy_is_sanitized(self, tmp_path):
        cfg = tmp_path / "instance.config.json"
        cfg.write_text(json.dumps({
            "media_gift_policy": {
                "silence_allowed": False,
                "delivery": {
                    "enabled": True,
                    "allowed_surfaces": ["discord", "", "discord"],
                    "allowed_targets": ["discord:#general"],
                    "cooldown_hours": 6,
                    "unknown": "ignored",
                },
            }
        }))
        config, _ = load_instance_config(config_path=str(cfg))
        policy = config["media_gift_policy"]
        assert policy["silence_allowed"] is False
        assert policy["delivery"]["enabled"] is True
        assert policy["delivery"]["allowed_surfaces"] == ["discord"]
        assert policy["delivery"]["cooldown_hours"] == 6
        assert "unknown" not in policy["delivery"]


class TestMediaGiftChoicePolicy:
    def test_subconscious_can_only_propose(self, store):
        denied = apply_media_gift_choice(
            store,
            decision="prepare_thread_artifact",
            actor_tier="subconscious",
            source="inner_salience",
            why_now="pressure crossed threshold",
            config=_config(),
        )
        assert denied["success"] is False
        assert denied["error"] == "subconscious_may_only_propose"
        assert store.read_jsonl("decisions")[-1]["type"] == "media_gift.denied"

        proposed = apply_media_gift_choice(
            store,
            decision="propose",
            actor_tier="subconscious",
            source="inner_salience",
            why_now="recurring private presence pressure",
            config=_config(),
        )
        assert proposed["success"] is True
        assert proposed["receipt"]["type"] == "media_gift.proposed"
        assert proposed["receipt"]["delivery_authorized"] is False

    def test_conscious_can_choose_silence_without_delivery(self, store):
        result = apply_media_gift_choice(
            store,
            decision="choose_silence",
            actor_tier="conscious",
            source="inner_salience",
            reason="The pressure is real but not ripe enough to share.",
            config=_config(),
        )
        assert result["success"] is True
        receipt = result["receipt"]
        assert receipt["type"] == "media_gift.silence_chosen"
        assert receipt["silence_valid"] is True
        assert receipt["outbound_delivery"] is False
        assert store.read_jsonl("outbox") == []

    def test_direct_prompt_is_choice_not_command_and_can_be_declined(self, store):
        result = apply_media_gift_choice(
            store,
            decision="decline",
            actor_tier="conscious",
            source="operator_prompt",
            reason="I choose not to send this one.",
            config=_config(delivery_enabled=True),
        )
        assert result["success"] is True
        receipt = result["receipt"]
        assert receipt["type"] == "media_gift.declined"
        assert receipt["operator_prompt_not_binding"] is True
        assert receipt["delivery_authorized"] is False
        assert store.read_jsonl("outbox") == []

    def test_delivery_approval_requires_configured_delivery_surface_and_artifact_first(self, store):
        no_artifact = apply_media_gift_choice(
            store,
            decision="approve_delivery",
            actor_tier="conscious",
            source="manual_review",
            why_now="A specific prepared gift is ready.",
            surface="discord",
            target_ref="discord:#general",
            config=_config(delivery_enabled=True),
        )
        assert no_artifact["success"] is False
        assert no_artifact["error"] == "artifact_first_required"

        artifact = _artifact(store)
        wrong_surface = apply_media_gift_choice(
            store,
            decision="approve_delivery",
            actor_tier="conscious",
            source="manual_review",
            why_now="A specific prepared gift is ready.",
            artifact_id=artifact["id"],
            surface="telegram",
            target_ref="telegram:home",
            config=_config(delivery_enabled=True),
        )
        assert wrong_surface["success"] is False
        assert wrong_surface["error"] == "surface_not_configured"

    def test_delivery_approval_is_receipt_only_and_cooldown_gated(self, store):
        artifact = _artifact(store)
        result = apply_media_gift_choice(
            store,
            decision="approve_delivery",
            actor_tier="conscious",
            source="manual_review",
            why_now="The artifact was prepared from inner salience and reviewed in-thread.",
            artifact_id=artifact["id"],
            surface="discord",
            target_ref="discord:#general",
            config=_config(delivery_enabled=True),
            now="2026-05-30T00:00:00Z",
        )
        assert result["success"] is True
        receipt = result["receipt"]
        assert receipt["type"] == "media_gift.delivery_approved"
        assert receipt["delivery_authorized"] is True
        assert receipt["requires_separate_dispatch"] is True
        assert receipt["outbound_delivery"] is False
        assert store.read_jsonl("outbox") == []
        assert store.read_jsonl("artifacts")[0]["delivery_state"] == "prepared"

        cooled = apply_media_gift_choice(
            store,
            decision="approve_delivery",
            actor_tier="conscious",
            source="manual_review",
            why_now="Trying again too soon.",
            artifact_id=artifact["id"],
            surface="discord",
            target_ref="discord:#general",
            config=_config(delivery_enabled=True),
            now="2026-05-30T01:00:00Z",
        )
        assert cooled["success"] is False
        assert cooled["error"] == "cooldown_active"

    def test_delivery_approval_never_broadens_artifact_surface(self, store):
        store.append_jsonl("threads", _thread(allowed_surfaces=["local"]))
        artifact = store_artifact(
            store,
            kind="image",
            ref_path="/private/demo/local-only.png",
            why_created="local-only gift",
            source_thread_id="sth_gift",
            allowed_surfaces=["local"],
        )["data"]
        result = apply_media_gift_choice(
            store,
            decision="approve_delivery",
            actor_tier="conscious",
            source="manual_review",
            why_now="Try to broaden surface.",
            artifact_id=artifact["id"],
            surface="discord",
            target_ref="discord:#general",
            config=_config(delivery_enabled=True),
        )
        assert result["success"] is False
        assert result["error"] == "artifact_surface_not_allowed"

    def test_scheduler_cannot_approve_delivery(self, store):
        artifact = _artifact(store)
        result = apply_media_gift_choice(
            store,
            decision="approve_delivery",
            actor_tier="conscious",
            source="scheduler",
            why_now="scheduled tick should not do this",
            artifact_id=artifact["id"],
            surface="discord",
            target_ref="discord:#general",
            config=_config(delivery_enabled=True),
        )
        assert result["success"] is False
        assert result["error"] == "scheduler_delivery_disabled"


class TestMediaGiftTool:
    def test_tool_writes_choice_receipt_from_instance_config(self, state_dir):
        Path(state_dir).mkdir(parents=True, exist_ok=True)
        (Path(state_dir) / "instance.config.json").write_text(json.dumps(_config()))
        result = json.loads(handle_sensorium_media_gift_decide(
            instance="test",
            state_dir=state_dir,
            decision="choose_silence",
            actor_tier="conscious",
            source="inner_salience",
            reason="Not ripe yet.",
        ))
        assert result["success"] is True
        assert result["data"]["receipt"]["type"] == "media_gift.silence_chosen"
        store = SensoriumStore(instance="test", state_dir=state_dir)
        assert store.read_jsonl("decisions")[-1]["type"] == "media_gift.silence_chosen"
