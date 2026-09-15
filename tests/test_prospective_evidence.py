from __future__ import annotations

import json
import multiprocessing as mp
import itertools
import random
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from agent_sensorium.prospective_evidence import (
    ProspectiveEvidenceCapture,
    _wait_for_observation_queue,
    observe_after_success,
)


def config(start: str = "2026-01-01T00:00:00Z") -> dict:
    expiry = datetime.fromisoformat(start.replace("Z", "+00:00")) + timedelta(days=14)
    return {
        "enabled": True,
        "start_at": start,
        "expires_at": expiry.isoformat().replace("+00:00", "Z"),
    }


def active(tmp_path: Path) -> ProspectiveEvidenceCapture:
    capture = ProspectiveEvidenceCapture(tmp_path / "profile", config())
    assert capture.activate()
    return capture


def evidence(receipt: str, source: str = "manual", **extra) -> dict:
    return {
        "source_receipt": receipt,
        "candidate_id": f"candidate-{receipt}",
        "source_class": source,
        "change_state": "new",
        **extra,
    }


def _worker(root: str, count: int, queue) -> None:
    capture = ProspectiveEvidenceCapture(root, config())
    queue.put(
        sum(
            capture.observe(
                "source_observed",
                evidence(f"worker-{mp.current_process().name}-{i}"),
                now="2026-01-01T01:00:00Z",
            )
            for i in range(count)
        )
    )


def test_off_is_inert_and_observer_contains_failure(tmp_path, monkeypatch):
    root = tmp_path / "off"
    assert not ProspectiveEvidenceCapture(root, {"enabled": False}).observe(
        "settled", evidence("raw"), now="2026-01-01T00:00:00Z"
    )
    assert not root.exists()
    monkeypatch.setattr(
        ProspectiveEvidenceCapture,
        "observe",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk")),
    )
    assert observe_after_success(root, config(), "settled", evidence("raw")) is None
    assert _wait_for_observation_queue(10)


def test_closed_schema_drops_privacy_probes_and_never_persists_raw_values(tmp_path):
    capture = active(tmp_path)
    bads = [
        {"raw_text": "secret"},
        {"source_receipt": "x", "unknown": "x"},
        {"source_receipt": "x", "attention_classes": {"external": True}},
        {"source_receipt": "https://private.example"},
        {"source_receipt": "x", "status": "failed"},
    ]
    for bad in bads:
        assert not capture.observe("source_observed", bad, now="2026-01-01T00:00:00Z")
    assert capture.observe("source_observed", evidence("raw-owner"), now="2026-01-01T00:00:00Z")
    rendered = capture.db_path.read_bytes().decode("latin1")
    assert "raw-owner" not in rendered
    row = capture._rows()[0]
    assert set(row) == {
        "schema",
        "sequence",
        "study_day",
        "time_bucket",
        "stage",
        "case_ref",
        "source_ref",
        "source_class",
        "change_state",
        "material",
        "no_effect",
        "dependence",
        "cross_domain",
        "attention_classes",
        "mixed_attention",
        "contradiction",
        "settlement",
    }
    assert row["material"] == row["no_effect"] == "unknown"


def test_material_outcome_is_yes_no_unknown_and_inhibition_is_not_no_effect(tmp_path):
    capture = active(tmp_path)
    assert capture.observe(
        "source_observed", evidence("quiet", change_state="no_change"), now="2026-01-01T00:00:00Z"
    )
    assert capture.observe(
        "source_observed",
        evidence("material", source_owner_transition=True),
        now="2026-01-01T00:00:00Z",
    )
    assert capture.observe(
        "settled", evidence("no", explicit_no_effect_evidence=True), now="2026-01-01T00:00:01Z"
    )
    rows = capture._rows()
    assert rows[0]["material"] == "unknown" and rows[0]["no_effect"] == "unknown"
    assert rows[1]["material"] == "yes" and rows[2]["no_effect"] == "no"


def test_opaque_source_candidate_aperture_linkage_and_case_deduplication(tmp_path):
    capture = active(tmp_path)
    assert capture.observe(
        "source_observed",
        evidence("source-a", candidate_id="candidate-a"),
        now="2026-01-01T00:00:00Z",
    )
    assert capture.observe(
        "candidate_updated",
        evidence("source-a", candidate_id="candidate-a"),
        now="2026-01-01T00:00:01Z",
    )
    assert capture.observe("opened", {"candidate_id": "candidate-a"}, now="2026-01-01T00:00:02Z")
    assert capture.observe(
        "settled",
        {"candidate_id": "candidate-a", "settlement": "settled"},
        now="2026-01-01T00:00:03Z",
    )
    rows = capture._rows()
    assert len({row["case_ref"] for row in rows}) == 1
    assert len(capture._cases(rows)) == 1
    assert all(
        "candidate-a" not in json.dumps(row) and "source-a" not in json.dumps(row) for row in rows
    )


def test_transactional_sequence_uses_max_after_selective_revocation(tmp_path):
    capture = active(tmp_path)
    for item in ("one", "two", "three"):
        assert capture.observe("source_observed", evidence(item), now="2026-01-01T00:00:00Z")
    assert capture.revoke("two")["revoked"]
    assert capture.observe("source_observed", evidence("four"), now="2026-01-01T00:00:00Z")
    sequences = [row["sequence"] for row in capture._rows()]
    assert len(sequences) == len(set(sequences)) and max(sequences) == 4
    assert not capture.observe("source_observed", evidence("two"), now="2026-01-01T00:00:00Z")


def test_eight_processes_account_for_all_800_observations(tmp_path):
    capture = active(tmp_path)
    queue = mp.Queue()
    workers = [
        mp.Process(target=_worker, args=(str(capture.profile_root), 100, queue), name=str(index))
        for index in range(8)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(45)
        assert worker.exitcode == 0
    accepted = sum(queue.get(timeout=3) for _ in workers)
    rows = capture._rows()
    assert accepted == 800 == len(rows)
    assert sorted(row["sequence"] for row in rows) == list(range(1, 801))


def test_capacity_rejection_is_explicit_without_corruption(tmp_path, monkeypatch):
    capture = active(tmp_path)
    monkeypatch.setattr("agent_sensorium.prospective_evidence.MAX_ROWS", 2)
    assert capture.observe("source_observed", evidence("a"), now="2026-01-01T00:00:00Z")
    assert capture.observe("source_observed", evidence("b"), now="2026-01-01T00:00:00Z")
    assert not capture.observe("source_observed", evidence("c"), now="2026-01-01T00:00:00Z")
    assert len(capture._rows()) == 2


def _quota_evidence(index: int, source: str) -> dict:
    return evidence(
        f"source-{index}",
        source,
        source_owner_transition=index < 24,
        explicit_no_effect_evidence=index < 12,
        shared_upstream_ref=index < 6,
        relation_evidence=index < 6,
        contradiction_evidence=index < 6,
        attention_classes=["external", "creative"] if index < 6 else ["operational"],
        same_window_external_protected=index < 6,
    )


def test_freeze_selects_valid_nonprefix_48_and_separates_gold_labels(tmp_path):
    capture = active(tmp_path)
    for index in range(96):
        # The first 48 are invalid (all manual); the later balanced rows carry
        # the same explicit structured evidence, so a valid non-prefix set exists.
        source = (
            "manual" if index < 48 else ["hermes_session", "artifact", "feedback"][(index - 48) % 3]
        )
        item = _quota_evidence(index, source)
        item.update(
            {
                "source_owner_transition": True,
                "explicit_no_effect_evidence": True,
                "shared_upstream_ref": True,
                "relation_evidence": True,
                "contradiction_evidence": True,
                "attention_classes": ["external", "creative"],
                "same_window_external_protected": True,
            }
        )
        assert capture.observe("contradiction", item, now="2026-01-01T00:00:00Z")
    result = capture.closeout(now="2026-01-15T00:00:00Z")
    assert result["verdict"] == "SUFFICIENT_PRIVATE_EVIDENCE"
    generator = json.loads(capture.generator_path.read_text())
    gold = json.loads(capture.gold_path.read_text())
    assert len(generator["cases"]) == len(gold["cases"]) == 48
    forbidden = {
        "material",
        "no_effect",
        "dependence",
        "cross_domain",
        "mixed_attention",
        "contradiction",
        "settlement",
    }
    assert not any(forbidden.intersection(case) for case in generator["cases"])
    manifest = json.loads(capture.manifest_path.read_text())
    assert manifest["counts"]["largest_source_class"] <= 16 and manifest["counts"]["unknown"] <= 12


def test_invalid_freeze_has_exact_safe_deficits_and_no_partial_generator_cases(tmp_path):
    capture = active(tmp_path)
    for index in range(10):
        assert capture.observe(
            "source_observed", evidence(f"tiny-{index}"), now="2026-01-01T00:00:00Z"
        )
    result = capture.closeout(now="2026-01-15T00:00:00Z")
    assert result["verdict"] == "INSUFFICIENT_PRIVATE_EVIDENCE"
    assert result["deficits"]["total"] == 38
    assert json.loads(capture.generator_path.read_text())["cases"] == []


def test_active_and_frozen_selective_revocation_invalidates_derivatives_and_preserves_unrelated(
    tmp_path,
):
    capture = active(tmp_path)
    for index in range(48):
        assert capture.observe(
            "contradiction" if index < 6 else "source_observed",
            _quota_evidence(index, ["manual", "hermes_session", "artifact", "feedback"][index % 4]),
            now="2026-01-01T00:00:00Z",
        )
    capture.closeout(now="2026-01-15T00:00:00Z")
    before = json.loads(capture.generator_path.read_text())["cases"]
    assert capture.revoke("source-0")["revoked"]
    after = json.loads(capture.generator_path.read_text())["cases"]
    assert len(after) == len(before) - 1 and capture.manifest_path.exists() is False
    assert any(case["case_ref"] != before[0]["case_ref"] for case in after)
    assert not capture.observe(
        "source_observed", _quota_evidence(0, "manual"), now="2026-01-01T00:00:00Z"
    )


def test_nonrenewal_tamper_purge_restart_and_retention_are_fail_closed(tmp_path):
    capture = active(tmp_path)
    capture.closeout(now="2026-01-15T00:00:00Z")
    shutil = __import__("shutil")
    shutil.rmtree(capture.study_root)
    restarted = ProspectiveEvidenceCapture(capture.profile_root, config("2026-02-01T00:00:00Z"))
    assert not restarted.activate()
    # Recreate an expired control only to exercise deterministic purge of retained fixtures/key.
    capture.study_root.mkdir(parents=True)
    capture.generator_path.write_text("{}")
    capture.gold_path.write_text("{}")
    capture.key_path.write_text("00" * 32)
    capture.control_path.write_text(
        json.dumps(
            {
                "schema": "sensorium.prospective_evidence.v0",
                "state": "closed",
                "frozen_expires_at": "2026-01-01T00:00:00Z",
                "tombstones_expires_at": "2026-01-01T00:00:00Z",
            }
        )
    )
    assert capture.maintenance(now="2026-02-01T00:00:00Z") == {"maintained": False}
    assert (
        capture.key_path.exists() and capture.generator_path.exists() and capture.gold_path.exists()
    )


def _retained_bytes(capture: ProspectiveEvidenceCapture) -> dict[str, bytes]:
    """Every closed-state artifact must remain byte-identical on failed authority."""
    return {
        path.name: path.read_bytes()
        for path in (
            capture.generator_path,
            capture.gold_path,
            capture.manifest_path,
            capture.key_path,
            capture.tombstone_path,
        )
        if path.exists()
    }


def _closed_capture(tmp_path: Path) -> ProspectiveEvidenceCapture:
    capture = active(tmp_path)
    assert capture.closeout(now="2026-01-15T00:00:00Z")["closed"]
    capture.tombstone_path.write_text(json.dumps(["a" * 24]))
    return capture


@pytest.mark.parametrize(
    "mutation",
    [
        lambda control: control.update(
            frozen_expires_at="2026-01-15T00:00:01Z", tombstones_expires_at="2026-01-15T00:00:02Z"
        ),
        lambda control: control.update(
            frozen_expires_at="2026-02-15T00:00:00Z", tombstones_expires_at="2026-05-16T00:00:00Z"
        ),
        lambda control: control.update(
            closed_at="2026-01-14T19:00:00-05:00",
            frozen_expires_at="2026-02-13T19:00:00-05:00",
            tombstones_expires_at="2026-04-14T20:00:00-04:00",
        ),
        lambda control: control.update(
            closed_at="2026-01-15T00:00:00.000Z",
            frozen_expires_at="2026-02-14T00:00:00.000Z",
            tombstones_expires_at="2026-04-15T00:00:00.000Z",
        ),
        lambda control: control.update(unrecognized="value"),
        lambda control: control.update(verdict={"nested": "SUFFICIENT_PRIVATE_EVIDENCE"}),
    ],
    ids=("short", "long", "offset", "fractional", "unknown-key", "nested"),
)
def test_forged_closed_retention_fails_closed_across_api_and_admin_paths(tmp_path, mutation):
    """Schema-shaped forged close controls must not authorize any retained mutation."""
    capture = _closed_capture(tmp_path)
    hostile = json.loads(capture.control_path.read_text())
    mutation(hostile)
    capture.control_path.write_text(json.dumps(hostile))
    before = _retained_bytes(capture)
    assert capture.closeout(now="2026-01-15T00:00:03Z") == {
        "closed": False,
        "reason": "closeout_not_authorized",
    }
    assert _retained_bytes(capture) == before
    assert capture.maintenance(now="2026-06-01T00:00:00Z") == {"maintained": False}
    assert _retained_bytes(capture) == before
    assert capture.revoke("unrelated") == {
        "revoked": False,
        "reason": "retention_control_unavailable",
    }
    assert _retained_bytes(capture) == before
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"prospective_evidence_capture": config()}))
    script = Path(__file__).parents[1] / "scripts" / "sensorium_prospective_evidence.py"
    for operation, expected in (
        ("status", {"active": False}),
        ("freeze", {"closed": False, "reason": "closeout_not_authorized"}),
        ("maintenance", {"maintained": False}),
    ):
        result = subprocess.run(
            [
                sys.executable,
                str(script),
                operation,
                "--profile-root",
                str(capture.profile_root),
                "--config",
                str(config_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(result.stdout)
        assert all(payload.get(key) == value for key, value in expected.items())
        assert _retained_bytes(capture) == before


def test_canonical_closed_retention_has_exact_30_90_day_maintenance_boundaries(tmp_path):
    capture = _closed_capture(tmp_path)
    control = json.loads(capture.control_path.read_text())
    assert control["frozen_expires_at"] == "2026-02-14T00:00:00Z"
    assert control["tombstones_expires_at"] == "2026-04-15T00:00:00Z"
    assert capture.closeout(now="2026-01-16T00:00:00Z") == {
        "closed": True,
        "idempotent": True,
        "verdict": "INSUFFICIENT_PRIVATE_EVIDENCE",
    }
    assert capture.maintenance(now="2026-02-13T23:59:59Z") == {"maintained": False}
    assert all(
        path.exists()
        for path in (
            capture.generator_path,
            capture.gold_path,
            capture.manifest_path,
            capture.key_path,
            capture.tombstone_path,
        )
    )
    assert capture.maintenance(now="2026-02-14T00:00:00Z") == {"maintained": True}
    assert not any(
        path.exists()
        for path in (
            capture.generator_path,
            capture.gold_path,
            capture.manifest_path,
            capture.key_path,
        )
    )
    assert capture.tombstone_path.exists()
    assert capture.maintenance(now="2026-04-14T23:59:59Z") == {"maintained": False}
    assert capture.tombstone_path.exists()
    assert capture.maintenance(now="2026-04-15T00:00:00Z") == {"maintained": True}
    assert not capture.tombstone_path.exists()


def _selector_case(index, source, **flags):
    return {
        "case_ref": f"case_{index:024x}",
        "source_class": source,
        "material": "yes" if flags.get("material") else "unknown",
        "no_effect": "no" if flags.get("no_effect") else "unknown",
        "dependence": "yes" if flags.get("dependence") else "unknown",
        "contradiction": "yes" if flags.get("contradiction") else "unknown",
        "cross_domain": "yes" if flags.get("cross_domain") else "unknown",
        "mixed_attention": "yes" if flags.get("mixed") else "unknown",
    }


def _oracle(cases):
    for chosen in itertools.combinations(cases, 48):
        counts = ProspectiveEvidenceCapture._counts(list(chosen))
        if not ProspectiveEvidenceCapture._deficits(counts):
            return True
    return False


def test_exact_selector_counterexample_oracle_and_5000_grouped_performance():
    cases = [_selector_case(i, "artifact", material=True) for i in range(16)]
    cases += [_selector_case(100 + i, "artifact", no_effect=True) for i in range(12)]
    cases += [
        _selector_case(
            200 + 12 * group + i,
            source,
            material=True,
            no_effect=True,
            dependence=True,
            contradiction=True,
            cross_domain=True,
            mixed=True,
        )
        for group, source in enumerate(("feedback", "hermes_session", "manual"))
        for i in range(12)
    ]
    selected = ProspectiveEvidenceCapture._select_48(cases)
    assert selected is not None and not ProspectiveEvidenceCapture._deficits(
        ProspectiveEvidenceCapture._counts(selected)
    )
    for seed in range(200):
        rng = random.Random(seed)
        pool = [
            _selector_case(
                i,
                rng.choice(
                    ("manual", "hermes_session", "artifact", "feedback", "machine", "unknown")
                ),
                material=rng.random() < 0.6,
                no_effect=rng.random() < 0.4,
                dependence=rng.random() < 0.3,
                contradiction=rng.random() < 0.3,
                cross_domain=rng.random() < 0.3,
                mixed=rng.random() < 0.3,
            )
            for i in range(48 + seed % 3)
        ]
        assert (ProspectiveEvidenceCapture._select_48(pool) is not None) is _oracle(pool)
    large = [
        _selector_case(
            i,
            ("manual", "hermes_session", "artifact", "feedback", "machine")[i % 5],
            material=True,
            no_effect=True,
            dependence=True,
            contradiction=True,
            cross_domain=True,
            mixed=True,
        )
        for i in range(4995)
    ]
    started = time.monotonic()
    selected = ProspectiveEvidenceCapture._select_48(large)
    assert selected is not None and time.monotonic() - started < 10


def test_direct_coalesced_alias_audit_revocation_and_restart(tmp_path):
    """A two-source coalesce has one case everywhere, and revocation is terminal."""
    capture = active(tmp_path)
    now = "2026-01-01T00:00:00Z"
    assert capture.observe("source_observed", evidence("A", candidate_id="candidate-A"), now=now)
    assert capture.observe("source_observed", evidence("B", candidate_id="candidate-B"), now=now)
    # Joining each prior source to X forces both aliases into one case.
    assert capture.observe("candidate_updated", evidence("A", candidate_id="candidate-X"), now=now)
    assert capture.observe("candidate_updated", evidence("B", candidate_id="candidate-X"), now=now)
    assert capture.observe("source_observed", evidence("Y", candidate_id="candidate-Y"), now=now)
    key = capture._key()
    assert key
    refs = {name: capture._opaque(key, name, "src") for name in ("A", "B", "Y")}
    entities = {
        name: capture._opaque(key, f"candidate-{name}", "case") for name in ("A", "B", "X", "Y")
    }
    connection = capture._connect()
    try:
        x_case = connection.execute(
            "SELECT case_ref FROM source_cases WHERE source_ref=?", (refs["A"],)
        ).fetchone()[0]
        assert {
            row[0]
            for row in connection.execute(
                "SELECT case_ref FROM observations WHERE source_ref IN (?,?)",
                (refs["A"], refs["B"]),
            )
        } == {x_case}
        assert {
            row[0]
            for row in connection.execute(
                "SELECT case_ref FROM source_cases WHERE source_ref IN (?,?)",
                (refs["A"], refs["B"]),
            )
        } == {x_case}
        assert {
            row[0]
            for row in connection.execute(
                "SELECT case_ref FROM entity_cases WHERE entity_ref IN (?,?,?)",
                (entities["A"], entities["B"], entities["X"]),
            )
        } == {x_case}
        y_case = connection.execute(
            "SELECT case_ref FROM source_cases WHERE source_ref=?", (refs["Y"],)
        ).fetchone()[0]
    finally:
        connection.close()
    assert capture.revoke("A")["revoked"]
    connection = capture._connect()
    try:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM observations WHERE case_ref=?", (x_case,)
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM source_cases WHERE case_ref=?", (x_case,)
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM entity_cases WHERE case_ref=?", (x_case,)
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM observations WHERE case_ref=?", (y_case,)
            ).fetchone()[0]
            == 1
        )
        assert connection.execute(
            "SELECT 1 FROM blocked WHERE source_ref=?", (refs["A"],)
        ).fetchone()
    finally:
        connection.close()
    restarted = ProspectiveEvidenceCapture(capture.profile_root, config())
    assert not restarted.observe(
        "source_observed", evidence("A", candidate_id="candidate-X"), now=now
    )
    assert restarted.observe("source_observed", evidence("B", candidate_id="candidate-X"), now=now)
    connection = restarted._connect()
    try:
        assert (
            connection.execute(
                "SELECT COUNT(DISTINCT case_ref) FROM source_cases WHERE source_ref=?", (refs["B"],)
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM source_cases WHERE source_ref=?", (refs["A"],)
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()


@pytest.mark.parametrize(
    "path_name", ["generator_path", "gold_path", "manifest_path", "tombstone_path"]
)
def test_tampered_frozen_artifacts_are_removed_on_all_read_modify_paths(tmp_path, path_name):
    """No lifecycle path may copy hostile nested frozen bytes into a new artifact."""
    capture = active(tmp_path)
    capture.closeout(now="2026-01-15T00:00:00Z")
    path = getattr(capture, path_name)
    hostile = {
        "raw_text": {"credential": "secret", "url": "https://bad", "path": "/bad", "id": ["bad"]}
    }
    path.write_text(json.dumps(hostile))
    # Revocation is the mutation path that must validate every sibling before writing.
    result = capture.revoke("unrelated")
    assert result["revoked"]
    rendered = "".join(
        item.read_text() for item in capture.study_root.glob("*.json") if item.exists()
    )
    assert "secret" not in rendered and "https://bad" not in rendered and "/bad" not in rendered
    assert (
        not capture.generator_path.exists()
        and not capture.gold_path.exists()
        and not capture.manifest_path.exists()
    )
    # Repeated closeout must neither recreate nor surface hostile frozen content.
    repeated = capture.closeout(now="2026-01-16T00:00:00Z")
    assert repeated == {
        "closed": True,
        "idempotent": True,
        "verdict": "INSUFFICIENT_PRIVATE_EVIDENCE",
    }
