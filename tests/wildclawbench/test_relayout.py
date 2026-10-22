"""Configuration and phase boundaries after moving the maintained engine."""

import contextlib
import io
import json
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.wildclawbench import protocol, run
from . import test_protocol as fixture_module


def config_file(tmp_path, **changes):
    value = json.loads(protocol.DEFAULT_CONFIG.read_text())
    value.update(rounds=3, days=3, nights=3, users=2, **changes)
    path = tmp_path / "experiment" / "config.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(value))
    return path


def test_three_round_configuration_resolves_data_paths_independently(tmp_path):
    path = config_file(tmp_path, runtime_dir="runtime", artifacts_dir="artifacts", outputs_dir="outputs",
                       source_worktree="/a/retired/worktree")
    config = protocol.load_config(path)
    assert config["rounds"] == config["days"] == config["nights"] == 3
    assert config["endpoint_snapshot"] == "S3"
    assert Path(config["experiment_root"]) == path.parent
    for field in ("runtime_dir", "artifacts_dir", "outputs_dir"):
        assert Path(config[field]).parent == path.parent
    assert "source_worktree" not in config


@pytest.mark.parametrize("changes", [{"nights": 6}, {"days": 0, "nights": 0, "rounds": 0}, {"users": 0},
                                    {"endpoint_snapshot": "S6"}, {"endpoint": "S6"}])
def test_invalid_round_or_endpoint_configuration_fails_before_work(tmp_path, changes):
    path = config_file(tmp_path)
    value = json.loads(path.read_text())
    value.update(changes)
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        protocol.load_config(path)


def test_dry_run_is_offline_and_does_not_create_run_directories(tmp_path, capsys):
    path = config_file(tmp_path, artifacts_dir="artifacts", outputs_dir="outputs", runtime_dir="runtime")
    with patch.object(run, "discover_tasks", return_value=[{}] * 30), patch.object(run, "check_services") as service:
        run.main(["--config", str(path), "--run-id", "trial-3", "--dry-run"])
    result = json.loads(capsys.readouterr().out)
    assert result["rounds"] == 3 and result["endpoint_snapshot"] == "S3"
    assert result["fixed_execution_count"] == 150 and result["model_calls"] == 0
    assert not (path.parent / "artifacts").exists()
    assert not (path.parent / "outputs").exists()
    service.assert_not_called()


def test_three_rounds_stop_at_s3_and_keep_execution_evidence_in_artifacts():
    fixture = fixture_module.ControllerTests()
    fixture.setUp()
    try:
        fixture.tasks = fixture.tasks[:2]
        config = {**fixture.config, "rounds": 3, "days": 3, "nights": 3, "users": 2,
                  "all_tasks": fixture.tasks, "output_run_dir": str(fixture.directory / "outputs")}
        protocol.write_json(fixture.directory / "pilot-gate.json", {"passed": True})
        adapter = types.SimpleNamespace(execute_task=fixture.execute, rescore_task=fixture.execute)
        evolver = types.SimpleNamespace(propose_candidates=fixture.propose)
        with patch.dict(sys.modules, {"scripts.wildclawbench.task_runtime": adapter,
                                      "scripts.wildclawbench.evolution": evolver}), \
             patch.object(run, "check_services"), patch.object(run, "generate"), \
             patch.object(run, "outcome_preference", return_value="candidate"), \
             contextlib.redirect_stdout(io.StringIO()):
            run.run_experiment(fixture.tasks, fixture.bank, fixture.directory, config)
            calls = list(fixture.calls)
            run.run_experiment(fixture.tasks, fixture.bank, fixture.directory, config)
        assert fixture.calls == calls
        assert {number for phase, number, _, _ in calls if phase == "day"} == {1, 2, 3}
        assert {number for phase, number, _, _ in calls if phase == "endpoint"} == {3}
        assert {version for phase, _, _, version in calls if phase == "endpoint"} == {0, 3}
        assert (fixture.directory / "banks/S3").is_dir()
        assert not (fixture.directory / "banks/S4").exists()
        assert not (fixture.directory / "outputs/executions").exists()
        assert len(list((fixture.directory / "executions/day").rglob("result.json"))) == 6
    finally:
        fixture.tearDown()


def test_log_export_never_copies_workspace_snapshots_or_code_to_outputs(tmp_path):
    from scripts.wildclawbench import task_runtime

    evidence = tmp_path / "artifacts" / "executions" / "case"
    evidence.mkdir(parents=True)
    logs = tmp_path / "outputs" / "logs" / "case"
    for name in ("chat.jsonl", "grade.stdout", "execution-binding.json", "automated-checks.py"):
        (evidence / name).write_text(name)
    (evidence / "workspace").mkdir()
    (evidence / "workspace/result.bin").write_bytes(b"artifact")
    exported = task_runtime._export_logs(evidence, {"log_dir": str(logs)})
    assert set(exported) == {"chat.jsonl", "grade.stdout"}
    assert {p.name for p in logs.iterdir()} == {"chat.jsonl", "grade.stdout"}
    assert (evidence / "execution-binding.json").is_file()
    assert (evidence / "workspace/result.bin").read_bytes() == b"artifact"


def test_diagnostic_cleanup_ignores_a_shared_or_changed_process_identity():
    from scripts.wildclawbench import alignment_validation
    from scripts.wildclawbench.services import router_control

    with patch.object(router_control, "owned", return_value=False), \
         patch.object(alignment_validation.os, "killpg") as signal_process:
        alignment_validation.stop_gateway(None)
        alignment_validation.stop_gateway({"pid": 123, "uid": 456, "start_ticks": "wrong"})
    signal_process.assert_not_called()


def test_diagnostic_failure_releases_only_its_owned_gateway(tmp_path):
    from scripts.wildclawbench import alignment_validation

    config = {"runtime_dir": str(tmp_path), "paid_api_budget_usd": 1,
              "alignment": {"version": protocol.ALIGNMENT_VERSION}, "infrastructure_retries": 0}
    ownership = {"pid": 123, "uid": 456, "start_ticks": "exact"}
    with patch.object(alignment_validation, "load_config", return_value=config), \
         patch.object(alignment_validation, "OLD_ROOT", tmp_path / "source"), \
         patch.object(alignment_validation, "prepare", return_value=(config, [])), \
         patch.object(alignment_validation, "start_gateway", return_value=ownership), \
         patch.object(alignment_validation, "stop_gateway") as cleanup, \
         patch.object(alignment_validation, "diagnostics", side_effect=RuntimeError("fixture failure")), \
         patch.object(sys, "argv", ["diagnostic", "diagnostics", "--config", "unused", "--run-id", "case"]):
        with pytest.raises(RuntimeError, match="fixture failure"):
            alignment_validation.main()
    cleanup.assert_called_once_with(ownership)
