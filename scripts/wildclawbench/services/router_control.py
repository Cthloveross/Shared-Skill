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

from scripts.wildclawbench.protocol import REPOSITORY
from scripts.wildclawbench.services.common import runtime_directory, service_config

ROOT = REPOSITORY
MODULE = "scripts.wildclawbench.gateway"
CONFIG_PATH: Path | None = None
PORT = 18182
ENDPOINT = "http://127.0.0.1:18182/v1"
DIRECTORY = runtime_directory() / "gateway"
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
            and MODULE.encode() in (process / "cmdline").read_bytes().split(b"\0")
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
        if sock.connect_ex(("127.0.0.1", PORT)) == 0:
            raise RuntimeError(f"Port {PORT} is occupied by another process")
    DIRECTORY.mkdir(parents=True, exist_ok=True)
    if CONFIG_PATH is None:
        raise ValueError("An explicit service --config is required")
    command = [sys.executable, "-m", MODULE, "--config", str(CONFIG_PATH), "--port", str(PORT)]
    with (DIRECTORY / "gateway.log").open("ab") as stream:
        process = subprocess.Popen(
            command,
            cwd=REPOSITORY,
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
        "endpoint": ENDPOINT,
    }
    STATE.write_text(json.dumps(state, indent=2) + "\n")
    return state


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["start", "stop", "status"])
    args, config = service_config(parser)
    from urllib.parse import urlparse
    CONFIG_PATH = args.config.resolve()
    DIRECTORY = Path(config["runtime_dir"]) / "gateway"
    STATE = DIRECTORY / "process.json"
    ENDPOINT = config["local_api_base_url"]
    PORT = urlparse(ENDPOINT).port or 80
    print(json.dumps(control(args.action), indent=2))
