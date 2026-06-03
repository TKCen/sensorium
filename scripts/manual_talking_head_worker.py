#!/usr/bin/env python3
"""Manual Sensorium talking-head artifact worker CLI.

Default mode is dry-run: it records planned private artifact refs only. Pass
--real-run to execute local Chatterbox + Comfy/Wan/InfiniteTalk steps.
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_sensorium.talking_head import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
