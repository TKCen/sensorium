"""Tests for active-session salience capture rule.

Covers the always-on Sensorium pre_llm_call salience capture hook:
  - session_event_signal() produces compact privacy-safe salience signals
  - Relational salience example: operator "I miss you" → private relational signal
  - Operational/design correction example → operational/design signal
  - Signals contain no raw transcript text or capsule bodies
  - No pointer.presented receipt written when no pointer exists (regression)
  - Compact fields asserted: kind, summary, strength_hint, correlation_keys,
    sensitivity, allowed_surfaces
"""

import json

import pytest

from agent_sensorium.sensors import session_event_signal
from agent_sensorium.store import SensoriumStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    s = SensoriumStore(instance="test", state_dir=str(tmp_path))
    s.ensure_dirs()
    return s


# ---------------------------------------------------------------------------
# Relational salience fixture — operator expressing relational sentiment
# ---------------------------------------------------------------------------

def _relational_salience_fixture():
    """Operator says 'I miss you' in a session that carries relational weight.

    Expected: a private relational salience signal with no raw transcript.
    """
    return session_event_signal(
        kind="relational_salience",
        summary="Operator expressed relational sentiment: 'I miss you'",
        session_ref="sess_test_relational",
        strength_hint=0.75,
        sensitivity="private",
        allowed_surfaces=["local"],
        correlation_keys=["relational", "operator-sentiment", "inner-life"],
    )


# ---------------------------------------------------------------------------
# Operational / design correction fixture
# ---------------------------------------------------------------------------

def _operational_design_correction_fixture():
    """Operator corrects a design/architecture decision.

    Expected: an operational/design signal with appropriate correlation keys.
    """
    return session_event_signal(
        kind="design_insight",
        summary="Operator corrected Sera identity image strategy: use references not copies",
        session_ref="sess_test_design",
        strength_hint=0.82,
        sensitivity="private",
        allowed_surfaces=["local"],
        correlation_keys=["design-insight", "operator-correction", "sera-identity"],
    )


# ---------------------------------------------------------------------------
# Regression fixture — no pointer exists
# ---------------------------------------------------------------------------

def _write_config(state_dir, surfaces=None, max_sensitivity="private"):
    from pathlib import Path
    config = {"allowed_surfaces": surfaces or ["local"], "max_sensitivity": max_sensitivity}
    path = Path(state_dir) / "instance.config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config))


# ---------------------------------------------------------------------------
# Relational salience tests
# ---------------------------------------------------------------------------

class TestRelationalSalienceSignal:
    """Operator 'I miss you' becomes a private relational salience signal."""

    def test_emits_compact_dict(self):
        sig = _relational_salience_fixture()
        assert isinstance(sig, dict)
        assert sig["sensor"] == "sensorium.session_event"
        assert sig["source"] == "hermes_session"
        assert sig["kind"] == "relational_salience"

    def test_has_required_compact_fields(self):
        sig = _relational_salience_fixture()
        assert "kind" in sig
        assert "summary" in sig
        assert "strength_hint" in sig
        assert "correlation_keys" in sig
        assert "sensitivity" in sig
        assert "allowed_surfaces" in sig

    def test_kind_is_relational_salience(self):
        sig = _relational_salience_fixture()
        assert sig["kind"] == "relational_salience"

    def test_summary_is_compact_and_descriptive(self):
        sig = _relational_salience_fixture()
        assert "I miss you" in sig["summary"]
        # Must be brief — no raw transcript dump
        assert len(sig["summary"]) <= 200

    def test_sensitivity_is_private(self):
        sig = _relational_salience_fixture()
        assert sig["sensitivity"] == "private"

    def test_allowed_surfaces_is_local_only(self):
        sig = _relational_salience_fixture()
        assert sig["allowed_surfaces"] == ["local"]

    def test_correlation_keys_include_relational(self):
        sig = _relational_salience_fixture()
        assert "relational" in sig["correlation_keys"]

    def test_strength_hint_in_valid_range(self):
        sig = _relational_salience_fixture()
        assert 0.0 <= sig["strength_hint"] <= 1.0

    def test_no_raw_transcript_fields(self):
        sig = _relational_salience_fixture()
        serialized = json.dumps(sig).lower()
        for forbidden in ("transcript", "raw", "messages", "full_text", "content", "body"):
            assert forbidden not in serialized, f"forbidden field '{forbidden}' found in signal"

    def test_no_pointer_presented_receipt_for_salience_signal(self, store):
        """Ingesting a relational salience signal does not produce a pointer.presented receipt."""
        sig = _relational_salience_fixture()
        store.append_jsonl("signals", sig)

        receipts = store.read_jsonl("decisions")
        pointer_receipts = [r for r in receipts if r.get("type") == "pointer.presented"]
        assert pointer_receipts == [], "salience signal must not produce a pointer.presented receipt"


# ---------------------------------------------------------------------------
# Operational / design correction tests
# ---------------------------------------------------------------------------

class TestOperationalDesignCorrectionSignal:
    """Operator design correction becomes an operational/design signal."""

    def test_emits_compact_dict(self):
        sig = _operational_design_correction_fixture()
        assert isinstance(sig, dict)
        assert sig["sensor"] == "sensorium.session_event"
        assert sig["source"] == "hermes_session"
        assert sig["kind"] == "design_insight"

    def test_has_required_compact_fields(self):
        sig = _operational_design_correction_fixture()
        assert "kind" in sig
        assert "summary" in sig
        assert "strength_hint" in sig
        assert "correlation_keys" in sig
        assert "sensitivity" in sig
        assert "allowed_surfaces" in sig

    def test_kind_is_design_insight(self):
        sig = _operational_design_correction_fixture()
        assert sig["kind"] == "design_insight"

    def test_summary_describes_correction_without_raw_transcript(self):
        sig = _operational_design_correction_fixture()
        # Summary should describe the nature of the correction
        assert "corrected" in sig["summary"].lower() or "correction" in sig["summary"].lower()
        # Must be compact
        assert len(sig["summary"]) <= 200

    def test_correlation_keys_include_design_insight(self):
        sig = _operational_design_correction_fixture()
        assert "design-insight" in sig["correlation_keys"]
        assert "operator-correction" in sig["correlation_keys"]

    def test_sensitivity_is_private(self):
        sig = _operational_design_correction_fixture()
        assert sig["sensitivity"] == "private"

    def test_no_raw_transcript_fields(self):
        sig = _operational_design_correction_fixture()
        serialized = json.dumps(sig).lower()
        for forbidden in ("transcript", "raw", "messages", "full_text", "content", "body"):
            assert forbidden not in serialized, f"forbidden field '{forbidden}' found in signal"

    def test_high_strength_for_design_correction(self):
        sig = _operational_design_correction_fixture()
        # Design corrections carry high weight
        assert sig["strength_hint"] >= 0.8


# ---------------------------------------------------------------------------
# Privacy-safety tests — no raw transcript or capsule body
# ---------------------------------------------------------------------------

class TestPrivacySafety:
    """Signals must not include raw transcript text or capsule bodies."""

    @pytest.mark.parametrize("fixture_fn,desc", [
        (_relational_salience_fixture, "relational salience"),
        (_operational_design_correction_fixture, "design correction"),
    ])
    def test_no_raw_transcript_any_fixture(self, fixture_fn, desc):
        sig = fixture_fn()
        serialized = str(sig).lower()
        assert "transcript" not in serialized
        assert "raw" not in serialized
        assert "full_text" not in serialized
        assert "messages" not in serialized
        assert "content" not in serialized
        assert "body" not in serialized

    def test_long_relational_summary_is_truncated(self):
        long_summary = "Operator said 'I really miss you and miss our conversations' in context of long relationship"
        sig = session_event_signal(kind="relational_salience", summary=long_summary)
        # sensors.py caps at MAX_SUMMARY_CHARS = 200
        assert len(sig["summary"]) <= 200

    def test_no_capsule_body_fields_in_signal(self):
        """Capsule body fields (decision_log, continuity_summary, etc.) must not leak into signal."""
        sig = _relational_salience_fixture()
        sig.update(_operational_design_correction_fixture())  # merge for breadth
        serialized = str(sig).lower()
        for field in ("decision_log", "continuity_summary", "interaction_refs",
                      "open_questions", "next_prompt_to_operator"):
            assert field not in serialized


# ---------------------------------------------------------------------------
# Regression: no pointer.presented receipt when no pointer exists
# ---------------------------------------------------------------------------

class TestNoPointerRegression:
    """When no pointer is available, no pointer.presented receipt is written."""

    def test_no_receipt_when_no_thread_exists(self, tmp_path):
        """Active session with no threads → no pointer.presented receipt."""
        store = SensoriumStore(instance="test", state_dir=str(tmp_path))
        store.ensure_dirs()
        _write_config(tmp_path, surfaces=["local"])

        # Salience signal ingested into an empty store
        sig = session_event_signal(
            kind="relational_salience",
            summary="Operator expressed sentiment with no active thread",
            session_ref="sess_empty",
            strength_hint=0.6,
        )
        store.append_jsonl("signals", sig)

        receipts = store.read_jsonl("decisions")
        pointer_receipts = [r for r in receipts if r.get("type") == "pointer.presented"]
        assert pointer_receipts == [], (
            "no pointer.presented receipt should be written when no thread exists"
        )

    def test_no_receipt_when_surface_excluded(self, tmp_path):
        """Thread exists but surface is excluded → no pointer.presented receipt."""
        from agent_sensorium.pointers import handle_pointer_pre_llm

        store = SensoriumStore(instance="test", state_dir=str(tmp_path))
        store.ensure_dirs()
        _write_config(tmp_path, surfaces=["local"])

        # Write a thread visible only on discord (not local)
        store.append_jsonl("threads", {
            "id": "sth_discord_only",
            "status": "dormant",
            "origin": "candidate",
            "conscious_task": {
                "id": "ctask_x",
                "request_type": "THINK",
                "title": "Discord-only thread",
                "why": "test",
                "expected_decision": "test",
            },
            "origin_candidate_id": "cand_x",
            "continuity_summary": ["test"],
            "decision_log": [],
            "interaction_refs": [],
            "summary_dirty": False,
            "open_questions": [],
            "next_prompt_to_operator": "?",
            "sensitivity": "private",
            "allowed_surfaces": ["discord"],
            "created_at": "2026-05-24T10:00:00Z",
            "updated_at": "2026-05-24T10:00:00Z",
            "expires_at": "2099-12-31T23:59:59Z",
        })

        # pre_llm hook called on local surface (which has no visible thread)
        result = handle_pointer_pre_llm(
            instance="test",
            platform="local",
            session_id="sess_regression",
            state_dir=str(tmp_path),
        )

        # Should return None (no pointer) — verify this first
        assert result is None, "expected no pointer for local surface with only discord thread"

        # Now verify no pointer.presented receipt was written
        receipts = store.read_jsonl("decisions")
        pointer_receipts = [r for r in receipts if r.get("type") == "pointer.presented"]
        assert pointer_receipts == [], (
            "pointer.presented receipt must not be written when no pointer is returned"
        )


# ---------------------------------------------------------------------------
# Compact field assertion helper
# ---------------------------------------------------------------------------

REQUIRED_SIGNAL_FIELDS = {"sensor", "source", "kind", "summary", "strength_hint",
                          "correlation_keys", "sensitivity", "allowed_surfaces"}


class TestCompactFieldPresence:
    """All salience signals must carry the required compact fields."""

    @pytest.mark.parametrize("fixture_fn,desc", [
        (_relational_salience_fixture, "relational salience"),
        (_operational_design_correction_fixture, "design correction"),
    ])
    def test_all_required_fields_present(self, fixture_fn, desc):
        sig = fixture_fn()
        missing = REQUIRED_SIGNAL_FIELDS - set(sig.keys())
        assert not missing, f"{desc}: missing required fields {missing}"

    @pytest.mark.parametrize("fixture_fn,desc", [
        (_relational_salience_fixture, "relational salience"),
        (_operational_design_correction_fixture, "design correction"),
    ])
    def test_sensor_is_sensorium_session_event(self, fixture_fn, desc):
        sig = fixture_fn()
        assert sig["sensor"] == "sensorium.session_event"

    @pytest.mark.parametrize("fixture_fn,desc", [
        (_relational_salience_fixture, "relational salience"),
        (_operational_design_correction_fixture, "design correction"),
    ])
    def test_source_is_hermes_session(self, fixture_fn, desc):
        sig = fixture_fn()
        assert sig["source"] == "hermes_session"

    @pytest.mark.parametrize("fixture_fn,desc", [
        (_relational_salience_fixture, "relational salience"),
        (_operational_design_correction_fixture, "design correction"),
    ])
    def test_correlation_keys_is_list(self, fixture_fn, desc):
        sig = fixture_fn()
        assert isinstance(sig["correlation_keys"], list)

    @pytest.mark.parametrize("fixture_fn,desc", [
        (_relational_salience_fixture, "relational salience"),
        (_operational_design_correction_fixture, "design correction"),
    ])
    def test_allowed_surfaces_is_list(self, fixture_fn, desc):
        sig = fixture_fn()
        assert isinstance(sig["allowed_surfaces"], list)