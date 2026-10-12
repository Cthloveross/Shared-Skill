#!/usr/bin/env python3
"""Own-process lifecycle for the experiment's local request router."""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "gateway.py"
DIRECTORY = ROOT / "runtime/gateway"
STATE = DIRECTORY / "process.json"


def ticks(pid):
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]


def owned(state):
    try:
        pid = int(state["pid"])
        process = Path(f"/proc/{pid}")
        return (
            process.stat().st_uid == os.getuid()
            and ticks(pid) == state["start_ticks"]
            and str(SCRIPT).encode() in (process / "cmdline").read_bytes().split(b"\0")
            and os.getpgid(pid) == pid
        )
    except (OSError, KeyError, ValueError):
        return False


def control(action):
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    running = owned(state)
    if action == "status":
        return {**state, "running": running}
    if action == "stop":
        if not running:
            return {"stopped": False, "reason": "No matching live experiment process"}
        os.killpg(state["pid"], signal.SIGTERM)
        deadline = time.monotonic() + 15
        while owned(state) and time.monotonic() < deadline:
            time.sleep(0.1)
        if owned(state):
            os.killpg(state["pid"], signal.SIGKILL)
        return {"stopped": True, "pid": state["pid"]}
    if running:
        return {**state, "running": True, "reused_existing_process": True}
    with socket.socket() as sock:
        if sock.connect_ex(("127.0.0.1", 18182)) == 0:
            raise RuntimeError("Port 18182 is occupied by another process")
    DIRECTORY.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, str(SCRIPT)]
    with (DIRECTORY / "gateway.log").open("ab") as stream:
        process = subprocess.Popen(
            command,
            cwd=ROOT.parents[2],
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    state = {
        "pid": process.pid,
        "uid": os.getuid(),
        "start_ticks": ticks(process.pid),
        "command": command,
        "started_unix": time.time(),
        "endpoint": "http://127.0.0.1:18182/v1",
    }
    STATE.write_text(json.dumps(state, indent=2) + "\n")
    return state


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["start", "stop", "status"])
    print(json.dumps(control(parser.parse_args().action), indent=2))
