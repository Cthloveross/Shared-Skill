"""Frozen protocol, task identities, content hashes and validation decisions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from urllib.parse import urlparse

import yaml

ROOT = Path(__file__).resolve().parent
REPOSITORY = ROOT.parents[2]


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    os.replace(temporary, path)


def canonical_hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def tree_hash(directory: Path) -> str:
    """Hash paths and bytes, rejecting symlinks that could escape a published bank."""
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Symlink in immutable skill bank: {path}")
        if path.is_file():
            relative = path.relative_to(directory).as_posix().encode()
            data = path.read_bytes()
            digest.update(len(relative).to_bytes(8, "big") + relative)
            digest.update(len(data).to_bytes(8, "big") + data)
    return digest.hexdigest()


def load_config(path: Path | None = None) -> dict:
    config = json.loads((path or ROOT / "config.json").read_text())
    local = ROOT / "local.json"
    if local.exists():
        config.update(json.loads(local.read_text()))
    config["experiment_root"] = str(ROOT)
    config["runtime_dir"] = str(ROOT / "runtime")
    config["llm_base_url"] = config["local_api_base_url"]
    config["llm_model"] = config["model_id"]
    config["llm_max_tokens"] = config["max_tokens"]
    config["llm_temperature"] = config["temperature"]
    config["agent_timeout"] = config["evolution_timeout_seconds"]
    inventory = ROOT / "runtime/assets/image-skill-inventory.json"
    if inventory.exists():
        image_skills = json.loads(inventory.read_text())
        config["environment_skill_names"] = sorted(
            {Path(item["directory"]).name for item in image_skills["raw_skill_directories"]}
        )
        config["image_skill_inventory_sha256"] = hashlib.sha256(inventory.read_bytes()).hexdigest()
        config["task_image_id"] = image_skills["task_image_id"]
    if config["paid_api_budget_usd"] != 0:
        raise ValueError("This protocol requires zero paid API budget")
    for key in ("model_base_url", "local_api_base_url", "audio_base_url", "image_base_url"):
        parsed = urlparse(config[key])
        if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError(f"{key} must be a local loopback service URL")
        if parsed.username or parsed.password:
            raise ValueError("Keep API credentials outside URLs")
    if (config["days"], config["nights"], config["users"]) != (6, 6, 8):
        raise ValueError("Protocol is fixed at six days/nights and eight users")
    return config


def public_config(config: dict) -> dict:
    return {
        k: v
        for k, v in config.items()
        if not any(token in k.lower() for token in ("api_key", "token", "password"))
        or k in {"max_tokens", "llm_max_tokens"}
    }


def source_directory(config: dict) -> Path:
    prepared = Path(config["runtime_dir"]) / "upstream/WildClawBench"
    if (prepared / "tasks").is_dir():
        return prepared
    reference = REPOSITORY / "reproduction/reference/wildclawbench/benchmark-code"
    if (reference / "tasks").is_dir():
        return reference
    raise RuntimeError("Pinned benchmark sources are missing; run prepare first")


def discover_tasks(config: dict) -> list[dict]:
    source = source_directory(config)
    entries = []
    for path in sorted((source / "tasks").glob("*/*.md")):
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", path.read_text(), re.S)
        if not match:
            raise ValueError(f"Missing task metadata: {path}")
        metadata = yaml.safe_load(match[1])
        task_id = metadata["id"]
        number = re.search(r"_task_(\d+)_", task_id)
        if not number:
            raise ValueError(task_id)
        short_id = f"{task_id[:2]}.{int(number[1]):02d}"
        entries.append(
            {
                "task_id": task_id,
                "short_id": short_id,
                "category": path.parent.name,
                "source_path": str(path),
                "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "timeout_seconds": int(metadata["timeout_seconds"]),
                "local_variant": short_id == "02.06" and config["ocr_local_variant"],
            }
        )
    entries.sort(key=lambda t: t["short_id"])
    if len(entries) != 60 or len({t["task_id"] for t in entries}) != 60:
        raise ValueError("All 60 distinct pinned tasks are required")
    for index, task in enumerate(entries):
        task["user_id"] = f"user-{index % config['users'] + 1:02d}"
    return entries


def create_initial_bank(config: dict, destination: Path) -> str:
    source = source_directory(config) / "skills"
    temporary = destination.with_name(destination.name + ".building")
    if temporary.exists():
        shutil.rmtree(temporary)
    temporary.mkdir(parents=True)
    for name in config["initial_skill_names"]:
        original = source / name
        if not (original / "SKILL.md").is_file():
            raise ValueError(f"Public initial skill missing: {name}")
        shutil.copytree(original, temporary / name)
    proposed_hash = tree_hash(temporary)
    if destination.exists():
        if tree_hash(destination) != proposed_hash:
            raise ValueError("Refusing to replace the initial bank with different bytes")
        shutil.rmtree(temporary)
    else:
        os.replace(temporary, destination)
    return proposed_hash


def accept_candidate(old: list[dict], new: list[dict], preference: str, tolerance: float = 1e-6) -> dict:
    """Predeclared local operationalization; never treat missing grades as zero."""
    if {r["task_id"] for r in old} != {r["task_id"] for r in new} or not old:
        return {"accepted": False, "reason": "mismatched_or_empty_related_tasks"}
    baseline = {r["task_id"]: r for r in old}
    if any(r.get("score") is None or r.get("status") != "completed" for r in old + new):
        return {"accepted": False, "reason": "validation_incomplete", "pending": True}
    differences = [r["score"] - baseline[r["task_id"]]["score"] for r in new]
    if any(delta < -tolerance for delta in differences):
        return {"accepted": False, "reason": "related_task_regression", "score_deltas": differences}
    quality_gain = sum(differences) > tolerance
    stability_old = (sum(r.get("timed_out", False) for r in old), sum(r.get("tool_errors", 0) for r in old))
    stability_new = (sum(r.get("timed_out", False) for r in new), sum(r.get("tool_errors", 0) for r in new))
    stability_gain = all(n <= o for n, o in zip(stability_new, stability_old)) and stability_new != stability_old
    accepted = preference == "candidate" and (quality_gain or stability_gain)
    return {
        "accepted": accepted,
        "reason": "improved" if accepted else "tie_or_comparator_rejected",
        "score_deltas": differences,
        "quality_gain": quality_gain,
        "stability_gain": stability_gain,
        "model_preference": preference,
    }
