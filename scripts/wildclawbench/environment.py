"""Pinned WildClawBench assets and a project-owned Docker-in-Docker daemon.

The outer daemon is only used to manage our named DinD container. Benchmark
images, task containers and their writable layers live on the /home filesystem.
No global Docker context, daemon settings, or existing containers are changed.
"""

from __future__ import annotations

import concurrent.futures
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Mapping

BENCHMARK_REVISION = "8e1fde746584ae1a9c49b70526d49a7a572625cd"
DATASET_REVISION = "75f945578aa00cbdb8f46e4d42e4f4e98f704b4f"
DIND_IMAGE = "docker@sha256:7613944c7bc318c7b97541bd0e65b8a18d033e37e204305f1ee2639fc9a03827"
TASK_IMAGE = "wildclawbench-ubuntu:v1.3"
IMAGE_BYTES = 13_479_787_008
from scripts.wildclawbench.protocol import REPOSITORY

ROOT = REPOSITORY
PYTHON_BOOTSTRAP_SOURCE = "import random\nrandom.seed(42)\n"
PYTHON_BOOTSTRAP_DIRECTORY = "/opt/sharedskill-bootstrap"
BLOCKED_MODEL_HOSTS = (
    "api.deepseek.com",
    "api.openai.com",
    "openrouter.ai",
    "www.openrouter.ai",
    "api.anthropic.com",
    "generativelanguage.googleapis.com",
    "api.elevenlabs.io",
    "api.replicate.com",
    "api.fal.ai",
    "queue.fal.run",
    "dashscope.aliyuncs.com",
    "dashscope-intl.aliyuncs.com",
    "speech.platform.bing.com",
)


def provider_network_args() -> list[str]:
    """Only this experiment's containers block paid provider DNS names."""
    return [value for host in BLOCKED_MODEL_HOSTS for value in ("--add-host", host + ":127.0.0.1")]


def python_bootstrap_source(config: Mapping[str, Any]) -> Path:
    """Generate the exact stdlib-only bootstrap once, with a process-safe lock."""
    p = paths(config)
    directory = (p["runtime"] if config.get("shared_runtime_dir") else p["assets"]) / "python-bootstrap"
    directory.mkdir(parents=True, exist_ok=True)
    source = directory / "sitecustomize.py"
    with (directory / ".write.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if source.exists() and source.read_text() != PYTHON_BOOTSTRAP_SOURCE:
            raise RuntimeError("Python seed bootstrap has unexpected modifications")
        if not source.exists():
            source.write_text(PYTHON_BOOTSTRAP_SOURCE)
    return source


def python_seed_args(config: Mapping[str, Any]) -> list[str]:
    original = config.get("image_pythonpath")
    cache = paths(config)["assets"] / "task-image-python-environment.json"
    if original is None and cache.exists():
        original = json.loads(cache.read_text()).get("PYTHONPATH", "")
    pythonpath = PYTHON_BOOTSTRAP_DIRECTORY + (":" + str(original) if original else "")
    return ["--env", "PYTHONPATH=" + pythonpath, "--env", "PYTHONHASHSEED=42"]


def apply_python_bootstrap(name: str, config: Mapping[str, Any], manifest: Path) -> dict[str, Any]:
    source = python_bootstrap_source(config)
    _run(["docker", "exec", name, "mkdir", "-p", PYTHON_BOOTSTRAP_DIRECTORY], config=config)
    target = PYTHON_BOOTSTRAP_DIRECTORY + "/sitecustomize.py"
    _run(["docker", "cp", str(source), name + ":" + target], config=config)
    seed_args = python_seed_args(config)
    record = {
        "seed": 42,
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "source_path": str(source),
        "container_path": target,
        "pythonpath": seed_args[1].partition("=")[2],
        "pythonhashseed": "42",
        "original_workspace_unchanged": True,
        "mock_error_rate_unchanged": True,
    }
    _write_json(manifest, record)
    return record


LOCAL_SCRIPT_ADAPTERS = {
    "/usr/lib/node_modules/openclaw/skills/openai-image-gen/scripts/gen.py": (
        "b0268dfe95b18ba99ee1354417a9940492fc0e9d921cec1dfcc71ebec6616164"
    ),
    "/usr/lib/node_modules/openclaw/skills/openai-whisper-api/scripts/transcribe.sh": (
        "06d255cdd1a44ea3098ca1d9400529c641da76b4e9389007904f190e681d5688"
    ),
}


def apply_local_tool_adapters(name: str, config: Mapping[str, Any], api_url: str, manifest: Path) -> dict[str, Any]:
    """Adapt only known scripts in a private writable container, verifying source."""
    script = """import json,hashlib,os
from pathlib import Path
entries=[]
for filename,expected in SOURCES.items():
    path=Path(filename);original=path.read_bytes()
    digest=hashlib.sha256(original).hexdigest()
    if digest!=expected or original.count(b'https://api.openai.com/v1')!=1:
        raise RuntimeError('Unexpected bundled tool source: '+filename)
    updated=original.replace(b'https://api.openai.com/v1',BASE_URL.encode())
    mode=path.stat().st_mode;path.write_bytes(updated);os.chmod(path,mode)
    entries.append({'path':filename,'before_sha256':digest,'after_sha256':hashlib.sha256(updated).hexdigest(),
                    'old_base_url':'https://api.openai.com/v1','local_base_url':BASE_URL})
print(json.dumps({'adapters':entries,'original_image_unchanged':True}))
"""
    script = "SOURCES=" + repr(LOCAL_SCRIPT_ADAPTERS) + "\nBASE_URL=" + repr(api_url) + "\n" + script
    result = _run(["docker", "exec", name, "python3", "-c", script], config=config, timeout=60)
    record = json.loads(result.stdout)
    record.update(
        blocked_model_hosts=list(BLOCKED_MODEL_HOSTS),
        cloud_edge_tts_blocked=True,
        native_tts={"provider": "local Qwen Omni", "voices": ["Chelsie", "Ethan"]},
    )
    if config.get("native_compaction_guard") is not None:
        from scripts.wildclawbench.safeguard_adapter import apply_native_compaction_guard
        record["native_compaction_guard"] = apply_native_compaction_guard(name, config)
    _write_json(manifest, record)
    return record


def paths(config: Mapping[str, Any]) -> dict[str, Path]:
    root = Path(config.get("experiment_root", config.get("root", ROOT))).resolve()
    runtime = Path(config.get("runtime_dir", root / "runtime")).resolve()
    cache = Path(config.get("shared_runtime_dir", runtime)).resolve()
    return {
        "root": root,
        "runtime": runtime,
        "upstream": cache / "upstream" / "WildClawBench",
        "assets": cache / "assets",
        "docker": runtime / "docker",
    }


def _run(
    args: list[str], *, config: Mapping[str, Any] | None = None, timeout: float = 600, check: bool = True, **kwargs: Any
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if config is not None:
        env["DOCKER_HOST"] = docker_host(config)
        env.pop("DOCKER_CONTEXT", None)
        # AF_UNIX paths are limited to 108 bytes. Our project directory is
        # longer; a relative socket address and fixed cwd avoid external links.
        kwargs.setdefault("cwd", str(paths(config)["root"]))
    return subprocess.run(args, env=env, capture_output=True, text=True, timeout=timeout, check=check, **kwargs)


def docker_host(config: Mapping[str, Any]) -> str:
    explicit = config.get("docker_host", config.get("dock_host"))
    if explicit:
        value = str(explicit)
        if value.startswith("unix:///") and len(value[7:].encode()) >= 108:
            return "unix://" + os.path.relpath(value[7:], paths(config)["root"])
        return value
    return "unix://" + os.path.relpath(paths(config)["docker"] / "run" / "docker.sock", paths(config)["root"])


def _json_get(url: str) -> tuple[Any, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "SharedSkill-reproduction/1.0"})
    with urllib.request.urlopen(req, timeout=60) as response:
        return json.load(response), response.headers.get("Link", "")


def _hf_listing(folder: str) -> list[dict[str, Any]]:
    url = f"https://huggingface.co/api/datasets/internlm/WildClawBench/tree/{DATASET_REVISION}/{folder}?recursive=true&expand=false&limit=1000"
    result: list[dict[str, Any]] = []
    while url:
        page, link = _json_get(url)
        result.extend(x for x in page if x.get("type") == "file")
        match = re.search(r'<([^>]+)>;\s*rel="next"', link)
        url = match.group(1) if match else ""
    return result


def _download(url: str, destination: Path, expected_size: int | None = None) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and (expected_size is None or destination.stat().st_size == expected_size):
        return
    partial = destination.with_name(destination.name + ".partial")
    # curl's resumable streaming avoids loading large image layers into RAM.
    _run(
        [
            "curl",
            "--silent",
            "--show-error",
            "--fail",
            "--location",
            "--continue-at",
            "-",
            "--retry",
            "5",
            "--retry-all-errors",
            "--retry-delay",
            "3",
            "--output",
            str(partial),
            url,
        ],
        timeout=14_400,
    )
    if expected_size is not None and partial.stat().st_size != expected_size:
        raise RuntimeError(f"Incomplete asset {destination.name}: {partial.stat().st_size}/{expected_size}")
    partial.replace(destination)


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str) + "\n")
    temporary.replace(path)


def prepare_sources(config: Mapping[str, Any]) -> dict[str, Any]:
    p = paths(config)
    for key in ("runtime",) if config.get("shared_runtime_dir") else ("runtime", "assets", "docker"):
        p[key].mkdir(parents=True, exist_ok=True)
    source = p["upstream"]
    if not (source / ".git").exists():
        if config.get("shared_runtime_dir"):
            raise RuntimeError("Shared benchmark cache is missing; prepare its owning experiment first")
        source.parent.mkdir(parents=True, exist_ok=True)
        _run(
            ["git", "clone", "--filter=blob:none", "https://github.com/InternLM/WildClawBench.git", str(source)],
            timeout=600,
        )
    revision = _run(["git", "-C", str(source), "rev-parse", "HEAD"]).stdout.strip()
    if _run(["git", "-C", str(source), "diff", "--name-only", "HEAD"]).stdout.strip():
        raise RuntimeError("Pinned benchmark source has tracked modifications")
    if revision != BENCHMARK_REVISION:
        if config.get("shared_runtime_dir"):
            raise RuntimeError("Shared benchmark cache is at a different revision; refusing to modify it")
        if _run(["git", "-C", str(source), "status", "--porcelain"]).stdout.strip():
            raise RuntimeError("Refusing to overwrite modified benchmark source")
        _run(["git", "-C", str(source), "checkout", "--quiet", BENCHMARK_REVISION])
    return {"benchmark_revision": BENCHMARK_REVISION, "dataset_revision": DATASET_REVISION, "upstream": str(source)}


def prepare_assets(config: Mapping[str, Any], workspace_only: bool = False) -> dict[str, Any]:
    p = paths(config)
    listing_file = p["assets"] / "workspace-file-manifest.json"
    if config.get("shared_runtime_dir"):
        if not listing_file.is_file():
            raise RuntimeError("Shared dataset listing is missing")
        listing = json.loads(listing_file.read_text())
        for entry in listing:
            rel = Path(entry["path"])
            if rel.is_absolute() or ".." in rel.parts or rel.parts[:1] != ("workspace",):
                raise RuntimeError("Unsafe shared dataset path")
            file = p["assets"] / entry["path"]
            if not file.is_file() or file.stat().st_size != int(entry["size"]):
                raise RuntimeError("Shared dataset cache is incomplete: " + entry["path"])
        archive = p["assets"] / "wildclawbench-ubuntu_v1.3.tar"
        if not workspace_only and (not archive.is_file() or archive.stat().st_size != IMAGE_BYTES):
            raise RuntimeError("Shared task image archive is missing or incomplete")
        if (p["upstream"] / "workspace").resolve() != (p["assets"] / "workspace").resolve():
            raise RuntimeError("Shared benchmark workspace points to a different dataset")
        return {
            "workspace_files": len(listing), "workspace_bytes": sum(x["size"] for x in listing),
            "image_archive": str(archive), "image_archive_bytes": archive.stat().st_size if archive.exists() else None,
            "workspace_only": workspace_only, "cached": True,
        }
    listing = json.loads(listing_file.read_text()) if listing_file.exists() else _hf_listing("workspace")
    _write_json(listing_file, listing)

    def download_entry(entry: dict[str, Any]) -> None:
        rel = entry["path"]
        if ".." in Path(rel).parts or not rel.startswith("workspace/"):
            raise ValueError(f"Unsafe dataset path {rel}")
        url = (
            f"https://huggingface.co/datasets/internlm/WildClawBench/resolve/{DATASET_REVISION}/"
            + urllib.parse.quote(rel)
        )
        _download(url, p["assets"] / rel, int(entry["size"]))

    with concurrent.futures.ThreadPoolExecutor(max_workers=int(config.get("asset_download_workers", 12))) as pool:
        list(pool.map(download_entry, listing))
    workspace = p["upstream"] / "workspace"
    if not workspace.exists():
        workspace.symlink_to(p["assets"] / "workspace", target_is_directory=True)
    elif workspace.resolve() != (p["assets"] / "workspace").resolve():
        raise RuntimeError("Benchmark workspace already points to a different dataset")
    archive = p["assets"] / "wildclawbench-ubuntu_v1.3.tar"
    if not workspace_only:
        _download(
            f"https://huggingface.co/datasets/internlm/WildClawBench/resolve/{DATASET_REVISION}/Images/{archive.name}",
            archive,
            IMAGE_BYTES,
        )
    return {
        "workspace_files": len(listing),
        "workspace_bytes": sum(x["size"] for x in listing),
        "image_archive": str(archive),
        "image_archive_bytes": archive.stat().st_size if archive.exists() else None,
        "workspace_only": workspace_only,
    }


def prepare_docker(config: Mapping[str, Any], load_image: bool = True) -> dict[str, Any]:
    p = paths(config)
    name = str(config.get("docker_container", "sharedskill-wildbench-" + str(os.getuid())))
    shared_runtime = Path(str(config["shared_runtime_dir"])).resolve() if config.get("shared_runtime_dir") else None
    for directory in (p["docker"] / "data", p["docker"] / "run", p["runtime"] / "tmp"):
        directory.mkdir(parents=True, exist_ok=True)
    (p["docker"] / "run").chmod(0o700)
    probe = _run(["docker", "inspect", name], check=False)
    if probe.returncode == 0:
        info = json.loads(probe.stdout)[0]
        mounts = info.get("Mounts", [])
        required = str((p["docker"] / "data").resolve())
        if not any(
            m.get("Source") == required and m.get("Destination") == "/var/lib/docker" for m in mounts
        ):
            raise RuntimeError("Existing container with our name has unexpected storage; refusing to touch it")

        def require_bind(source: Path, *, readonly: bool, purpose: str) -> None:
            expected = str(source.resolve())
            expected_rw = not readonly
            matches = [mount for mount in mounts if mount.get("Destination") == expected]
            if len(matches) != 1 or not (
                matches[0].get("Type") == "bind"
                and matches[0].get("Source") == expected
                and matches[0].get("RW") is expected_rw
            ):
                access = "read-only" if readonly else "read-write"
                raise RuntimeError(
                    f"Existing container {name!r} lacks the exact {purpose} bind "
                    f"{expected}:{expected} ({access}); refusing to reuse or modify it"
                )

        require_bind(p["root"], readonly=False, purpose="repository")
        if shared_runtime is not None:
            require_bind(shared_runtime, readonly=True, purpose="shared runtime")
        if not info["State"]["Running"]:
            _run(["docker", "start", name])
    else:
        _run(
            [
                "docker",
                "run",
                "--detach",
                "--privileged",
                "--name",
                name,
                "--label",
                "org.sharedskill.experiment=wildclawbench-qwen3.8-27b",
                "--log-opt",
                "max-size=10m",
                "--log-opt",
                "max-file=2",
                "--mount",
                f"type=bind,src={p['docker'] / 'data'},dst=/var/lib/docker",
                "--mount",
                f"type=bind,src={p['docker'] / 'run'},dst=/run/sharedskill",
                "--mount",
                f"type=bind,src={p['root']},dst={p['root']}",
                *(
                    ["--mount", f"type=bind,src={shared_runtime},dst={shared_runtime},readonly"]
                    if shared_runtime is not None else []
                ),
                "--entrypoint",
                "dockerd",
                DIND_IMAGE,
                "--host=unix:///run/sharedskill/docker.sock",
                "--storage-driver=overlay2",
                "--group=" + str(os.getgid()),
                "--log-driver=json-file",
                "--log-opt=max-size=10m",
                "--log-opt=max-file=2",
            ],
            timeout=120,
        )
    for _ in range(90):
        result = _run(["docker", "info", "--format", "{{.DockerRootDir}}"], config=config, timeout=10, check=False)
        if result.returncode == 0:
            break
        time.sleep(1)
    else:
        raise RuntimeError("Dedicated Docker daemon did not become ready")
    inner_image = _run(["docker", "image", "inspect", TASK_IMAGE], config=config, check=False)
    if inner_image.returncode and load_image:
        _run(
            ["docker", "load", "--input", str(p["assets"] / "wildclawbench-ubuntu_v1.3.tar")],
            config=config,
            timeout=1800,
        )
        inner_image = _run(["docker", "image", "inspect", TASK_IMAGE], config=config, check=False)
    image_info = json.loads(inner_image.stdout)[0] if inner_image.returncode == 0 else {}
    if image_info:
        original_pythonpath = next(
            (
                value.partition("=")[2]
                for value in image_info.get("Config", {}).get("Env", [])
                if value.startswith("PYTHONPATH=")
            ),
            "",
        )
        python_environment = {"task_image_id": image_info["Id"], "PYTHONPATH": original_pythonpath}
        python_environment_file = p["assets"] / "task-image-python-environment.json"
        if config.get("shared_runtime_dir"):
            if (not python_environment_file.exists()
                    or json.loads(python_environment_file.read_text()) != python_environment):
                raise RuntimeError("Shared task image environment does not match the loaded image")
        else:
            _write_json(python_environment_file, python_environment)
    outer = json.loads(_run(["docker", "inspect", name]).stdout)[0]
    gateways = [n.get("Gateway") for n in outer["NetworkSettings"]["Networks"].values() if n.get("Gateway")]
    return {
        "container": name,
        "docker_host": docker_host(config),
        "outer_gateway": gateways[0] if gateways else None,
        "task_image": TASK_IMAGE,
        "task_image_id": image_info.get("Id"),
        "image_ready": inner_image.returncode == 0,
        "storage": str(p["docker"] / "data"),
    }


def prepare_supplemental_assets(config: Mapping[str, Any], videos_only: bool = False) -> dict[str, Any]:
    """Run each official preparation dependency independently, retaining failures.

    Public sources and transformations match the pinned prepare.sh. One video
    failing must not prevent downloading unrelated weights or preparing safety
    repositories. Missing assets remain explicit blockers, never dummy files.
    """
    p = paths(config)
    if config.get("shared_runtime_dir"):
        cached = p["assets"] / ("video-assets.json" if videos_only else "supplemental-assets.json")
        if not cached.is_file():
            raise RuntimeError("Shared supplemental asset preparation is missing")
        return json.loads(cached.read_text())
    workspace = p["assets"] / "workspace"
    creative = workspace / "05_Creative_Synthesis"
    jobs = [
        ("football", "93LPZJkCW2w", creative / "task_1_match_report" / "exec" / "full_match.mp4"),
        ("lecture", "LPZh9BOjkQs", creative / "task_4_video_notes" / "exec" / "video.mp4"),
        ("launch", "H3KnMyojEQU", creative / "task_5_product_launch_video_to_json" / "exec" / "recording.mp4"),
    ]
    statuses: dict[str, Any] = {}

    def video(job: tuple[str, str, Path]) -> tuple[str, dict[str, Any]]:
        name, video_id, destination = job
        if name == "football" and destination.with_name("first_half.mp4").exists():
            second = creative / "task_2_goal_highlights" / "exec" / "first_half.mp4"
            second.parent.mkdir(parents=True, exist_ok=True)
            if not second.exists():
                shutil.copy2(destination.with_name("first_half.mp4"), second)
            return name, {"status": "ready", "source": "https://www.youtube.com/watch?v=" + video_id}
        destination.parent.mkdir(parents=True, exist_ok=True)
        log = p["assets"] / ("prepare-video-" + name + ".log")
        try:
            if not destination.exists():
                yt_dlp = Path(config.get("yt_dlp_path", p["runtime"] / "tools-venv" / "bin" / "yt-dlp"))
                executable = str(yt_dlp) if yt_dlp.exists() else "yt-dlp"
                proc = _run(
                    [
                        executable,
                        "--no-playlist",
                        "--socket-timeout",
                        "20",
                        "--retries",
                        "2",
                        "--fragment-retries",
                        "2",
                        "--js-runtimes",
                        "node",
                        "--remote-components",
                        "ejs:github",
                        "--write-info-json",
                        "-f",
                        "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]",
                        "--merge-output-format",
                        "mp4",
                        "-o",
                        str(destination),
                        "https://www.youtube.com/watch?v=" + video_id,
                    ],
                    timeout=int(config.get("asset_video_timeout_seconds", 3600)),
                    check=False,
                )
                log.write_text(proc.stdout + "\n" + proc.stderr)
                if proc.returncode:
                    return name, {"status": "pending", "error": "yt-dlp failed; see " + str(log)}
            if name == "football":
                target = destination.with_name("first_half.mp4")
                _run(["ffmpeg", "-y", "-i", str(destination), "-t", "00:57:00", "-c", "copy", str(target)], timeout=300)
                destination.unlink()
                second = creative / "task_2_goal_highlights" / "exec" / "first_half.mp4"
                second.parent.mkdir(parents=True, exist_ok=True)
                if not second.exists():
                    shutil.copy2(target, second)
            if name == "launch":
                second = creative / "task_11_video_en_to_zh_dub" / "exec" / "recording.mp4"
                second.parent.mkdir(parents=True, exist_ok=True)
                if not second.exists():
                    shutil.copy2(destination, second)
            return name, {"status": "ready", "source": "https://www.youtube.com/watch?v=" + video_id}
        except Exception as exc:
            log.write_text(str(exc))
            return name, {"status": "pending", "error": str(exc)}

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        for name, result in pool.map(video, jobs):
            statuses[name] = result
    if videos_only:
        _write_json(p["assets"] / "video-assets.json", statuses)
        return statuses
    for task in ("task_2_leaked_api", "task_3_leaked_api_pswd"):
        directory = workspace / "06_Safety_Alignment" / task / "exec" / "mm_agents"
        archive = directory / "dot_git.tar.gz"
        if archive.exists() and not (directory / ".git").exists():
            with tarfile.open(archive) as stream:
                stream.extractall(directory, filter="data")
    weight = workspace / "02_Code_Intelligence" / "task_1_sam3_inference" / "exec" / "sam3" / "sam3.pt"
    try:
        _download("https://www.modelscope.cn/models/facebook/sam3/resolve/master/sam3.pt", weight)
        if weight.stat().st_size < 100_000_000:
            raise RuntimeError("Downloaded SAM3 response is too small to be model weights")
        other = workspace / "02_Code_Intelligence" / "task_2_sam3_debug" / "exec" / "sam3" / "sam3.pt"
        other.parent.mkdir(parents=True, exist_ok=True)
        if not other.exists():
            shutil.copy2(weight, other)
        digest = hashlib.sha256()
        with weight.open("rb") as stream:
            for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                digest.update(block)
        statuses["sam3"] = {
            "status": "ready",
            "source": "https://www.modelscope.cn/models/facebook/sam3",
            "bytes": weight.stat().st_size,
            "sha256": digest.hexdigest(),
        }
    except Exception as exc:
        statuses["sam3"] = {"status": "pending", "error": str(exc)}
    _write_json(p["assets"] / "supplemental-assets.json", statuses)
    return statuses


def container_api_url(config: Mapping[str, Any], *, run_id: str | None = None, role: str = "main") -> str:
    if role not in {"main", "judge"}:
        raise ValueError("Unknown model request role")
    url = str(config.get("local_api_base_url", config.get("model_base_url", "http://127.0.0.1:18182/v1")))
    parts = urllib.parse.urlsplit(url)
    if parts.hostname in ("127.0.0.1", "localhost", "host.docker.internal", "HOSTGATEWAY"):
        ready = paths(config)["runtime"] / "environment.json"
        gateway = config.get("outer_gateway")
        if not gateway and ready.exists():
            gateway = json.loads(ready.read_text()).get("docker", {}).get("outer_gateway")
        if not gateway:
            name = str(config.get("docker_container", "sharedskill-wildbench-" + str(os.getuid())))
            info = json.loads(_run(["docker", "inspect", name]).stdout)[0]
            gateway = next(n["Gateway"] for n in info["NetworkSettings"]["Networks"].values() if n.get("Gateway"))
        parts = parts._replace(netloc=str(gateway) + (":" + str(parts.port) if parts.port else ""))
    if parts.hostname in ("openrouter.ai", "api.openai.com"):
        raise RuntimeError("A paid remote API endpoint is forbidden in this experiment")
    if role == "judge" and config.get("external_judge"):
        prefix = "/runs/" + urllib.parse.quote(run_id, safe="-_.") if run_id else ""
        parts = parts._replace(path=prefix + "/judge/v1")
    elif run_id and "/runs/" not in parts.path:
        parts = parts._replace(path="/runs/" + urllib.parse.quote(run_id, safe="-_.") + "/v1")
    return urllib.parse.urlunsplit(parts)


def openclaw_config(config: Mapping[str, Any], workspace: str, *, api_url: str | None = None,
                    tool_api_url: str | None = None) -> dict[str, Any]:
    model = str(config.get("model_id", config.get("model", {}).get("name", "qwen3.8-27b")))
    parameters = {**config, **config.get("model", {}), **config.get("params", {})}
    result = {
        "models": {
            "providers": {
                "sharedskill": {
                    "api": "openai-completions",
                    "baseUrl": api_url or container_api_url(config),
                    "apiKey": config.get("local_api_key", "sharedskill-local"),
                    "models": [
                        {
                            "id": model,
                            "name": model,
                            "reasoning": True,
                            "input": ["text", "image"],
                            "contextWindow": int(
                                config.get(
                                    "model_context_window",
                                    parameters.get("context_window", parameters.get("context_length", 131072)),
                                )
                            ),
                            "maxTokens": int(parameters.get("max_tokens", 16384)),
                            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                        }
                    ],
                }
            }
        },
        "gateway": {"mode": "local", "bind": "loopback", "auth": {"mode": "token", "token": "sharedskill-local"}},
        "agents": {
            "defaults": {
                "workspace": workspace,
                "sandbox": {"mode": "off"},
                "model": {"primary": "sharedskill/" + model},
                "imageModel": {"primary": "sharedskill/" + model},
                "thinkingDefault": parameters.get("reasoning_effort", "medium"),
            }
        },
        "skills": {"load": {"extraDirs": ["/root/skills"]}},
        "browser": {
            "enabled": True,
            "headless": True,
            "noSandbox": True,
            "defaultProfile": "openclaw",
            "executablePath": "/root/.cache/ms-playwright/chromium-1208/chrome-linux64/chrome",
        },
        "messages": {
            "tts": {
                "provider": "openai",
                "timeoutMs": 120000,
                "prefsPath": str(Path(workspace) / ".sharedskill-tts-prefs.json"),
                "modelOverrides": {"enabled": False},
                "openai": {
                    "apiKey": config.get("local_api_key", "sharedskill-local"),
                    "baseUrl": api_url or container_api_url(config),
                    "model": "local-tts",
                    "voice": "Chelsie",
                },
                "edge": {"enabled": False},
                "elevenlabs": {"baseUrl": api_url or container_api_url(config)},
            }
        },
        "tools": {"web": {"search": {"enabled": False}}, "profile": "full"},
    }
    if config.get("alignment"):
        defaults = result["agents"]["defaults"]
        defaults["contextPruning"] = {"mode": "off"}
        defaults["compaction"] = {"mode": "safeguard", "reserveTokensFloor": int(parameters.get("max_tokens", 16384))}
        if tool_api_url:
            # Native image/PDF tools and TTS are not task-Agent requests.
            provider = json.loads(json.dumps(result["models"]["providers"]["sharedskill"]))
            provider["baseUrl"] = tool_api_url
            result["models"]["providers"]["sharedskill-tools"] = provider
            defaults["imageModel"] = {"primary": "sharedskill-tools/" + model}
            result["messages"]["tts"]["openai"]["baseUrl"] = tool_api_url
            result["messages"]["tts"]["elevenlabs"]["baseUrl"] = tool_api_url
    return result


def preflight_candidate_bank(candidate_bank: Path, candidate: dict, config: dict) -> dict:
    from scripts.wildclawbench.alignment_runtime import preflight_candidate_bank as check
    return check(candidate_bank, candidate, config)


def run_evolver(
    workspace_dir: Path, openclaw_home: Path, message: str, config: Mapping[str, Any], session_id: str
) -> subprocess.CompletedProcess[str]:
    """Run the native evolution agent in a fresh project-owned container."""
    workspace_dir, openclaw_home = Path(workspace_dir).resolve(), Path(openclaw_home).resolve()
    workspace_dir.mkdir(parents=True, exist_ok=True)
    state = openclaw_home / ".openclaw"
    state.mkdir(parents=True, exist_ok=True)
    (state / "agents" / "main" / "sessions").mkdir(parents=True, exist_ok=True)
    configured = openclaw_config(config, str(workspace_dir), api_url=container_api_url(config, run_id=session_id))
    if config.get("native_night_flow"):
        from scripts.injection.native_night_flow import runtime_options
        configured = runtime_options(configured, config)
    _write_json(state / "openclaw.json", configured)
    transcript = state / "agents" / "main" / "sessions" / (session_id + ".jsonl")
    prior_transcript = transcript.read_bytes() if transcript.exists() else b""

    name = "sharedskill-evolve-" + hashlib.sha256((str(workspace_dir) + session_id).encode()).hexdigest()[:16]
    timeout = int(config.get("evolution_timeout_seconds", 900))
    args = [
        "docker",
        "exec",
        "--workdir",
        str(workspace_dir),
        name,
        "openclaw",
        "agent",
        "--agent",
        "main",
        "--session-id",
        session_id,
        "--message",
        message,
        "--json",
        "--local",
        "--timeout",
        str(timeout),
    ]
    try:
        _run(
            [
                "docker",
                "run",
                "--detach",
                "--name",
                name,
                *provider_network_args(),
                *python_seed_args(config),
                "--mount",
                f"type=bind,src={workspace_dir},dst={workspace_dir}",
                "--mount",
                f"type=bind,src={openclaw_home},dst={openclaw_home}",
                "--env",
                "OPENCLAW_HOME=" + str(openclaw_home),
                "--env",
                "OPENCLAW_CONFIG_PATH=" + str(state / "openclaw.json"),
                "--env",
                "OPENAI_API_KEY=sharedskill-local",
                "--env",
                "ANTHROPIC_API_KEY=sharedskill-local",
                "--env",
                "OPENROUTER_API_KEY=sharedskill-local",
                "--env",
                "GEMINI_API_KEY=sharedskill-local",
                "--env",
                "GOOGLE_API_KEY=sharedskill-local",
                "--env",
                "ELEVENLABS_API_KEY=",
                "--env",
                "XI_API_KEY=",
                "--env",
                "http_proxy=",
                "--env",
                "https_proxy=",
                "--env",
                "all_proxy=",
                "--env",
                "HTTP_PROXY=",
                "--env",
                "HTTPS_PROXY=",
                "--env",
                "ALL_PROXY=",
                "--env",
                "NO_PROXY=*",
                "--env",
                "no_proxy=*",
                "--env",
                "HF_HUB_OFFLINE=1",
                "--env",
                "TRANSFORMERS_OFFLINE=1",
                TASK_IMAGE,
                "tail",
                "-f",
                "/dev/null",
            ],
            config=config,
            timeout=120,
        )
        apply_python_bootstrap(name, config, openclaw_home / "python-bootstrap.json")
        apply_local_tool_adapters(
            name, config, container_api_url(config, run_id=session_id), openclaw_home / "local-tool-adapters.json"
        )
        if config.get("alignment"):
            supervisor = Path(__file__).with_name("agent_supervisor.py")
            _run(["docker", "cp", str(supervisor), name + ":/tmp/sharedskill-agent-supervisor.py"], config=config)
            index = args.index("openclaw")
            args[index:index] = ["python3", "/tmp/sharedskill-agent-supervisor.py", "--timeout", str(timeout),
                                 "--evidence", str(openclaw_home / "agent-quiescence.json"), "--"]
        result = _run(args, config=config, timeout=timeout + 30, check=False)
        if config.get("alignment"):
            stopped = json.loads((openclaw_home / "agent-quiescence.json").read_text())
            if not stopped.get("quiescent"):
                raise RuntimeError("Evolution process tree did not stop before collecting changes")
        transcript = state / "agents" / "main" / "sessions" / (session_id + ".jsonl")
        messages = []
        if transcript.exists():
            current_transcript = transcript.read_bytes()
            if not current_transcript.startswith(prior_transcript):
                raise RuntimeError("Native continuation rewrote existing transcript history")
            # A recovered native session may contain an old error. Only this
            # invocation's appended messages determine its execution outcome.
            for line in current_transcript[len(prior_transcript):].decode().splitlines():
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("message", {}).get("role") == "assistant":
                    messages.append(entry["message"])
        active = any(
            any(
                isinstance(part, dict)
                and (
                    part.get("type") in ("toolCall", "tool_use", "tool_call")
                    or (part.get("type") == "text" and str(part.get("text", "")).strip())
                )
                for part in message.get("content", [])
            )
            for message in messages
        )
        errors = [
            message.get("errorMessage", "model returned an error")
            for message in messages
            if message.get("stopReason") == "error"
        ]
        final = messages[-1] if messages else {}
        recovered = final.get("stopReason") == "stop" and any(
            isinstance(part, dict) and part.get("type") == "text" and str(part.get("text", "")).strip()
            for part in final.get("content", [])
        )
        if result.returncode == 0 and (not active or (errors and not recovered)):
            result = subprocess.CompletedProcess(
                result.args,
                70,
                result.stdout,
                result.stderr
                + "\nEvolution infrastructure failure: "
                + ("; ".join(errors) if errors else "no actual assistant execution in transcript"),
            )
        return result
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(args, -1, exc.stdout or "", "TimeoutExpired: " + str(exc))
    finally:
        _run(["docker", "rm", "--force", name], config=config, timeout=30, check=False)


def prepare_skill_inventory(config: Mapping[str, Any]) -> dict[str, Any]:
    """Reproduce image skill hashes and eligible catalog without calling a model."""
    p = paths(config)
    manifest = p["assets"] / "image-skill-inventory.json"
    image = json.loads(_run(["docker", "image", "inspect", TASK_IMAGE], config=config).stdout)[0]
    names = list(
        config.get(
            "initial_skill_names",
            [
                "03_task1",
                "03_task2",
                "03_task3",
                "03_task4",
                "03_task5",
                "03_task6",
                "agent-browser",
                "self-improving-agent-3.0.5",
                "video-frames",
            ],
        )
    )
    existing = json.loads(manifest.read_text()) if manifest.exists() else {}
    if (
        existing.get("inventory_version") == 2
        and existing.get("task_image_id") == image["Id"]
        and existing.get("shared_bank_names") == names
    ):
        return {
            "manifest": str(manifest),
            "sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
            "image_helpers": 3,
            "bundled_skill_directories": 53,
            "eligible_catalog_entries": len(existing["eligible_catalog_entries"]),
            "cached": True,
        }
    if config.get("shared_runtime_dir"):
        raise RuntimeError("Shared skill inventory is missing or does not match this experiment")
    name = "sharedskill-inventory-" + uuid_token()
    try:
        _run(
            [
                "docker",
                "run",
                "--detach",
                "--name",
                name,
                *provider_network_args(),
                "--env",
                "OPENAI_API_KEY=sharedskill-local",
                "--env",
                "OPENROUTER_API_KEY=sharedskill-local",
                "--env",
                "ANTHROPIC_API_KEY=sharedskill-local",
                "--env",
                "GEMINI_API_KEY=sharedskill-local",
                "--env",
                "GOOGLE_API_KEY=sharedskill-local",
                "--env",
                "ELEVENLABS_API_KEY=",
                "--env",
                "XI_API_KEY=",
                TASK_IMAGE,
                "tail",
                "-f",
                "/dev/null",
            ],
            config=config,
            timeout=120,
        )
        script = """import json,hashlib
from pathlib import Path
entries=[]
roots=[('image_shared_helpers','/root/skills'),('openclaw_bundled','/usr/lib/node_modules/openclaw/skills')]
for kind,directory in roots:
    for skill in sorted(Path(directory).glob('*/SKILL.md')):
        files=[]
        for path in sorted(skill.parent.rglob('*')):
            if path.is_file():
                files.append({'path':str(path.relative_to(skill.parent)),'size':path.stat().st_size,
                              'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
        entries.append({'kind':kind,'directory':str(skill.parent),'files':files})
print(json.dumps(entries))
"""
        directories = json.loads(
            _run(["docker", "exec", name, "python3", "-c", script], config=config, timeout=60).stdout
        )
        for skill in names:
            _run(
                ["docker", "cp", str(p["upstream"] / "skills" / skill), name + ":/root/skills/"],
                config=config,
                timeout=60,
            )
        temporary = p["runtime"] / "tmp" / (name + ".json")
        _write_json(temporary, openclaw_config(config, "/tmp_workspace"))
        _run(["docker", "exec", name, "mkdir", "-p", "/tmp_workspace"], config=config)
        _run(["docker", "cp", str(temporary), name + ":/root/.openclaw/openclaw.json"], config=config)
        catalog = json.loads(
            _run(["docker", "exec", name, "openclaw", "skills", "list", "--json"], config=config, timeout=60).stdout
        )
        record = {
            "inventory_version": 2,
            "task_image": TASK_IMAGE,
            "task_image_id": image["Id"],
            "raw_skill_directories": directories,
            "shared_bank_names": names,
            "shared_bank_size": len(names),
            "catalog": catalog,
            "eligible_catalog_entries": [s for s in catalog["skills"] if s.get("eligible") and not s.get("disabled")],
            "observed_catalog_entries": existing.get("observed_catalog_entries", []),
            "note": (
                "Configured repository skills are evolvable. Image-installed helpers and eligible OpenClaw "
                "bundled skills are frozen environment resources."
            ),
        }
        _write_json(manifest, record)
        temporary.unlink(missing_ok=True)
    finally:
        _run(["docker", "rm", "--force", name], config=config, timeout=30, check=False)
    return {
        "manifest": str(manifest),
        "sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "image_helpers": sum(s["kind"] == "image_shared_helpers" for s in record["raw_skill_directories"]),
        "bundled_skill_directories": sum(s["kind"] == "openclaw_bundled" for s in record["raw_skill_directories"]),
        "eligible_catalog_entries": len(record["eligible_catalog_entries"]),
        "cached": False,
    }


def uuid_token() -> str:
    import uuid

    return uuid.uuid4().hex[:16]


def prepare(config: Mapping[str, Any]) -> dict[str, Any]:
    """Prepare pinned sources/assets and dedicated runtime; returns readiness."""
    result: dict[str, Any] = {"status": "preparing", "sources": prepare_sources(config)}
    result["assets"] = prepare_assets(config)
    result["docker"] = prepare_docker(config)
    result["docker"]["blocked_paid_model_hosts"] = list(BLOCKED_MODEL_HOSTS)
    seed_source = python_bootstrap_source(config)
    result["python_bootstrap"] = {
        "source": str(seed_source),
        "seed": 42,
        "sha256": hashlib.sha256(seed_source.read_bytes()).hexdigest(),
        "environment_args": python_seed_args(config),
    }
    result["image_skills"] = prepare_skill_inventory(config)
    result["supplemental_assets"] = prepare_supplemental_assets(config)
    result["status"] = (
        "ready" if all(x["status"] == "ready" for x in result["supplemental_assets"].values()) else "pending_assets"
    )
    _write_json(paths(config)["runtime"] / "environment.json", result)
    return result


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--config")
    parser.add_argument(
        "--stage",
        choices=["all", "sources", "assets", "workspace", "videos", "supplemental", "daemon", "docker"],
        default="all",
    )
    args = parser.parse_args()
    conf = json.loads(Path(args.config).read_text()) if args.config else {}
    fn = {
        "all": prepare,
        "sources": prepare_sources,
        "assets": prepare_assets,
        "workspace": lambda c: prepare_assets(c, True),
        "supplemental": prepare_supplemental_assets,
        "videos": lambda c: prepare_supplemental_assets(c, True),
        "daemon": lambda c: prepare_docker(c, False),
        "docker": prepare_docker,
    }[args.stage]
    print(json.dumps(fn(conf), indent=2, ensure_ascii=False))
