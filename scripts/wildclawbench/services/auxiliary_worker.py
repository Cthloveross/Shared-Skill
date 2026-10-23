"""Bounded HTTP front end for one disposable, experiment-owned model worker.

The worker owns CUDA. Cancelling a request kills that worker and its descendants;
releasing an asyncio task alone would leave GPU inference running.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response

from scripts.wildclawbench.agent_supervisor import identity, processes


DEFAULT_LIMITS = {
    "queue_size": 2,
    "queue_timeout_seconds": 15,
    "request_timeout_seconds": 300,
    "model_load_timeout_seconds": 600,
    "ffmpeg_timeout_seconds": 30,
    "max_body_bytes": 32 * 1024 * 1024,
    "max_audio_bytes": 16 * 1024 * 1024,
    "max_audio_seconds": 300,
    "max_text_chars": 20000,
    "max_speech_chars": 2000,
    "max_audio_tokens": 2048,
    "max_image_steps": 50,
    "max_image_count": 4,
    "max_image_pixels": 1536 * 1536,
}


def limits(config):
    supplied = config.get("auxiliary_limits", {})
    if not isinstance(supplied, dict) or set(supplied) - set(DEFAULT_LIMITS):
        raise ValueError("Unknown auxiliary_limits fields")
    result = {**DEFAULT_LIMITS, **supplied}
    for key, value in result.items():
        integer = key.startswith("max_") and key != "max_audio_seconds" or key == "queue_size"
        if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                or value <= 0 or (integer and type(value) is not int)):
            raise ValueError(f"auxiliary_limits.{key} must be a positive {'integer' if integer else 'number'}")
    return result


def model_root(config):
    shared = Path(config.get("shared_runtime_dir", config["runtime_dir"]))
    return Path(config.get("auxiliary_model_root", shared / "models")).resolve()


def stop_tree(pid, ticks, timeout=3):
    """Stop only the authenticated worker subtree; never scan unrelated host PIDs."""
    current = identity(pid)
    known = {}
    if current is not None and current[2] == ticks:
        known[pid] = ticks
        # Freeze before killing to prevent children escaping during discovery.
        for _ in range(8):
            table = {pid: identity(pid), **processes(pid)}
            fresh = False
            for child, entry in table.items():
                if entry is None or entry[1] in {"Z", "X"}:
                    continue
                known[child] = entry[2]
                if entry[1] not in {"T", "t"}:
                    try:
                        os.kill(child, signal.SIGSTOP)
                        fresh = True
                    except ProcessLookupError:
                        pass
            if not fresh:
                break
            time.sleep(.01)
    for child, started in known.items():
        entry = identity(child)
        if entry and entry[2] == started:
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + timeout
    while True:
        survivors = [child for child, started in known.items()
                     if (entry := identity(child)) and entry[2] == started and entry[1] not in {"Z", "X"}]
        if not survivors or time.monotonic() >= deadline:
            return {"quiescent": not survivors, "survivors": survivors, "identities": known}
        time.sleep(.01)


class Worker:
    def __init__(self, kind, config, command=None):
        self.kind, self.config, self.limits = kind, config, limits(config)
        self.directory = Path(config["runtime_dir"]) / ("auxiliary-" + kind)
        self.directory.mkdir(parents=True, exist_ok=True)
        # Unix socket paths have a ~108-byte kernel limit; experiment paths do not.
        # The service deliberately sets TMPDIR under runtime for model scratch
        # files. Do not inherit it for AF_UNIX paths, which must remain short.
        self.socket_directory = tempfile.TemporaryDirectory(prefix="sharedskill-aux-", dir="/tmp")
        self.socket = Path(self.socket_directory.name) / "worker.sock"
        self.command = command
        self.process = None
        self.ticks = None
        self.failed_closed = False
        self.fenced_handle = None
        self.lock = asyncio.Lock()
        self.waiters = 0
        shared = Path(config.get("shared_runtime_dir", config["runtime_dir"]))
        self.gpu_lock = Path(config.get("auxiliary_gpu_lock", shared / "auxiliary-gpu.lock"))
        self.gpu_lock.parent.mkdir(parents=True, exist_ok=True)

    def alive(self):
        return self.process is not None and self.process.poll() is None

    def stop(self, reason):
        if self.process is None:
            return
        record = stop_tree(self.process.pid, self.ticks)
        record.update(reason=reason, pid=self.process.pid, at_unix=time.time())
        with (self.directory / "worker-stops.jsonl").open("a") as stream:
            stream.write(json.dumps(record) + "\n")
        self.failed_closed = not record["quiescent"]
        if record["quiescent"]:
            self.process.wait(timeout=3)
            self.process = None
            self.socket.unlink(missing_ok=True)
            if self.fenced_handle is not None:
                fcntl.flock(self.fenced_handle, fcntl.LOCK_UN)
                self.fenced_handle.close()
                self.fenced_handle = None

    async def ensure_started(self, request=None, deadline=None):
        if self.failed_closed:
            raise HTTPException(503, "Previous model worker did not stop; service is fenced")
        if self.alive():
            return
        if self.process is not None:
            self.stop("worker exited")
        self.socket.unlink(missing_ok=True)
        command = ([part.replace("{socket}", str(self.socket)) for part in self.command] if self.command else
                   [sys.executable, "-B", "-m", "scripts.wildclawbench.services.auxiliary_server",
                                   "--kind", self.kind, "--worker-socket", str(self.socket),
                                   "--config", str(self.config["service_config_path"])])
        with (self.directory / "worker.log").open("ab", buffering=0) as log:
            self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                            start_new_session=True)
        self.ticks = identity(self.process.pid)[2]
        (self.directory / "worker-process.json").write_text(json.dumps({
            "pid": self.process.pid, "start_ticks": self.ticks, "command": command,
            "socket": str(self.socket), "socket_directory_mode": "0700",
            "model_root": str(model_root(self.config)), "started_unix": time.time()}, indent=2) + "\n")
        limit = min(deadline or float("inf"), time.monotonic() + self.limits["model_load_timeout_seconds"])
        try:
            async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=str(self.socket)), timeout=1,
                                         trust_env=False) as client:
                while self.alive() and time.monotonic() < limit:
                    if request is not None and await request.is_disconnected():
                        raise HTTPException(499, "Client disconnected while loading model")
                    try:
                        response = await client.get("http://worker/healthz")
                        if response.status_code == 200:
                            return
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(.05)
            raise HTTPException(503, "Model worker failed to become ready before its deadline")
        except BaseException:
            self.stop("model load failed or cancelled")
            raise

    @asynccontextmanager
    async def slot(self, request=None, deadline=None):
        if self.failed_closed:
            raise HTTPException(503, "Previous model worker did not stop; service is fenced")
        if self.waiters >= self.limits["queue_size"]:
            raise HTTPException(429, "Auxiliary request queue is full")
        self.waiters += 1
        acquired, handle = False, None
        limit = min(deadline or float("inf"), time.monotonic() + self.limits["queue_timeout_seconds"])
        try:
            while True:
                if request is not None and await request.is_disconnected():
                    raise HTTPException(499, "Client disconnected while queued")
                if time.monotonic() >= limit:
                    raise HTTPException(503, "Auxiliary queue deadline exceeded")
                if not acquired:
                    try:
                        await asyncio.wait_for(self.lock.acquire(), timeout=min(.05, max(.001, limit - time.monotonic())))
                        acquired = True
                        handle = self.gpu_lock.open("a")
                    except asyncio.TimeoutError:
                        continue
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(.02)
            yield
        finally:
            if handle is not None:
                if self.failed_closed:
                    # Do not let another auxiliary service use this GPU until
                    # the previous worker's exit is confirmed.
                    self.fenced_handle = handle
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)
                    handle.close()
            if acquired:
                self.lock.release()
            self.waiters -= 1

    async def forward(self, request, raw, deadline):
        async with self.slot(request, deadline):
            await self.ensure_started(request, deadline)
            client = httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=str(self.socket)), timeout=None,
                                       trust_env=False)
            headers = {key: value for key, value in request.headers.items()
                       if key.lower() not in {"host", "content-length", "connection", "authorization"}}
            headers["x-public-base-url"] = str(request.base_url)
            operation = asyncio.create_task(client.request(request.method, "http://worker" + request.url.path,
                                                            content=raw, headers=headers))
            try:
                while not operation.done():
                    if time.monotonic() >= deadline:
                        raise HTTPException(504, "Auxiliary inference exceeded its hard deadline")
                    if await request.is_disconnected():
                        raise HTTPException(499, "Client disconnected during inference")
                    await asyncio.wait({operation}, timeout=.05)
                result = await operation
                if time.monotonic() >= deadline:
                    raise HTTPException(504, "Auxiliary response arrived after its hard deadline")
                if result.status_code >= 500:
                    self.stop("model worker returned an infrastructure error")
                return Response(result.content, result.status_code,
                                media_type=result.headers.get("content-type"))
            except BaseException:
                # Kill and confirm before releasing the shared GPU slot.
                self.stop("request failed, cancelled, disconnected, or expired")
                raise
            finally:
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
                await client.aclose()


def create_proxy(kind, config, *, command=None):
    worker = Worker(kind, config, command)

    @asynccontextmanager
    async def lifespan(app):
        try:
            async with worker.slot():
                await worker.ensure_started()
            yield
        finally:
            worker.stop("service shutdown")
            if not worker.failed_closed:
                worker.socket_directory.cleanup()

    app = FastAPI(title=f"Bounded local {kind} service", lifespan=lifespan)
    app.state.worker = worker

    @app.get("/health")
    @app.get("/healthz")
    @app.get("/v1/models")
    async def health(request: Request):
        if not worker.alive() or worker.failed_closed:
            raise HTTPException(503, "Model worker is not loaded")
        async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=str(worker.socket)), timeout=2,
                                     trust_env=False) as client:
            response = await client.get("http://worker" + request.url.path)
        return Response(response.content, response.status_code, media_type=response.headers.get("content-type"))

    @app.api_route("/{path:path}", methods=["GET", "POST"])
    async def proxy(request: Request, path: str):
        started = time.monotonic()
        deadline = started + worker.limits["request_timeout_seconds"]
        header = request.headers.get("x-request-deadline-unix")
        if header is not None:
            try:
                remaining = float(header) - time.time()
                if not math.isfinite(remaining):
                    raise ValueError()
            except ValueError:
                raise HTTPException(400, "Invalid request deadline")
            deadline = min(deadline, started + remaining)
        if time.monotonic() >= deadline:
            raise HTTPException(504, "Auxiliary request deadline already expired")
        async def execute():
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > worker.limits["max_body_bytes"]:
                    raise HTTPException(413, "Auxiliary request body exceeds configured limit")
            return await worker.forward(request, bytes(raw), deadline)
        try:
            return await asyncio.wait_for(execute(), timeout=max(.001, deadline - time.monotonic()))
        except asyncio.TimeoutError:
            raise HTTPException(504, "Auxiliary request exceeded its hard deadline")

    return app


def owned_command(command, *, timeout, **kwargs):
    """Bind ffmpeg/ffprobe lifetime to the worker, even if its API is killed."""
    return subprocess.run([sys.executable, "-B", "-m", __name__, str(os.getpid()), *command],
                          timeout=timeout, **kwargs)


if __name__ == "__main__":
    import ctypes
    expected_parent = int(sys.argv[1])
    if os.getppid() != expected_parent or ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
        raise SystemExit(70)
    if os.getppid() != expected_parent:
        raise SystemExit(70)
    os.execvp(sys.argv[2], sys.argv[2:])
