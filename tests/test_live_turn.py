"""Tests for live-turn salience receipts and cheap residue review."""

from agent_sensorium.live_turn import (
    live_turn_receipt_metrics,
    normalize_live_turn_intent,
    review_turn_for_residue,
    should_ingest_live_residue,
)


def test_full_foreground_resolution_with_no_residue_does_not_ingest():
    intent = normalize_live_turn_intent(
        foreground_action_taken=True,
        foreground_resolution="full",
        residue="none",
        durable_capture="docs",
    )

    should_ingest, reason = should_ingest_live_residue(intent)

    assert should_ingest is False
    assert reason == "foreground_owned_no_residue"


def test_residue_allows_ingest_even_when_foreground_acted():
    intent = normalize_live_turn_intent(
        foreground_action_taken=True,
        foreground_resolution="partial",
        residue="watch",
        durable_capture="docs",
    )

    should_ingest, reason = should_ingest_live_residue(intent)

    assert should_ingest is True
    assert reason == "residue_present"


def test_omitted_residue_keeps_backward_compatible_ingest_intent():
    intent = normalize_live_turn_intent()

    should_ingest, reason = should_ingest_live_residue(intent)

    assert intent["residue"] == "later_review"
    assert should_ingest is True
    assert reason == "residue_present"


def test_turn_review_flags_uncaptured_salience_but_ignores_documented_turn():
    missed = review_turn_for_residue(
        user_text="This matters and we should watch it later.",
        assistant_text="Agreed.",
    )
    captured = review_turn_for_residue(
        user_text="Document this before we start.",
        assistant_text="Documented it.",
        docs_updated=True,
    )

    assert missed["decision"] == "sensorium_residue_candidate"
    assert missed["reason"] == "salience_cue_without_capture"
    assert captured["decision"] == "no_action"
    assert captured["reason"] == "salience_captured_elsewhere"


def test_live_turn_receipt_metrics_counts_without_raw_text_leakage():
    metrics = live_turn_receipt_metrics([
        {
            "type": "live_turn.ingest_decision",
            "ts": "2026-06-27T10:00:00Z",
            "surface": "discord_SECRET_SURFACE",
            "summary": "SECRET_RAW_TRANSCRIPT should never leak",
            "reason": "SECRET_REASON should never leak",
            "foreground_resolution": "full",
            "residue": "none",
            "durable_capture": "docs",
            "background_action_allowed": True,
            "ingested": False,
            "skipped_reason": "foreground_owned_no_residue",
        },
        {
            "type": "live_turn.ingest_decision",
            "ts": "2026-06-27T10:01:00Z",
            "surface": "local",
            "summary": "another private line",
            "foreground_resolution": "partial",
            "residue": "watch",
            "durable_capture": "none",
            "ingested": True,
        },
        {"type": "other.receipt", "summary": "SECRET_OTHER"},
    ])

    assert metrics["receipt_count"] == 2
    assert metrics["ingested_count"] == 1
    assert metrics["skipped_count"] == 1
    assert metrics["foreground_owned_no_residue_count"] == 1
    assert metrics["background_action_allowed_count"] == 1
    assert metrics["residue_breakdown"] == {"none": 1, "watch": 1}
    assert metrics["foreground_resolution_breakdown"] == {"full": 1, "partial": 1}
    assert metrics["durable_capture_breakdown"] == {"docs": 1, "none": 1}

    serialized = repr(metrics)
    assert "SECRET" not in serialized
    assert "discord_SECRET_SURFACE" not in serialized
    assert metrics["recent"][0] == {
        "ts": "2026-06-27T10:01:00Z",
        "ingested": True,
        "residue": "watch",
        "foreground_resolution": "partial",
        "durable_capture": "none",
        "skipped_reason": "none",
        "background_action_allowed": False,
    }


def test_live_turn_receipt_metrics_collapses_unknown_scalars():
    metrics = live_turn_receipt_metrics([
        {
            "type": "live_turn.ingest_decision",
            "ts": "2026-06-27T10:00:00Z",
            "foreground_resolution": "SECRET_RESOLUTION",
            "residue": "SECRET_RESIDUE",
            "durable_capture": "SECRET_CAPTURE",
            "ingested": False,
            "skipped_reason": "SECRET_SKIP_REASON",
        }
    ])

    serialized = repr(metrics)
    assert "SECRET" not in serialized
    assert metrics["residue_breakdown"] == {"unknown": 1}
    assert metrics["foreground_resolution_breakdown"] == {"unknown": 1}
    assert metrics["durable_capture_breakdown"] == {"unknown": 1}
    assert metrics["skipped_reason_breakdown"] == {"other": 1}
    assert metrics["recent"][0]["skipped_reason"] == "other"
