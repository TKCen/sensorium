# Sentinel Journal

## 2025-07-03 - Unified Instance Name Validation
**Vulnerability:** Path traversal in `SensoriumStore` via the `instance` parameter.
**Learning:** The `SensoriumStore` class used the `instance` string directly to construct filesystem paths without validation, assuming callers had already sanitized it. This led to a vulnerability where a malicious instance name like `../outside` could create directories outside the intended base directory.
**Prevention:** Centralize profile/instance name validation in `agent_sensorium/schemas.py` and enforce it strictly in the `SensoriumStore` constructor (Fail-fast). This ensures that no store can be initialized with an unsafe name, regardless of where the request originated.
