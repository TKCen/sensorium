#!/usr/bin/env python3
"""Demo local script sensor — script-sensor contract canary.

Emits exactly one compact JSON signal on stdout and exits 0. Deterministic,
stdlib-only, no network, no model calls. Used by tests and docs to exercise
the trusted local script-sensor contract: argv-only execution, JSON stdout,
quiet success on empty output.
"""

import json
import sys


def main() -> int:
    signal = {
        "sensor": "demo_script_sensor",
        "source": "machine",
        "kind": "demo_script_canary",
        "summary": "Demo script sensor canary signal",
        "strength_hint": 0.1,
        "sensitivity": "local_only",
        "allowed_surfaces": ["local"],
        "correlation_keys": ["demo-script-sensor"],
    }
    print(json.dumps(signal))
    return 0


if __name__ == "__main__":
    sys.exit(main())
