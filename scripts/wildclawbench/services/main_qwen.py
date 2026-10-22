#!/usr/bin/env python3
"""Own and recover only this experiment's GPU 1 vLLM process."""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import shutil
from urllib.parse import urlparse
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from scripts.wildclawbench.protocol import REPOSITORY
from scripts.wildclawbench.services.common import runtime_directory, service_config

ROOT = REPOSITORY
RUNTIME = runtime_directory() / "main-service"
STATE = RUNTIME / "process.json"
GPU = "GPU-04d83de3-985b-d799-0e97-0bb7498b1f00"
REVISION = "1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0"
MODEL = Path.home() / ".cache/huggingface/hub/models--Qwen--Qwen3.8-27B/snapshots" / REVISION
VLLM = Path(shutil.which("vllm") or "vllm")
GPU_MEMORY_UTILIZATION = 0.55

MODEL_ID = "qwen3.8-27b"
PORT = 18181
SERVICE_SETTINGS: dict = {}


def utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def start_ticks(pid: int) -> str:
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]


def owned(state: dict) -> bool:
    try:
        pid = int(state["pid"])
        process = Path(f"/proc/{pid}")
        argv = (process / "cmdline").read_bytes().split(b"\0")
        return (
            process.stat().st_uid == os.getuid()
            and start_ticks(pid) == state["start_ticks"]
            and str(VLLM).encode() in argv
            and MODEL_ID.encode() in argv
            and os.getpgid(pid) == pid
        )
    except (OSError, KeyError, ValueError):
        return False


def state_read() -> dict:
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def gpu_snapshot() -> str:
    return subprocess.check_output(
        [
            "nvidia-smi",
            "--query-gpu=index,uuid,name,memory.total,memory.used,memory.free,utilization.gpu",
            "--format=csv",
        ],
        text=True,
    ).strip()


def start() -> dict:
    state = state_read()
    if owned(state):
        return {**state, "running": True, "reused_existing_process": True}
    with socket.socket() as sock:
        if sock.connect_ex(("127.0.0.1", PORT)) == 0:
            raise RuntimeError(f"port {PORT} is occupied by a process not owned by this experiment")
    processes = subprocess.check_output(
        ["nvidia-smi", "--query-compute-apps=gpu_uuid,pid", "--format=csv,noheader"], text=True
    )
    gpu_uuid = subprocess.check_output(
        ["nvidia-smi", "--id=" + GPU, "--query-gpu=uuid", "--format=csv,noheader"], text=True
    ).strip()
    if any(line.partition(",")[0].strip() == gpu_uuid for line in processes.splitlines()):
        raise RuntimeError("GPU 1 has a compute process; refusing to replace it")
    index = json.loads((MODEL / "model.safetensors.index.json").read_text())
    missing = [name for name in set(index["weight_map"].values()) if not (MODEL / name).is_file()]
    if missing:
        raise RuntimeError(f"incomplete fixed model snapshot: {missing}")
    command = [
        str(VLLM),
        "serve",
        str(MODEL),
        "--served-model-name",
        MODEL_ID,
        "--host",
        "0.0.0.0",
        "--port",
        str(PORT),
        "--dtype",
        "bfloat16",
        "--tensor-parallel-size",
        "1",
        "--max-model-len",
        str(SERVICE_SETTINGS.get("context_length", 131072)),
        "--seed",
        str(SERVICE_SETTINGS.get("seed", 42)),
        "--max-num-seqs",
        str(SERVICE_SETTINGS.get("users", 8)),
        "--gpu-memory-utilization",
        str(GPU_MEMORY_UTILIZATION),
        "--enable-auto-tool-choice",
        "--tool-call-parser",
        "qwen3_xml",
        "--reasoning-parser",
        "qwen3",
        "--generation-config",
        "vllm",
        "--override-generation-config",
        json.dumps(
            {
                key: SERVICE_SETTINGS.get(key, default) for key, default in (
                    ("temperature", 1.0), ("top_p", 0.95), ("top_k", 20),
                    ("min_p", 0.0), ("presence_penalty", 0.0), ("repetition_penalty", 1.0))
            }
        ),
        "--default-chat-template-kwargs",
        json.dumps({"enable_thinking": True, "reasoning_effort": SERVICE_SETTINGS.get("reasoning_effort", "medium")}),
    ]
    RUNTIME.mkdir(parents=True, exist_ok=True)
    temporary = RUNTIME / "tmp"
    temporary.mkdir(exist_ok=True)
    safe_environment = {
        "CUDA_VISIBLE_DEVICES": GPU,
        "CUDA_DEVICE_ORDER": "PCI_BUS_ID",
        "VLLM_CACHE_ROOT": str(RUNTIME / "vllm-cache"),
        "XDG_CACHE_HOME": str(RUNTIME / "cache"),
        "TMPDIR": str(temporary),
        "VLLM_RPC_BASE_PATH": "tmp",
        "PYTHONDONTWRITEBYTECODE": "1",
        "HF_HUB_OFFLINE": "1",
        "TOKENIZERS_PARALLELISM": "false",
    }
    before = gpu_snapshot()
    with (RUNTIME / "vllm.log").open("ab", buffering=0) as log:
        process = subprocess.Popen(
            command,
            cwd=RUNTIME,
            env={
                **os.environ,
                **safe_environment,
                "PATH": str(VLLM.parent) + os.pathsep + os.environ.get("PATH", ""),
            },
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    state = {
        "pid": process.pid,
        "start_ticks": start_ticks(process.pid),
        "started_at": utc(),
        "command": command,
        "environment": safe_environment,
        "model_repository": "Qwen/Qwen3.8-27B",
        "model_revision": REVISION,
        "served_model": MODEL_ID,
        "gpu_uuid": GPU,
        "api_base_url": f"http://127.0.0.1:{PORT}/v1",
        "log": str(RUNTIME / "vllm.log"),
        "gpu_before_start": before,
    }
    write_json(STATE, state)
    with (RUNTIME / "start-history.jsonl").open("a") as history:
        history.write(json.dumps(state) + "\n")
    return state


def status() -> dict:
    state = state_read()
    running = owned(state)
    result = {**state, "running": running, "checked_at": utc()}
    if running:
        import httpx

        try:
            response = httpx.get(f"http://127.0.0.1:{PORT}/health", timeout=3, trust_env=False)
            result["healthy"] = response.status_code == 200
        except httpx.HTTPError:
            result["healthy"] = False
    else:
        result["healthy"] = False
    return result


def stop() -> dict:
    state = state_read()
    if not owned(state):
        return {"stopped": False, "reason": "no matching experiment-owned live process"}
    os.killpg(int(state["pid"]), signal.SIGTERM)
    deadline = time.monotonic() + 30
    while owned(state) and time.monotonic() < deadline:
        time.sleep(0.2)
    if owned(state):
        os.killpg(int(state["pid"]), signal.SIGKILL)
    state["stopped_at"] = utc()
    write_json(STATE, state)
    return {"stopped": True, "pid": state["pid"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "status", "stop"])
    args, config = service_config(parser)
    global RUNTIME, STATE, GPU, REVISION, MODEL, VLLM, MODEL_ID, PORT, GPU_MEMORY_UTILIZATION, SERVICE_SETTINGS
    SERVICE_SETTINGS = config
    RUNTIME = Path(config["runtime_dir"]) / "main-service"
    STATE = RUNTIME / "process.json"
    GPU = str(config.get("gpu_uuid", config.get("gpu_index", 0)))
    REVISION = config["model_revision"]
    MODEL = Path(config.get("main_model_path", Path.home() / ".cache/huggingface/hub/models--Qwen--Qwen3.8-27B/snapshots" / REVISION))
    VLLM = Path(config.get("vllm_executable", shutil.which("vllm") or "vllm"))
    MODEL_ID = config["model_id"]
    PORT = urlparse(config["model_base_url"]).port or 80
    GPU_MEMORY_UTILIZATION = float(config.get("gpu_memory_utilization", 0.55))
    print(json.dumps({"start": start, "status": status, "stop": stop}[args.action](), indent=2))


if __name__ == "__main__":
    main()
