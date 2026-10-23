#!/usr/bin/env python3
"""Local Omni audio and SDXL image APIs; every result uses actual model inference."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import math
import os
import subprocess
import threading
import time
import uuid
import wave
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, Response

from scripts.wildclawbench.services.common import runtime_directory, service_config
from scripts.wildclawbench.services.auxiliary_worker import limits, model_root, create_proxy, owned_command

RUNTIME = runtime_directory()
MODEL_ROOT = RUNTIME / "models"
LIMITS = limits({})
OMNI_SYSTEM = (
    "You are Qwen, a virtual human developed by the Qwen Team, Alibaba Group, "
    "capable of perceiving auditory and visual inputs, as well as generating text and speech."
)


@contextmanager
def gpu_job(local_lock):
    """The parent owns the bounded cross-service lock; refuse private overlap."""
    if not local_lock.acquire(blocking=False):
        raise HTTPException(429, "Private model worker already has an active job")
    try:
        yield
    finally:
        try:
            torch.cuda.empty_cache()
        finally:
            local_lock.release()


def integer(value, name, maximum, minimum=1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")
    return value


def bounded_text(value, name, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be nonempty text of at most {maximum} characters")
    return value


def audio_file(path):
    """Reject compressed duration bombs before the model/audio decoder loads them."""
    if not 0 < path.stat().st_size <= LIMITS["max_audio_bytes"]:
        raise ValueError("Audio file exceeds configured byte limit or is empty")
    # Decode only enough audio to establish whether it fits the limit. Unlike
    # metadata duration, a decoded sample count also works for AAC/stream inputs.
    cap = LIMITS["max_audio_seconds"] + .1
    destination = path.with_name(path.stem + ".validated.wav")
    owned_command(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-y", "-i", str(path),
                   "-map", "0:a:0", "-vn", "-t", str(cap), "-ac", "1", "-ar", "16000",
                   "-acodec", "pcm_s16le", "-fs", str(int(cap * 16000 * 2) + 4096),
                   "-f", "wav", str(destination)], check=True, capture_output=True,
                  timeout=LIMITS["ffmpeg_timeout_seconds"])
    with wave.open(str(destination), "rb") as decoded:
        duration = decoded.getnframes() / decoded.getframerate()
    if not math.isfinite(duration) or not 0 < duration <= LIMITS["max_audio_seconds"]:
        raise ValueError("Audio duration exceeds configured limit")
    return duration


def image_parameters(body):
    bounded_text(body.get("prompt"), "prompt", LIMITS["max_text_chars"])
    negative = body.get("negative_prompt")
    if negative is not None:
        bounded_text(negative, "negative_prompt", LIMITS["max_text_chars"])
    size = body.get("size", "1024x1024")
    if not isinstance(size, str) or not __import__("re").fullmatch(r"\d{3,4}x\d{3,4}", size):
        raise ValueError("Invalid image size")
    width, height = map(int, size.split("x"))
    if min(width, height) < 256 or max(width, height) > 1536 or width % 8 or height % 8 or width * height > LIMITS["max_image_pixels"]:
        raise ValueError("Image dimensions exceed configured limits")
    count = integer(body.get("n", 1), "n", LIMITS["max_image_count"])
    steps = integer(body.get("steps", 30), "steps", LIMITS["max_image_steps"])
    seed = integer(body.get("seed", 42), "seed", 2**32 - count, minimum=0)
    guidance = body.get("guidance_scale", 7.5)
    if isinstance(guidance, bool) or not isinstance(guidance, (int, float)) or not math.isfinite(guidance) or not 0 <= guidance <= 20:
        raise ValueError("guidance_scale must be finite and between 0 and 20")
    if body.get("response_format", "url") not in {"url", "b64_json"}:
        raise ValueError("Unsupported image response_format")
    return width, height, count, steps, seed, guidance


class AudioModel:
    def __init__(self):
        from transformers import Qwen2_5OmniForConditionalGeneration, Qwen2_5OmniProcessor

        directory = MODEL_ROOT / "qwen2.5-omni-7b"
        self.model = Qwen2_5OmniForConditionalGeneration.from_pretrained(
            directory,
            dtype=torch.bfloat16,
            device_map="cuda:0",
            attn_implementation="sdpa",
            local_files_only=True,
        ).eval()
        self.processor = Qwen2_5OmniProcessor.from_pretrained(directory, local_files_only=True)
        self.infer = torch.inference_mode()(self.infer)

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
            MODEL_ROOT / "sdxl-base-1.0",
            torch_dtype=torch.float16,
            variant="fp16",
            use_safetensors=True,
            local_files_only=True,
        ).to("cuda:0")
        self.infer = torch.inference_mode()(self.infer)

    def infer(self, body):
        width, height, count, steps, seed, guidance = image_parameters(body)
        images = []
        for index in range(count):
            images.extend(
                self.pipeline(
                    prompt=body["prompt"],
                    negative_prompt=body.get("negative_prompt"),
                    width=width,
                    height=height,
                    num_images_per_prompt=1,
                    num_inference_steps=steps,
                    guidance_scale=guidance,
                    generator=torch.Generator(device="cuda:0").manual_seed(seed + index),
                ).images
            )
        return images


def create_worker_app(kind: str) -> FastAPI:
    global torch
    import torch
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
        if isinstance(exception, HTTPException):
            raise exception
        raise HTTPException(status_code=422 if isinstance(exception, (ValueError, subprocess.CalledProcessError))
                            else 503, detail=str(exception)) from exception

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
            max_tokens = integer(body.get("max_tokens", 2048), "max_tokens", LIMITS["max_audio_tokens"])
            seed = integer(body.get("seed", 42), "seed", 2**32 - 1, minimum=0)
            messages = body.get("messages")
            if not isinstance(messages, list) or not 1 <= len(messages) <= 64:
                raise ValueError("messages must contain between 1 and 64 entries")
            total_text, total_audio, audio_count = 0, 0, 0
            conversation = [{"role": "system", "content": [{"type": "text", "text": OMNI_SYSTEM}]}]
            for message in messages:
                if not isinstance(message, dict) or message.get("role") not in {"system", "user", "assistant"}:
                    raise ValueError("Invalid audio message")
                parts = message.get("content")
                if isinstance(parts, str):
                    parts = [{"type": "text", "text": parts}]
                content = []
                for index, part in enumerate(parts or []):
                    if part["type"] == "text":
                        total_text += len(bounded_text(part.get("text"), "text", LIMITS["max_text_chars"]))
                        if total_text > LIMITS["max_text_chars"]:
                            raise ValueError("Audio conversation exceeds text limit")
                        content.append(part)
                    elif part["type"] == "input_audio":
                        audio = part["input_audio"]
                        audio_format = audio.get("format", "wav")
                        if audio_format not in {"wav", "mp3", "flac", "ogg", "opus", "aac"}:
                            raise ValueError("unsupported input_audio format")
                        encoded = audio.get("data")
                        audio_count += 1
                        if not isinstance(encoded, str) or len(encoded) > 4 * ((LIMITS["max_audio_bytes"] + 2) // 3) or audio_count > 8:
                            raise ValueError("Audio data exceeds configured limit")
                        audio_path = path / f"audio-{len(conversation)}-{index}.{audio_format}"
                        audio_path.write_bytes(base64.b64decode(encoded, validate=True))
                        total_audio += audio_file(audio_path)
                        if total_audio > LIMITS["max_audio_seconds"]:
                            raise ValueError("Total audio duration exceeds configured limit")
                        content.append({"type": "audio", "audio": str(audio_path.with_name(audio_path.stem + ".validated.wav"))})
                    else:
                        raise ValueError(f"unsupported audio chat content type {part['type']}")
                conversation.append({"role": message["role"], "content": content})
            with gpu_job(lock):
                answer, _, prompt_tokens, completion_tokens = model.infer(
                    conversation,
                    max_tokens=max_tokens,
                    seed=seed,
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
                        "finish_reason": "length" if completion_tokens >= max_tokens else "stop",
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
            integer(max_tokens, "max_tokens", LIMITS["max_audio_tokens"])
            integer(seed, "seed", 2**32 - 1, minimum=0)
            if prompt is not None:
                bounded_text(prompt, "prompt", LIMITS["max_text_chars"])
            if language is not None:
                bounded_text(language, "language", 128)
            if response_format not in {"json", "text"}:
                raise ValueError("Local transcription supports json and text without fabricated timestamps")
            suffix = Path(file.filename or "input.wav").suffix.lower()
            audio_path = path / ("input" + (suffix if len(suffix) <= 10 else ".audio"))
            digest, size = hashlib.sha256(), 0
            with audio_path.open("wb") as destination:
                for chunk in iter(lambda: file.file.read(8 * 1024 * 1024), b""):
                    if size + len(chunk) > LIMITS["max_audio_bytes"]:
                        raise ValueError("Audio upload exceeds configured byte limit")
                    destination.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
            body["file"] = {"name": file.filename, "bytes": size, "sha256": digest.hexdigest()}
            (path / "request.json").write_text(json.dumps(body, indent=2) + "\n")
            audio_file(audio_path)
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
                        {"type": "audio", "audio": str(audio_path.with_name(audio_path.stem + ".validated.wav"))},
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

            input_text = bounded_text(body.get("input"), "input", LIMITS["max_speech_chars"])
            max_tokens = integer(body.get("max_tokens", 2048), "max_tokens", LIMITS["max_audio_tokens"])
            seed = integer(body.get("seed", 42), "seed", 2**32 - 1, minimum=0)
            requested_format = body.get("response_format", "mp3")
            if requested_format not in {"wav", "mp3", "flac", "opus", "aac", "pcm"}:
                raise ValueError("unsupported speech response_format")
            voice = body.get("voice", "Chelsie")
            speaker = "Ethan" if str(voice).lower() in {"ethan", "onyx", "echo"} else "Chelsie"
            conversation = [
                {"role": "system", "content": [{"type": "text", "text": OMNI_SYSTEM}]},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Read the following text aloud exactly, without adding any words: " + input_text,
                        }
                    ],
                },
            ]
            with gpu_job(lock):
                text, audio, _, _ = model.infer(
                    conversation,
                    max_tokens=max_tokens,
                    speech=True,
                    speaker=speaker,
                    seed=seed,
                )
            if audio is None or not 0 < audio.numel() <= 24000 * LIMITS["max_audio_seconds"]:
                raise RuntimeError("Omni returned no generated waveform")
            output = path / "speech.wav"
            sf.write(output, audio.reshape(-1).detach().float().cpu().numpy(), 24000)
            if requested_format != "wav":
                converted = path / ("speech." + requested_format)
                command = ["ffmpeg", "-loglevel", "error", "-y", "-i", str(output)]
                if requested_format == "pcm":
                    command += ["-f", "s16le", "-acodec", "pcm_s16le"]
                command.append(str(converted))
                owned_command(command, check=True, capture_output=True, timeout=LIMITS["ffmpeg_timeout_seconds"])
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
                    item["url"] = request.headers.get("x-public-base-url", str(request.base_url)).rstrip("/") + f"/artifacts/{request_id}/image-{index}.png"
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
    parser.add_argument("--port", type=int)
    parser.add_argument("--worker-socket", type=Path)
    args, config = service_config(parser)
    global RUNTIME, MODEL_ROOT, LIMITS
    RUNTIME = Path(config["runtime_dir"])
    MODEL_ROOT = model_root(config)
    LIMITS = limits(config)
    if args.worker_socket:
        # The GPU owner cannot outlive its dedicated API parent.
        import ctypes
        import signal
        parent = os.getppid()
        if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent:
            raise RuntimeError("Cannot bind model worker lifetime to API parent")
        os.umask(0o077)
        uvicorn.run(create_worker_app(args.kind), uds=str(args.worker_socket), access_log=False)
    else:
        if not args.port:
            parser.error("--port is required for the public service")
        config["service_config_path"] = str(args.config.resolve())
        uvicorn.run(create_proxy(args.kind, config), host="0.0.0.0", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
