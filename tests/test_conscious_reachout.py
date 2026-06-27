"""Tests for policy-gated conscious reach-out decisions."""

import pytest

from agent_sensorium.conscious_reachout import (
    CONSCIOUS_REACHOUT_DELIVERED_TYPE,
    CONSCIOUS_REACHOUT_DENIED_TYPE,
    CONSCIOUS_REACHOUT_RECEIPT_TYPE,
    apply_conscious_reachout_decision,
    conscious_reachout_metrics,
)
from agent_sensorium.outbox import FakeDiscordAdapter
from agent_sensorium.store import SensoriumStore


@pytest.fixture
def store(tmp_path):
    s = SensoriumStore(instance="test", state_dir=str(tmp_path / "sensorium"))
    s.ensure_dirs()
    return s


def _config(**overrides):
    cfg = {
        "conscious_reachout": {
            "enabled": True,
            "allowed_surfaces": ["local", "discord"],
            "allowed_targets": ["discord:chan_1", "discord:dm_1", "local"],
            "max_sensitivity": "private",
            "direct_delivery_enabled": False,
            "cooldown_minutes": 240,
            "max_message_chars": 1000,
            "default_delivery_mode": "context_pointer",
        }
    }
    cfg["conscious_reachout"].update(overrides)
    return cfg


def _thread():
    return {
        "id": "sth_reach",
        "status": "dormant",
        "origin_candidate_id": "cand_reach",
        "conscious_task": {"id": "ct_reach", "request_type": "PRIVATE_EXPRESSION", "title": "reach out"},
        "allowed_surfaces": ["local", "discord"],
        "sensitivity": "private",
        "interaction_refs": [],
        "decision_log": [],
        "created_at": "2026-06-27T00:00:00Z",
        "updated_at": "2026-06-27T00:00:00Z",
    }


def test_subconscious_cannot_prepare_or_deliver(store):
    result = apply_conscious_reachout_decision(
        store,
        decision="reach_out",
        actor_tier="subconscious",
        message="I want to leave the snail house.",
        surface="discord",
        target_ref="discord:chan_1",
        target={"channel_id": "chan_1"},
        config=_config(direct_delivery_enabled=True),
    )

    assert result["success"] is False
    assert result["error"] == "subconscious_may_not_reach_out"
    receipt = store.read_jsonl("decisions")[-1]
    assert receipt["type"] == CONSCIOUS_REACHOUT_DENIED_TYPE
    assert receipt["background_action_allowed"] is False
    assert "I want" not in str(receipt)


def test_conscious_can_prepare_message_without_direct_delivery(store):
    store.append_jsonl("threads", _thread())

    result = apply_conscious_reachout_decision(
        store,
        decision="prepare_message",
        actor_tier="conscious",
        message="A bounded message authored by the conscious layer.",
        reason="selected and worth preparing",
        surface="discord",
        target_ref="discord:chan_1",
        target={"channel_id": "chan_1"},
        thread_id="sth_reach",
        config=_config(),
    )

    assert result["success"] is True
    receipt = result["receipt"]
    assert receipt["type"] == CONSCIOUS_REACHOUT_RECEIPT_TYPE
    assert receipt["decision"] == "prepare_message"
    assert receipt["outbound_delivery"] is False
    assert receipt["message_chars"] > 0
    assert "bounded message" not in str(receipt)
    assert store.read_jsonl("outbox")[0]["request_type"] == "REACH_OUT"


def test_direct_reachout_requires_policy_and_adapter(store):
    no_direct = apply_conscious_reachout_decision(
        store,
        decision="reach_out",
        actor_tier="conscious",
        message="A direct chosen message.",
        surface="discord",
        target_ref="discord:chan_1",
        target={"channel_id": "chan_1"},
        execute=True,
        config=_config(direct_delivery_enabled=False),
    )
    assert no_direct["success"] is False
    assert no_direct["error"] == "direct_delivery_disabled"

    no_adapter = apply_conscious_reachout_decision(
        store,
        decision="reach_out",
        actor_tier="conscious",
        message="A direct chosen message.",
        surface="discord",
        target_ref="discord:chan_1",
        target={"channel_id": "chan_1"},
        execute=True,
        config=_config(direct_delivery_enabled=True),
    )
    assert no_adapter["success"] is False
    assert no_adapter["error"] == "missing_delivery_adapter"
    assert store.read_jsonl("outbox") == []


def test_direct_reachout_with_adapter_records_delivered_receipt_and_cooldown(store):
    adapter = FakeDiscordAdapter()
    result = apply_conscious_reachout_decision(
        store,
        decision="reach_out",
        actor_tier="conscious",
        message="A direct chosen message.",
        surface="discord",
        target_ref="discord:chan_1",
        target={"channel_id": "chan_1"},
        execute=True,
        adapter=adapter,
        config=_config(direct_delivery_enabled=True, cooldown_minutes=240),
        now="2026-06-27T10:00:00Z",
    )

    assert result["success"] is True
    assert adapter.calls[0]["method"] == "create_thread"
    receipt = store.read_jsonl("decisions")[-1]
    assert receipt["type"] == CONSCIOUS_REACHOUT_DELIVERED_TYPE
    assert receipt["delivery_authorized"] is True
    assert receipt["outbound_delivery"] is True
    assert "direct chosen" not in str(receipt)

    cooled = apply_conscious_reachout_decision(
        store,
        decision="reach_out",
        actor_tier="conscious",
        message="Another message.",
        surface="discord",
        target_ref="discord:chan_1",
        target={"channel_id": "chan_1"},
        execute=True,
        adapter=adapter,
        config=_config(direct_delivery_enabled=True, cooldown_minutes=240),
        now="2026-06-27T11:00:00Z",
    )
    assert cooled["success"] is False
    assert cooled["error"] == "cooldown_active"


def test_conscious_reachout_metrics_are_compact(store):
    apply_conscious_reachout_decision(
        store,
        decision="prepare_message",
        actor_tier="conscious",
        message="Prepare this but do not leak it in metrics.",
        surface="local",
        target_ref="local",
        config=_config(allowed_targets=[]),
    )
    apply_conscious_reachout_decision(
        store,
        decision="reach_out",
        actor_tier="subconscious",
        message="Blocked body.",
        surface="local",
        target_ref="local",
        config=_config(allowed_targets=[]),
    )

    metrics = conscious_reachout_metrics(store.read_jsonl("decisions"))
    assert metrics["receipt_count"] == 2
    assert metrics["prepared_count"] == 1
    assert metrics["blocked_count"] == 1
    assert metrics["blocked_breakdown"] == {"subconscious_may_not_reach_out": 1}
    assert "leak it" not in str(metrics)


def test_conscious_reachout_metrics_hash_unknown_breakdown_keys(store):
    store.append_jsonl("decisions", {
        "type": CONSCIOUS_REACHOUT_DENIED_TYPE,
        "decision": "SECRET_DECISION_VALUE",
        "blocked_reason": "SECRET_BLOCK_REASON",
        "surface": "SECRET_SURFACE",
        "target_ref": "discord:secret-channel",
        "ts": "2026-06-27T10:00:00Z",
    })

    metrics = conscious_reachout_metrics(store.read_jsonl("decisions"))

    rendered = str(metrics)
    assert "SECRET_DECISION_VALUE" not in rendered
    assert "SECRET_BLOCK_REASON" not in rendered
    assert "SECRET_SURFACE" not in rendered
    assert "decision:" in rendered
    assert "blocked:" in rendered
    assert "surface:" in rendered


def test_deliver_prepared_dispatches_existing_outbox_and_marks_it_dispatched(store):
    store.append_jsonl("threads", _thread())
    prepared = apply_conscious_reachout_decision(
        store,
        decision="reach_out",
        actor_tier="conscious",
        message="Prepared direct message.",
        surface="discord",
        target_ref="discord:chan_1",
        target={"channel_id": "chan_1"},
        thread_id="sth_reach",
        execute=False,
        config=_config(direct_delivery_enabled=True),
    )
    outbox_id = prepared["receipt"]["outbox_id"]
    adapter = FakeDiscordAdapter()

    delivered = apply_conscious_reachout_decision(
        store,
        decision="deliver_prepared",
        actor_tier="conscious",
        surface="discord",
        target_ref="discord:chan_1",
        outbox_id=outbox_id,
        execute=True,
        adapter=adapter,
        config=_config(direct_delivery_enabled=True),
        now="2026-06-27T12:00:00Z",
    )

    assert delivered["success"] is True
    assert adapter.calls[0]["method"] == "create_thread"
    assert store.read_jsonl("outbox")[0]["status"] == "dispatched"
    receipt = store.read_jsonl("decisions")[-1]
    assert receipt["type"] == CONSCIOUS_REACHOUT_DELIVERED_TYPE
    assert receipt["outbox_id"] == outbox_id
    assert "Prepared direct message" not in str(receipt)
