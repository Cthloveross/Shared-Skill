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
REPOSITORY = ROOT.parents[1]
DEFAULT_CONFIG = REPOSITORY / "experiments/wildclawbench-qwen3.8-27b/config.json"
ALIGNMENT_VERSION = "native-night-atomic-v1"


def alignment_settings(config: dict) -> dict | None:
    """An explicit version keeps new experiments separate from immutable legacy runs."""
    if "alignment" not in config:
        return None
    value = config["alignment"]
    if not isinstance(value, dict) or value.get("version") != ALIGNMENT_VERSION:
        raise ValueError("Unknown alignment protocol version")
    policies = {
        "source_policy": "native-history-explicit-day-sources-v1",
        "acceptance_policy": "related-source-nonregression-quality-or-stability-v1",
        "compaction_mode": "safeguard",
        "context_pruning": "off",
    }
    for key, expected in policies.items():
        if key in value and value[key] != expected:
            raise ValueError(f"Unsupported alignment {key}")
    if "native_candidate_scope" in value and value["native_candidate_scope"] != "per-skill-v1":
        raise ValueError("Unsupported native candidate scope")
    return {**policies, **value}


def alignment_candidate_id(candidate: dict, config: dict) -> str:
    settings = alignment_settings(config)
    if not settings or not config.get("protocol_hash"):
        raise ValueError("Atomic candidates require a frozen alignment protocol hash")
    identity = {
        "alignment": settings,
        "protocol_hash": config["protocol_hash"],
        "parent": candidate["source_bank_sha256"],
        "bundles": [{key: bundle[key] for key in (
            "name", "action", "bundle_sha256", "native_tree_sha256", "files"
        )} for bundle in candidate["bundles"]],
        "source_task_ids": candidate["source_task_ids"],
        "source_session_ids": candidate["source_session_ids"],
        "source_evidence": candidate["source_evidence"],
    }
    from scripts.injection import resilient_runner
    if resilient_runner.enabled(config):
        identity["source_refs"] = resilient_runner.validate_typed_sources(candidate)
    if settings.get("native_candidate_scope") == "per-skill-v1":
        identity["dependency_contract"] = candidate.get("dependency_contract")
    return canonical_hash(identity)[:24]


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


def _read_verified_task_manifest(config: dict) -> bytes:
    manifest_path = Path(config["task_manifest"])
    if not manifest_path.is_absolute():
        manifest_path = Path(config["experiment_root"]) / manifest_path
    raw = manifest_path.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    declared = config.get("task_manifest_sha256")
    if declared is not None and declared != actual:
        raise ValueError("Task manifest bytes differ from task_manifest_sha256")
    # Preserve compatibility with older configurations that did not declare a
    # digest.  Once read, later reads must still match these exact bytes.
    config["task_manifest_sha256"] = actual
    return raw


def load_config(path: Path | None = None) -> dict:
    config_path = (path or DEFAULT_CONFIG).resolve()
    config = json.loads(config_path.read_text())
    required = {"model_id", "model_base_url", "local_api_base_url", "audio_base_url", "image_base_url",
                "max_tokens", "temperature", "evolution_timeout_seconds", "paid_api_budget_usd", "users"}
    local = config_path.parent / "local.json"
    if local.exists():
        config.update(json.loads(local.read_text()))
    missing = sorted(required - config.keys())
    if missing:
        raise ValueError("Missing required experiment configuration fields: " + ", ".join(missing))
    experiment_root = Path(config.get("experiment_root", config_path.parent))
    experiment_root = (
        experiment_root if experiment_root.is_absolute() else config_path.parent / experiment_root
    ).resolve()
    config["experiment_root"] = str(experiment_root)
    if config.get("task_manifest"):
        _read_verified_task_manifest(config)
    runtime = Path(config.get("runtime_dir", REPOSITORY / "runtime"))
    config["runtime_dir"] = str((runtime if runtime.is_absolute() else experiment_root / runtime).resolve())
    if config.get("shared_runtime_dir"):
        cache = Path(config["shared_runtime_dir"])
        config["shared_runtime_dir"] = str((cache if cache.is_absolute() else experiment_root / cache).resolve())
    for field, default in (("artifacts_dir", REPOSITORY / "artifacts"), ("outputs_dir", REPOSITORY / "outputs")):
        value = Path(config.get(field, default))
        config[field] = str((value if value.is_absolute() else experiment_root / value).resolve())
    config["config_path"] = str(config_path)
    config.pop("source_worktree", None)  # The engine is the installed repository package.
    config["llm_base_url"] = config["local_api_base_url"]
    config["llm_model"] = config["model_id"]
    config["llm_max_tokens"] = config["max_tokens"]
    config["llm_temperature"] = config["temperature"]
    config["agent_timeout"] = config["evolution_timeout_seconds"]
    inventory = Path(config.get("shared_runtime_dir", config["runtime_dir"])) / "assets/image-skill-inventory.json"
    if inventory.exists():
        image_skills = json.loads(inventory.read_text())
        config["environment_skill_names"] = sorted(
            {Path(item["directory"]).name for item in image_skills["raw_skill_directories"]}
        )
        config["image_skill_inventory_sha256"] = hashlib.sha256(inventory.read_bytes()).hexdigest()
        config["task_image_id"] = image_skills["task_image_id"]
    external_judge = config.get("external_judge") is True
    if config["paid_api_budget_usd"] != 0 and not external_judge:
        raise ValueError("A paid budget requires an explicitly configured external judge")
    if external_judge:
        judge_url = urlparse(config["judge_base_url"])
        if judge_url.scheme != "https" or not judge_url.hostname or judge_url.username or judge_url.password:
            raise ValueError("External judge URL must use HTTPS without embedded credentials")
    for key in ("model_base_url", "local_api_base_url", "audio_base_url", "image_base_url"):
        parsed = urlparse(config[key])
        if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError(f"{key} must be a local loopback service URL")
        if parsed.username or parsed.password:
            raise ValueError("Keep API credentials outside URLs")
    rounds = config.get("rounds", config.get("days"))
    if isinstance(rounds, bool) or not isinstance(rounds, int) or rounds <= 0:
        raise ValueError("rounds/days must be a positive integer")
    if any(isinstance(config.get(key, rounds), bool) or not isinstance(config.get(key, rounds), int)
           or config.get(key, rounds) != rounds for key in ("days", "nights")):
        raise ValueError("days, nights and rounds must agree")
    users = config.get("users")
    if isinstance(users, bool) or not isinstance(users, int) or users <= 0:
        raise ValueError("users must be a positive integer")
    config.update(rounds=rounds, days=rounds, nights=rounds)
    expected_endpoint = f"S{rounds}"
    if any(config.get(key, expected_endpoint) != expected_endpoint for key in ("endpoint", "endpoint_snapshot")):
        raise ValueError("endpoint_snapshot must select the final configured round")
    config["endpoint_snapshot"] = expected_endpoint
    alignment_settings(config)
    return config


def fixed_execution_count(tasks: list[dict], config: dict) -> int:
    if config.get("execution_plan") == "longitudinal-day-night-text-endpoint-v1":
        return len(tasks) * int(config.get("rounds", config.get("days", 6)))
    return len(tasks) * (int(config.get("rounds", config.get("days", 6))) + 2)


def output_root(run_dir: Path, config: dict | None = None) -> Path:
    if config is None:
        frozen = run_dir / "frozen-protocol.json"
        config = json.loads(frozen.read_text()).get("config", {}) if frozen.exists() else {}
    return Path(config["output_run_dir"]) if config.get("output_run_dir") else run_dir


def execution_root(run_dir: Path, config: dict | None = None) -> Path:
    return run_dir / "executions"


def public_config(config: dict) -> dict:
    return {
        k: v
        for k, v in config.items()
        if not any(token in k.lower() for token in ("api_key", "token", "password"))
        or k in {
            "max_tokens", "llm_max_tokens", "judge_max_tokens",
            "agent_tokenizer_url", "agent_tokenizer_timeout_seconds",
        }
    }


def source_directory(config: dict) -> Path:
    prepared = Path(config.get("shared_runtime_dir", config["runtime_dir"])) / "upstream/WildClawBench"
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
                "local_variant": short_id == "02.06" and config.get("ocr_local_variant", False),
            }
        )
    entries.sort(key=lambda t: t["short_id"])
    if len(entries) != 60 or len({t["task_id"] for t in entries}) != 60:
        raise ValueError("All 60 distinct pinned tasks are required")
    if config.get("task_manifest"):
        raw = _read_verified_task_manifest(config)
        manifest = json.loads(raw)
        selected = manifest.get("tasks", manifest.get("task_short_ids", [])) if isinstance(manifest, dict) else manifest
        short_ids = [item["short_id"] if isinstance(item, dict) else item for item in selected]
        if len(short_ids) != len(set(short_ids)) or not short_ids:
            raise ValueError("Task subset must contain distinct task identities")
        official = {task["short_id"]: task for task in entries}
        if set(short_ids) - set(official):
            raise ValueError("Task subset contains identities outside the pinned official pool")
        if len(short_ids) != int(config.get("expected_task_count", 30)):
            raise ValueError("Task subset size does not match expected_task_count")
        for item in selected:
            if isinstance(item, dict):
                for key in ("task_id", "source_sha256", "timeout_seconds", "category"):
                    if key in item and item[key] != official[item["short_id"]][key]:
                        raise ValueError(f"Task subset differs from official {key}: {item['short_id']}")
        entries = [task for task in entries if task["short_id"] in set(short_ids)]
    for index, task in enumerate(entries):
        task["user_id"] = f"user-{index % config['users'] + 1:02d}"
    expected_fixed = fixed_execution_count(entries, config)
    if config.get("fixed_execution_count", expected_fixed) != expected_fixed:
        raise ValueError("Fixed execution count does not match tasks, days and endpoint arms")
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
