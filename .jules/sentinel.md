# Sentinel's Journal - Critical Security Learnings

## 2025-05-15 - Path Traversal in SensoriumStore and Unified Sanitization
**Vulnerability:** The `SensoriumStore` constructor and dashboard `_resolve_instance` helper lacked unified, strict validation for profile/instance names, potentially allowing path traversal to arbitrary directories outside the intended state root via `instance` arguments like `../../evil`.
**Learning:** Distributed validation logic (one in `config.py` for CLI/tools, another partial one in `plugin_api.py` for the dashboard) led to gaps where core classes like `SensoriumStore` were not enforcing safety at the lowest layer. Moving sanitization to a shared `schemas.py` and enforcing it in the store constructor provides a centralized defense-in-depth bottleneck.
**Prevention:** Always enforce path-sensitive input validation at the constructor or resource-resolution level using a unified shared helper. Follow a "fail-fast" policy that rejects ambiguous input (like leading/trailing whitespace or leading dots) rather than silently trimming it, ensuring consistency between tests and production behavior.
