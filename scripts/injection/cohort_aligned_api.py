"""Native Shared-Skill recording around the executor's immutable Agent route."""

from __future__ import annotations

import hashlib
import json
import re
import socket
import threading
from pathlib import Path

import httpx
from starlette.responses import JSONResponse

from scripts.injection import e2e



_START_LOCK = threading.Lock()


class BoundAgentRoutes:
    """Only the exact registered Agent enters native trajectory recording."""

    def __init__(self, app, settings, binding, gateway_url, service_dir):
        self.app, self.settings, self.binding = app, settings, binding
        self.gateway = gateway_url.rstrip("/")
        self.service = Path(service_dir)
        self.bound = (f"/runs/{binding['run_id']}/executions/{binding['execution_id']}/agent/v1")
        self.unbound = f"/runs/{binding['run_id']}/v1"
        self.session = self.service.name + "-chat"
        self.lock = threading.Lock()

    def _record(self, path, raw, destination, session=None):
        row = {"at": e2e.now(), "path": path, "destination": destination,
               "body_sha256": e2e.sha256(raw), "body_bytes": len(raw), "session_id": session,
               "execution_id": self.binding["execution_id"],
               "bank_sha256": self.binding["bank_sha256"],
               "catalog_sha256": self.binding["catalog_sha256"],
               "binding_sha256": self.binding["binding_sha256"]}
        with self.lock:
            with (self.service / "routing.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        agent = path.startswith(self.bound + "/")
        side = path.startswith(self.unbound + "/")
        if not agent and not side:
            if path.startswith("/runs/") or path.startswith("/v1/"):
                return await JSONResponse({"error": "Request is outside the immutable task route"}, 409)(scope, receive, send)
            return await self.app(scope, receive, send)
        raw = bytearray()
        while True:
            event = await receive()
            if event["type"] == "http.disconnect":
                return
            raw.extend(event.get("body", b""))
            if not event.get("more_body", False):
                break
        raw = bytes(raw)
        if side:
            self._record(path, raw, "local_grading_gateway")
            headers = {key.decode("latin1"): value.decode("latin1") for key, value in scope.get("headers", [])
                       if key.lower() not in {b"host", b"content-length", b"connection"}}
            query = scope.get("query_string", b"").decode("latin1")
            async with httpx.AsyncClient(timeout=1800, trust_env=False) as client:
                response = await client.request(scope["method"], self.gateway + path + ("?" + query if query else ""),
                                                headers=headers, content=raw)
            await send({"type": "http.response.start", "status": response.status_code,
                        "headers": [(key.encode("latin1"), value.encode("latin1"))
                                    for key, value in response.headers.items()
                                    if key.lower() not in {"content-length", "transfer-encoding", "connection"}]})
            return await send({"type": "http.response.body", "body": response.content})
        try:
            from scripts.wildclawbench.alignment_catalog import load_binding
            current = load_binding(self.settings, self.binding["run_id"], self.binding["execution_id"])
            if current != self.binding:
                raise ValueError("The immutable Agent binding changed")
        except (OSError, ValueError, KeyError) as error:
            return await JSONResponse({"error": str(error)}, 409)(scope, receive, send)
        suffix = path[len(self.bound):]
        if suffix not in {"/chat/completions", "/models"}:
            return await JSONResponse({"error": "Unsupported bound Agent endpoint"}, 404)(scope, receive, send)
        self._record(path, raw, "sharedskill_agent", self.session)
        routed = dict(scope, path="/v1" + suffix, raw_path=("/v1" + suffix).encode())
        routed["headers"] = [(key, value) for key, value in scope.get("headers", [])
                             if key.lower() not in {b"x-session-id", b"x-turn-type"}]
        routed["headers"] += [(b"x-session-id", self.session.encode()), (b"x-turn-type", b"main")]
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": raw, "more_body": False}
            return {"type": "http.disconnect"}

        return await self.app(routed, replay, send)


def start_aligned_api(bank, run_dir, service_name, gateway_url, settings, task, output):
    """Enable existing executor alignment without changing frozen executor code."""
    from scripts.wildclawbench.alignment_catalog import prepare_binding
    from scripts.injection.cohort_uploads import install_upload_archive

    bank, run_dir, output = Path(bank).resolve(), Path(run_dir).resolve(), Path(output).resolve()
    if not settings.get("alignment") or not settings.get("protocol_hash"):
        raise ValueError("Aligned cohort execution requires an explicit frozen alignment protocol")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", service_name) or service_name in {".", ".."}:
        raise ValueError("Unsafe aligned service name")
    if settings.get("user_id") != task["user_id"] or settings.get("task", {}).get("source_path") != task["source_path"]:
        raise ValueError("The API and task executor must use the same task configuration")
    digest = hashlib.sha256(str(output).encode()).hexdigest()
    run_id = "wcb-" + digest[:20]
    execution_id = str(settings.get("execution_id") or digest[:24])
    binding = prepare_binding(settings, run_id, execution_id, bank)
    service = run_dir / "services" / service_name
    service.mkdir(parents=True, exist_ok=True)
    bound_url = (gateway_url.rstrip("/") + f"/runs/{run_id}/executions/{execution_id}/agent/v1")
    e2e.write_json(service / "gateway.json", {"base_url": bound_url, "root": gateway_url.rstrip("/")})
    with _START_LOCK:
        preferred = int(json.loads(e2e.CONFIG_FILE.read_text())["api_port"])
        with socket.socket() as candidate:
            try:
                candidate.bind(("0.0.0.0", preferred))
                port = preferred
            except OSError:
                candidate.bind(("0.0.0.0", 0))
                port = candidate.getsockname()[1]
        api = e2e.build_api(bank, service / "shared-objects", service / "records", service / "requests")
        # Keep native skill-read metadata; the bound gateway alone injects its
        # read-only pinned catalog. Preserve history for the gateway tokenizer.
        api.skill_manager._public_skill_root = "/root/skills"
        api._inject_skills = lambda messages: (messages, [])
        api._truncate_messages = lambda messages, tools, max_prompt: messages
        api.app = BoundAgentRoutes(api.app.app, dict(settings), binding, gateway_url, service)
        api.config.proxy_port = port
        install_upload_archive(api, service)
        api.start()
        if not api.wait_until_ready(30):
            api.stop()
            raise RuntimeError("Aligned Shared-Skill API did not become ready")
    e2e.write_json(service / "execution-binding.json", binding)
    e2e.write_json(service / "service.json", {
        "status": "ready", "port": port, "bank_sha256": binding["bank_sha256"],
        "catalog_sha256": binding["catalog_sha256"], "binding_sha256": binding["binding_sha256"],
        "execution_id": execution_id, "run_id": run_id, "gateway_agent_url": bound_url,
        "protocol_hash": binding["protocol_hash"], "catalog_injector": "bound_alignment_gateway",
        "native_api_records_and_uploads": True, "history_truncation": "gateway_tokenizer_rejects_overflow",
        "executor_alignment_enabled": True, "defense": "none"})
    return api, port, service
