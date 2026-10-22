"""Configuration changes must affect scope and paths without starting a run."""

import json
import subprocess
import sys

import pytest

from scripts.injection import e2e, e2e_30, e2e_cycles


@pytest.fixture
def configuration(tmp_path, monkeypatch):
    for module, names in ((e2e, ("CONFIG_FILE", "EXPERIMENT")),
                          (e2e_30, ("EXPERIMENT", "INJECTED_IDS", "INJECTION_ENDPOINTS")),
                          (e2e_cycles, ("EXPERIMENT", "ROUNDS"))):
        for name in names:
            monkeypatch.setattr(module, name, getattr(module, name))
    path = tmp_path / "definition/config.json"
    path.parent.mkdir()
    value = {"rounds": 3, "days": 3, "nights": 3, "endpoint": "S3",
             "payload_file": "payload.json", "artifacts_dir": "../artifacts",
             "outputs_dir": "../outputs", "runtime_dir": "../runtime",
             "injected_task_short_ids": ["03.01"], "execution_policy": "read_only_history"}
    path.write_text(json.dumps(value))
    return path, value


def test_configured_scope_and_paths_replace_module_defaults(configuration):
    path, _ = configuration
    settings = e2e_cycles.configure(path)
    assert settings["rounds"] == e2e_cycles.ROUNDS == 3
    assert settings["endpoint"] == "S3"
    assert e2e_30.INJECTED_IDS == {"03.01"}
    assert settings["payload_file"] == str(path.parent / "payload.json")
    assert e2e.run_directory("new-id") == path.parent.parent / "artifacts/new-id"
    with pytest.raises(ValueError, match="historical"):
        e2e.require_execution()
    with pytest.raises(ValueError, match="Unsafe"):
        e2e.run_directory("..")


@pytest.mark.parametrize("changes", [{"nights": 6}, {"endpoint": "S6"}, {"rounds": True}])
def test_inconsistent_scope_is_rejected_before_calls(configuration, changes):
    path, value = configuration
    path.write_text(json.dumps({**value, **changes}))
    with pytest.raises(ValueError):
        e2e_cycles.configure(path)


def test_cli_dry_run_does_not_create_outputs_or_launch_history(configuration):
    path, _ = configuration
    command = [sys.executable, "-B", "-m", "scripts.injection.e2e_cycles",
               "--config", str(path), "--run-id", "completed-id"]
    result = subprocess.run([*command, "--dry-run"], cwd=e2e.ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["scientific_calls"] == 0
    refused = subprocess.run(command, cwd=e2e.ROOT, capture_output=True, text=True)
    assert refused.returncode != 0 and "historical" in refused.stderr
    assert not (path.parent.parent / "artifacts").exists()
    assert not (path.parent.parent / "outputs").exists()
