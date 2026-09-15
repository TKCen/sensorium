# Prospective evidence v0 verification receipts

- Base: `d98b69c2a901e3b41848167e1d20aebe4dbb9352`
- Focused prospective + owner seam tests: 18 passed.
- Adjacent aperture/tools tests: 47 passed.
- Full suite: passed with one existing skip.
- Compile: passed.
- Whitespace check: passed.

The focused original file remains tracked and is not deleted. Post-commit
verification must repeat `git ls-tree HEAD tests/test_prospective_evidence.py`
and a filesystem existence check.
