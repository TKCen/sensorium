## 2025-05-15 - Unified Instance Name Validation
**Vulnerability:** Path traversal risk when constructor for `SensoriumStore` accepted unvalidated `instance` names to construct file paths.
**Learning:** Even if individual tools or handlers validate inputs, the underlying storage layer should enforce its own security invariants to ensure defense in depth.
**Prevention:** Centralize shared validation logic in a low-level module (like `schemas.py`) and enforce it in the constructor of core state-management classes.
