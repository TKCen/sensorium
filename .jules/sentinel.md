# Sentinel Security Journal

## 2025-05-14 - Path Traversal Protection in SensoriumStore
**Vulnerability:** `SensoriumStore` accepted an unvalidated `instance` name, which was used directly in constructing filesystem paths, allowing for potential path traversal.
**Learning:** Shared validation logic should reside in low-level modules (like `schemas.py`) to be accessible by both configuration and storage layers without circular dependencies. Fail-fast validation at the storage constructor level provides defense-in-depth even if higher-level tools miss a check.
**Prevention:** Always validate identifiers that map to filesystem components using a strict allow-list policy (regex, no slashes, no leading dots).
