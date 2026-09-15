# Prospective evidence v0 review runbook

This candidate does **not** authorize activation. Review source only.

```bash
python -m pytest -q tests/test_prospective_evidence.py tests/test_prospective_owner_seams.py
python -m pytest tests -q
python -m py_compile agent_sensorium/*.py scripts/*.py
git diff --check
git ls-tree HEAD tests/test_prospective_evidence.py
```

For review, inspect `prospective_evidence.py`, `tools.py`, and
`conscious_aperture.py` together. Confirm that structured correction is separate
from candidate promotion and settlement, and that attention snapshot is after
the one successful aperture write. Do not activate the study as part of this
review.
