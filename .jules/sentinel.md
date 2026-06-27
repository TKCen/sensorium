## 2025-05-22 - [Instance Path Traversal & Command Injection Fixes]
**Vulnerability:** Path traversal in `SensoriumStore` via unvalidated instance names and potential command injection in `talking_head.py` due to `shell=True`.
**Learning:** Profile names were used directly as directory components without validation, and shell execution was used for local pipelining with partially controlled templates.
**Prevention:** Centralized `sanitize_profile_name` in `schemas.py` to break circular imports between `config` and `store`, ensuring all instance names are validated. Replaced `shell=True` with `shell=False` and `shlex.split()` for secure subprocess execution.
