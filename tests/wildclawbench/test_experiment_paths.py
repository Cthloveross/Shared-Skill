"""Independent experiment state and read-only shared caches; no real services."""

import json
import subprocess
from pathlib import Path

import pytest

from scripts.wildclawbench import environment
from scripts.wildclawbench import task_runtime
def config_for(tmp_path):
    root = tmp_path / "new-experiment"
    root.mkdir()
    shared = tmp_path / "shared-runtime"
    (shared / "assets").mkdir(parents=True)
    return {
        "experiment_root": str(root), "runtime_dir": str(root / "runtime"),
        "shared_runtime_dir": str(shared), "docker_container": "independent-test-daemon",
        "local_api_base_url": "http://127.0.0.1:19182/v1", "outer_gateway": "172.17.0.1",
    }


def test_child_receives_separate_data_root_and_shared_input_root(tmp_path, monkeypatch):
    config = config_for(tmp_path)
    output = Path(config["runtime_dir"]) / "runs/example/execution"

    def child(command, **kwargs):
        inherited = json.loads(Path(command[command.index("--config") + 1]).read_text())
        assert inherited == config
        paths = environment.paths(inherited)
        assert paths["runtime"] == Path(config["runtime_dir"])
        assert paths["upstream"] == Path(config["shared_runtime_dir"]) / "upstream/WildClawBench"
        assert paths["assets"] == Path(config["shared_runtime_dir"]) / "assets"
        assert paths["docker"] == Path(config["runtime_dir"]) / "docker"
        assert command[1:3] == ["-m", "scripts.wildclawbench.task_runtime"]
        (output / "runtime-result.json").write_text(json.dumps({"status": "completed", "score": 0.5}))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(task_runtime.subprocess, "run", child)
    assert task_runtime.execute_task("test", tmp_path / "bank", output, config)["score"] == 0.5


def test_judge_role_is_explicit_and_main_route_stays_local(tmp_path):
    config = config_for(tmp_path)
    assert environment.container_api_url(config, run_id="test") == "http://172.17.0.1:19182/runs/test/v1"
    assert environment.container_api_url(config, run_id="test", role="judge") == "http://172.17.0.1:19182/runs/test/v1"
    config["external_judge"] = True
    assert environment.container_api_url(config, run_id="test", role="judge") == "http://172.17.0.1:19182/runs/test/judge/v1"
    assert environment.container_api_url(config, role="judge") == "http://172.17.0.1:19182/judge/v1"
    assert "api.deepseek.com:127.0.0.1" in environment.provider_network_args()


def test_shared_asset_prepare_and_seed_bootstrap_do_not_write_cache(tmp_path, monkeypatch):
    config = config_for(tmp_path)
    paths = environment.paths(config)
    workspace = paths["assets"] / "workspace"
    workspace.mkdir()
    (workspace / "input.txt").write_bytes(b"input")
    paths["upstream"].mkdir(parents=True)
    (paths["upstream"] / "workspace").symlink_to(workspace, target_is_directory=True)
    listing = paths["assets"] / "workspace-file-manifest.json"
    listing.write_text(json.dumps([{"path": "workspace/input.txt", "size": 5}]))
    archive = paths["assets"] / "wildclawbench-ubuntu_v1.3.tar"
    archive.write_bytes(b"tar")
    monkeypatch.setattr(environment, "IMAGE_BYTES", 3)
    monkeypatch.setattr(environment, "_download", lambda *args: (_ for _ in ()).throw(AssertionError("download")))
    before = {path: path.read_bytes() for path in (listing, archive, workspace / "input.txt")}
    result = environment.prepare_assets(config)
    assert result["cached"]
    bootstrap = environment.python_bootstrap_source(config)
    assert bootstrap.is_relative_to(paths["runtime"])
    assert {path: path.read_bytes() for path in before} == before
    assert not (paths["assets"] / "python-bootstrap").exists()


def test_private_dind_uses_own_storage_and_readonly_shared_mount(tmp_path, monkeypatch):
    config = config_for(tmp_path)
    paths = environment.paths(config)
    image = {"Id": "sha256:test", "Config": {"Env": []}}
    cached = paths["assets"] / "task-image-python-environment.json"
    cached.write_text(json.dumps({"task_image_id": image["Id"], "PYTHONPATH": ""}))
    before = cached.read_bytes()
    calls = []
    inspected = False

    def docker(command, **kwargs):
        nonlocal inspected
        calls.append(command)
        if command[:2] == ["docker", "inspect"]:
            if not inspected:
                inspected = True
                return subprocess.CompletedProcess(command, 1, "", "not present")
            info = [{"NetworkSettings": {"Networks": {"default": {"Gateway": "172.17.0.1"}}}}]
            return subprocess.CompletedProcess(command, 0, json.dumps(info), "")
        if command[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(command, 0, json.dumps([image]), "")
        return subprocess.CompletedProcess(command, 0, "ready", "")

    monkeypatch.setattr(environment, "_run", docker)
    result = environment.prepare_docker(config)
    launch = next(command for command in calls if command[:2] == ["docker", "run"])
    assert f"type=bind,src={paths['docker'] / 'data'},dst=/var/lib/docker" in launch
    assert f"type=bind,src={config['shared_runtime_dir']},dst={config['shared_runtime_dir']},readonly" in launch
    assert result["storage"] == str(paths["docker"] / "data")
    assert cached.read_bytes() == before
    assert not (Path(config["shared_runtime_dir"]) / "docker").exists()


@pytest.mark.parametrize("missing", ["repository", "shared runtime"])
def test_existing_dind_requires_current_source_binds(tmp_path, monkeypatch, missing):
    config = config_for(tmp_path)
    paths = environment.paths(config)
    repository = str(paths["root"])
    shared = str(Path(config["shared_runtime_dir"]).resolve())
    mounts = [
        {
            "Type": "bind",
            "Source": str((paths["docker"] / "data").resolve()),
            "Destination": "/var/lib/docker",
            "RW": True,
        },
        {"Type": "bind", "Source": repository, "Destination": repository, "RW": True},
        {"Type": "bind", "Source": shared, "Destination": shared, "RW": False},
    ]
    destination = repository if missing == "repository" else shared
    for mount in mounts:
        if mount["Destination"] == destination:
            mount["Source"] = str(tmp_path / "stale-worktree")
    info = [{"Mounts": mounts, "State": {"Running": False}}]
    calls = []

    def docker(command, **kwargs):
        calls.append(command)
        assert command[:2] == ["docker", "inspect"]
        return subprocess.CompletedProcess(command, 0, json.dumps(info), "")

    monkeypatch.setattr(environment, "_run", docker)
    with pytest.raises(RuntimeError, match=f"exact {missing} bind"):
        environment.prepare_docker(config, load_image=False)
    assert calls == [["docker", "inspect", config["docker_container"]]]
