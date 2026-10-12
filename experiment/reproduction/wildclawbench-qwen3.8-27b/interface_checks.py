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
from protocol import write_json


def verify_interfaces(config: dict, output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    record_file = output / "verification.json"
    previous = json.loads(record_file.read_text()) if record_file.exists() else {}
    if previous.get("passed") and previous.get("protocol_hash") == config["protocol_hash"]:
        return previous
    base = config["local_api_base_url"].removesuffix("/v1") + "/runs/interface-checks/v1"
    client = httpx.Client(timeout=900, trust_env=False, headers={"Authorization": "Bearer " + config["local_api_key"]})
    record = {"passed": False, "protocol_hash": config["protocol_hash"], "started_unix": time.time(), "checks": {}}

    def request(name, endpoint, body):
        start = time.monotonic()
        response = client.post(base + "/" + endpoint, json=body)
        (output / (name + ".response.bin")).write_bytes(response.content)
        write_json(output / (name + ".request.json"), body)
        record["checks"][name] = {"http_status": response.status_code, "seconds": time.monotonic() - start}
        response.raise_for_status()
        return response

    def chat(name, messages, **kwargs):
        body = {"model": config["model_id"], "max_tokens": 1024, "messages": messages, **kwargs}
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
            tool_choice={"type": "function", "function": {"name": "local_probe"}},
        )
        call = tools["tool_calls"][0]["function"]
        if call["name"] != "local_probe" or json.loads(call["arguments"])["text"] != "LOCAL_READY":
            raise RuntimeError("Tool-call schema/arguments check failed")
        buffer = io.BytesIO()
        Image.new("RGB", (64, 64), (255, 0, 0)).save(buffer, format="PNG")
        vision = chat(
            "vision",
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Name the dominant color in this image in one English word."},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()
                            },
                        },
                    ],
                }
            ],
        )
        if "red" not in vision.get("content", "").lower():
            raise RuntimeError("Vision modality check failed")
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
