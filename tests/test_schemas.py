"""Tests for agent_sensorium.schemas."""

import pytest

from agent_sensorium.schemas import (
    intersect_allowed_surfaces,
    merge_sensitivity,
    new_id,
    normalize_signal,
    utc_now_iso,
    validate_signal,
)


class TestNewId:
    def test_prefix(self):
        assert new_id("sig").startswith("sig_")
        assert new_id("evt").startswith("evt_")
        assert new_id("cand").startswith("cand_")

    def test_unique(self):
        ids = {new_id("sig") for _ in range(100)}
        assert len(ids) == 100


class TestUtcNowIso:
    def test_format(self):
        ts = utc_now_iso()
        assert ts.endswith("Z")
        assert "T" in ts


class TestNormalizeSignal:
    def test_adds_defaults(self):
        raw = {"sensor": "test", "source": "manual", "kind": "note", "summary": "hi"}
        sig = normalize_signal(raw)
        assert sig["id"].startswith("sig_")
        assert "ts" in sig
        assert sig["sensitivity"] == "private"
        assert sig["strength_hint"] == 0.5
        assert sig["ttl_hours"] == 72

    def test_preserves_existing(self):
        raw = {
            "id": "sig_custom",
            "sensor": "test",
            "source": "manual",
            "kind": "note",
            "summary": "hi",
            "strength_hint": 0.9,
        }
        sig = normalize_signal(raw)
        assert sig["id"] == "sig_custom"
        assert sig["strength_hint"] == 0.9


class TestValidateSignal:
    def test_valid_passes(self):
        sig = {"sensor": "test", "source": "manual", "kind": "note", "summary": "hi"}
        validate_signal(sig)  # should not raise

    def test_missing_fields_raises(self):
        with pytest.raises(ValueError, match="missing required fields"):
            validate_signal({"sensor": "test"})

    def test_invalid_sensitivity_raises(self):
        sig = {
            "sensor": "test",
            "source": "manual",
            "kind": "note",
            "summary": "hi",
            "sensitivity": "bogus",
        }
        with pytest.raises(ValueError, match="Invalid sensitivity"):
            validate_signal(sig)


class TestMergeSensitivity:
    def test_most_restrictive_wins(self):
        assert merge_sensitivity(["private", "public_safe"]) == "private"
        assert merge_sensitivity(["local_only", "private"]) == "local_only"
        assert merge_sensitivity(["public_safe", "public_safe"]) == "public_safe"

    def test_empty_defaults_private(self):
        assert merge_sensitivity([]) == "private"


class TestIntersectAllowedSurfaces:
    def test_intersection(self):
        result = intersect_allowed_surfaces([["local", "dashboard"], ["local", "dm"]])
        assert result == ["local"]

    def test_empty_input(self):
        assert intersect_allowed_surfaces([]) == []

    def test_single_item(self):
        assert intersect_allowed_surfaces([["local", "dashboard"]]) == ["dashboard", "local"]

    def test_no_overlap(self):
        assert intersect_allowed_surfaces([["local"], ["dm"]]) == []
