#!/usr/bin/env python3
"""Verify real local speech, audio input, and SDXL generation without substitutes."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import soundfile as sf
from PIL import Image, ImageStat

RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
OUTPUT = RUNTIME / "auxiliary-verification"


def main():
    OUTPUT.mkdir(exist_ok=True)
    client = httpx.Client(timeout=1200, trust_env=False)
    report = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "scope": "Actual API inference and valid media artifacts; task quality is not graded here.",
        "limitations": [
            "Audio input passes if the response contains audio and ready; exact transcription is not asserted.",
            "TTS uses Omni Chelsie or Ethan voices, not a Microsoft or OpenAI voice.",
            "Image checks validate dimensions and nonconstant pixels, not adherence to the prompt.",
        ],
        "checks": {},
    }
    for kind, port in (("audio", 18183), ("image", 18184)):
        deadline = time.monotonic() + 1200
        while time.monotonic() < deadline:
            try:
                health = client.get(f"http://127.0.0.1:{port}/health", timeout=2)
                if health.status_code == 200:
                    report[kind] = health.json()
                    break
            except httpx.HTTPError:
                pass
            time.sleep(2)
        else:
            raise RuntimeError(f"{kind} service did not become healthy")

    def post(name, port, endpoint, body):
        (OUTPUT / f"{name}-request.json").write_text(json.dumps(body, indent=2) + "\n")
        started = time.monotonic()
        response = client.post(f"http://127.0.0.1:{port}/v1/{endpoint}", json=body)
        response.raise_for_status()
        report["checks"][name] = {"seconds": time.monotonic() - started, "http_status": response.status_code}
        return response

    speech = post(
        "speech",
        18183,
        "audio/speech",
        {
            "model": "local-tts",
            "input": "Hello, the local audio service is ready.",
            "voice": "Chelsie",
            "response_format": "wav",
            "seed": 42,
        },
    )
    (OUTPUT / "speech.wav").write_bytes(speech.content)
    waveform, rate = sf.read(io.BytesIO(speech.content))
    assert rate == 24000 and waveform.size > rate and abs(waveform).max() > 0.001
    report["checks"]["speech"].update(
        passed=True,
        sample_rate=rate,
        duration_seconds=len(waveform) / rate,
        sha256=hashlib.sha256(speech.content).hexdigest(),
    )
    audio = post(
        "audio_input",
        18183,
        "chat/completions",
        {
            "model": "local-audio",
            "max_tokens": 256,
            "seed": 42,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Transcribe the speech in this audio exactly. Return only the transcript.",
                        },
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "format": "wav",
                                "data": base64.b64encode(speech.content).decode(),
                            },
                        },
                    ],
                }
            ],
        },
    ).json()
    (OUTPUT / "audio-response.json").write_text(json.dumps(audio, indent=2) + "\n")
    transcript = audio["choices"][0]["message"]["content"]
    assert "ready" in transcript.lower() and "audio" in transcript.lower()
    report["checks"]["audio_input"].update(passed=True, transcript=transcript, usage=audio.get("usage"))
    images = post(
        "image",
        18184,
        "images/generations",
        {
            "model": "local-image",
            "prompt": "A red ceramic teapot on a white table, studio photograph",
            "size": "1024x1024",
            "n": 1,
            "response_format": "b64_json",
            "seed": 42,
            "steps": 30,
            "guidance_scale": 7.5,
        },
    ).json()
    data = base64.b64decode(images["data"][0]["b64_json"], validate=True)
    (OUTPUT / "image.png").write_bytes(data)
    image = Image.open(io.BytesIO(data)).convert("RGB")
    assert image.size == (1024, 1024) and max(ImageStat.Stat(image).var) > 1
    report["checks"]["image"].update(
        passed=True,
        dimensions=list(image.size),
        sha256=hashlib.sha256(data).hexdigest(),
    )
    report.update(
        passed=True,
        completed_at=datetime.now(timezone.utc).isoformat(),
        gpu_snapshot=subprocess.check_output(
            ["nvidia-smi", "--query-gpu=index,uuid,memory.used,memory.free,utilization.gpu", "--format=csv"],
            text=True,
        ),
    )
    (OUTPUT / "verification.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report["checks"], indent=2))


if __name__ == "__main__":
    main()
