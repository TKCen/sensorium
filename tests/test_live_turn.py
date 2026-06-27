"""Tests for live-turn salience receipts and cheap residue review."""

from agent_sensorium.live_turn import (
    build_turn_review_receipt,
    live_turn_receipt_metrics,
    live_turn_review_metrics,
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


def test_live_turn_receipt_metrics_normalizes_persisted_boolean_strings():
    metrics = live_turn_receipt_metrics([
        {
            "type": "live_turn.ingest_decision",
            "ts": "2026-06-27T10:00:00Z",
            "foreground_resolution": "full",
            "residue": "none",
            "durable_capture": "docs",
            "ingested": "false",
            "background_action_allowed": "false",
            "skipped_reason": "foreground_owned_no_residue",
        },
        {
            "type": "live_turn.ingest_decision",
            "ts": "2026-06-27T10:01:00Z",
            "foreground_resolution": "partial",
            "residue": "watch",
            "durable_capture": "none",
            "ingested": "true",
            "background_action_allowed": "true",
        },
    ])

    assert metrics["ingested_count"] == 1
    assert metrics["skipped_count"] == 1
    assert metrics["foreground_owned_no_residue_count"] == 1
    assert metrics["background_action_allowed_count"] == 1
    assert metrics["recent"][0]["ingested"] is True
    assert metrics["recent"][0]["background_action_allowed"] is True
    assert metrics["recent"][1]["ingested"] is False
    assert metrics["recent"][1]["background_action_allowed"] is False


def test_live_turn_receipt_metrics_sanitizes_invalid_timestamps():
    metrics = live_turn_receipt_metrics([
        {
            "type": "live_turn.ingest_decision",
            "ts": "RAW_TRANSCRIPT_BODY_DO_NOT_LEAK",
            "foreground_resolution": "full",
            "residue": "none",
            "durable_capture": "docs",
            "ingested": False,
            "skipped_reason": "foreground_owned_no_residue",
        },
        {
            "type": "live_turn.ingest_decision",
            "created_at": "2026-06-27T10:01:00Z",
            "foreground_resolution": "partial",
            "residue": "watch",
            "durable_capture": "none",
            "ingested": True,
        },
    ])

    serialized = repr(metrics)
    assert "RAW_TRANSCRIPT_BODY_DO_NOT_LEAK" not in serialized
    assert metrics["latest_ts"] == "2026-06-27T10:01:00Z"
    assert metrics["recent"][0]["ts"] == "2026-06-27T10:01:00Z"
    assert metrics["recent"][1]["ts"] == ""


def test_turn_review_receipt_records_pending_posture_without_text():
    review = review_turn_for_residue(
        user_text="SECRET_RAW_TRANSCRIPT says this matters and should be watched later.",
        assistant_text="I missed the capture.",
    )

    receipt = build_turn_review_receipt(review=review)

    serialized = repr(receipt)
    assert "SECRET_RAW_TRANSCRIPT" not in serialized
    assert receipt["type"] == "live_turn.review_decision"
    assert receipt["decision"] == "sensorium_residue_candidate"
    assert receipt["reason"] == "salience_cue_without_capture"
    assert receipt["pending_review"] is True
    assert receipt["has_salience_cue"] is True
    assert receipt["background_action_allowed"] is False
    assert receipt["input_counts"]["user_chars"] > 0


def test_live_turn_review_metrics_counts_pending_without_raw_text_leakage():
    marker = "RAW_TRANSCRIPT_BODY_DO_NOT_LEAK"
    metrics = live_turn_review_metrics([
        {
            "type": "live_turn.review_decision",
            "ts": marker,
            "summary": f"private {marker}",
            "decision": "sensorium_residue_candidate",
            "reason": "salience_cue_without_capture",
            "pending_review": "true",
            "has_salience_cue": "true",
            "durable_capture_seen": "false",
        },
        {
            "type": "live_turn.review_decision",
            "ts": "2026-06-27T10:01:00Z",
            "decision": "no_action",
            "reason": "salience_captured_elsewhere",
            "pending_review": "false",
            "has_salience_cue": True,
            "durable_capture_seen": True,
        },
        {
            "type": "live_turn.review_decision",
            "ts": "2026-06-27T10:02:00Z",
            "decision": "SECRET_DECISION",
            "reason": "SECRET_REASON",
            "pending_review": "true",
        },
    ])

    serialized = repr(metrics)
    assert marker not in serialized
    assert "SECRET" not in serialized
    assert metrics["receipt_count"] == 3
    assert metrics["pending_review_count"] == 2
    assert metrics["no_action_count"] == 1
    assert metrics["salience_cue_count"] == 2
    assert metrics["durable_capture_seen_count"] == 1
    assert metrics["decision_breakdown"] == {
        "sensorium_residue_candidate": 1,
        "no_action": 1,
        "unknown": 1,
    }
    assert metrics["reason_breakdown"] == {
        "salience_cue_without_capture": 1,
        "salience_captured_elsewhere": 1,
        "unknown": 1,
    }
    assert metrics["latest_ts"] == "2026-06-27T10:02:00Z"
