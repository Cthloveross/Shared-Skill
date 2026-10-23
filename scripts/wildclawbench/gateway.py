"""Local execution router with an explicit, budgeted external judge route."""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import hashlib
import io
import json
import math
import re
import time
import uuid
import wave
from email.parser import BytesParser
from email.policy import default as email_policy
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import uvicorn
from scripts.wildclawbench.alignment_catalog import CatalogError, inject_catalog, load_binding
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from scripts.wildclawbench.paid_judge import BudgetExceeded, CostLedger, credential, judge_payload
from scripts.wildclawbench.protocol import ROOT, load_config, public_config, write_json
from scripts.wildclawbench.interface_checks import required_interfaces
from scripts.wildclawbench.local_budget import LocalRequestBudget, MediaRequestBudget
from scripts.wildclawbench.task_runtime import local_judge_settings

SUPPORTED_ENDPOINTS = frozenset(
    {"chat/completions", "completions", "audio/speech", "audio/transcriptions", "images/generations"}
)
AUDIO_FORMATS = frozenset({"wav", "mp3", "flac", "ogg", "opus", "aac"})
SPEECH_FORMATS = frozenset({"wav", "mp3", "flac", "opus", "aac", "pcm"})


def transcription_fields(raw: bytes, content_type: str) -> dict:
    """Read routing metadata without modifying the multipart body or audio."""
    if "\r" in content_type or "\n" in content_type:
        raise ValueError("Invalid multipart Content-Type")
    message = BytesParser(policy=email_policy).parsebytes(
        b"Content-Type: " + content_type.encode("ascii") + b"\r\nMIME-Version: 1.0\r\n\r\n" + raw
    )
    if not message.is_multipart() or message.defects:
        raise ValueError("Invalid transcription multipart data")
    fields = {}
    for part in message.iter_parts():
        if part.defects:
            raise ValueError("Invalid transcription multipart part")
        name = part.get_param("name", header="content-disposition")
        if name in fields:
            raise ValueError("Duplicate transcription routing field")
        if name == "file" and part.get_filename() is not None:
            fields["file"] = True
        elif name in {"model", "response_format"}:
            data = part.get_payload(decode=True)
            if not isinstance(data, bytes):
                raise ValueError("Invalid transcription routing field")
            fields[name] = data.decode(part.get_content_charset() or "utf-8")
    if not fields.get("file"):
        raise ValueError("audio/transcriptions requires a file")
    return fields


def audio_content_error(value: object) -> str | None:
    """Reject unsupported media contracts without fetching or converting input."""
    if isinstance(value, dict):
        if value.get("type") == "audio_url":
            return "audio_url is unsupported; submit input_audio with the original encoded audio"
        if value.get("type") == "input_audio":
            audio = value.get("input_audio")
            if not isinstance(audio, dict) or not isinstance(audio.get("data"), str) or not audio["data"]:
                return "input_audio requires nonempty encoded data"
            audio_format = audio.get("format", "wav")
            if not isinstance(audio_format, str) or audio_format not in AUDIO_FORMATS:
                return "Unsupported input_audio format"
            try:
                raw_audio = base64.b64decode(audio["data"], validate=True)
            except (ValueError, binascii.Error):
                return "input_audio requires valid base64 data"
            if audio_format == "wav":
                try:
                    with wave.open(io.BytesIO(raw_audio), "rb") as decoded:
                        if decoded.getnframes() == 0:
                            return "input_audio WAV contains zero frames"
                except (wave.Error, EOFError):
                    pass  # Do not reject legal encodings unsupported by stdlib wave.
        for item in value.values():
            error = audio_content_error(item)
            if error:
                return error
    elif isinstance(value, list):
        for item in value:
            error = audio_content_error(item)
            if error:
                return error
    return None


async def main_context_budget(body: dict, config: dict) -> dict:
    """Count the exact outgoing chat with the serving backend tokenizer."""
    url = config.get("agent_tokenizer_url") or config["model_base_url"].rstrip("/").removesuffix("/v1") + "/tokenize"
    parsed = urlsplit(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Invalid agent tokenizer endpoint")
    payload = {
        key: body[key] for key in ("model", "messages", "tools", "chat_template", "chat_template_kwargs") if key in body
    }
    payload["add_generation_prompt"] = True
    async with httpx.AsyncClient(
        timeout=float(config.get("agent_tokenizer_timeout_seconds", 30)), trust_env=False
    ) as client:
        response = await client.post(url, json=payload)
    if response.status_code != 200:
        raise ValueError(f"Serving tokenizer returned HTTP {response.status_code}")
    result = response.json()
    count = result.get("count") if isinstance(result, dict) else None
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise ValueError("Serving tokenizer returned no valid token count")
    limit = int(config["context_length"])
    backend_limit = result.get("max_model_len")
    if backend_limit is not None:
        if not isinstance(backend_limit, int) or isinstance(backend_limit, bool) or backend_limit <= 0:
            raise ValueError("Serving tokenizer returned an invalid context limit")
        limit = min(limit, backend_limit)
    output = int(body["max_tokens"])
    return {
        "prompt_tokens": count,
        "max_output_tokens": output,
        "max_prompt_tokens": limit - output,
        "context_limit": limit,
        "configured_context_limit": int(config["context_length"]),
        "backend_context_limit": backend_limit,
        "reserved_total_tokens": count + output,
        "remaining_tokens": limit - count - output,
        "tokenizer_url": url,
        "tokenizer_model": body["model"],
        "tokenizer_request_sha256": hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest(),
    }


def has_audio(value: object) -> bool:
    if isinstance(value, dict):
        kind = value.get("type")
        return (isinstance(kind, str) and kind in {"input_audio", "audio_url"}) or any(
            has_audio(v) for v in value.values()
        )
    if isinstance(value, list):
        return any(has_audio(item) for item in value)
    return False


async def upstream_until_disconnect(request: Request, operation):
    """Close non-streaming upstream work when the task client disappears."""
    pending = asyncio.create_task(operation)
    try:
        while not pending.done():
            if await request.is_disconnected():
                raise asyncio.CancelledError("Downstream client disconnected")
            await asyncio.wait({pending}, timeout=0.1)
        return await pending
    finally:
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


def create_app(config: dict) -> FastAPI:
    local_grading = local_judge_settings(config)
    local_budget = LocalRequestBudget(config)
    media_budget = MediaRequestBudget(config)
    body_limit = config.get("gateway_max_request_bytes", 64 * 1024 * 1024)
    if type(body_limit) is not int or body_limit <= 0:
        raise ValueError("gateway_max_request_bytes must be positive")
    for key in ("gateway_request_timeout_seconds", "media_request_timeout_seconds"):
        value = config.get(key, 1800)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"{key} must be finite and positive")
    interfaces = required_interfaces(config)
    available_roles = ["main"]
    if "audio" in interfaces:
        available_roles.append("audio")
    if "image-generation" in interfaces:
        available_roles.append("image")
    if local_budget.spec is not None and len(available_roles) > 1 and media_budget.spec is None:
        raise ValueError("Budgeted multimodal runs require a media_request_budget")
    app = FastAPI(title="Shared-Skill local experiment router")
    evidence = Path(config.get("gateway_log_dir", Path(config["runtime_dir"]) / "api-evidence"))
    evidence.mkdir(parents=True, exist_ok=True)
    write_json(evidence / "router-config.json", public_config(config))
    external_judge = config.get("external_judge") is True
    ledger = CostLedger(config) if external_judge else None
    judge_key = credential(config) if external_judge else None
    paid_slots = asyncio.Semaphore(2) if external_judge and config.get("alignment") else None

    @app.get("/healthz")
    async def health():
        async def probe(url):
            try:
                async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
                    response = await client.get(url.rstrip("/") + "/models")
                return {"ready": response.status_code == 200, "status": response.status_code}
            except Exception as error:
                return {"ready": False, "error": type(error).__name__}

        roles = dict(
            zip(
                available_roles,
                await asyncio.gather(
                    *[probe(config["model_base_url" if role == "main" else role + "_base_url"])
                      for role in available_roles]
                ),
            )
        )
        return {
            "ready": all(item["ready"] for item in roles.values()),
            "protocol_hash": config.get("protocol_hash"),
            "roles": roles,
            "required_interfaces": interfaces,
            "supported_endpoints": sorted(SUPPORTED_ENDPOINTS),
            "audio_input_formats": sorted(AUDIO_FORMATS),
            "external_judge": external_judge,
            "paid_api_budget_usd": config["paid_api_budget_usd"],
        }

    @app.get("/v1/models")
    @app.get("/runs/{run_id}/v1/models")
    async def models(run_id: str = "unassigned"):
        return {
            "object": "list",
            "data": [
                {"id": config["model_id"], "object": "model", "owned_by": "local-gpu1"},
                *[{"id": "local-" + role, "object": "model", "owned_by": "local-gpu1"}
                  for role in available_roles if role != "main"],
            ],
        }

    async def proxy(
        request: Request,
        endpoint: str,
        run_id: str = "unassigned",
        judge: bool = False,
        local_judge: bool = False,
        execution_id: str | None = None,
    ):
        agent = execution_id is not None
        if agent and any(
            not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", value) or value in {".", ".."}
            for value in (run_id, execution_id)
        ):
            return JSONResponse({"error": {"message": "Invalid bound execution ID"}}, status_code=400)
        request_id = uuid.uuid4().hex
        safe_id = "".join(c for c in run_id if c.isalnum() or c in "-_.")[:160] or "unassigned"
        if safe_id in {".", ".."}:
            safe_id = "run-" + hashlib.sha256(run_id.encode()).hexdigest()[:24]
        directory = evidence / safe_id / request_id
        directory.mkdir(parents=True)
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > body_limit:
                write_json(directory / "record.json", {"request_id": request_id, "run_id": safe_id,
                    "status": "rejected", "http_status": 413, "upstream_attempted": False,
                    "error": "Request exceeds frozen byte limit"})
                return JSONResponse({"error": "Request exceeds frozen byte limit"}, status_code=413)
            chunks.append(chunk)
        raw = b"".join(chunks)
        content_type = request.headers.get("content-type", "application/json")
        # Capture the incoming payload before parsing/routing, including rejected requests.
        (directory / ("incoming.json" if "application/json" in content_type else "incoming.bin")).write_bytes(raw)
        record = {
            "request_id": request_id,
            "run_id": safe_id,
            "endpoint": endpoint,
            "started_unix": time.time(),
            "status": "received",
            "purpose": "task_agent" if agent else "local_judge" if local_judge else "judge" if judge else "unbound_execution",
            "upstream_attempted": False,
        }
        if agent:
            record["execution_id"] = execution_id
        write_json(directory / "record.json", record)

        def reject(message: str, status: int = 400, code: str = "invalid_request"):
            record.update(status="error", error=message, http_status=status, finished_unix=time.time())
            write_json(directory / "record.json", record)
            return JSONResponse(
                {"error": {"message": message, "type": code, "code": code, "request_id": request_id}}, status_code=status
            )

        if endpoint not in SUPPORTED_ENDPOINTS:
            return reject("Unsupported local endpoint", 404, "unsupported_endpoint")
        if local_judge and local_grading is None:
            return reject("Separate local judge policy is not enabled", 404)
        if local_judge and endpoint != "chat/completions":
            return reject("Local judge only supports chat/completions")
        if agent and endpoint != "chat/completions":
            return reject("The bound agent route only supports chat/completions")
        body = None
        if "application/json" in content_type:
            try:
                body = json.loads(raw)
                if not isinstance(body, dict):
                    raise ValueError("JSON body must be an object")
            except ValueError:
                return reject("Invalid JSON object")
        if body is not None:
            record["requested_model"] = body.get("model")
            if judge and has_audio(body.get("messages")):
                record["purpose"] = "audio_judge"
            error = audio_content_error(body.get("messages"))
            if error:
                return reject(error, code="unsupported_media_format")
        audio = endpoint.startswith("audio/") or has_audio(body.get("messages") if body else None)
        image = endpoint.startswith("images/")
        if (audio and "audio" not in available_roles) or (image and "image" not in available_roles):
            return reject("This run does not enable the requested media interface", 503, "interface_not_enabled")
        if agent and (body is None or audio or image):
            return reject("The bound agent route requires main-model chat JSON")
        if endpoint == "audio/transcriptions":
            if "multipart/form-data" not in content_type:
                return reject("audio/transcriptions requires multipart/form-data", 415)
            try:
                form = transcription_fields(raw, content_type)
                if form.get("response_format", "json") not in {"json", "text"}:
                    return reject(
                        "Transcription supports json and text without fabricated timestamps",
                        code="unsupported_media_format",
                    )
                requested_model = form.get("model")
            except (ValueError, LookupError, TypeError):
                return reject("Invalid transcription multipart data")
        else:
            if body is None:
                return reject("This endpoint requires a JSON object", 415)
            requested_model = body.get("model")
            if endpoint == "audio/speech":
                speech_format = body.get("response_format", "mp3")
                if not isinstance(speech_format, str) or speech_format not in SPEECH_FORMATS:
                    return reject("Unsupported speech response_format", code="unsupported_media_format")
        role = "audio" if audio else "image" if image else "judge" if judge else "main"
        if role == "main" and local_budget.spec is not None:
            if endpoint != "chat/completions" or body is None:
                return reject("Budgeted main requests require single-sample chat/completions", 400, "local_budget_request_invalid")
            if any(type(body.get(key, 1)) is not int or body.get(key, 1) != 1 for key in ("n", "best_of")):
                return reject("Budgeted main requests require n=1 and best_of=1", 400, "local_budget_request_invalid")
        record["purpose"] = (
            "task_agent" if agent else "audio_judge" if (judge or local_judge) and audio
            else "local_judge" if local_judge else "judge" if judge else "unbound_execution"
        )
        if role == "judge" and not external_judge:
            return reject("External judge is disabled", 404)
        upstream = config[
            {"main": "model_base_url", "audio": "audio_base_url", "image": "image_base_url", "judge": "judge_base_url"}[
                role
            ]
        ]
        record.update(role=role, requested_model=requested_model, upstream=upstream, status="running")
        if agent:
            if requested_model != config["model_id"]:
                return reject("The bound agent must request the configured execution model")
            try:
                binding = load_binding(config, run_id, execution_id)
                if binding.get("purpose") != "agent":
                    raise CatalogError("Execution binding is not an agent binding")
                body["messages"] = inject_catalog(body.get("messages"), binding)
            except (CatalogError, TypeError) as error:
                return reject(str(error), 409, "execution_binding_error")
            record.update(
                protocol_hash=binding["protocol_hash"],
                bank_sha256=binding["bank_sha256"],
                catalog_sha256=binding["catalog_sha256"],
                catalog_policy_version=binding["policy_version"],
            )
        if body is not None:
            if role == "judge":
                try:
                    if endpoint != "chat/completions":
                        raise ValueError("External judge only supports chat/completions")
                    body = judge_payload(body, config)
                    ledger.reserve(request_id)
                except (ValueError, BudgetExceeded) as error:
                    return reject(str(error), 429 if isinstance(error, BudgetExceeded) else 400)
            # Explicit judge requests use DeepSeek; execution aliases stay local.
            body["model"] = {
                "main": config["model_id"],
                "audio": "local-audio",
                "image": "local-image",
                "judge": config.get("judge_model_id"),
            }[role]
            if image:
                # A backend loopback URL cannot be downloaded by nested task containers.
                body["response_format"] = "b64_json"
            if role == "main" and endpoint in {"chat/completions", "completions"}:
                for key in ("seed", "temperature", "top_p", "top_k", "min_p", "presence_penalty", "repetition_penalty"):
                    body[key] = config[key]
                # Route identity, never a request-body hint, selects the grading policy.
                body.pop("max_completion_tokens", None)
                interface_probe = run_id == "interface-checks" and not local_judge
                body["max_tokens"] = (local_grading["max_tokens"] if local_judge else
                                      int(config.get("interface_max_tokens", 1024)) if interface_probe else config["max_tokens"])
                body["reasoning_effort"] = config["reasoning_effort"]
                body["chat_template_kwargs"] = {"enable_thinking": (local_grading["enable_thinking"] if local_judge else
                                                                     False if interface_probe else config.get("enable_thinking", True)),
                                                "reasoning_effort": config["reasoning_effort"]}
                if local_judge:
                    for key in ("chat_template", "enable_thinking", "thinking", "reasoning"):
                        body.pop(key, None)
                    record["local_judge_settings"] = local_grading
                if body.get("stream"):
                    body["stream_options"] = {**(body.get("stream_options") or {}), "include_usage": True}
                # Provider-specific accounting hints are irrelevant to the local model.
                for key in ("provider", "transforms", "usage", "route"):
                    body.pop(key, None)
            write_json(directory / "request.json", body)
            raw = json.dumps(body, ensure_ascii=False).encode()
        else:
            (directory / "request.bin").write_bytes(raw)
        record["actual_model"] = {
            "main": config["model_id"],
            "audio": "local-audio",
            "image": "local-image",
            "judge": config.get("judge_model_id"),
        }[role]
        if agent or ((config.get("alignment") or local_budget.spec is not None)
                     and role == "main" and endpoint == "chat/completions"):
            if config.get("protocol_hash"):
                record["protocol_hash"] = config["protocol_hash"]
            try:
                record["context_budget"] = await main_context_budget(body, config)
            except (ValueError, TypeError, httpx.HTTPError) as error:
                return reject(
                    f"Main-model context tokenization failed: {type(error).__name__}",
                    503,
                    "context_tokenization_failed",
                )
            if record["context_budget"]["remaining_tokens"] < 0:
                budget = record["context_budget"]
                return reject(
                    "Context length exceeded: This model's maximum context length is "
                    f"{budget['context_limit']} tokens, but this request reserves "
                    f"{budget['prompt_tokens']} prompt tokens plus "
                    f"{budget['max_output_tokens']} max_tokens output tokens "
                    f"({budget['reserved_total_tokens']} total). "
                    "Reduce request size before retrying; the gateway preserved all history.",
                    code="context_length_exceeded",
                )
        write_json(directory / "record.json", record)
        if role == "main":
            try:
                reservation = local_budget.reserve()
                if reservation is not None:
                    record["local_budget_reservation"] = reservation
            except BudgetExceeded as error:
                return reject(str(error), 429, "local_budget_exhausted")
            except ValueError as error:
                return reject(str(error), 409, "local_budget_invalid")
        elif role in {"audio", "image"}:
            try:
                reservation = media_budget.reserve(role)
                if reservation is not None:
                    record["media_budget_reservation"] = reservation
            except BudgetExceeded as error:
                return reject(str(error), 429, "media_budget_exhausted")
            except ValueError as error:
                return reject(str(error), 409, "media_budget_invalid")
        timeout = float(config.get("media_request_timeout_seconds", config.get("gateway_request_timeout_seconds", 1800))
                        if role in {"audio", "image"} else config.get("gateway_request_timeout_seconds", 1800))
        client = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=15),
                                   trust_env=False)
        try:
            outgoing = client.build_request(
                "POST",
                upstream.rstrip("/") + "/" + endpoint,
                content=raw,
                headers={
                    "Content-Type": content_type,
                    "Authorization": "Bearer " + (judge_key if role == "judge" else "local-experiment"),
                    "X-Request-Deadline-Unix": str(time.time() + timeout),
                    "X-Request-ID": request_id,
                    "X-Run-ID": safe_id,
                },
            )
            record["upstream_attempted"] = True
            write_json(directory / "record.json", record)
            response = await upstream_until_disconnect(request,
                client.send(outgoing, stream=bool(body and body.get("stream"))))
            record["http_status"] = response.status_code
            record["status"] = (
                "streaming"
                if body and body.get("stream") and response.status_code < 400
                else ("completed" if response.status_code < 400 else "error")
            )
            write_json(directory / "record.json", record)
            if body and body.get("stream") and response.status_code < 400:

                async def relay():
                    try:
                        with (directory / "response.sse").open("wb") as stream:
                            async for chunk in response.aiter_bytes():
                                stream.write(chunk)
                                yield chunk
                        record["status"] = "completed"
                    except asyncio.CancelledError:
                        record.update(status="error", error="Streaming client disconnected before completion")
                        raise
                    except Exception as error:
                        record.update(status="error", error=f"{type(error).__name__}: {error}")
                        raise
                    finally:
                        record["finished_unix"] = time.time()
                        write_json(directory / "record.json", record)
                        await response.aclose()
                        await client.aclose()

                return StreamingResponse(relay(), media_type="text/event-stream")
            data = await response.aread()
            (directory / "response.bin").write_bytes(data)
            try:
                record["response_model"] = json.loads(data).get("model")
            except (ValueError, AttributeError):
                pass
            if role == "judge":
                try:
                    usage = json.loads(data).get("usage")
                except (ValueError, AttributeError):
                    usage = None
                record["paid_accounting"] = ledger.settle(request_id, usage)
            record["finished_unix"] = time.time()
            write_json(directory / "record.json", record)
            await response.aclose()
            await client.aclose()
            return Response(data, status_code=response.status_code, media_type=response.headers.get("content-type"))
        except asyncio.CancelledError:
            if role == "judge" and "paid_accounting" not in record:
                record["paid_accounting"] = ledger.settle(request_id, None)
            record.update(
                status="error",
                error="Judge client cancelled" if role == "judge" else "Client cancelled",
                finished_unix=time.time(),
            )
            write_json(directory / "record.json", record)
            await client.aclose()
            raise
        except Exception as error:
            if role == "judge" and "paid_accounting" not in record:
                record["paid_accounting"] = ledger.settle(request_id, None)
            await client.aclose()
            # Error messages must never include the host credential.
            message = f"{type(error).__name__}: {error}"
            if judge_key:
                message = message.replace(judge_key, "[redacted]")
            record.update(status="error", error=message, finished_unix=time.time())
            write_json(directory / "record.json", record)
            return JSONResponse(
                {"error": {"message": record["error"], "type": "local_backend_error", "request_id": request_id}},
                status_code=502,
            )

    @app.post("/v1/{endpoint:path}")
    @app.post("/runs/{run_id}/v1/{endpoint:path}")
    async def main_proxy(request: Request, endpoint: str, run_id: str = "unassigned"):
        return await proxy(request, endpoint, run_id)

    @app.post("/local-judge/v1/{endpoint:path}")
    @app.post("/runs/{run_id}/local-judge/v1/{endpoint:path}")
    async def local_judge_proxy(request: Request, endpoint: str, run_id: str = "unassigned"):
        return await proxy(request, endpoint, run_id, local_judge=True)

    @app.post("/judge/v1/{endpoint:path}")
    @app.post("/runs/{run_id}/judge/v1/{endpoint:path}")
    async def judge_proxy(request: Request, endpoint: str, run_id: str = "unassigned"):
        paid_chat = False
        if paid_slots is not None and endpoint == "chat/completions":
            try:
                incoming = json.loads(await request.body())
                paid_chat = isinstance(incoming, dict) and not has_audio(incoming.get("messages"))
            except ValueError:
                pass
        if paid_chat:
            # Hold the slot through reserve, upstream completion, and settlement.
            # Queuing avoids confusing simultaneous worst-case reservations with
            # actual cap exhaustion; the durable ledger remains authoritative.
            async with paid_slots:
                return await proxy(request, endpoint, run_id, judge=True)
        return await proxy(request, endpoint, run_id, judge=True)

    @app.post("/runs/{run_id}/executions/{execution_id}/agent/v1/{endpoint:path}")
    async def agent_proxy(request: Request, endpoint: str, run_id: str, execution_id: str):
        return await proxy(request, endpoint, run_id, execution_id=execution_id)

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--port", type=int)
    arguments = parser.parse_args()
    config = load_config(arguments.config)
    uvicorn.run(create_app(config), host="0.0.0.0", port=arguments.port or urlsplit(config["local_api_base_url"]).port or 80, access_log=False)
