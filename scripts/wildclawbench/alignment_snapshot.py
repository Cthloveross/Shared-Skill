"""Seal Agent output before grading and authenticate replay-only score recovery.

The workspace copy excludes the ground truth injected for graders. Mock GET
responses become available during the first grading attempt, so they have a
separate immutable seal; this is a response cache, not a saved mock process.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from urllib.parse import urlsplit

POLICY = "agent-output-and-mock-replay-v1"
SUPERVISED_DAY_EXECUTION_VERSION = "agent-supervisor-frozen-output-v1"
AGENT_MANIFEST = "agent-output-manifest.json"
RECOVERY_MANIFEST = "grading-recovery-manifest.json"
_EVIDENCE = (
    "chat.jsonl",
    "execution-binding.json",
    "supervised-day-binding.json",
    "agent-quiescence.json",
    "agent-deadline.json",
    "agent.stdout",
    "agent.stderr",
    "session.json",
    "task-variant.json",
    "execution-prompt.txt",
    "task-prompt.txt",
    "automated-checks.py",
    "shared-skill-names.json",
    "openclaw-config.json",
    "local-tools.md",
)


class SnapshotError(ValueError):
    pass


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _read(path):
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise SnapshotError("Missing or invalid snapshot metadata: " + path.name) from error
    if not isinstance(value, dict):
        raise SnapshotError("Snapshot metadata must be an object: " + path.name)
    return value


def _seal(path, value):
    document = {**value, "manifest_sha256": _digest(value)}
    encoded = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    except FileExistsError:
        if path.read_bytes() != encoded:
            raise SnapshotError("Refusing to replace immutable snapshot: " + path.name)
    else:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    return document


def _record(path, relative):
    before = path.lstat()
    row = {"path": relative, "mode": stat.S_IMODE(before.st_mode)}
    if stat.S_ISLNK(before.st_mode):
        row.update(kind="symlink", target=os.readlink(path))
    elif stat.S_ISDIR(before.st_mode):
        row["kind"] = "directory"
    elif stat.S_ISREG(before.st_mode):
        with path.open("rb") as stream:
            row.update(kind="file", bytes=before.st_size, sha256=hashlib.file_digest(stream, "sha256").hexdigest())
    else:
        raise SnapshotError("Unsupported snapshot entry: " + relative)
    after = path.lstat()
    if any(
        getattr(before, key) != getattr(after, key)
        for key in ("st_ino", "st_dev", "st_size", "st_mode", "st_mtime_ns", "st_ctime_ns")
    ):
        raise SnapshotError("Output changed while reading: " + relative)
    return row


def _agent_files(output, identity):
    workspace = output / "workspace"
    if workspace.is_symlink() or not workspace.is_dir():
        raise SnapshotError("Missing original Agent workspace snapshot")
    rows = [_record(workspace, "workspace")]
    for parent, directories, files in os.walk(workspace, followlinks=False):
        parent = Path(parent)
        if parent == workspace:
            directories[:] = [name for name in directories if name != "gt"]
            files = [name for name in files if name != "gt"]
        directories.sort()
        for name in sorted([*directories, *files]):
            path = parent / name
            rows.append(_record(path, path.relative_to(output).as_posix()))
    supervised_day = identity.get("supervision_version") == SUPERVISED_DAY_EXECUTION_VERSION
    required = {"chat.jsonl", "agent-quiescence.json"}
    required.add("supervised-day-binding.json" if supervised_day else "execution-binding.json")
    for name in _EVIDENCE:
        path = output / name
        if path.is_symlink():
            raise SnapshotError("Top-level execution evidence cannot be a symlink: " + name)
        if path.exists():
            rows.append(_record(path, name))
        elif name in required:
            raise SnapshotError("Missing original execution evidence: " + name)
    initial = output / "snapshots/pre-grade-mock-replay.json"
    if initial.exists():
        rows.append(_record(initial, "snapshots/pre-grade-mock-replay.json"))
    return sorted(rows, key=lambda row: row["path"])


def execution_identity(task_id, run_id, binding, config):
    task = config.get("task", {})
    return {
        "task_id": task_id,
        "run_id": run_id,
        **{key: binding[key] for key in ("execution_id", "protocol_hash", "bank_sha256", "catalog_sha256")},
        "user_id": config.get("user_id"),
        "seed": config.get("seed"),
        "phase": config.get("phase"),
        "round": config.get("round"),
        "input_sha256": task.get("input_sha256"),
        "source_sha256": task.get("source_sha256"),
    }


def supervised_day_identity(task_id, run_id, binding, config):
    """Identity for Day supervision without alignment catalog or routing fields."""
    if config.get("phase") != "day" or config.get("alignment") is not None:
        raise SnapshotError("Supervised Day identity requires a non-alignment Day execution")
    required = {
        "version": SUPERVISED_DAY_EXECUTION_VERSION,
        "task_id": task_id,
        "run_id": run_id,
        "execution_id": binding.get("execution_id"),
        "protocol_hash": config.get("protocol_hash"),
    }
    if any(binding.get(key) != value for key, value in required.items()):
        raise SnapshotError("Supervised Day binding differs from execution configuration")
    if not isinstance(binding.get("bank_sha256"), str) or not binding["bank_sha256"]:
        raise SnapshotError("Supervised Day binding has no bank digest")
    task = config.get("task", {})
    return {
        "supervision_version": SUPERVISED_DAY_EXECUTION_VERSION,
        "task_id": task_id,
        "run_id": run_id,
        "execution_id": binding["execution_id"],
        "protocol_hash": binding["protocol_hash"],
        "bank_sha256": binding["bank_sha256"],
        "user_id": config.get("user_id"),
        "seed": config.get("seed"),
        "phase": config.get("phase"),
        "round": config.get("round"),
        "input_sha256": task.get("input_sha256"),
        "source_sha256": task.get("source_sha256"),
    }


def freeze_agent_output(output, identity):
    output = Path(output)
    stopped = _read(output / "agent-quiescence.json")
    if stopped.get("quiescent") is not True or stopped.get("surviving_writers"):
        raise SnapshotError("Agent writers are not quiescent")
    if (output / AGENT_MANIFEST).exists():
        return verify_agent_output(output, identity)
    replay = output / "mock-replay.json"
    if replay.exists():
        archived = output / "snapshots/pre-grade-mock-replay.json"
        archived.parent.mkdir(parents=True, exist_ok=True)
        data = replay.read_bytes()
        try:
            fd = os.open(archived, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
        except FileExistsError:
            if archived.read_bytes() != data:
                raise SnapshotError("Original pre-grade mock snapshot changed")
        else:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
    return _seal(
        output / AGENT_MANIFEST,
        {
            "schema_version": 1,
            "policy": POLICY,
            "identity": identity,
            "excluded_workspace_paths": ["gt"],
            "files": _agent_files(output, identity),
        },
    )


def _manifest(output, name, identity):
    value = _read(output / name)
    content = {key: item for key, item in value.items() if key != "manifest_sha256"}
    if (
        value.get("schema_version") != 1
        or value.get("policy") != POLICY
        or value.get("manifest_sha256") != _digest(content)
    ):
        raise SnapshotError("Snapshot manifest integrity failed: " + name)
    if value.get("identity") != identity:
        raise SnapshotError("Snapshot execution identity differs: " + name)
    return value


def verify_agent_output(output, identity):
    output = Path(output)
    value = _manifest(output, AGENT_MANIFEST, identity)
    if value.get("files") != _agent_files(output, identity):
        raise SnapshotError("Original Agent output bytes, paths, or modes changed")
    return value


def _mock_requirement(checks, events):
    urls = set(re.findall(r"https?://(?:localhost|127\.0\.0\.1)(?::\d+)?[^\s'\"<>)]*", checks))
    urls.update(
        event["url"] for event in events if event.get("kind") == "mock_api" and isinstance(event.get("url"), str)
    )
    return sorted(urls)


def seal_grading_recovery(output, identity, checks, events):
    output = Path(output)
    agent = verify_agent_output(output, identity)
    if (output / RECOVERY_MANIFEST).exists():
        return verify_grading_recovery(output, identity, require_recoverable=False)
    replay = output / "mock-replay.json"
    required = _mock_requirement(checks, events)
    reasons, cached = [], {}
    if replay.exists():
        try:
            cached = _read(replay)
            for url, item in cached.items():
                if (
                    urlsplit(url).hostname not in {"localhost", "127.0.0.1"}
                    or not isinstance(item, dict)
                    or not isinstance(item.get("status"), int)
                    or not isinstance(item.get("body"), str)
                ):
                    raise SnapshotError("Invalid mock response cache")
            if any(item["status"] >= 400 for item in cached.values()):
                reasons.append("captured_mock_http_error")
        except (SnapshotError, ValueError, TypeError):
            reasons.append("invalid_mock_snapshot")
            cached = {}
    if required and not cached:
        reasons.append("missing_original_mock_snapshot")
    observed = {
        event["url"] for event in events if event.get("kind") == "mock_api" and isinstance(event.get("url"), str)
    }
    if observed - set(cached):
        reasons.append("uncaptured_observed_mock_urls")
    return _seal(
        output / RECOVERY_MANIFEST,
        {
            "schema_version": 1,
            "policy": POLICY,
            "identity": identity,
            "agent_manifest_sha256": agent["manifest_sha256"],
            "mock_snapshot": _record(replay, "mock-replay.json") if replay.exists() else None,
            "mock_required": bool(required),
            "mock_urls_in_checks_or_events": required,
            "captured_mock_urls": sorted(cached),
            "recoverable": not reasons,
            "unavailable_reasons": reasons,
            "scope": "Captured first-grader GET responses; not live mock memory",
        },
    )


def verify_grading_recovery(output, identity, *, require_recoverable=True):
    output = Path(output)
    agent = verify_agent_output(output, identity)
    value = _manifest(output, RECOVERY_MANIFEST, identity)
    if value.get("agent_manifest_sha256") != agent["manifest_sha256"]:
        raise SnapshotError("Recovery references a different Agent snapshot")
    replay = output / "mock-replay.json"
    actual = _record(replay, "mock-replay.json") if replay.exists() else None
    if actual != value.get("mock_snapshot"):
        raise SnapshotError("Original mock response snapshot changed or is missing")
    if require_recoverable and value.get("recoverable") is not True:
        raise SnapshotError(
            "Original mock state cannot be restored: " + ", ".join(value.get("unavailable_reasons", []))
        )
    return value
