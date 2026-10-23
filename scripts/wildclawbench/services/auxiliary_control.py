#!/usr/bin/env python3
"""Start or stop only this experiment's auxiliary inference processes."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import httpx

from scripts.wildclawbench.protocol import REPOSITORY
from scripts.wildclawbench.services.common import runtime_directory, service_config
from scripts.wildclawbench.services.auxiliary_worker import model_root, limits, stop_tree

ROOT = REPOSITORY
RUNTIME = runtime_directory()
MODULE = "scripts.wildclawbench.services.auxiliary_server"
CONFIG_PATH: Path | None = None
MAIN_HEALTH = "http://127.0.0.1:18181/health"
PYTHON = RUNTIME / "auxiliary-venv" / "bin" / "python"
MODELS = RUNTIME / "models"
DOWNLOADS = RUNTIME / "auxiliary-downloads.json"
REVISIONS = {}
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
            and MODULE.encode() in argv
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
        stopped = stop_tree(state["pid"], state["start_ticks"])
        return {"stopped": stopped["quiescent"], "pid": state["pid"], **stopped}
    if running:
        return {**state, "running": True, "reused_existing_process": True}
    if httpx.get(MAIN_HEALTH, timeout=3, trust_env=False).status_code != 200:
        raise RuntimeError("main GPU 1 model must be healthy before auxiliary loading")
    with socket.socket() as sock:
        if sock.connect_ex(("127.0.0.1", PORTS[kind])) == 0:
            raise RuntimeError(f"port {PORTS[kind]} is occupied by a process outside this experiment")
    free = int(
        subprocess.check_output(
            ["nvidia-smi", "--id=" + GPU, "--query-gpu=memory.free", "--format=csv,noheader,nounits"], text=True, timeout=10
        ).strip()
    )
    if free < {"audio": 32000, "image": 13000}[kind]:
        raise RuntimeError(f"GPU 1 has insufficient free memory for {kind}: {free} MiB")
    downloads = json.loads(DOWNLOADS.read_text())
    if downloads["models"][kind]["status"] != "downloaded":
        raise RuntimeError(f"fixed {kind} model download is incomplete")
    if REVISIONS.get(kind) and downloads["models"][kind].get("revision") != REVISIONS[kind]:
        raise RuntimeError(f"Prepared {kind} model revision differs from configured revision")
    if not PYTHON.is_file() or not (MODELS / {"audio": "qwen2.5-omni-7b", "image": "sdxl-base-1.0"}[kind]).is_dir():
        raise RuntimeError("Prepared auxiliary Python or model directory is missing")
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / "tmp"
    temporary.mkdir(exist_ok=True)
    if CONFIG_PATH is None:
        raise ValueError("An explicit service --config is required")
    command = [str(PYTHON), "-m", MODULE, "--kind", kind, "--port", str(PORTS[kind]), "--config", str(CONFIG_PATH)]
    safe_environment = {
        "CUDA_VISIBLE_DEVICES": GPU,
        "SHAREDSKILL_RUNTIME_DIR": str(RUNTIME),
        "CUDA_DEVICE_ORDER": "PCI_BUS_ID",
        "HF_HUB_OFFLINE": "1",
        "XDG_CACHE_HOME": str(directory / "cache"),
        "TMPDIR": str(temporary),
        "PYTHONDONTWRITEBYTECODE": "1",
        "TOKENIZERS_PARALLELISM": "false",
    }
    with (directory / "service.log").open("ab", buffering=0) as log:
        process = subprocess.Popen(
            command,
            cwd=REPOSITORY,
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
    global RUNTIME, PYTHON, MODELS, DOWNLOADS, REVISIONS, GPU, PORTS, CONFIG_PATH, MAIN_HEALTH
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "status", "stop"])
    parser.add_argument("kind", choices=list(PORTS))
    args, config = service_config(parser)
    from urllib.parse import urlparse
    CONFIG_PATH = args.config.resolve()
    RUNTIME = Path(config["runtime_dir"])
    shared = Path(config.get("shared_runtime_dir", RUNTIME))
    PYTHON = Path(config.get("auxiliary_python", shared / "auxiliary-venv/bin/python"))
    MODELS = model_root(config)
    DOWNLOADS = Path(config.get("auxiliary_download_manifest", shared / "auxiliary-downloads.json"))
    REVISIONS = {kind: config.get(kind + "_model_revision") for kind in ("audio", "image")}
    limits(config)
    GPU = str(config.get("gpu_uuid", config.get("gpu_index", 0)))
    PORTS = {kind: urlparse(config[kind + "_base_url"]).port or 80 for kind in ("audio", "image")}
    MAIN_HEALTH = config["model_base_url"].removesuffix("/v1") + "/health"
    print(json.dumps(run(args.action, args.kind), indent=2))


if __name__ == "__main__":
    main()
