## 2025-05-22 - Unified Instance Name Validation
**Vulnerability:** Path traversal via unvalidated 'instance' name in SensoriumStore.
**Learning:** While the dashboard and tool wrappers had some validation, the core storage layer (SensoriumStore) was trusting the instance name provided in its constructor. This could allow an attacker controlling the instance name to escape the intended state directory.
**Prevention:** Always enforce strict input validation at the lowest possible layer (defense in depth). Relocated 'sanitize_profile_name' to a shared schemas module to ensure it can be consistently applied across configuration, storage, and API layers without circular dependencies.
