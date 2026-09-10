# Candidate-v2 prospective-capture implementation report

- **status:** `READY_FOR_INDEPENDENT_REVIEW`
- **base:** `d98b69c2a901e3b41848167e1d20aebe4dbb9352`
- **scope:** passive, detached prospective study only; no activation, live state,
  configuration, scheduler, service, source, or outbound mutation.

## Closed owner seams

1. The accepted signal owner now emits a separate `correction` or `retraction`
   observation only for the exact structured `kind` values
   `explicit_correction`, `correction`, and `retraction`.  Candidate promotion,
   settlement, and prose are not inputs to that decision.
2. A successful non-dry-run Conscious aperture open emits a single, best-effort
   `attention_snapshot` after its canonical candidate rewrite and open receipt.
   It uses only the candidates already present in that selection transaction.
   Exact candidate-kind mapping is closed; unknown values remain `unknown`.
3. Mixed attention is positive only where that one snapshot includes `external`
   and a protected nonexternal class. The observer is post-success and has no
   return path into selection.

## Verification

- `python -m pytest -q tests/test_prospective_evidence.py tests/test_prospective_owner_seams.py` — 18 passed.
- `python -m pytest -q tests/test_conscious_aperture.py tests/test_tools.py` — 47 passed.
- `python -m pytest tests -q` — passed (one existing skipped test).
- `python -m py_compile agent_sensorium/*.py scripts/*.py` — passed.
- `git diff --check` — passed.

See `prospective-evidence-v0-owner-matrix.md` for the bounded seam inventory
and `prospective-evidence-v0-privacy-verification.md` for the data boundary.
