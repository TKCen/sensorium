# Sentinel Journal

## 2025-05-15 - [Path Traversal in SensoriumStore]
**Vulnerability:** The `SensoriumStore` constructor accepted arbitrary `instance` names, including absolute paths and those containing `..`, allowing state to be read from or written to locations outside the intended `~/.hermes/agent-sensorium/` base directory.
**Learning:** Even if some higher-level functions (like `init_profile_config`) perform validation, the core data-access class (`SensoriumStore`) remained vulnerable, which could be exploited if it's instantiated directly with unsanitized input.
**Prevention:** Always validate and sanitize all components of a file path at the lowest level possible, preferably in the constructor of the class responsible for file operations.
