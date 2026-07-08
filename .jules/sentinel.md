## 2025-05-15 - Path Traversal in SensoriumStore

**Vulnerability:** The `SensoriumStore` constructor directly used the `instance` argument to construct the state root directory via string concatenation/Path joining (`Path(_DEFAULT_BASE) / instance`). An attacker could provide a malicious instance name like `../../tmp/evil` to escape the intended state directory and access or create files anywhere the process has permissions.

**Learning:** Validation logic for instance names (`sanitize_profile_name`) already existed but was located in `config.py`. Because `store.py` is a low-level module that `config.py` depends on, `store.py` could not easily import this validation without creating a circular dependency. This led to the validation being omitted in the storage layer.

**Prevention:** Move pure validation and normalization helpers to a dedicated `schemas.py` or utility module that has no dependencies on other project modules. This allows both high-level (config) and low-level (store) modules to share the same security enforcement logic. Always sanitize user-provided strings that are used to construct filesystem paths.
