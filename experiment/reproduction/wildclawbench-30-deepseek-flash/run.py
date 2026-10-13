#!/usr/bin/env python3
"""Use the audited experiment engine with this trial's own configuration."""

import json
import os
import sys
from pathlib import Path

root = Path(__file__).resolve().parent
config = root / "config.json"
source = Path(json.loads(config.read_text())["source_worktree"])
engine = source / "experiment/reproduction/wildclawbench-qwen3.8-27b/run.py"
if not engine.is_file():
    raise SystemExit("Experiment engine is missing; see EXPERIMENT.md for the source checkout")
os.execv(sys.executable, [sys.executable, str(engine), *sys.argv[1:], "--config", str(config)])
