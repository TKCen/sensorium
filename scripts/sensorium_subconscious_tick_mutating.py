#!/usr/bin/env python3
"""Cron wrapper for the event-gated Subconscious tick.

The base script is also useful in dry-run mode. This wrapper is the scheduled
runtime: it allows only internal subconscious_advisory candidate creation, still
with no outbound messages, no external Kanban tasks, and a per-instance lock.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

script = Path(__file__).with_name("sensorium_subconscious_tick.py")
proc = subprocess.run(
    [sys.executable, str(script), "--enable-candidate-creation"],
    text=True,
    capture_output=True,
    timeout=120,
)
if proc.returncode != 0:
    print((proc.stderr or proc.stdout).strip())
raise SystemExit(proc.returncode)
