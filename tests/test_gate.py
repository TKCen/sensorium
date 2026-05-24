"""Tests for agent_sensorium.gate."""

from agent_sensorium.gate import (
    DEFAULT_CONFIG,
    candidate_fingerprint,
    event_to_candidate,
    promote_signal_to_event,
    should_promote_signal,
    signal_fingerprint,
)


def _make_signal(**overrides):
    base = {
        "id": "sig_test",
        "sensor": "test_sensor",
        "source": "manual",
        "kind": "note",
        "summary": "A test signal",
        "strength_hint": 0.5,
        "correlation_keys": ["test"],
        "sensitivity": "private",
        "allowed_surfaces": ["local"],
    }
    base.update(overrides)
    return base


class TestShouldPromoteSignal:
    def test_weak_signal_not_promoted(self):
        sig = _make_signal(strength_hint=0.3)
        promoted, reason = should_promote_signal(sig)
        assert promoted is False

    def test_strong_signal_promoted(self):
        sig = _make_signal(strength_hint=0.8)
        promoted, reason = should_promote_signal(sig)
        assert promoted is True
        assert "strength" in reason

    def test_important_kind_with_sufficient_strength(self):
        sig = _make_signal(kind="design_decision", strength_hint=0.65)
        promoted, reason = should_promote_signal(sig)
        assert promoted is True
        assert "kind" in reason

    def test_important_kind_below_kind_threshold(self):
        sig = _make_signal(kind="design_decision", strength_hint=0.4)
        promoted, reason = should_promote_signal(sig)
        assert promoted is False

    def test_unknown_kind_needs_full_strength(self):
        sig = _make_signal(kind="random_observation", strength_hint=0.7)
        promoted, _ = should_promote_signal(sig)
        assert promoted is False

    def test_custom_config(self):
        config = {
            "thresholds": {
                "single_signal_strength": 0.5,
                "important_kind_strength": 0.3,
                "candidate_pressure": 0.4,
            },
            "promote_kinds": ["note"],
        }
        sig = _make_signal(kind="note", strength_hint=0.35)
        promoted, _ = should_promote_signal(sig, config)
        assert promoted is True


class TestPromoteSignalToEvent:
    def test_event_shape(self):
        sig = _make_signal(strength_hint=0.9, kind="design_decision")
        event = promote_signal_to_event(sig)
        assert event["id"].startswith("evt_")
        assert event["type"] == "sensor.event.promoted"
        assert event["kind"] == "design_decision"
        assert event["source_signal_ids"] == ["sig_test"]
        assert event["strength"] == 0.9
        assert event["sensitivity"] == "private"

    def test_event_inherits_correlation_keys(self):
        sig = _make_signal(correlation_keys=["a", "b"])
        event = promote_signal_to_event(sig)
        assert event["correlation_keys"] == ["a", "b"]


class TestEventToCandidate:
    def test_candidate_shape(self):
        event = {
            "id": "evt_test",
            "ts": "2026-01-01T00:00:00Z",
            "type": "sensor.event.promoted",
            "kind": "design_decision",
            "summary": "Test event",
            "strength": 0.8,
            "correlation_keys": ["test"],
            "sensitivity": "private",
            "allowed_surfaces": ["local", "dashboard"],
        }
        cand = event_to_candidate(event)
        assert cand["id"].startswith("cand_")
        assert cand["status"] == "candidate"
        assert cand["kind"] == "design_decision"
        assert cand["event_ids"] == ["evt_test"]
        assert 0 < cand["pressure"] <= 1.0
        assert cand["sensitivity"] == "private"
        assert cand["allowed_surfaces"] == ["local", "dashboard"]


class TestFingerprints:
    def test_signal_fingerprint_deterministic(self):
        sig = _make_signal()
        fp1 = signal_fingerprint(sig)
        fp2 = signal_fingerprint(sig)
        assert fp1 == fp2
        assert len(fp1) == 16

    def test_different_signals_different_fingerprints(self):
        sig1 = _make_signal(summary="First")
        sig2 = _make_signal(summary="Second")
        assert signal_fingerprint(sig1) != signal_fingerprint(sig2)

    def test_candidate_fingerprint_deterministic(self):
        cand = {"kind": "test", "correlation_keys": ["a"], "summary": "Test"}
        fp1 = candidate_fingerprint(cand)
        fp2 = candidate_fingerprint(cand)
        assert fp1 == fp2
