#!/usr/bin/env python3
"""Download the fixed, ungated auxiliary models into the experiment runtime."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from huggingface_hub import snapshot_download

from scripts.wildclawbench.services.common import runtime_directory, service_config

RUNTIME = runtime_directory()
MODELS = {
    "audio": {
        "repository": "Qwen/Qwen2.5-Omni-7B",
        "revision": "ae9e1690543ffd5c0221dc27f79834d0294cba00",
        "directory": str(RUNTIME / "models" / "qwen2.5-omni-7b"),
    },
    "image": {
        "repository": "stabilityai/stable-diffusion-xl-base-1.0",
        "revision": "462165984030d82259a11f4367a4eed129e94a7b",
        "directory": str(RUNTIME / "models" / "sdxl-base-1.0"),
        "allow_patterns": [
            "model_index.json",
            "scheduler/*",
            "tokenizer/*",
            "tokenizer_2/*",
            "text_encoder/config.json",
            "text_encoder/model.fp16.safetensors",
            "text_encoder_2/config.json",
            "text_encoder_2/model.fp16.safetensors",
            "unet/config.json",
            "unet/diffusion_pytorch_model.fp16.safetensors",
            "vae/config.json",
            "vae/diffusion_pytorch_model.fp16.safetensors",
        ],
    },
}


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    args, config = service_config(parser)
    global RUNTIME
    RUNTIME = Path(config["runtime_dir"])
    for name, relative in (("audio", "qwen2.5-omni-7b"), ("image", "sdxl-base-1.0")):
        MODELS[name]["directory"] = str(RUNTIME / "models" / relative)
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "models": {}}
    destination = RUNTIME / "auxiliary-downloads.json"
    RUNTIME.mkdir(parents=True, exist_ok=True)
    for name, model in MODELS.items():
        started = time.monotonic()
        report["models"][name] = {**model, "status": "downloading"}
        destination.write_text(json.dumps(report, indent=2) + "\n")
        snapshot_download(
            model["repository"],
            revision=model["revision"],
            local_dir=model["directory"],
            cache_dir=str(RUNTIME / "model-cache"),
            allow_patterns=model.get("allow_patterns"),
            max_workers=4,
        )
        report["models"][name].update(status="downloaded", download_seconds=time.monotonic() - started)
        destination.write_text(json.dumps(report, indent=2) + "\n")
    report["completed_at"] = datetime.now(timezone.utc).isoformat()
    destination.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
