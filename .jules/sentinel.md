## 2025-06-29 - [Path Traversal in SensoriumStore]
**Vulnerability:** The `SensoriumStore` constructor accepted an `instance` (profile) name without validation, which was used to construct file system paths. This allowed an attacker (or a malicious/buggy tool) to use path traversal sequences (like `../`) to read or write files outside the intended storage root.
**Learning:** Even when a validation function (`sanitize_profile_name`) exists in the codebase, it must be applied at the lowest possible layer (the data store constructor) to ensure all access paths are protected.
**Prevention:** Always validate and sanitize strings used for directory or file names at the point of ingestion or constructor initialization. Use allow-listing for allowed characters.
