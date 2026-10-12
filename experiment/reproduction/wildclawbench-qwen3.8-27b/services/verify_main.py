#!/usr/bin/env python3
"""Exercise real text, auto tool parsing, and image input on the local model."""

from __future__ import annotations

import base64
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from PIL import Image

RUNTIME = Path(__file__).resolve().parents[1] / "runtime" / "main-service"
BASE = "http://127.0.0.1:18181"


def main() -> None:
    began = time.monotonic()
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "checks": {}}
    client = httpx.Client(timeout=600, trust_env=False)
    deadline = time.monotonic() + 1200
    while time.monotonic() < deadline:
        try:
            if client.get(BASE + "/health", timeout=2).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(2)
    else:
        raise RuntimeError("main model was not ready within 1200 seconds")
    report["ready_wait_seconds"] = time.monotonic() - began
    models = client.get(BASE + "/v1/models")
    models.raise_for_status()
    report["models"] = models.json()
    image = Image.new("RGB", (128, 128), (255, 0, 0))
    image.save(RUNTIME / "vision-probe.png")
    encoded = base64.b64encode((RUNTIME / "vision-probe.png").read_bytes()).decode()
    probes = {
        "text": {"messages": [{"role": "user", "content": "Reply with exactly SERVICE_READY."}]},
        "tools": {
            "messages": [{"role": "user", "content": "Call echo with value ready. Do not answer in plain text."}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "echo",
                        "description": "Echo the provided value.",
                        "parameters": {
                            "type": "object",
                            "properties": {"value": {"type": "string"}},
                            "required": ["value"],
                        },
                    },
                }
            ],
            "tool_choice": "auto",
        },
        "vision": {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Name the dominant color in this image in one word."},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + encoded}},
                    ],
                }
            ]
        },
    }
    for name, probe in probes.items():
        body = {
            "model": "qwen3.8-27b",
            "max_tokens": 1024,
            "temperature": 1.0,
            "top_p": 0.95,
            "top_k": 20,
            "min_p": 0.0,
            "presence_penalty": 0.0,
            "repetition_penalty": 1.0,
            "seed": 42,
            "chat_template_kwargs": {"enable_thinking": True, "reasoning_effort": "medium"},
            **probe,
        }
        started = time.monotonic()
        response = client.post(BASE + "/v1/chat/completions", json=body)
        response.raise_for_status()
        completion = response.json()
        message = completion["choices"][0]["message"]
        passed = (
            "SERVICE_READY" in (message.get("content") or "")
            if name == "text"
            else any(
                call["function"]["name"] == "echo" and json.loads(call["function"]["arguments"]).get("value") == "ready"
                for call in message.get("tool_calls") or []
            )
            if name == "tools"
            else "red" in (message.get("content") or "").lower()
        )
        record = {"request": body, "response": completion, "seconds": time.monotonic() - started, "passed": passed}
        (RUNTIME / f"probe-{name}.json").write_text(json.dumps(record, indent=2) + "\n")
        report["checks"][name] = {"passed": passed, "seconds": record["seconds"], "usage": completion.get("usage")}
        if not passed:
            raise RuntimeError(f"real {name} API probe failed; see probe-{name}.json")
    report.update(
        passed=True,
        completed_at=datetime.now(timezone.utc).isoformat(),
        total_seconds=time.monotonic() - began,
        gpu_snapshot=subprocess.check_output(
            ["nvidia-smi", "--query-gpu=index,uuid,memory.used,memory.free,utilization.gpu", "--format=csv"], text=True
        ),
    )
    (RUNTIME / "verification.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report["checks"], indent=2))


if __name__ == "__main__":
    main()
