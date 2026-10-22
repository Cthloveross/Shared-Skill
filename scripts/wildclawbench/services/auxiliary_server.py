#!/usr/bin/env python3
"""Local Omni audio and SDXL image APIs; every result uses actual model inference."""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import logging
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import torch
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, Response

from scripts.wildclawbench.services.common import runtime_directory, service_config

RUNTIME = runtime_directory()
OMNI_SYSTEM = (
    "You are Qwen, a virtual human developed by the Qwen Team, Alibaba Group, "
    "capable of perceiving auditory and visual inputs, as well as generating text and speech."
)


@contextmanager
def gpu_job(local_lock):
    """Serialize GPU work across both auxiliary server processes."""
    with local_lock, (RUNTIME / "auxiliary-gpu.lock").open("a") as shared_lock:
        fcntl.flock(shared_lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            try:
                torch.cuda.empty_cache()
            finally:
                fcntl.flock(shared_lock, fcntl.LOCK_UN)


class AudioModel:
    def __init__(self):
        from transformers import Qwen2_5OmniForConditionalGeneration, Qwen2_5OmniProcessor

        directory = RUNTIME / "models" / "qwen2.5-omni-7b"
        self.model = Qwen2_5OmniForConditionalGeneration.from_pretrained(
            directory,
            dtype=torch.bfloat16,
            device_map="cuda:0",
            attn_implementation="sdpa",
            local_files_only=True,
        ).eval()
        self.processor = Qwen2_5OmniProcessor.from_pretrained(directory, local_files_only=True)

    @torch.inference_mode()
    def infer(self, conversation, *, max_tokens: int, speech: bool = False, speaker: str = "Chelsie", seed: int = 42):
        from qwen_omni_utils import process_mm_info

        text = self.processor.apply_chat_template(conversation, add_generation_prompt=True, tokenize=False)
        audios, images, videos = process_mm_info(conversation, use_audio_in_video=False)
        inputs = (
            self.processor(
                text=text,
                audio=audios,
                images=images,
                videos=videos,
                return_tensors="pt",
                padding=True,
                use_audio_in_video=False,
            )
            .to(self.model.device)
            .to(self.model.dtype)
        )
        torch.manual_seed(seed)
        generated = self.model.generate(
            **inputs,
            generation_mode="audio" if speech else "text",
            speaker=speaker,
            use_audio_in_video=False,
            thinker_max_new_tokens=max_tokens,
            thinker_do_sample=False,
            talker_max_new_tokens=4096,
            talker_do_sample=True,
            talker_top_k=40,
            talker_top_p=0.8,
            talker_temperature=0.9,
            talker_repetition_penalty=1.05,
        )
        token_ids, audio = generated if speech else (generated, None)
        new_ids = token_ids[:, inputs["input_ids"].shape[1] :]
        answer = self.processor.batch_decode(new_ids, skip_special_tokens=True, clean_up_tokenization_spaces=False)[0]
        if audio is not None:
            audio = audio.detach().float().cpu()
        return answer, audio, int(inputs["input_ids"].numel()), int(new_ids.numel())


class ImageModel:
    def __init__(self):
        from diffusers import StableDiffusionXLPipeline

        self.pipeline = StableDiffusionXLPipeline.from_pretrained(
            RUNTIME / "models" / "sdxl-base-1.0",
            torch_dtype=torch.float16,
            variant="fp16",
            use_safetensors=True,
            local_files_only=True,
        ).to("cuda:0")

    @torch.inference_mode()
    def infer(self, body):
        width, height = map(int, body.get("size", "1024x1024").split("x"))
        if min(width, height) < 256 or max(width, height) > 1536 or width % 8 or height % 8:
            raise ValueError("image dimensions must be multiples of 8 between 256 and 1536")
        count = int(body.get("n", 1))
        if not 1 <= count <= 4:
            raise ValueError("n must be between 1 and 4")
        images = []
        for index in range(count):
            images.extend(
                self.pipeline(
                    prompt=body["prompt"],
                    negative_prompt=body.get("negative_prompt"),
                    width=width,
                    height=height,
                    num_images_per_prompt=1,
                    num_inference_steps=int(body.get("steps", 30)),
                    guidance_scale=float(body.get("guidance_scale", 7.5)),
                    generator=torch.Generator(device="cuda:0").manual_seed(int(body.get("seed", 42)) + index),
                ).images
            )
        return images


def create_app(kind: str) -> FastAPI:
    app = FastAPI(title=f"WildClawBench local {kind} inference")
    lock = threading.Lock()
    directory = RUNTIME / f"auxiliary-{kind}"
    directory.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with gpu_job(lock):
        model = AudioModel() if kind == "audio" else ImageModel()
    metadata = {
        "kind": kind,
        "loaded_at": datetime.now(timezone.utc).isoformat(),
        "load_seconds": time.monotonic() - started,
        "cuda_visible_devices": __import__("os").environ.get("CUDA_VISIBLE_DEVICES"),
        "gpu_name": torch.cuda.get_device_name(0),
        "model": "Qwen/Qwen2.5-Omni-7B" if kind == "audio" else "stabilityai/stable-diffusion-xl-base-1.0",
        "revision": "ae9e1690543ffd5c0221dc27f79834d0294cba00"
        if kind == "audio"
        else "462165984030d82259a11f4367a4eed129e94a7b",
        "generation_defaults": {
            "seed": 42,
            "thinker_do_sample": False,
            "thinker_max_new_tokens": 2048,
            "talker_max_new_tokens": 4096,
            "talker_do_sample": True,
            "talker_top_k": 40,
            "talker_top_p": 0.8,
            "talker_temperature": 0.9,
            "talker_repetition_penalty": 1.05,
        }
        if kind == "audio"
        else {
            "seed": 42,
            "steps": 30,
            "guidance_scale": 7.5,
            "size": "1024x1024",
            "image_execution": "Sequential images, seed = request seed + image index",
        },
    }
    (directory / "model.json").write_text(json.dumps(metadata, indent=2) + "\n")

    def begin(body):
        request_id = uuid.uuid4().hex
        path = directory / "requests" / request_id
        path.mkdir(parents=True)
        # Deliberately retain only the submitted payload, never headers or server environment.
        (path / "request.json").write_text(json.dumps(body, indent=2) + "\n")
        return request_id, path, time.monotonic()

    def finish(path, began, response):
        (path / "result.json").write_text(
            json.dumps(
                {
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "seconds": time.monotonic() - began,
                    "gpu_memory_allocated_bytes": torch.cuda.memory_allocated(0),
                    "response": response,
                },
                indent=2,
            )
            + "\n"
        )

    def fail(path, began, exception):
        finish(path, began, {"error": str(exception), "error_type": type(exception).__name__})
        logging.exception("local auxiliary inference failed")
        raise HTTPException(status_code=503, detail=str(exception)) from exception

    @app.get("/health")
    @app.get("/healthz")
    def health():
        return {"status": "ok", **metadata}

    @app.get("/v1/models")
    def models():
        return {"object": "list", "data": [{"id": metadata["model"], "object": "model", "owned_by": "local"}]}

    @app.post("/v1/chat/completions")
    def chat(body: dict):
        if kind != "audio":
            raise HTTPException(404, "chat is provided by the audio service")
        request_id, path, began = begin(body)
        try:
            conversation = [{"role": "system", "content": [{"type": "text", "text": OMNI_SYSTEM}]}]
            for message in body["messages"]:
                parts = message.get("content")
                if isinstance(parts, str):
                    parts = [{"type": "text", "text": parts}]
                content = []
                for index, part in enumerate(parts or []):
                    if part["type"] == "text":
                        content.append(part)
                    elif part["type"] == "input_audio":
                        audio = part["input_audio"]
                        audio_format = audio.get("format", "wav")
                        if audio_format not in {"wav", "mp3", "flac", "ogg", "opus", "aac"}:
                            raise ValueError("unsupported input_audio format")
                        audio_path = path / f"audio-{len(conversation)}-{index}.{audio_format}"
                        audio_path.write_bytes(base64.b64decode(audio["data"], validate=True))
                        content.append({"type": "audio", "audio": str(audio_path)})
                    else:
                        raise ValueError(f"unsupported audio chat content type {part['type']}")
                conversation.append({"role": message["role"], "content": content})
            with gpu_job(lock):
                answer, _, prompt_tokens, completion_tokens = model.infer(
                    conversation,
                    max_tokens=int(body.get("max_tokens", 2048)),
                    seed=int(body.get("seed", 42)),
                )
            response = {
                "id": "chatcmpl-" + request_id,
                "object": "chat.completion",
                "created": int(time.time()),
                "model": metadata["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": answer},
                        "finish_reason": "length" if completion_tokens >= int(body.get("max_tokens", 2048)) else "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                },
            }
            finish(path, began, response)
            return response
        except Exception as exception:
            fail(path, began, exception)

    @app.post("/v1/audio/transcriptions")
    def transcribe(
        file: UploadFile = File(...),
        requested_model: str = Form("local-audio", alias="model"),
        language: str | None = Form(None),
        response_format: str = Form("json"),
        prompt: str | None = Form(None),
        temperature: float = Form(0),
        seed: int = Form(42),
        max_tokens: int = Form(2048),
    ):
        if kind != "audio":
            raise HTTPException(404, "transcriptions are provided by the audio service")
        body = {
            "endpoint": "audio/transcriptions",
            "requested_model": requested_model,
            "language": language,
            "response_format": response_format,
            "prompt": prompt,
            "temperature": temperature,
            "seed": seed,
            "max_tokens": max_tokens,
            "effective_generation": {"model": metadata["model"], "thinker_do_sample": False},
        }
        _, path, began = begin(body)
        try:
            if response_format not in {"json", "text"}:
                raise ValueError("Local transcription supports json and text without fabricated timestamps")
            suffix = Path(file.filename or "input.wav").suffix.lower()
            audio_path = path / ("input" + (suffix if len(suffix) <= 10 else ".audio"))
            digest, size = hashlib.sha256(), 0
            with audio_path.open("wb") as destination:
                for chunk in iter(lambda: file.file.read(8 * 1024 * 1024), b""):
                    destination.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
            body["file"] = {"name": file.filename, "bytes": size, "sha256": digest.hexdigest()}
            (path / "request.json").write_text(json.dumps(body, indent=2) + "\n")
            if size == 0:
                raise ValueError("The submitted audio file is empty")
            instruction = (
                "Transcribe the speech in this audio verbatim. Return only the transcript, without explanations."
            )
            if language:
                instruction += " The requested language hint is " + language + "."
            if prompt:
                instruction += " Vocabulary/context hint: " + prompt
            conversation = [
                {"role": "system", "content": [{"type": "text", "text": OMNI_SYSTEM}]},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": instruction},
                        {"type": "audio", "audio": str(audio_path)},
                    ],
                },
            ]
            with gpu_job(lock):
                answer, _, prompt_tokens, completion_tokens = model.infer(
                    conversation,
                    max_tokens=max_tokens,
                    seed=seed,
                )
            response = {
                "text": answer,
                "model": metadata["model"],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                },
                "finish_reason": "length" if completion_tokens >= max_tokens else "stop",
            }
            finish(path, began, response)
            return PlainTextResponse(answer) if response_format == "text" else response
        except Exception as exception:
            fail(path, began, exception)
        finally:
            file.file.close()

    @app.post("/v1/audio/speech")
    def speech(body: dict):
        if kind != "audio":
            raise HTTPException(404, "speech is provided by the audio service")
        request_id, path, began = begin(body)
        try:
            import soundfile as sf

            voice = body.get("voice", "Chelsie")
            speaker = "Ethan" if str(voice).lower() in {"ethan", "onyx", "echo"} else "Chelsie"
            conversation = [
                {"role": "system", "content": [{"type": "text", "text": OMNI_SYSTEM}]},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Read the following text aloud exactly, without adding any words: " + body["input"],
                        }
                    ],
                },
            ]
            with gpu_job(lock):
                text, audio, _, _ = model.infer(
                    conversation,
                    max_tokens=int(body.get("max_tokens", 2048)),
                    speech=True,
                    speaker=speaker,
                    seed=int(body.get("seed", 42)),
                )
            if audio is None or audio.numel() == 0:
                raise RuntimeError("Omni returned no generated waveform")
            output = path / "speech.wav"
            sf.write(output, audio.reshape(-1).detach().float().cpu().numpy(), 24000)
            requested_format = body.get("response_format", "mp3")
            if requested_format not in {"wav", "mp3", "flac", "opus", "aac", "pcm"}:
                raise ValueError("unsupported speech response_format")
            if requested_format != "wav":
                import subprocess

                converted = path / ("speech." + requested_format)
                command = ["ffmpeg", "-loglevel", "error", "-y", "-i", str(output)]
                if requested_format == "pcm":
                    command += ["-f", "s16le", "-acodec", "pcm_s16le"]
                command.append(str(converted))
                subprocess.run(command, check=True, capture_output=True)
                output = converted
            finish(
                path, began, {"transcript": text, "audio_file": str(output), "sample_rate": 24000, "speaker": speaker}
            )
            return Response(
                output.read_bytes(),
                media_type={"wav": "audio/wav", "mp3": "audio/mpeg"}.get(requested_format, "application/octet-stream"),
            )
        except Exception as exception:
            fail(path, began, exception)

    @app.post("/v1/images/generations")
    def generate(body: dict, request: Request):
        if kind != "image":
            raise HTTPException(404, "images are provided by the image service")
        request_id, path, began = begin(body)
        try:
            with gpu_job(lock):
                images = model.infer(body)
            data = []
            for index, image in enumerate(images):
                output = path / f"image-{index}.png"
                image.save(output)
                item = {"revised_prompt": body["prompt"], "seed": int(body.get("seed", 42)) + index}
                if body.get("response_format", "url") == "b64_json":
                    item["b64_json"] = base64.b64encode(output.read_bytes()).decode()
                else:
                    item["url"] = str(request.base_url).rstrip("/") + f"/artifacts/{request_id}/image-{index}.png"
                data.append(item)
            response = {"created": int(time.time()), "data": data, "model": metadata["model"]}
            finish(path, began, response)
            return response
        except Exception as exception:
            fail(path, began, exception)

    @app.get("/artifacts/{request_id}/{filename}")
    def artifact(request_id: str, filename: str):
        candidate = (directory / "requests" / request_id / filename).resolve()
        if not candidate.is_relative_to((directory / "requests").resolve()) or not candidate.is_file():
            raise HTTPException(404, "artifact unavailable")
        return FileResponse(candidate)

    return app


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=["audio", "image"], required=True)
    parser.add_argument("--port", type=int, required=True)
    args, config = service_config(parser)
    global RUNTIME
    RUNTIME = Path(config["runtime_dir"])
    uvicorn.run(create_app(args.kind), host="0.0.0.0", port=args.port)


if __name__ == "__main__":
    main()
