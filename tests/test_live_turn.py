"""Tests for live-turn salience receipts and cheap residue review."""

from agent_sensorium.live_turn import (
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
