#!/usr/bin/env python3
"""Start or stop only this experiment's auxiliary inference processes."""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
SCRIPT = Path(__file__).resolve().with_name("auxiliary_server.py")
PYTHON = RUNTIME / "auxiliary-venv" / "bin" / "python"
GPU = "GPU-04d83de3-985b-d799-0e97-0bb7498b1f00"
PORTS = {"audio": 18183, "image": 18184}


def start_ticks(pid):
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]


def owned(state):
    try:
        pid = int(state["pid"])
        process = Path(f"/proc/{pid}")
        argv = (process / "cmdline").read_bytes().split(b"\0")
        return (
            process.stat().st_uid == os.getuid()
            and start_ticks(pid) == state["start_ticks"]
            and str(SCRIPT).encode() in argv
            and state["kind"].encode() in argv
            and os.getpgid(pid) == pid
        )
    except (KeyError, OSError, ValueError):
        return False


def run(action, kind):
    directory = RUNTIME / f"auxiliary-{kind}"
    state_file = directory / "process.json"
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    running = owned(state)
    if action == "status":
        healthy = False
        if running:
            try:
                healthy = (
                    httpx.get(f"http://127.0.0.1:{PORTS[kind]}/health", timeout=3, trust_env=False).status_code == 200
                )
            except httpx.HTTPError:
                pass
        return {**state, "running": running, "healthy": healthy}
    if action == "stop":
        if not running:
            return {"stopped": False, "reason": "no matching experiment-owned live process"}
        os.killpg(state["pid"], signal.SIGTERM)
        deadline = time.monotonic() + 30
        while owned(state) and time.monotonic() < deadline:
            time.sleep(0.2)
        if owned(state):
            os.killpg(state["pid"], signal.SIGKILL)
        return {"stopped": True, "pid": state["pid"]}
    if running:
        return {**state, "running": True, "reused_existing_process": True}
    if httpx.get("http://127.0.0.1:18181/health", timeout=3, trust_env=False).status_code != 200:
        raise RuntimeError("main GPU 1 model must be healthy before auxiliary loading")
    with socket.socket() as sock:
        if sock.connect_ex(("127.0.0.1", PORTS[kind])) == 0:
            raise RuntimeError(f"port {PORTS[kind]} is occupied by a process outside this experiment")
    free = int(
        subprocess.check_output(
            ["nvidia-smi", "--id=" + GPU, "--query-gpu=memory.free", "--format=csv,noheader,nounits"], text=True
        ).strip()
    )
    if free < {"audio": 32000, "image": 13000}[kind]:
        raise RuntimeError(f"GPU 1 has insufficient free memory for {kind}: {free} MiB")
    downloads = json.loads((RUNTIME / "auxiliary-downloads.json").read_text())
    if downloads["models"][kind]["status"] != "downloaded":
        raise RuntimeError(f"fixed {kind} model download is incomplete")
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / "tmp"
    temporary.mkdir(exist_ok=True)
    command = [str(PYTHON), str(SCRIPT), "--kind", kind, "--port", str(PORTS[kind])]
    safe_environment = {
        "CUDA_VISIBLE_DEVICES": GPU,
        "CUDA_DEVICE_ORDER": "PCI_BUS_ID",
        "HF_HUB_OFFLINE": "1",
        "XDG_CACHE_HOME": str(directory / "cache"),
        "TMPDIR": str(temporary),
        "PYTHONPYCACHEPREFIX": str(directory / "pycache"),
        "TOKENIZERS_PARALLELISM": "false",
    }
    with (directory / "service.log").open("ab", buffering=0) as log:
        process = subprocess.Popen(
            command,
            cwd=directory,
            env={**os.environ, **safe_environment},
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    state = {
        "pid": process.pid,
        "start_ticks": start_ticks(process.pid),
        "kind": kind,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "command": command,
        "environment": safe_environment,
        "gpu_free_before_load_mib": free,
        "api_base_url": f"http://127.0.0.1:{PORTS[kind]}/v1",
    }
    state_file.write_text(json.dumps(state, indent=2) + "\n")
    with (directory / "start-history.jsonl").open("a") as history:
        history.write(json.dumps(state) + "\n")
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "status", "stop"])
    parser.add_argument("kind", choices=list(PORTS))
    args = parser.parse_args()
    print(json.dumps(run(args.action, args.kind), indent=2))


if __name__ == "__main__":
    main()
