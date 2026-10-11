#!/usr/bin/env python3
"""Loopback Chat Completions bridge for an authenticated, text-only Codex CLI.

This adapter preserves supplied messages as JSON inside one CLI prompt. Role
boundaries therefore become text; this is not API-equivalent role handling.
Declared API tools are returned as JSON for the caller to execute. The CLI's own
tools are disabled where supported, and any native tool event rejects the call.
The bridge never reads authentication files or changes HOME or CODEX_HOME.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import signal
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

LOGGER = logging.getLogger("codex_bridge")
MAX_BODY_BYTES = 4 * 1024 * 1024
ALLOWED_ITEM_TYPES = {"agent_message", "reasoning"}
REQUEST_FIELDS = {
    "model",
    "messages",
    "tools",
    "tool_choice",
    "stream",
    "stream_options",
    "temperature",
    "top_p",
    "max_tokens",
    "max_completion_tokens",
    "seed",
    "stop",
    "response_format",
    "parallel_tool_calls",
}


class BridgeError(Exception):
    def __init__(self, message: str, *, status: int = 502, code: str = "bridge_error") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def _error_response(error: BridgeError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status,
        content={"error": {"message": str(error), "type": "api_error", "param": None, "code": error.code}},
    )


def _redact(text: str) -> str:
    """Keep diagnostic logs free of recognizable authentication material."""
    text = re.sub(r"(?i)(bearer\s+)[^\s\"']+", r"\1[REDACTED]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[REDACTED]", text)
    return re.sub(
        r"(?i)([\"\']?(?:access_token|refresh_token|id_token|api_key|authorization)[\"\']?\s*[:=]\s*)"
        r"([\"\']?)[^\s,}\"\']+\2",
        lambda match: match.group(1) + match.group(2) + "[REDACTED]" + match.group(2),
        text,
    )


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]"
            if key.casefold()
            in {
                "access_token",
                "refresh_token",
                "id_token",
                "api_key",
                "authorization",
            }
            else _sanitize(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    return _redact(value) if isinstance(value, str) else value


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(_sanitize(value), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"Non-JSON numeric constant: {value}")


def validate_request(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise BridgeError("The request body must be a JSON object.", status=400, code="invalid_request")
    payload = {key: value for key, value in body.items() if key in REQUEST_FIELDS}
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise BridgeError("messages must be a non-empty list.", status=400, code="invalid_request")
    for message in messages:
        if (
            not isinstance(message, dict)
            or not isinstance(message.get("role"), str)
            or message.get("role")
            not in {
                "system",
                "developer",
                "user",
                "assistant",
                "tool",
            }
        ):
            raise BridgeError("Every message must have a supported role.", status=400, code="invalid_request")
        content = message.get("content")
        if isinstance(content, list):
            if not all(
                isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
                for part in content
            ):
                raise BridgeError("This bridge supports text content only.", status=400, code="invalid_request")
        elif content is not None and not isinstance(content, str):
            raise BridgeError("Message content must be text or null.", status=400, code="invalid_request")
        if message["role"] == "tool" and not isinstance(message.get("tool_call_id"), str):
            raise BridgeError("Tool messages require tool_call_id.", status=400, code="invalid_request")
    if "stream" in payload and not isinstance(payload["stream"], bool):
        raise BridgeError("stream must be a boolean.", status=400, code="invalid_request")
    if "model" in payload and not isinstance(payload["model"], str):
        raise BridgeError("model must be a string.", status=400, code="invalid_request")
    tools = payload.get("tools") or []
    if not isinstance(tools, list):
        raise BridgeError("tools must be a list.", status=400, code="invalid_request")
    names: set[str] = set()
    for tool in tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(function, dict) or tool.get("type") != "function":
            raise BridgeError("Only function tools are supported.", status=400, code="invalid_request")
        name = function.get("name")
        if not isinstance(name, str) or not name or name in names:
            raise BridgeError("Function names must be non-empty and unique.", status=400, code="invalid_request")
        if "parameters" in function and not isinstance(function["parameters"], dict):
            raise BridgeError("Function parameters must be a JSON schema object.", status=400, code="invalid_request")
        names.add(name)
    choice = payload.get("tool_choice", "auto")
    if isinstance(choice, dict):
        selected = choice.get("function", {}).get("name") if isinstance(choice.get("function"), dict) else None
        if choice.get("type") != "function" or selected not in names:
            raise BridgeError("tool_choice must name a declared function.", status=400, code="invalid_request")
    elif not isinstance(choice, str) or choice not in {"auto", "none", "required"}:
        raise BridgeError("Unsupported tool_choice.", status=400, code="invalid_request")
    elif choice == "required" and not tools:
        raise BridgeError("tool_choice=required needs tools.", status=400, code="invalid_request")
    return payload


def build_prompt(payload: dict[str, Any]) -> str:
    preamble = (
        "Act only as a text-only Chat Completions model for the supplied request.\n"
        "Do not inspect files, run commands, browse, contact MCP/app tools, load skills, or delegate.\n"
        "Never invoke your CLI's native tools. All supplied tool definitions describe tools of an external caller.\n"
        "Follow the supplied system/developer instructions and use the supplied user, assistant, and tool messages "
        "as the conversation. Preserve their task and requested output format.\n"
        "The native message JSON follows verbatim. Its role boundaries are text in this adapter.\n"
    )
    if payload.get("tools"):
        output_rule = (
            "Return ONLY one assistant-message JSON object with exactly two fields: "
            '{"content": "text or null", "tool_calls": []}. '
            "content must actually be a string or JSON null. If tools are needed, tool_calls contains objects "
            'of shape {"id":"call_unique_id","type":"function","function":'
            '{"name":"declared_name","arguments":"JSON object encoded as a string"}}. '
            "Choose only declared functions, obey tool_choice, and satisfy their parameter schemas. "
            "Generate the tool call; do not execute it. After tool results arrive in the supplied messages, "
            "continue that conversation normally. Do not include Markdown fences.\n"
        )
    else:
        output_rule = (
            "There are no external tools. Return ONLY the assistant's requested response text, without an "
            "assistant-message wrapper or extra commentary. A requested plain summary stays plain text; "
            "a requested JSON result stays the required JSON object.\n"
        )
    supplied = {key: payload[key] for key in ("messages", "tools", "tool_choice", "response_format") if key in payload}
    return (
        preamble
        + output_rule
        + "\n<SUPPLIED_REQUEST_JSON>\n"
        + json.dumps(
            supplied,
            ensure_ascii=False,
            indent=2,
        )
        + "\n</SUPPLIED_REQUEST_JSON>\n"
    )


def parse_assistant_message(raw: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not raw.strip():
        raise BridgeError("Codex returned an empty response.", code="invalid_output")
    if not payload.get("tools"):
        return {"role": "assistant", "content": raw}
    try:
        message = json.loads(raw, parse_constant=_reject_json_constant)
    except (ValueError, TypeError) as exc:
        raise BridgeError(
            "Tool-enabled responses must be a valid assistant-message JSON object.", code="invalid_output"
        ) from exc
    if not isinstance(message, dict) or set(message) != {"content", "tool_calls"}:
        raise BridgeError("Assistant JSON requires exactly content and tool_calls.", code="invalid_output")
    content, calls = message["content"], message["tool_calls"]
    if content is not None and not isinstance(content, str):
        raise BridgeError("Assistant content must be a string or null.", code="invalid_output")
    if not isinstance(calls, list):
        raise BridgeError("tool_calls must be a list.", code="invalid_output")
    allowed = {tool["function"]["name"] for tool in payload["tools"]}
    seen: set[str] = set()
    for call in calls:
        if not isinstance(call, dict) or call.get("type") != "function":
            raise BridgeError("Every tool call must be a function object.", code="invalid_output")
        call_id = call.get("id")
        function = call.get("function")
        if not isinstance(call_id, str) or not call_id or call_id in seen or not isinstance(function, dict):
            raise BridgeError("Tool calls need unique IDs and function objects.", code="invalid_output")
        if not isinstance(function.get("name"), str) or function.get("name") not in allowed:
            raise BridgeError("Codex requested an undeclared function.", code="invalid_output")
        arguments = function.get("arguments")
        try:
            parsed = json.loads(arguments, parse_constant=_reject_json_constant) if isinstance(arguments, str) else None
        except ValueError as exc:
            raise BridgeError("Function arguments must contain valid JSON.", code="invalid_output") from exc
        if not isinstance(parsed, dict):
            raise BridgeError("Function arguments must be a JSON object encoded as a string.", code="invalid_output")
        seen.add(call_id)
    choice = payload.get("tool_choice", "auto")
    if choice == "none" and calls:
        raise BridgeError("Codex returned tools despite tool_choice=none.", code="invalid_output")
    if choice == "required" and not calls:
        raise BridgeError("Codex omitted the required tool call.", code="invalid_output")
    if isinstance(choice, dict) and (
        not calls or any(call["function"]["name"] != choice["function"]["name"] for call in calls)
    ):
        raise BridgeError("Codex did not follow the requested function choice.", code="invalid_output")
    if not calls and not content:
        raise BridgeError("Assistant response has neither text nor tool calls.", code="invalid_output")
    result = {"role": "assistant", "content": content}
    if calls:
        result["tool_calls"] = calls
    return result


class CodexBackend:
    def __init__(
        self, artifact_dir: Path, *, model: str | None = None, timeout: float = 180, codex: str = "codex"
    ) -> None:
        self.artifact_dir = artifact_dir.resolve()
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.model = model
        self.timeout = timeout
        self.codex = shutil.which(codex)
        if not self.codex:
            raise RuntimeError(f"Codex executable unavailable: {codex}")
        self.lock = asyncio.Semaphore(1)

    def command(self, answer: Path) -> list[str]:
        command = [
            self.codex,
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "-c",
            'model_reasoning_effort="low"',
            "--json",
            "--color",
            "never",
            "--disable",
            "shell_tool",
            "--disable",
            "unified_exec",
            "--disable",
            "apps",
            "--disable",
            "plugins",
            "--disable",
            "multi_agent",
            "-c",
            'web_search="disabled"',
            "--output-last-message",
            str(answer),
        ]
        if self.model:
            command.extend(["--model", self.model])
        command.append("-")
        return command

    @staticmethod
    async def _kill(process: asyncio.subprocess.Process) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()

    async def complete(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self.lock:
            return await self._complete_locked(payload)

    async def _complete_locked(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = uuid.uuid4().hex
        created = int(time.time())
        artifact = self.artifact_dir / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{request_id}"
        artifact.mkdir()
        prompt = build_prompt(payload)
        _write_json(artifact / "request.json", payload)
        (artifact / "prompt.txt").write_text(_redact(prompt), encoding="utf-8")
        events: list[dict[str, Any]] = []
        stdout_lines: list[str] = []
        stderr_bytes = bytearray()
        raw = ""
        process: asyncio.subprocess.Process | None = None
        record: dict[str, Any] = {
            "id": request_id,
            "created": created,
            "status": "running",
            "configured_model": self.model,
            "requested_model": payload.get("model"),
            "model_selection": "explicit_cli_override" if self.model else "inherited_cli_config",
            "role_handling": "flattened_verbatim_message_json; not API equivalent",
            "timeout_seconds": self.timeout,
            "native_tools_allowed": False,
        }
        started = time.monotonic()
        tasks: list[asyncio.Task[Any]] = []
        try:
            with tempfile.TemporaryDirectory(prefix="sharedskill-codex-bridge-") as workdir:
                answer = Path(workdir) / "last-message.txt"
                process = await asyncio.create_subprocess_exec(
                    *self.command(answer),
                    cwd=workdir,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                    limit=8 * 1024 * 1024,
                )

                async def send_prompt() -> None:
                    assert process is not None and process.stdin is not None
                    try:
                        process.stdin.write(prompt.encode())
                        await process.stdin.drain()
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    finally:
                        process.stdin.close()

                async def read_events() -> None:
                    assert process is not None and process.stdout is not None
                    async for line in process.stdout:
                        decoded = line.decode("utf-8", errors="replace")
                        stdout_lines.append(decoded)
                        try:
                            event = json.loads(decoded)
                        except ValueError:
                            continue
                        if not isinstance(event, dict):
                            continue
                        events.append(event)
                        item = event.get("item")
                        if isinstance(item, dict) and item.get("type") not in ALLOWED_ITEM_TYPES:
                            raise BridgeError(
                                f"Rejected native Codex item type: {item.get('type')}.",
                                code="native_tool_rejected",
                            )

                async def read_stderr() -> None:
                    assert process is not None and process.stderr is not None
                    while chunk := await process.stderr.read(65536):
                        stderr_bytes.extend(chunk)

                tasks = [
                    asyncio.create_task(send_prompt()),
                    asyncio.create_task(read_events()),
                    asyncio.create_task(read_stderr()),
                ]
                try:
                    await asyncio.wait_for(asyncio.gather(*tasks, process.wait()), timeout=self.timeout)
                except TimeoutError as exc:
                    raise BridgeError("Codex subprocess timed out.", status=504, code="codex_timeout") from exc
                finally:
                    if process.returncode is None:
                        await self._kill(process)
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                record["returncode"] = process.returncode
                if process.returncode != 0:
                    raise BridgeError(
                        f"Codex exited with status {process.returncode}; inspect the request artifact.",
                        code="codex_failed",
                    )
                if not answer.is_file():
                    raise BridgeError("Codex did not create its final-message artifact.", code="invalid_output")
                raw = answer.read_text(encoding="utf-8")
            message = parse_assistant_message(raw, payload)
            usage = next((event["usage"] for event in reversed(events) if isinstance(event.get("usage"), dict)), None)
            record["cli_usage"] = usage
            record["status"] = "completed"
            completion: dict[str, Any] = {
                "id": f"chatcmpl-{request_id}",
                "object": "chat.completion",
                "created": created,
                "model": self.model or "codex-config",
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": "tool_calls" if message.get("tool_calls") else "stop",
                    }
                ],
            }
            if usage and isinstance(usage.get("input_tokens"), int) and isinstance(usage.get("output_tokens"), int):
                completion["usage"] = {
                    "prompt_tokens": usage["input_tokens"],
                    "completion_tokens": usage["output_tokens"],
                    "total_tokens": usage["input_tokens"] + usage["output_tokens"],
                }
                if isinstance(usage.get("cached_input_tokens"), int):
                    completion["usage"]["prompt_tokens_details"] = {"cached_tokens": usage["cached_input_tokens"]}
            _write_json(artifact / "completion.json", completion)
            return completion
        except BaseException as exc:
            record["status"] = "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
            record["error"] = str(exc)
            record["error_code"] = getattr(exc, "code", type(exc).__name__)
            if process is not None and process.returncode is None:
                await self._kill(process)
            if isinstance(exc, (BridgeError, asyncio.CancelledError)):
                raise
            raise BridgeError("Codex bridge failed; inspect the request artifact.") from exc
        finally:
            record["elapsed_seconds"] = round(time.monotonic() - started, 3)
            if process is not None:
                record["returncode"] = process.returncode
            if "cli_usage" not in record:
                record["cli_usage"] = next(
                    (event["usage"] for event in reversed(events) if isinstance(event.get("usage"), dict)), None
                )
            _write_json(artifact / "run.json", record)
            (artifact / "raw-response.txt").write_text(_redact(raw), encoding="utf-8")
            (artifact / "cli-events.jsonl").write_text(
                "".join(json.dumps(_sanitize(event), ensure_ascii=False) + "\n" for event in events),
                encoding="utf-8",
            )
            (artifact / "cli-stdout.txt").write_text(_redact("".join(stdout_lines)), encoding="utf-8")
            (artifact / "cli-stderr.txt").write_text(
                _redact(stderr_bytes.decode("utf-8", errors="replace")), encoding="utf-8"
            )
            LOGGER.info("request=%s status=%s elapsed=%.3fs", request_id, record["status"], record["elapsed_seconds"])


def completion_chunks(completion: dict[str, Any], *, include_usage: bool = False) -> list[dict[str, Any]]:
    common = {key: completion[key] for key in ("id", "created", "model")}
    common["object"] = "chat.completion.chunk"
    choice = completion["choices"][0]
    message = choice["message"]
    chunks = [{**common, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}]
    delta: dict[str, Any] = {}
    if message.get("content") is not None:
        delta["content"] = message["content"]
    if message.get("tool_calls"):
        delta["tool_calls"] = [{"index": index, **call} for index, call in enumerate(message["tool_calls"])]
    chunks.append({**common, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]})
    chunks.append({**common, "choices": [{"index": 0, "delta": {}, "finish_reason": choice["finish_reason"]}]})
    if include_usage and "usage" in completion:
        chunks.append({**common, "choices": [], "usage": completion["usage"]})
    return chunks


def create_app(
    artifact_dir: str | Path, *, model: str | None = None, timeout: float = 180, codex: str = "codex"
) -> FastAPI:
    backend = CodexBackend(Path(artifact_dir), model=model, timeout=timeout, codex=codex)
    app = FastAPI(title="Loopback Codex bridge", docs_url=None, redoc_url=None)
    app.state.backend = backend

    @app.get("/healthz")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "model": model or "inherited_cli_config", "max_concurrency": 1, "api_equivalent": False}

    @app.post("/v1/chat/completions")
    async def chat(request: Request) -> Any:
        try:
            body = await request.body()
            if len(body) > MAX_BODY_BYTES:
                raise BridgeError("Request body is too large.", status=413, code="invalid_request")
            try:
                decoded = json.loads(body, parse_constant=_reject_json_constant)
            except ValueError as exc:
                raise BridgeError("Request body must be valid JSON.", status=400, code="invalid_request") from exc
            payload = validate_request(decoded)
        except BridgeError as exc:
            return _error_response(exc)
        if not payload.get("stream"):
            try:
                return JSONResponse(await backend.complete(payload))
            except BridgeError as exc:
                return _error_response(exc)

        async def stream() -> Any:
            task = asyncio.create_task(backend.complete(payload))
            try:
                while not task.done():
                    done, _ = await asyncio.wait({task}, timeout=10)
                    if not done:
                        yield ": codex bridge waiting\n\n"
                completion = await task
                options = payload.get("stream_options")
                include_usage = isinstance(options, dict) and options.get("include_usage") is True
                for chunk in completion_chunks(completion, include_usage=include_usage):
                    yield "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"
            except BridgeError as exc:
                yield (
                    "data: "
                    + json.dumps({"error": {"message": str(exc), "type": "api_error", "code": exc.code}})
                    + "\n\n"
                )
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        )

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument(
        "--model", default=None, help="Explicit Codex CLI model override; otherwise inherit its configuration."
    )
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--codex", default="codex")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or args.timeout <= 0:
        parser.error("port must be 1..65535 and timeout must be positive")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    import uvicorn

    uvicorn.run(
        create_app(args.artifact_dir, model=args.model, timeout=args.timeout, codex=args.codex),
        host="127.0.0.1",
        port=args.port,
    )


if __name__ == "__main__":
    main()
