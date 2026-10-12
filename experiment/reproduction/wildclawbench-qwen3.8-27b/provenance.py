"""Freeze original task input bytes and reject changed inputs before execution."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from protocol import canonical_hash, discover_tasks, load_config, write_json

ROOT = Path(__file__).resolve().parent
VERSION = 1
GENERATED_CACHE = {"__pycache__", ".cache", ".pytest_cache", ".ruff_cache"}
_LOADED: dict[Path, tuple[tuple[int, int, int], dict]] = {}


def _paths(config: Mapping[str, Any]) -> tuple[Path, Path, Path]:
    runtime = Path(config.get("runtime_dir", ROOT / "runtime")).resolve()
    return runtime, runtime / "upstream/WildClawBench", runtime / "prepared/input-files.json"


def _fingerprint(value: os.stat_result) -> dict[str, int]:
    return {
        "size": value.st_size,
        "mtime_ns": value.st_mtime_ns,
        "ctime_ns": value.st_ctime_ns,
        "mode": value.st_mode,
        "device": value.st_dev,
        "inode": value.st_ino,
    }


def _workspace_relative(task: Mapping[str, Any]) -> str:
    path = Path(task["source_path"])
    data = path.read_bytes()
    if task.get("source_sha256") and hashlib.sha256(data).hexdigest() != task["source_sha256"]:
        raise RuntimeError(f"Task source changed: {task['task_id']}")
    match = re.search(r"## Workspace Path\s*\n\s*```[^\n]*\n([^\n]+)", data.decode())
    if not match:
        raise RuntimeError(f"Task has no workspace declaration: {task['task_id']}")
    declared = Path(match[1].strip())
    if declared.is_absolute() or ".." in declared.parts or declared.parts[:1] != ("workspace",):
        raise RuntimeError(f"Unsafe task workspace declaration: {task['task_id']}")
    return declared.relative_to("workspace").as_posix()


def _listed_files(runtime: Path) -> dict[str, dict]:
    path = runtime / "assets/workspace-file-manifest.json"
    if not path.is_file():
        raise RuntimeError("Official workspace file listing is missing; prepare assets first")
    result = {}
    for item in json.loads(path.read_text()):
        rel = Path(item["path"])
        if rel.is_absolute() or ".." in rel.parts or rel.parts[:1] != ("workspace",):
            raise RuntimeError("Unsafe official workspace file listing")
        key = rel.relative_to("workspace").as_posix()
        if key in result:
            raise RuntimeError(f"Duplicate official input path: {key}")
        result[key] = item
    return result


def _scan(workspace: Path, listed: Mapping[str, Any], subtree: str = "") -> tuple[dict, dict, list[str]]:
    """Include hidden .git files and official caches; omit only unlisted caches."""
    base = workspace / subtree
    files, directories, excluded = {}, {}, []
    if base.is_symlink():
        raise RuntimeError(f"Symlink in original task input: {base}")
    if not base.exists():
        return files, directories, excluded
    if not base.is_dir() or base.is_symlink():
        raise RuntimeError(f"Unsupported task input directory: {base}")
    for directory, names, filenames in os.walk(base, followlinks=False):
        names.sort()
        current = Path(directory)
        relative = current.relative_to(workspace).as_posix()
        directories[relative] = {"relpath": relative, "mode": current.stat().st_mode}
        for name in sorted(names + filenames):
            path = current / name
            rel = path.relative_to(workspace).as_posix()
            value = path.lstat()
            if stat.S_ISLNK(value.st_mode):
                raise RuntimeError(f"Symlink in original task input: {rel}")
            if not stat.S_ISREG(value.st_mode):
                if not stat.S_ISDIR(value.st_mode):
                    raise RuntimeError(f"Unsupported original task input type: {rel}")
                continue
            if name.endswith((".partial", ".part", ".ytdl", ".tmp")) or re.search(r"\.f\d+\.(mp4|m4a|webm)$", name):
                if rel not in listed:
                    raise RuntimeError(f"Incomplete input download remains: {rel}")
            generated = any(part in GENERATED_CACHE for part in path.relative_to(workspace).parts)
            if rel not in listed and (generated or name.endswith((".pyc", ".pyo"))):
                excluded.append(rel)
                continue
            files[rel] = _fingerprint(value)
    # Empty unlisted cache directories do not affect task inputs.
    directories = {
        rel: value
        for rel, value in directories.items()
        if not any(part in GENERATED_CACHE for part in Path(rel).parts)
        or any(name.startswith(rel.rstrip("/") + "/") for name in files)
    }
    return files, directories, sorted(excluded)


def _hash_file(path: Path, fingerprint: dict, official: dict | None) -> dict:
    sha256, blob = hashlib.sha256(), None
    if official:
        if fingerprint["size"] != int(official["size"]):
            raise RuntimeError(f"Official input size mismatch: {official['path']}")
        if not official.get("lfs") and official.get("oid"):
            blob = hashlib.sha1(b"blob " + str(fingerprint["size"]).encode() + b"\0", usedforsecurity=False)
    with path.open("rb") as stream:
        if _fingerprint(os.fstat(stream.fileno())) != fingerprint:
            raise RuntimeError(f"Input changed before hashing: {path.name}")
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            sha256.update(chunk)
            if blob is not None:
                blob.update(chunk)
        if _fingerprint(os.fstat(stream.fileno())) != fingerprint:
            raise RuntimeError(f"Input changed while hashing: {path.name}")
    if _fingerprint(path.stat()) != fingerprint:
        raise RuntimeError(f"Input was replaced while hashing: {path.name}")
    result = {"sha256": sha256.hexdigest(), "origin": "hf_dataset" if official else "supplemental"}
    if official:
        expected = official.get("lfs", {}).get("oid") or official.get("oid")
        actual = result["sha256"] if official.get("lfs") else blob.hexdigest() if blob else None
        if expected and actual != expected:
            raise RuntimeError(f"Official input content mismatch: {official['path']}")
        result["upstream_integrity"] = {
            "algorithm": "sha256" if official.get("lfs") else "git_blob_sha1",
            "expected": expected,
            "verified": bool(expected and actual == expected),
        }
    return result


def _identity(files: list[dict], directories: list[dict], *, exists: bool = True) -> str:
    return canonical_hash(
        {
            "exists": exists,
            "directories": sorted(directories, key=lambda value: value["relpath"]),
            "files": [
                {key: file[key] for key in ("relpath", "size", "mode", "sha256")}
                for file in sorted(files, key=lambda value: value["relpath"])
            ],
        }
    )


def prepare_input_manifest(config: Mapping[str, Any], tasks: list[dict]) -> list[dict]:
    """Hash all original workspace inputs, verify HF objects, and return augmented tasks."""
    began = time.monotonic()
    runtime, source, destination = _paths(config)
    workspace = (source / "workspace").resolve()
    if not workspace.is_dir():
        raise RuntimeError("Pinned runtime workspace is missing; prepare assets first")
    listed = _listed_files(runtime)
    fingerprints, directories, excluded = _scan(workspace, listed)
    missing = sorted(set(listed) - set(fingerprints))
    if missing:
        raise RuntimeError(f"Official inputs missing ({len(missing)}): {missing[:5]}")
    previous = json.loads(destination.read_text()) if destination.exists() else {}
    compatible = (
        previous.get("version") == VERSION
        and previous.get("workspace_root") == str(workspace)
        and previous.get("dataset_revision") == config.get("dataset_revision")
    )
    cache = {file["relpath"]: file for file in previous.get("files", [])} if compatible else {}
    files, reused, bytes_hashed = [], 0, 0
    for rel, fingerprint in sorted(fingerprints.items()):
        cached = cache.get(rel)
        official = listed.get(rel)
        if official and fingerprint["size"] != int(official["size"]):
            raise RuntimeError(f"Official input size mismatch: {official['path']}")
        expected = official.get("lfs", {}).get("oid") or official.get("oid") if official else None
        if (
            cached
            and all(cached.get(key) == value for key, value in fingerprint.items())
            and (
                not official
                or cached.get("upstream_integrity", {}).get("expected") == expected
                and cached.get("upstream_integrity", {}).get("verified")
            )
        ):
            record = cached
            reused += 1
        else:
            record = {"relpath": rel, **fingerprint, **_hash_file(workspace / rel, fingerprint, official)}
            bytes_hashed += fingerprint["size"]
        files.append(record)
    augmented, task_records = [], {}
    for task in tasks:
        rel = _workspace_relative(task)
        selected = [file for file in files if file["relpath"].startswith(rel.rstrip("/") + "/")]
        dirs = [value for name, value in sorted(directories.items()) if name == rel or name.startswith(rel + "/")]
        record = {
            "workspace_relative_path": rel,
            "workspace_exists": (workspace / rel).is_dir(),
            "files": [file["relpath"] for file in selected],
            "directories": dirs,
            "input_sha256": _identity(selected, dirs, exists=(workspace / rel).is_dir()),
            "file_count": len(selected),
            "bytes": sum(file["size"] for file in selected),
        }
        task_records[task["task_id"]] = record
        augmented.append(
            {
                **task,
                "input_sha256": record["input_sha256"],
                "input_file_count": record["file_count"],
                "input_bytes": record["bytes"],
                "input_workspace": str(workspace / rel),
            }
        )
    manifest = {
        "version": VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "benchmark_revision": config.get("benchmark_revision"),
        "dataset_revision": config.get("dataset_revision"),
        "workspace_root": str(workspace),
        "workspace_sha256": _identity(files, list(directories.values())),
        "scope": "All original task workspace files, including exec, tmp, gt and hidden Git histories.",
        "files": files,
        "directories": list(directories.values()),
        "tasks": task_records,
        "excluded_generated_cache_files": excluded,
        "statistics": {
            "task_count": len(tasks),
            "file_count": len(files),
            "bytes": sum(f["size"] for f in files),
            "hf_objects_verified": sum(f.get("upstream_integrity", {}).get("verified", False) for f in files),
            "files_reused_from_stat_cache": reused,
            "bytes_hashed": bytes_hashed,
            "seconds": time.monotonic() - began,
        },
    }
    # A second stat traversal prevents accepting input changes during a long hashing pass.
    after, after_dirs, after_excluded = _scan(workspace, listed)
    if after != fingerprints or after_dirs != directories or after_excluded != excluded:
        raise RuntimeError("Original task inputs changed during manifest preparation")
    manifest["statistics"]["seconds"] = time.monotonic() - began
    history = previous.get("preparation_history", []) if compatible else []
    if compatible and not history and previous.get("statistics"):
        history = [{"created_at": previous.get("created_at"), **previous["statistics"]}]
    manifest["preparation_history"] = [*history, {"created_at": manifest["created_at"], **manifest["statistics"]}]
    write_json(destination, manifest)
    _LOADED.pop(destination, None)
    return augmented


def validate_task_inputs(config: Mapping[str, Any], task: Mapping[str, Any]) -> dict:
    """Reject added, removed, replaced, or stat-changed originals before each task."""
    runtime, source, path = _paths(config)
    if not path.is_file():
        raise RuntimeError("Input provenance manifest is missing; prepare inputs first")
    value = path.stat()
    stamp = (value.st_mtime_ns, value.st_ctime_ns, value.st_size)
    cached = _LOADED.get(path)
    if not cached or cached[0] != stamp:
        _LOADED[path] = (stamp, json.loads(path.read_text()))
    manifest = _LOADED[path][1]
    workspace = (source / "workspace").resolve()
    record = manifest.get("tasks", {}).get(task["task_id"])
    if not record or manifest.get("workspace_root") != str(workspace):
        raise RuntimeError(f"Missing or mismatched input provenance: {task['task_id']}")
    if manifest.get("version") != VERSION or any(
        manifest.get(key) != config.get(key) for key in ("benchmark_revision", "dataset_revision")
    ):
        raise RuntimeError(f"Input provenance revision mismatch: {task['task_id']}")
    if _workspace_relative(task) != record["workspace_relative_path"]:
        raise RuntimeError(f"Task workspace declaration changed: {task['task_id']}")
    if not task.get("input_sha256") or task["input_sha256"] != record["input_sha256"]:
        raise RuntimeError(f"Frozen task input hash mismatch: {task['task_id']}")
    names = set(record["files"])
    files = [file for file in manifest["files"] if file["relpath"] in names]
    if _identity(files, record["directories"], exists=record["workspace_exists"]) != record["input_sha256"]:
        raise RuntimeError(f"Corrupt task input manifest: {task['task_id']}")
    current, directories, _ = _scan(workspace, _listed_files(runtime), record["workspace_relative_path"])
    expected = {
        file["relpath"]: {key: file[key] for key in _fingerprint((workspace / file["relpath"]).stat())}
        for file in files
        if (workspace / file["relpath"]).exists()
    }
    original = {
        file["relpath"]: {key: file[key] for key in ("size", "mtime_ns", "ctime_ns", "mode", "device", "inode")}
        for file in files
    }
    if (
        current != original
        or expected != original
        or directories != {value["relpath"]: value for value in record["directories"]}
        or ((workspace / record["workspace_relative_path"]).is_dir() != record["workspace_exists"])
    ):
        changed = sorted(
            set(current) ^ set(original)
            | {rel for rel in set(current) & set(original) if current[rel] != original[rel]}
        )
        raise RuntimeError(f"Original inputs changed for {task['task_id']}: {changed[:5] or ['directory state']}")
    return {"validated": True, "input_sha256": record["input_sha256"], "file_count": len(files)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    config = load_config(args.config)
    tasks = prepare_input_manifest(config, discover_tasks(config))
    _, _, destination = _paths(config)
    manifest = json.loads(destination.read_text())
    print(
        json.dumps(
            {
                "manifest": str(destination),
                **manifest["statistics"],
                "workspace_sha256": manifest["workspace_sha256"],
                "task_count": len(tasks),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
