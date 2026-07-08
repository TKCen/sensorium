"""Provider subscription budget threshold/debounce tests."""

from agent_sensorium.provider_budget import evaluate_provider_budget_sample
from agent_sensorium.sensors import classify_provider_budget_pressure


RESET = "2026-07-06T02:00:00+02:00"


def _sample(*, provider="minimax", window="weekly", used=0, status=0, reset=RESET, reset_after=None, window_seconds=604800, selected_model="general"):
    if reset_after is None:
        reset_after = round(window_seconds * (1 - (used / 100)))
    return {
        "available": True,
        "generated_at": "2026-07-02T14:00:00+02:00",
        "providers": [
            {
                "provider": provider,
                "available": True,
                "selected_model": selected_model,
                "windows": [
                    {
                        "window": window,
                        "used_percent": used,
                        "status": status,
                        "reset_at": reset,
                        "reset_after_seconds": reset_after,
                        "window_seconds": window_seconds,
                    }
                ],
            }
        ],
    }


def _first_event(sample, state=None):
    events, state = evaluate_provider_budget_sample(sample, state=state)
    return (events[0] if events else None), state


def test_minimax_weekly_thresholds_emit_70_85_95_100_and_no_duplicate_same_band():
    event, state = _first_event(_sample(used=69))
    assert event is None

    event, state = _first_event(_sample(used=70), state)
    assert event["band"] == "watch"
    assert event["state_key"] == f"minimax:weekly:{RESET}:watch"

    event, state = _first_event(_sample(used=70), state)
    assert event is None

    event, state = _first_event(_sample(used=85), state)
    assert event["band"] == "degraded"
    assert event["state_key"] == f"minimax:weekly:{RESET}:degraded"

    event, state = _first_event(_sample(used=95), state)
    assert event["band"] == "critical"
    assert event["state_key"] == f"minimax:weekly:{RESET}:critical"

    event, state = _first_event(_sample(used=100, status=2), state)
    assert event["band"] == "exhausted"
    assert event["state_key"] == f"minimax:weekly:{RESET}:exhausted"
    assert "resets" in event["reason"]


def test_minimax_5h_thresholds_use_same_bands():
    five_reset = "2026-07-04T19:00:00+02:00"
    state = None
    for used, expected in ((70, "watch"), (85, "degraded"), (95, "critical"), (100, "exhausted")):
        event, state = _first_event(
            _sample(
                window="5h",
                used=used,
                status=2 if expected == "exhausted" else 0,
                reset=five_reset,
                window_seconds=18000,
            ),
            state,
        )
        assert event["window"] == "5h"
        assert event["band"] == expected


def test_pace_ahead_degraded_and_critical_transitions():
    # 30% used at 30% elapsed is equilibrium.
    event, state = _first_event(_sample(used=30, reset_after=700, window_seconds=1000))
    assert event is None

    # 41% used at 30% elapsed is 11pp ahead: degraded before raw 70% watch.
    event, state = _first_event(_sample(used=41, reset_after=700, window_seconds=1000), state)
    assert event["band"] == "degraded"
    assert event["values"]["over_expected_pp"] == 11.0
    assert "pace degraded" in event["reason"]

    # 50% used at 30% elapsed is 20pp ahead: critical before raw 95%.
    event, state = _first_event(_sample(used=50, reset_after=700, window_seconds=1000), state)
    assert event["band"] == "critical"
    assert event["values"]["over_expected_pp"] == 20.0
    assert "pace critical" in event["reason"]


def test_recovery_and_reset_emit_after_prior_pressure():
    event, state = _first_event(_sample(used=85))
    assert event["band"] == "degraded"

    event, state = _first_event(_sample(used=20), state)
    assert event["band"] == "healthy"
    assert event["transition"] == "degraded_to_recovered"

    event, state = _first_event(_sample(used=95, reset="2026-07-13T02:00:00+02:00"), state)
    assert event["band"] == "critical"

    events, state = evaluate_provider_budget_sample(
        _sample(used=5, reset="2026-07-20T02:00:00+02:00"),
        state=state,
    )
    assert events[0]["band"] == "healthy"
    assert events[0]["transition"] == "critical_to_recovered"
    assert events[0]["metric_family"] == "weekly_reset"


def test_first_observation_already_exhausted_emits_and_records_missed_warnings():
    event, state = _first_event(_sample(used=100, status=2))
    assert event["band"] == "exhausted"
    assert event["missed_warning"] is True
    assert event["missed_bands"] == ["watch", "degraded", "critical"]
    assert state["missed_warnings"][0]["observed_band"] == "exhausted"


def test_provider_blocked_or_limit_reached_is_critical_before_percent_thresholds():
    sample = _sample(used=12)
    sample["providers"][0]["allowed"] = False

    event, state = _first_event(sample)

    assert event is not None
    assert event["band"] == "critical"
    assert event["reason"] == "weekly provider blocked or limit reached"
    assert state["missed_warnings"][0]["missed_bands"] == ["watch", "degraded"]


def test_sensorium_signal_is_private_local_compact_and_mentions_reset_for_exhausted():
    signal, state = classify_provider_budget_pressure(_sample(used=100, status=2), state={})
    assert signal is not None
    assert signal["kind"] == "inference_budget_pressure"
    assert signal["sensor"] == "sensorium.provider_budget_pressure"
    assert signal["sensitivity"] == "private"
    assert signal["allowed_surfaces"] == ["local"]
    assert "minimax-energy" in signal["correlation_keys"]
    assert "provider-budget" in signal["correlation_keys"]
    assert "paid-stack-utilization" in signal["correlation_keys"]
    assert signal["pressure_level"] == "exhausted"
    assert RESET in signal["summary"]
    assert signal["values"]["primary"]["selected_model"] == "general"
    assert state["missed_warnings"]
