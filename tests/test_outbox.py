"""Tests for agent_sensorium.outbox — prepare/dispatch lifecycle."""

import pytest

from agent_sensorium.outbox import (
    OUTBOX_DEFAULTS,
    FakeDiscordAdapter,
    dispatch_outbox_request,
    prepare_outbox_request,
)
from agent_sensorium.store import SensoriumStore


@pytest.fixture
def state_dir(tmp_path):
    return str(tmp_path / "sensorium")


@pytest.fixture
def store(state_dir):
    s = SensoriumStore(instance="test", state_dir=state_dir)
    s.ensure_dirs()
    return s


def _make_thread(
    thread_id="sth_test1",
    status="dormant",
    allowed_surfaces=None,
    sensitivity="private",
    origin_candidate_id="cand_1",
):
    if allowed_surfaces is None:
        allowed_surfaces = ["local", "discord"]
    return {
        "id": thread_id,
        "status": status,
        "origin": "candidate",
        "conscious_task": {"id": "ct_1", "request_type": "THINK", "title": "Test thread"},
        "origin_candidate_id": origin_candidate_id,
        "continuity_summary": [],
        "decision_log": [],
        "interaction_refs": [],
        "summary_dirty": False,
        "open_questions": [],
        "next_prompt_to_operator": "test",
        "sensitivity": sensitivity,
        "allowed_surfaces": allowed_surfaces,
        "created_at": "2026-05-24T10:00:00Z",
        "updated_at": "2026-05-24T10:00:00Z",
        "expires_at": "2026-05-31T10:00:00Z",
    }


class TestPrepareOutboxSuccess:
    def test_prepare_peripheral_reference_succeeds(self, store):
        store.append_jsonl("threads", _make_thread())

        result = prepare_outbox_request(
            store,
            thread_id="sth_test1",
            request_type="THINK",
            surface="discord",
            delivery_mode="peripheral_reference",
            target={"channel_id": "ch_123"},
            title="Test pointer",
            message_preview="Check thread",
        )

        assert result["success"] is True
        data = result["data"]
        assert data["id"].startswith("obx_")
        assert data["status"] == "prepared"
        assert data["delivery_mode"] == "peripheral_reference"
        assert data["surface"] == "discord"

        outbox_rows = store.read_jsonl("outbox")
        assert len(outbox_rows) == 1

        decision_rows = store.read_jsonl("decisions")
        assert len(decision_rows) == 1
        assert decision_rows[0]["type"] == "outbox.prepared"

    def test_prepare_context_pointer_succeeds(self, store):
        store.append_jsonl("threads", _make_thread())

        result = prepare_outbox_request(
            store,
            thread_id="sth_test1",
            request_type="THINK",
            surface="discord",
            delivery_mode="context_pointer",
            target={"channel_id": "ch_456"},
            title="Context pointer title",
            message_preview="Context preview",
        )

        assert result["success"] is True
        data = result["data"]
        assert data["id"].startswith("obx_")
        assert data["status"] == "prepared"
        assert data["delivery_mode"] == "context_pointer"
        assert data["surface"] == "discord"

        outbox_rows = store.read_jsonl("outbox")
        assert len(outbox_rows) == 1

        decision_rows = store.read_jsonl("decisions")
        assert len(decision_rows) == 1
        assert decision_rows[0]["type"] == "outbox.prepared"


class TestPrepareIdempotency:
    def test_same_prepare_call_is_idempotent(self, store):
        store.append_jsonl("threads", _make_thread())

        kwargs = dict(
            thread_id="sth_test1",
            request_type="THINK",
            surface="discord",
            delivery_mode="peripheral_reference",
            target={"channel_id": "ch_123"},
            title="Test pointer",
            message_preview="Check thread",
        )

        result1 = prepare_outbox_request(store, **kwargs)
        result2 = prepare_outbox_request(store, **kwargs)

        assert result1["success"] is True
        assert result2["success"] is True
        assert store.read_jsonl("outbox") == [store.read_jsonl("outbox")[0]]
        assert len(store.read_jsonl("outbox")) == 1
        assert result2.get("idempotent_hit") is True


class TestPrepareDenials:
    def test_surface_not_allowed_by_thread(self, store):
        store.append_jsonl("threads", _make_thread(allowed_surfaces=["local"]))

        result = prepare_outbox_request(
            store,
            thread_id="sth_test1",
            request_type="THINK",
            surface="discord",
            delivery_mode="peripheral_reference",
            target={"channel_id": "ch_123"},
        )

        assert result["success"] is False
        assert result["error"] == "surface_not_allowed"

        decision_rows = store.read_jsonl("decisions")
        assert len(decision_rows) == 1
        assert decision_rows[0]["type"] == "outbox.denied"

    def test_direct_discord_channel_thread_denied_by_default(self, store, tmp_path):
        store.append_jsonl("threads", _make_thread())

        # Default config does not include discord_channel_thread in allowed_delivery_modes
        result = prepare_outbox_request(
            store,
            thread_id="sth_test1",
            request_type="THINK",
            surface="discord",
            delivery_mode="discord_channel_thread",
            target={"channel_id": "ch_123"},
        )

        assert result["success"] is False
        assert result["error"] == "delivery_mode_not_allowed"

        # Even with it in allowed_delivery_modes, but direct_modes_enabled=False, it's denied
        store2 = SensoriumStore(instance="test2", state_dir=str(tmp_path / "sensorium2"))
        store2.ensure_dirs()
        store2.append_jsonl("threads", _make_thread())

        result2 = prepare_outbox_request(
            store2,
            thread_id="sth_test1",
            request_type="THINK",
            surface="discord",
            delivery_mode="discord_channel_thread",
            target={"channel_id": "ch_123"},
            config={
                "enabled": True,
                "direct_modes_enabled": False,
                "allowed_delivery_modes": [
                    "peripheral_reference",
                    "context_pointer",
                    "discord_channel_thread",
                ],
            },
        )

        assert result2["success"] is False
        assert result2["error"] == "direct_modes_disabled"

    def test_direct_discord_channel_thread_allowed_when_config_enables(self, store):
        store.append_jsonl("threads", _make_thread())

        direct_config = {
            "enabled": True,
            "direct_modes_enabled": True,
            "allowed_delivery_modes": [
                "peripheral_reference",
                "context_pointer",
                "discord_channel_thread",
            ],
            "discord": {"enabled": True, "token_env": "DISCORD_TOKEN"},
        }

        result = prepare_outbox_request(
            store,
            thread_id="sth_test1",
            request_type="THINK",
            surface="discord",
            delivery_mode="discord_channel_thread",
            target={"channel_id": "ch_123"},
            config=direct_config,
        )

        assert result["success"] is True
        assert result["data"]["delivery_mode"] == "discord_channel_thread"

    def test_archived_thread_cannot_produce_outbox_request(self, store):
        store.append_jsonl("threads", _make_thread(status="archived"))

        result = prepare_outbox_request(
            store,
            thread_id="sth_test1",
            request_type="THINK",
            surface="discord",
            delivery_mode="peripheral_reference",
            target={"channel_id": "ch_123"},
        )

        assert result["success"] is False
        assert result["error"] == "thread_not_openable"

    def test_closed_thread_cannot_produce_outbox_request(self, store):
        store.append_jsonl("threads", _make_thread(status="closed"))

        result = prepare_outbox_request(
            store,
            thread_id="sth_test1",
            request_type="THINK",
            surface="discord",
            delivery_mode="peripheral_reference",
            target={"channel_id": "ch_123"},
        )

        assert result["success"] is False
        assert result["error"] == "thread_not_openable"


class TestPrepareDryRun:
    def test_dry_run_does_not_write(self, store):
        store.append_jsonl("threads", _make_thread())

        result = prepare_outbox_request(
            store,
            thread_id="sth_test1",
            request_type="THINK",
            surface="discord",
            delivery_mode="peripheral_reference",
            target={"channel_id": "ch_123"},
            title="Dry run title",
            message_preview="Dry preview",
            dry_run=True,
        )

        assert result["success"] is True
        assert result.get("dry_run") is True
        assert store.read_jsonl("outbox") == []
        assert store.read_jsonl("decisions") == []


class TestDispatchAdapter:
    def _direct_config(self):
        return {
            "enabled": True,
            "direct_modes_enabled": True,
            "allowed_delivery_modes": [
                "peripheral_reference",
                "context_pointer",
                "discord_channel_thread",
            ],
            "discord": {"enabled": True, "token_env": "DISCORD_TOKEN"},
        }

    def test_fake_adapter_success_records_platform_refs(self, store):
        store.append_jsonl("threads", _make_thread())
        config = self._direct_config()

        prepare_result = prepare_outbox_request(
            store,
            thread_id="sth_test1",
            request_type="THINK",
            surface="discord",
            delivery_mode="discord_channel_thread",
            target={"channel_id": "ch_123"},
            title="Direct thread title",
            message_preview="Hello from Sensorium",
            config=config,
        )
        assert prepare_result["success"] is True
        outbox_id = prepare_result["data"]["id"]

        dispatch_result = dispatch_outbox_request(
            store,
            outbox_id=outbox_id,
            adapter=FakeDiscordAdapter(),
            config=config,
            execute=True,
        )

        assert dispatch_result["success"] is True
        assert dispatch_result["data"]["status"] == "dispatched"
        assert "thread_id" in dispatch_result["data"]["platform_refs"]

        decision_rows = store.read_jsonl("decisions")
        dispatched = [r for r in decision_rows if r["type"] == "outbox.dispatched"]
        assert len(dispatched) == 1

    def test_fake_adapter_failure_records_failed_status(self, store):
        store.append_jsonl("threads", _make_thread())
        config = self._direct_config()

        prepare_result = prepare_outbox_request(
            store,
            thread_id="sth_test1",
            request_type="THINK",
            surface="discord",
            delivery_mode="discord_channel_thread",
            target={"channel_id": "ch_123"},
            title="Failing thread",
            message_preview="This will fail",
            config=config,
        )
        assert prepare_result["success"] is True
        outbox_id = prepare_result["data"]["id"]

        dispatch_result = dispatch_outbox_request(
            store,
            outbox_id=outbox_id,
            adapter=FakeDiscordAdapter(fail=True),
            config=config,
            execute=True,
        )

        assert dispatch_result["success"] is False
        assert dispatch_result["error"] == "dispatch_failed"

        outbox_rows = store.read_jsonl("outbox")
        assert outbox_rows[0]["status"] == "failed"
        assert outbox_rows[0]["status"] != "dispatched"

        decision_rows = store.read_jsonl("decisions")
        failed = [r for r in decision_rows if r["type"] == "outbox.failed"]
        assert len(failed) == 1

    def test_dispatch_without_execute_is_noop(self, store):
        store.append_jsonl("threads", _make_thread())
        config = self._direct_config()

        prepare_result = prepare_outbox_request(
            store,
            thread_id="sth_test1",
            request_type="THINK",
            surface="discord",
            delivery_mode="discord_channel_thread",
            target={"channel_id": "ch_123"},
            config=config,
        )
        assert prepare_result["success"] is True
        outbox_id = prepare_result["data"]["id"]

        dispatch_result = dispatch_outbox_request(
            store,
            outbox_id=outbox_id,
            adapter=FakeDiscordAdapter(),
            config=config,
        )

        assert dispatch_result["success"] is False
        assert dispatch_result["error"] == "execute_not_set"


class TestThreadInteractionRefs:
    def test_prepare_updates_thread_interaction_refs(self, store):
        store.append_jsonl("threads", _make_thread())

        result = prepare_outbox_request(
            store,
            thread_id="sth_test1",
            request_type="THINK",
            surface="discord",
            delivery_mode="peripheral_reference",
            target={"channel_id": "ch_123"},
            title="Interaction ref test",
            message_preview="Checking refs",
        )

        assert result["success"] is True
        outbox_id = result["data"]["id"]

        threads = store.read_jsonl("threads")
        thread = threads[0]
        refs = thread.get("interaction_refs", [])
        assert any(
            r.get("type") == "outbox_prepared" and r.get("outbox_id") == outbox_id
            for r in refs
        )
