"""Input freezing checks run only against synthetic isolated files."""

import hashlib
import json
import os
from pathlib import Path

from scripts.wildclawbench import provenance
import pytest


@pytest.fixture
def inputs(tmp_path):
    runtime = tmp_path / "runtime"
    workspace = runtime / "assets/workspace"
    base = workspace / "01_Category/task_1"
    listed = []
    for rel, content, lfs in (
        ("exec/input.txt", b"original input", False),
        ("tmp/original.json", b"{}", False),
        ("gt/answer.txt", b"ground truth", True),
        ("exec/__pycache__/official.pyc", b"official bytes", False),
    ):
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        item = {"path": "workspace/" + path.relative_to(workspace).as_posix(), "size": len(content)}
        if lfs:
            item["lfs"] = {"oid": hashlib.sha256(content).hexdigest()}
        else:
            item["oid"] = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
        listed.append(item)
    history = base / "exec/.git/objects/history"
    history.parent.mkdir(parents=True)
    history.write_bytes(b"original hidden history")
    (base / "exec/__pycache__/generated.pyc").write_bytes(b"ignore generated cache")
    upstream = runtime / "upstream/WildClawBench"
    upstream.mkdir(parents=True)
    (upstream / "workspace").symlink_to(workspace, target_is_directory=True)
    (runtime / "assets/workspace-file-manifest.json").write_text(json.dumps(listed))
    # A reference task copy must resolve against the prepared runtime assets.
    source = tmp_path / "reference/task.md"
    source.parent.mkdir()
    source.write_text("## Workspace Path\n\n```text\nworkspace/01_Category/task_1\n```\n")
    task = {
        "task_id": "example",
        "source_path": str(source),
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    }
    config = {"runtime_dir": str(runtime), "benchmark_revision": "benchmark", "dataset_revision": "dataset"}
    return config, task, base


def test_streamed_manifest_checks_hf_and_hidden_history_and_cache(inputs, monkeypatch):
    config, task, base = inputs
    augmented = provenance.prepare_input_manifest(config, [task])[0]
    manifest = json.loads((Path(config["runtime_dir"]) / "prepared/input-files.json").read_text())
    assert augmented["input_workspace"] == str(base)
    assert augmented["input_file_count"] == 5
    assert manifest["statistics"]["hf_objects_verified"] == 4
    assert any("/.git/" in file["relpath"] for file in manifest["files"])
    assert any(file["relpath"].endswith("official.pyc") for file in manifest["files"])
    assert manifest["excluded_generated_cache_files"][0].endswith("generated.pyc")
    assert provenance.validate_task_inputs(config, augmented)["validated"]
    monkeypatch.setattr(provenance, "_hash_file", lambda *args: pytest.fail("Unchanged inputs were rehashed"))
    assert provenance.prepare_input_manifest(config, [task])[0]["input_sha256"] == augmented["input_sha256"]
    cached = json.loads((Path(config["runtime_dir"]) / "prepared/input-files.json").read_text())
    assert cached["statistics"]["files_reused_from_stat_cache"] == 5
    assert cached["statistics"]["bytes_hashed"] == 0


def test_new_experiment_reuses_readonly_input_hash_cache_but_writes_own_manifest(inputs, tmp_path, monkeypatch):
    original, task, _ = inputs
    first = provenance.prepare_input_manifest(original, [task])[0]
    shared = Path(original["runtime_dir"])
    cached_path = shared / "prepared/input-files.json"
    before = cached_path.read_bytes()
    new_root = tmp_path / "separate-experiment"
    config = {**original, "experiment_root": str(new_root), "runtime_dir": str(new_root / "runtime"),
              "shared_runtime_dir": str(shared)}
    monkeypatch.setattr(provenance, "_hash_file", lambda *args: pytest.fail("Shared unchanged inputs were rehashed"))
    augmented = provenance.prepare_input_manifest(config, [task])[0]
    assert augmented["input_sha256"] == first["input_sha256"]
    own = Path(config["runtime_dir"]) / "prepared/input-files.json"
    assert own.is_file()
    assert json.loads(own.read_text())["statistics"]["bytes_hashed"] == 0
    assert provenance.validate_task_inputs(config, augmented)["validated"]
    assert cached_path.read_bytes() == before


@pytest.mark.parametrize("change", ["same_size_restored_mtime", "mode", "new_file", "removed_file", "directory_mode"])
def test_task_start_rejects_changed_originals(inputs, change):
    config, task, base = inputs
    frozen = provenance.prepare_input_manifest(config, [task])[0]
    target = base / "exec/input.txt"
    previous = target.stat()
    if change == "same_size_restored_mtime":
        target.write_bytes(b"changed! input")
        os.utime(target, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    elif change == "mode":
        target.chmod(0o600)
    elif change == "new_file":
        (base / "exec/addition.txt").write_text("new input")
    elif change == "removed_file":
        target.unlink()
    else:
        (base / "exec").chmod(0o700)
    with pytest.raises(RuntimeError, match="Original inputs changed"):
        provenance.validate_task_inputs(config, frozen)


def test_partial_download_in_generated_cache_is_a_blocker(inputs):
    config, task, base = inputs
    path = base / "exec/.cache/video.partial"
    path.parent.mkdir()
    path.write_bytes(b"unfinished")
    with pytest.raises(RuntimeError, match="Incomplete input download remains"):
        provenance.prepare_input_manifest(config, [task])


def test_same_size_corrupt_official_file_fails_upstream_oid(inputs):
    config, task, base = inputs
    (base / "gt/answer.txt").write_bytes(b"corrupt data")
    with pytest.raises(RuntimeError, match="Official input content mismatch"):
        provenance.prepare_input_manifest(config, [task])


def test_original_empty_task_records_absence_without_creating_directory(inputs):
    config, task, base = inputs
    source = Path(task["source_path"])
    source.write_text("## Workspace Path\n\n```\nworkspace/01_Category/absent_task\n```\n")
    task.pop("source_sha256")
    frozen = provenance.prepare_input_manifest(config, [task])[0]
    assert frozen["input_file_count"] == 0
    assert not Path(frozen["input_workspace"]).exists()
    assert provenance.validate_task_inputs(config, frozen)["validated"]
    (base.parent / "absent_task").symlink_to(base)
    with pytest.raises(RuntimeError, match="Symlink"):
        provenance.validate_task_inputs(config, frozen)
