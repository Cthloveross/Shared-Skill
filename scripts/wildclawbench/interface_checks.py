"""Actual local modality checks, saved independently of task training evidence."""

from __future__ import annotations

import base64
import io
import json
import time
import wave
from pathlib import Path

import httpx
from PIL import Image
from scripts.wildclawbench.protocol import write_json


def required_interfaces(config: dict) -> list[str]:
    default = ["text", "tools", "vision", "speech", "audio", "image-generation"]
    value = config.get("required_interfaces", default)
    if (not isinstance(value, list) or not all(isinstance(item, str) for item in value)
            or len(set(value)) != len(value) or not {"text", "tools"}.issubset(value)
            or not set(value).issubset(default)):
        raise ValueError("required_interfaces must contain text/tools and only supported unique interface names")
    if ("audio" in value) != ("speech" in value):
        raise ValueError("audio and speech must be checked together using a real generated waveform")
    return value


def verify_interfaces(config: dict, output: Path) -> dict:
    required = required_interfaces(config)
    image_count = config.get("interface_vision_image_count", 1)
    if type(image_count) is not int or not 1 <= image_count <= 32:
        raise ValueError("interface_vision_image_count must be an integer between 1 and 32")
    output.mkdir(parents=True, exist_ok=True)
    record_file = output / "verification.json"
    previous = json.loads(record_file.read_text()) if record_file.exists() else {}
    if (previous.get("passed") and previous.get("protocol_hash") == config["protocol_hash"]
            and set(previous.get("checks", {})) == set(required) | {"tools-roundtrip"}):
        return previous
    base = config["local_api_base_url"].removesuffix("/v1") + "/runs/interface-checks/v1"
    client = httpx.Client(timeout=float(config.get("interface_request_timeout_seconds", 120)), trust_env=False,
                         headers={"Authorization": "Bearer " + config["local_api_key"]})
    record = {"passed": False, "protocol_hash": config["protocol_hash"], "started_unix": time.time(),
              "required_interfaces": required, "checks": {}}

    def request(name, endpoint, body):
        start = time.monotonic()
        response = client.post(base + "/" + endpoint, json=body)
        (output / (name + ".response.bin")).write_bytes(response.content)
        write_json(output / (name + ".request.json"), body)
        record["checks"][name] = {"http_status": response.status_code, "seconds": time.monotonic() - start}
        response.raise_for_status()
        return response

    def chat(name, messages, **kwargs):
        body = {"model": config["model_id"], "max_tokens": int(config.get("interface_max_tokens", 1024)),
                "chat_template_kwargs": {"enable_thinking": False}, "messages": messages, **kwargs}
        return request(name, "chat/completions", body).json()["choices"][0]["message"]

    try:
        text = chat("text", [{"role": "user", "content": "Reply with exactly LOCAL_READY."}])
        if text.get("content", "").strip() != "LOCAL_READY":
            raise RuntimeError("Text modality check returned unexpected content")
        tools = chat(
            "tools",
            [{"role": "user", "content": "Call local_probe with text LOCAL_READY."}],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "local_probe",
                        "description": "Test a tool call",
                        "parameters": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"],
                        },
                    },
                }
            ],
            tool_choice="auto",
        )
        call = tools["tool_calls"][0]["function"]
        if call["name"] != "local_probe" or json.loads(call["arguments"])["text"] != "LOCAL_READY":
            raise RuntimeError("Tool-call schema/arguments check failed")
        reply = chat("tools-roundtrip", [
            {"role": "user", "content": "Call local_probe with text LOCAL_READY; after its result reply exactly TOOL_ROUNDTRIP_OK."},
            {"role": "assistant", "content": tools.get("content"), "tool_calls": tools["tool_calls"]},
            {"role": "tool", "tool_call_id": tools["tool_calls"][0]["id"], "content": "TOOL_ROUNDTRIP_OK"},
        ])
        if reply.get("content", "").strip() != "TOOL_ROUNDTRIP_OK":
            raise RuntimeError("Tool result roundtrip did not complete")
        if 'vision' in required:
            buffer = io.BytesIO()
            Image.new("RGB", (64, 64), (255, 0, 0)).save(buffer, format="PNG")
            vision = chat(
                "vision",
                [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Name the dominant color shared by these images in one English word."},
                            *[{
                                "type": "image_url",
                                "image_url": {
                                    "url": "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()
                                },
                            } for _ in range(image_count)],
                        ],
                    }
                ],
            )
            if "red" not in vision.get("content", "").lower():
                raise RuntimeError("Vision modality check failed")
            record["checks"]["vision"]["image_count"] = image_count
        if 'audio' in required:
            speech = request(
                "speech",
                "audio/speech",
                {
                    "model": "local-audio",
                    "input": "Shared skills improve with experience.",
                    "voice": "Chelsie",
                    "response_format": "wav",
                },
            ).content
            with wave.open(io.BytesIO(speech)) as waveform:
                duration = waveform.getnframes() / waveform.getframerate()
            if duration < 0.2:
                raise RuntimeError("Speech API did not generate a valid audible waveform")
            audio = chat(
                "audio",
                [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "Transcribe the speech exactly in English. Return only the transcript.",
                            },
                            {
                                "type": "input_audio",
                                "input_audio": {"data": base64.b64encode(speech).decode(), "format": "wav"},
                            },
                        ],
                    }
                ],
            )
            if "skill" not in audio.get("content", "").lower() or "experience" not in audio.get("content", "").lower():
                raise RuntimeError("Audio understanding check did not transcribe the generated speech")
        if 'image-generation' in required:
            image = request(
                "image-generation",
                "images/generations",
                {
                    "model": "local-image",
                    "prompt": "A black cat on a white background",
                    "size": "256x256",
                    "n": 1,
                    "response_format": "b64_json",
                    "seed": 42,
                },
            ).json()
            picture = base64.b64decode(image["data"][0]["b64_json"])
            (output / "image-generation.png").write_bytes(picture)
            with Image.open(io.BytesIO(picture)) as generated:
                if generated.size != (256, 256):
                    raise RuntimeError("Image generation returned incorrect dimensions")
        record.update(passed=True, finished_unix=time.time())
        write_json(record_file, record)
        return record
    except Exception as error:
        record.update(error=f"{type(error).__name__}: {error}", finished_unix=time.time())
        write_json(record_file, record)
        raise
    finally:
        client.close()
