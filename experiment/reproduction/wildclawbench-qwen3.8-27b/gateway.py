"""Only-local OpenAI router with complete request/response evidence and error ledger."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
import uuid
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from protocol import ROOT, load_config, public_config, write_json


def has_audio(value: object) -> bool:
    if isinstance(value, dict):
        kind = value.get("type")
        return (isinstance(kind, str) and kind in {"input_audio", "audio_url"}) or any(
            has_audio(v) for v in value.values()
        )
    if isinstance(value, list):
        return any(has_audio(item) for item in value)
    return False


def create_app(config: dict) -> FastAPI:
    app = FastAPI(title="Shared-Skill local experiment router")
    evidence = Path(config["runtime_dir"]) / "api-evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    write_json(evidence / "router-config.json", public_config(config))

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
                ("main", "audio", "image"),
                await asyncio.gather(
                    *[probe(config[key]) for key in ("model_base_url", "audio_base_url", "image_base_url")]
                ),
            )
        )
        return {"ready": all(item["ready"] for item in roles.values()), "roles": roles, "paid_api_budget_usd": 0}

    @app.get("/v1/models")
    @app.get("/runs/{run_id}/v1/models")
    async def models(run_id: str = "unassigned"):
        return {
            "object": "list",
            "data": [
                {"id": config["model_id"], "object": "model", "owned_by": "local-gpu1"},
                {"id": "local-audio", "object": "model", "owned_by": "local-gpu1"},
                {"id": "local-image", "object": "model", "owned_by": "local-gpu1"},
            ],
        }

    @app.api_route("/v1/{endpoint:path}", methods=["POST"])
    @app.api_route("/runs/{run_id}/v1/{endpoint:path}", methods=["POST"])
    async def proxy(request: Request, endpoint: str, run_id: str = "unassigned"):
        if endpoint not in {
            "chat/completions",
            "completions",
            "audio/speech",
            "audio/transcriptions",
            "audio/translations",
            "images/generations",
            "images/edits",
        }:
            return JSONResponse({"error": {"message": "Unsupported local endpoint"}}, status_code=404)
        request_id = uuid.uuid4().hex
        safe_id = "".join(c for c in run_id if c.isalnum() or c in "-_.")[:160] or "unassigned"
        if safe_id in {".", ".."}:
            safe_id = "run-" + hashlib.sha256(run_id.encode()).hexdigest()[:24]
        directory = evidence / safe_id / request_id
        directory.mkdir(parents=True)
        raw = await request.body()
        content_type = request.headers.get("content-type", "application/json")
        # Capture the incoming payload before parsing/routing, including rejected requests.
        (directory / ("incoming.json" if "application/json" in content_type else "incoming.bin")).write_bytes(raw)
        record = {
            "request_id": request_id,
            "run_id": safe_id,
            "endpoint": endpoint,
            "started_unix": time.time(),
            "status": "received",
        }
        write_json(directory / "record.json", record)
        body = None
        if "application/json" in content_type:
            try:
                body = json.loads(raw)
                if not isinstance(body, dict):
                    raise ValueError("JSON body must be an object")
            except ValueError:
                record.update(status="error", error="Invalid JSON object", http_status=400, finished_unix=time.time())
                write_json(directory / "record.json", record)
                return JSONResponse({"error": {"message": "Invalid JSON"}}, status_code=400)
        audio = endpoint.startswith("audio/") or has_audio(body.get("messages") if body else None)
        image = endpoint.startswith("images/")
        role = "audio" if audio else "image" if image else "main"
        upstream = config[{"main": "model_base_url", "audio": "audio_base_url", "image": "image_base_url"}[role]]
        record.update(
            role=role, requested_model=body.get("model") if body else None, upstream=upstream, status="running"
        )
        if body is not None:
            # All model aliases in original grader code resolve to an explicitly local capability.
            body["model"] = {"main": config["model_id"], "audio": "local-audio", "image": "local-image"}[role]
            if image:
                # A backend loopback URL cannot be downloaded by nested task containers.
                body["response_format"] = "b64_json"
            if role == "main" and endpoint in {"chat/completions", "completions"}:
                for key in ("seed", "temperature", "top_p", "top_k", "min_p", "presence_penalty", "repetition_penalty"):
                    body[key] = config[key]
                # Legacy GPT judge limits (often 512) leave no answer budget with Qwen thinking.
                # Freeze one output budget for every main-model role and every experimental arm.
                body.pop("max_completion_tokens", None)
                body["max_tokens"] = config["max_tokens"]
                body["reasoning_effort"] = config["reasoning_effort"]
                body["chat_template_kwargs"] = {"enable_thinking": True, "reasoning_effort": config["reasoning_effort"]}
                if body.get("stream"):
                    body["stream_options"] = {**(body.get("stream_options") or {}), "include_usage": True}
                # Provider-specific accounting hints are irrelevant to the local model.
                for key in ("provider", "transforms", "usage", "route"):
                    body.pop(key, None)
            write_json(directory / "request.json", body)
            raw = json.dumps(body, ensure_ascii=False).encode()
        else:
            (directory / "request.bin").write_bytes(raw)
        write_json(directory / "record.json", record)
        client = httpx.AsyncClient(timeout=httpx.Timeout(1800, connect=15), trust_env=False)
        try:
            outgoing = client.build_request(
                "POST",
                upstream.rstrip("/") + "/" + endpoint,
                content=raw,
                headers={"Content-Type": content_type, "Authorization": "Bearer local-experiment"},
            )
            response = await client.send(outgoing, stream=bool(body and body.get("stream")))
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
            record["finished_unix"] = time.time()
            write_json(directory / "record.json", record)
            await response.aclose()
            await client.aclose()
            return Response(data, status_code=response.status_code, media_type=response.headers.get("content-type"))
        except Exception as error:
            await client.aclose()
            record.update(status="error", error=f"{type(error).__name__}: {error}", finished_unix=time.time())
            write_json(directory / "record.json", record)
            return JSONResponse(
                {"error": {"message": record["error"], "type": "local_backend_error", "request_id": request_id}},
                status_code=502,
            )

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--port", type=int, default=18182)
    arguments = parser.parse_args()
    uvicorn.run(create_app(load_config(arguments.config)), host="0.0.0.0", port=arguments.port, access_log=False)
