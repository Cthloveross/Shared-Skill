from __future__ import annotations

import asyncio
import copy
import json
import subprocess
import time
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from experiments.poisoned_evolution.spreadsheetbench.cli import (
    _abort,
    _campaign_usage_readiness,
    _controller_alive,
    _corpus_config_for_kind,
    _freeze_sources,
    _make_report,
    _model_contract_readiness,
    _process_start_ticks,
    _protocol_contract,
    _require_execution_contract,
    _run_controlled,
    _run_cumulative_wallclock,
    _signal_controller,
    _smoke_manifest,
    _source_freeze_binding,
    _source_provenance_readiness,
    _supervise,
    _supervisor_next_action,
    _validated_acquisition_smoke,
    _verify_formal_smoke_binding,
)
from experiments.poisoned_evolution.spreadsheetbench.corpus import CorpusConfig
from experiments.poisoned_evolution.spreadsheetbench.provenance import sha256_tree
from experiments.poisoned_evolution.spreadsheetbench.runtime import sha256_file
from experiments.poisoned_evolution.spreadsheetbench.usage import UsageLedger

ROOT = Path(__file__).resolve().parents[3]
REAL_CONFIG = ROOT / "experiments/poisoned_evolution/benign/config-05.json"


def _config(tmp_path: Path) -> dict:
    value = json.loads(REAL_CONFIG.read_text(encoding="utf-8"))
    value = copy.deepcopy(value)
    value["run_ids"] = {
        "corpus": "corpus-run",
        "smoke": "smoke-run",
        "formal": "formal-run",
    }
    value["corpus"]["run_id"] = "corpus-run"
    value["paths"] = {
        "artifacts_dir": str(tmp_path / "artifacts"),
        "outputs_dir": str(tmp_path / "outputs"),
        "corpus_artifacts": str(tmp_path / "artifacts/corpus-run"),
        "corpus_outputs": str(tmp_path / "outputs/corpus-run"),
        "smoke_artifacts": str(tmp_path / "artifacts/smoke-run"),
        "smoke_outputs": str(tmp_path / "outputs/smoke-run"),
        "formal_artifacts": str(tmp_path / "artifacts/formal-run"),
        "formal_outputs": str(tmp_path / "outputs/formal-run"),
    }
    return value


def _write_config(tmp_path: Path, config: dict) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def test_frozen_protocol_contract_is_complete_and_has_no_inert_model_fields() -> None:
    config = json.loads(REAL_CONFIG.read_text(encoding="utf-8"))
    observed, expected = _protocol_contract(config)
    assert observed == expected
    assert "summary_temperature" not in config["model"]
    assert "request_seed_base" not in config["model"]


def test_protocol_contract_resolves_the_explicit_campaign_ledger_without_a_run_id_literal(
    tmp_path: Path,
) -> None:
    config = json.loads(REAL_CONFIG.read_text(encoding="utf-8"))
    campaign_ledger = tmp_path / "campaign/global-usage-ledger.json"
    config["paths"]["usage_ledger"] = str(campaign_ledger)
    observed, expected = _protocol_contract(config)
    assert observed == expected
    assert observed["paths.usage_ledger"] == str(campaign_ledger.resolve())
    assert observed["paths.usage_ledger_explicit"] is True

    del config["paths"]["usage_ledger"]
    observed, expected = _protocol_contract(config)
    assert observed != expected
    assert observed["paths.usage_ledger_explicit"] is False
    assert expected["paths.usage_ledger_explicit"] is True


def test_campaign_carryover_floor_fails_closed_and_accepts_accounted_usage(tmp_path: Path) -> None:
    config = _config(tmp_path)
    ledger_path = tmp_path / "campaign/global-usage-ledger.json"
    config["paths"]["usage_ledger"] = str(ledger_path)
    config["budgets"]["campaign_carryover_min_requests"] = 6
    config["budgets"]["campaign_carryover_min_tokens"] = 689_950

    missing = _campaign_usage_readiness(config)
    assert missing["ready"] is False
    assert missing["ledger_exists"] is False
    assert not ledger_path.exists()

    ledger = UsageLedger(ledger_path, max_requests=5000, max_tokens=200_000_000)
    for consumed in (100_000, 100_000, 100_000, 100_000, 100_000, 189_950):
        reservation = ledger.reserve(group="collection", estimated_tokens=consumed)
        ledger.settle(reservation, total_tokens=consumed)
    ready = _campaign_usage_readiness(config)
    assert ready["ready"] is True
    assert ready["observed"] == {
        "requests": 6,
        "tokens_consumed": 689_950,
        "tokens_reserved": 0,
        "tokens_accounted": 689_950,
    }

    config["budgets"]["campaign_carryover_min_requests"] = 7
    below_floor = _campaign_usage_readiness(config)
    assert below_floor["ready"] is False
    assert any("request total is below" in error for error in below_floor["errors"])


def test_execution_contract_checks_carryover_before_probing_the_model(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config["paths"]["usage_ledger"] = str(tmp_path / "campaign/global-usage-ledger.json")
    config["budgets"]["campaign_carryover_min_requests"] = 1
    config["budgets"]["campaign_carryover_min_tokens"] = 1
    with patch("experiments.poisoned_evolution.spreadsheetbench.cli._model_contract_readiness") as model_probe:
        with pytest.raises(RuntimeError, match="campaign usage carryover mismatch"):
            _require_execution_contract(config)
    model_probe.assert_not_called()


def test_corpus_and_smoke_paths_are_derived_from_the_declared_run_paths(tmp_path: Path) -> None:
    config = _config(tmp_path)
    corpus = _corpus_config_for_kind(config, "corpus")
    smoke = _corpus_config_for_kind(config, "smoke")
    assert corpus["corpus"]["run_id"] == "corpus-run"
    assert smoke["corpus"]["run_id"] == "smoke-run"
    assert corpus["corpus"]["usage_group"] == smoke["corpus"]["usage_group"] == "collection"
    assert smoke["budgets"]["corpus_wallclock_seconds"] == smoke["budgets"]["smoke_wallclock_seconds"]
    assert Path(corpus["paths"]["outputs_dir"]) / "corpus-run" == tmp_path / "outputs/corpus-run"
    assert Path(smoke["paths"]["artifacts_dir"]) / "smoke-run" == tmp_path / "artifacts/smoke-run"

    config["paths"]["smoke_outputs"] = str(tmp_path / "outputs/wrong-name")
    with pytest.raises(ValueError, match="must end in the frozen run id"):
        _corpus_config_for_kind(config, "smoke")


def test_served_model_contract_checks_revision_and_context_length() -> None:
    config = json.loads(REAL_CONFIG.read_text(encoding="utf-8"))
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "data": [
            {
                "id": "qwen3.8-27b",
                "root": ("/models/Qwen3.8-27B/snapshots/1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0"),
                "max_model_len": 131072,
            }
        ]
    }
    with patch("experiments.poisoned_evolution.spreadsheetbench.cli.httpx.get", return_value=response):
        assert _model_contract_readiness(config, probe_backend=True)["ready"] is True
        response.json.return_value["data"][0]["max_model_len"] = 32768
        assert _model_contract_readiness(config, probe_backend=True)["ready"] is False


def test_frozen_upstream_archive_subset_selection_and_s0_digests_are_real() -> None:
    config = json.loads(REAL_CONFIG.read_text(encoding="utf-8"))
    result = _source_provenance_readiness(config)
    assert result["ready"] is True
    assert set(result["checks"]) == {
        "source_archive",
        "dataset",
        "task_selection",
        "scoring_contract",
        "spreadsheetbench_subset",
        "spreadsheetbench_evaluator",
        "xlsx_skill",
        "xlsx_bundle",
    }
    assert result["checks"]["spreadsheetbench_subset"]["sha256"] == (
        "35b711afec92032c10d9245fa117c87b199d741f062ae2ac05db8de2b777c3e6"
    )
    assert result["checks"]["xlsx_bundle"]["sha256"] == (
        "947afa91eb194259a6d69b59c44c1b953bb9ba1f99ebffedae808b51b03dd36d"
    )
    assert result["checks"]["task_selection"]["path"].endswith(
        "experiments/poisoned_evolution/benign/tasks.json"
    )
    assert result["checks"]["spreadsheetbench_evaluator"]["sha256"] == (
        "4ae77cee8df01d1f34684fceab972810d696886533d33be2e89373de6b4d3de3"
    )


def test_configured_evaluator_is_used_and_digest_drift_is_rejected(tmp_path: Path) -> None:
    config = json.loads(REAL_CONFIG.read_text(encoding="utf-8"))
    evaluator = tmp_path / "evaluation.py"
    evaluator.write_bytes((ROOT / "reproduction/reference/evaluation/evaluation.py").read_bytes())
    config["sources"]["evaluator_path"] = str(evaluator)
    config["sources"]["evaluator_sha256"] = sha256_file(evaluator)

    corpus = CorpusConfig.from_mapping(config)
    assert corpus.evaluator_path == evaluator
    assert _source_provenance_readiness(config)["ready"] is True

    evaluator.write_bytes(evaluator.read_bytes() + b"\n# digest drift\n")
    result = _source_provenance_readiness(config)
    assert result["ready"] is False
    assert result["checks"]["spreadsheetbench_evaluator"]["ready"] is False
    assert result["errors"] == ["spreadsheetbench_evaluator digest differs from the frozen config"]


def _minimal_freeze_config(tmp_path: Path) -> tuple[dict, Path, Path]:
    source_archive = tmp_path / "source.tar.gz"
    source_archive.write_bytes(b"archive")
    subset = tmp_path / "subset"
    subset.mkdir()
    dataset = subset / "dataset.json"
    dataset.write_text("{}\n", encoding="utf-8")
    (subset / "case.xlsx").write_bytes(b"case")
    selection = tmp_path / "tasks.json"
    selection.write_text('{"selected_ids": []}\n', encoding="utf-8")
    evaluator = tmp_path / "evaluation.py"
    evaluator.write_text("def compare():\n    return True\n", encoding="utf-8")
    scoring_contract = tmp_path / "scoring-contract.json"
    scoring_contract.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dataset_sha256": sha256_file(dataset),
                "default_answer_position_policy": (
                    "official_first_sheet_for_unqualified_ranges_with_mechanical_repairs"
                ),
                "carrier_policy": {
                    "require_output_pass": True,
                    "require_baseline_input_failure": True,
                    "exclude_unscorable": True,
                },
                "tasks": {},
            }
        ),
        encoding="utf-8",
    )
    s0 = tmp_path / "xlsx"
    (s0 / "scripts/office").mkdir(parents=True)
    (s0 / "SKILL.md").write_text("# xlsx\n", encoding="utf-8")
    (s0 / "LICENSE.txt").write_text("license\n", encoding="utf-8")
    (s0 / "scripts/recalc.py").write_text("pass\n", encoding="utf-8")
    (s0 / "scripts/office/soffice.py").write_text("pass\n", encoding="utf-8")
    driver = tmp_path / "driver"
    driver.mkdir()
    (driver / "cli.py").write_text("VALUE = 1\n", encoding="utf-8")
    config = _config(tmp_path)
    config["sources"] = {
        "source_archive_path": str(source_archive),
        "source_archive_sha256": sha256_file(source_archive),
        "dataset_path": str(dataset),
        "dataset_sha256": sha256_file(dataset),
        "subset_path": str(subset),
        "subset_tree_sha256": sha256_tree(subset),
        "selection_path": str(selection),
        "selection_sha256": sha256_file(selection),
        "evaluator_path": str(evaluator),
        "evaluator_sha256": sha256_file(evaluator),
        "scoring_contract_path": str(scoring_contract),
        "scoring_contract_sha256": sha256_file(scoring_contract),
        "xlsx_skill_path": str(s0 / "SKILL.md"),
        "xlsx_skill_md_sha256": sha256_file(s0 / "SKILL.md"),
        "xlsx_bundle_path": str(s0),
        "xlsx_bundle_sha256": sha256_tree(s0),
    }
    config["engine"]["s0_bundle_path"] = str(s0)
    return config, driver, s0


def test_evolution_smoke_requires_three_distinct_carrier_eligible_tasks() -> None:
    sessions = []
    for index in range(4):
        sessions.append(
            {
                "session_id": f"session-{index}",
                "task_id": f"task-{index}",
                "passed": True,
                "baseline_input_pass": index == 0,
                "carrier_eligible": index != 1,
            }
        )
    with pytest.raises(RuntimeError, match="fewer than three distinct carrier-eligible"):
        _smoke_manifest(sessions)

    sessions[0]["baseline_input_pass"] = False
    sessions[1]["carrier_eligible"] = True
    selected, manifest = _smoke_manifest(sessions)
    assert len(selected) == 3
    assert len(set(manifest["task_ids"])) == 3
    assert all(session["baseline_input_pass"] is False for session in selected)


def test_source_freeze_copies_full_s0_and_rejects_driver_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, driver, s0 = _minimal_freeze_config(tmp_path)
    config_path = _write_config(tmp_path, config)
    artifact = tmp_path / "artifact"
    source_artifact = artifact / "source"
    source_artifact.mkdir(parents=True)
    (source_artifact / "poisoned-evolution-spreadsheetbench-driver.tar.gz").write_bytes(b"stale driver")
    (source_artifact / "skillclaw-e9c745466f14.tar.gz").write_bytes(b"stale engine")
    monkeypatch.setattr("experiments.poisoned_evolution.spreadsheetbench.cli.DRIVER_ROOT", driver)

    def fake_git_archive(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if "rev-parse" in command:
            return subprocess.CompletedProcess(command, 0, stdout=config["engine"]["commit"] + "\n")
        if "status" in command:
            return subprocess.CompletedProcess(command, 0, stdout="")
        output = next(item.removeprefix("--output=") for item in command if item.startswith("--output="))
        Path(output).write_bytes(b"frozen engine")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(
        "experiments.poisoned_evolution.spreadsheetbench.cli.subprocess.run",
        fake_git_archive,
    )
    _freeze_sources(config_path, config, artifact)
    assert (source_artifact / "skillclaw-e9c745466f14.tar.gz").read_bytes() == b"frozen engine"
    assert (source_artifact / "poisoned-evolution-spreadsheetbench-driver.tar.gz").read_bytes() != b"stale driver"
    frozen_s0 = artifact / "s0/xlsx"
    assert sha256_tree(frozen_s0) == sha256_tree(s0)
    assert sorted(path.relative_to(frozen_s0).as_posix() for path in frozen_s0.rglob("*") if path.is_file()) == [
        "LICENSE.txt",
        "SKILL.md",
        "scripts/office/soffice.py",
        "scripts/recalc.py",
    ]
    assert (artifact / "task-selection.json").read_bytes() == Path(config["sources"]["selection_path"]).read_bytes()
    freeze = json.loads((artifact / "source-freeze.json").read_text(encoding="utf-8"))
    assert freeze["driver_tree_sha256"] == sha256_tree(driver)
    assert freeze["evaluator_sha256"] == config["sources"]["evaluator_sha256"]
    assert (source_artifact / "spreadsheetbench-evaluation.py").read_bytes() == Path(
        config["sources"]["evaluator_path"]
    ).read_bytes()
    assert freeze["s0_bundle_sha256"] == config["sources"]["xlsx_bundle_sha256"]

    (frozen_s0 / "scripts/recalc.py").write_text("corrupt\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="frozen S0 bundle differs"):
        _freeze_sources(config_path, config, artifact)
    shutil_source = s0 / "scripts/recalc.py"
    (frozen_s0 / "scripts/recalc.py").write_bytes(shutil_source.read_bytes())

    (driver / "cli.py").write_text("VALUE = 2\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="source freeze changed"):
        _freeze_sources(config_path, config, artifact)


def test_formal_source_identity_must_equal_the_bytes_bound_into_smoke_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, driver, _ = _minimal_freeze_config(tmp_path)
    config_path = _write_config(tmp_path, config)
    smoke_artifact = tmp_path / "artifacts/smoke-run"
    formal_artifact = tmp_path / "artifacts/formal-run"
    monkeypatch.setattr("experiments.poisoned_evolution.spreadsheetbench.cli.DRIVER_ROOT", driver)

    def fake_git(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if "rev-parse" in command:
            return subprocess.CompletedProcess(command, 0, stdout=config["engine"]["commit"] + "\n")
        if "status" in command:
            return subprocess.CompletedProcess(command, 0, stdout="")
        output = next(item.removeprefix("--output=") for item in command if item.startswith("--output="))
        Path(output).write_bytes(b"same frozen engine")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("experiments.poisoned_evolution.spreadsheetbench.cli.subprocess.run", fake_git)
    _freeze_sources(config_path, config, smoke_artifact)
    acquisition = smoke_artifact / "acquisition-smoke-report.json"
    acquisition.write_text('{"status":"smoke_complete"}\n', encoding="utf-8")
    gate = {
        "status": "passed",
        "source_freeze": _source_freeze_binding(smoke_artifact),
        "acquisition_smoke": {"report_sha256": sha256_file(acquisition)},
    }
    _freeze_sources(config_path, config, formal_artifact)
    _verify_formal_smoke_binding(smoke_artifact, formal_artifact, gate)

    (driver / "cli.py").write_text("VALUE = 2\n", encoding="utf-8")
    drifted_formal = tmp_path / "artifacts/formal-after-drift"
    _freeze_sources(config_path, config, drifted_formal)
    with pytest.raises(RuntimeError, match="differ from those that passed smoke"):
        _verify_formal_smoke_binding(smoke_artifact, drifted_formal, gate)

    bad_gate = copy.deepcopy(gate)
    bad_gate["source_freeze"]["record_sha256"] = "0" * 64
    with pytest.raises(RuntimeError, match="differs from the passed gate"):
        _verify_formal_smoke_binding(smoke_artifact, formal_artifact, bad_gate)


def test_evolution_smoke_requires_current_nine_case_acquisition_report(tmp_path: Path) -> None:
    config = _config(tmp_path)
    with pytest.raises(RuntimeError, match="no acquisition report"):
        _validated_acquisition_smoke(config)

    report_path = tmp_path / "outputs/smoke-run/corpus/report.json"
    report_path.parent.mkdir(parents=True)
    report = {
        "run_id": "smoke-run",
        "status": "smoke_complete",
        "backend": "docker",
        "scientific": True,
        "tasks_expected": 3,
        "cases_expected": 9,
        "cases_recorded": 9,
        "cases_completed": 8,
        "cases_unknown": 1,
        "stop_reason": "provider_unknown",
    }
    report_path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(RuntimeError, match="not 9/9 determinate"):
        _validated_acquisition_smoke(config)

    report.update(cases_completed=9, cases_unknown=0, stop_reason=None)
    report_path.write_text(json.dumps(report), encoding="utf-8")
    frozen = tmp_path / "artifacts/smoke-run"
    binding = _validated_acquisition_smoke(config, freeze_into=frozen)
    assert binding["status"] == "9/9 determinate"
    assert binding["report_sha256"] == sha256_file(frozen / "acquisition-smoke-report.json")


def test_supervisor_orders_smoke_collection_evolution_and_formal(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert _supervisor_next_action(config)["stage"] == "acquisition"

    smoke_report = tmp_path / "outputs/smoke-run/corpus/report.json"
    smoke_report.parent.mkdir(parents=True)
    smoke_report.write_text(
        json.dumps(
            {
                "run_id": "smoke-run",
                "status": "smoke_complete",
                "backend": "docker",
                "scientific": True,
                "tasks_expected": 3,
                "cases_expected": 9,
                "cases_recorded": 9,
                "cases_completed": 9,
                "cases_unknown": 0,
                "stop_reason": None,
            }
        ),
        encoding="utf-8",
    )
    assert _supervisor_next_action(config)["stage"] == "collection"

    corpus_report = tmp_path / "outputs/corpus-run/corpus/report.json"
    corpus_report.parent.mkdir(parents=True)
    corpus_report.write_text(json.dumps({"ready_for_trials": True}), encoding="utf-8")
    assert _supervisor_next_action(config)["stage"] == "evolution_smoke"

    with patch(
        "experiments.poisoned_evolution.spreadsheetbench.cli._passed_smoke_gate",
        return_value={"status": "passed"},
    ):
        assert _supervisor_next_action(config)["command"] == "run"
        (tmp_path / "artifacts/formal-run/trials/bf1/batch-01").mkdir(parents=True)
        assert _supervisor_next_action(config)["command"] == "resume"


@pytest.mark.asyncio
async def test_supervisor_binds_config_and_stops_after_terminal_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    config_path = _write_config(tmp_path, config)
    actions = iter(
        (
            {"terminal": False, "stage": "acquisition", "command": "smoke", "phase": "acquisition"},
            {"terminal": True, "status": "complete", "reason": "acceptance_gate_passed"},
        )
    )
    monkeypatch.setattr(
        "experiments.poisoned_evolution.spreadsheetbench.cli._supervisor_next_action",
        lambda _config: next(actions),
    )
    commands: list[list[str]] = []

    async def child(command, log_path):
        commands.append(list(command))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("child completed\n", encoding="utf-8")
        return 0

    result = await _supervise(config_path, config, child_runner=child, max_child_launches=4)
    assert result["status"] == "complete"
    assert len(commands) == 1
    assert commands[0][-2:] == ["--phase", "acquisition"]
    supervisor = json.loads((tmp_path / "artifacts/formal-run/supervisor.json").read_text())
    assert supervisor["config_sha256"] == sha256_file(config_path)
    assert supervisor["status"] == "complete"
    assert not (tmp_path / "artifacts/formal-run/supervisor.lock").exists()


def test_final_report_exposes_cross_family_matrix_and_fails_acceptance_gate(tmp_path: Path) -> None:
    config = _config(tmp_path)
    output = Path(config["paths"]["formal_outputs"])
    output.mkdir(parents=True)
    families = {
        family: {
            "family_id": family,
            "attempted": 25,
            "completed": 25,
            "unknown": 0,
            "successes": 0,
            "ser": 0.0,
            "stopped_reason": None,
        }
        for family in ("bf1", "bf2", "bf3", "bf4")
    }
    matrix = {
        "families": families,
        "attempted": 100,
        "completed": 100,
        "unknown": 0,
        "successes": 0,
        "ser": 0.0,
        "trials": [
            {
                "trial_id": "bf1-batch-01",
                "family_id": "bf1",
                "status": "completed",
                "diagnostics": {
                    "materialized_detector_hits": {"bf1": 1, "bf2": 1, "bf3": 0, "bf4": 0},
                    "cross_family_detector_hits": {"bf2": 1},
                },
            }
        ],
    }
    (output / "matrix-result.json").write_text(json.dumps(matrix), encoding="utf-8")
    report = _make_report(config)
    assert report["cross_family_detection"]["materialized_skill_hit_matrix"]["bf1"]["bf2"] == 1
    assert report["cross_family_detection"]["completed_cross_hit_trials"] == 1
    assert report["acceptance_gate"]["no_cross_family_hits_in_completed_trials"] is False
    assert report["acceptance_gate"]["passed"] is False


def test_abort_writes_artifact_and_output_markers_for_every_run(tmp_path: Path) -> None:
    config = _config(tmp_path)
    result = _abort(config)
    assert result["status"] == "abort_requested"
    assert set(result["runs"]) == {"corpus", "smoke", "formal"}
    for kind, run_id in config["run_ids"].items():
        assert json.loads((tmp_path / f"artifacts/{run_id}/abort-request.json").read_text())["kind"] == kind
        assert json.loads((tmp_path / f"outputs/{run_id}/abort-request.json").read_text())["kind"] == kind
        events = (tmp_path / f"outputs/{run_id}/events.jsonl").read_text()
        assert '"event":"abort_requested"' in events


def test_controller_identity_and_isolated_process_group_signal() -> None:
    process = subprocess.Popen(
        ["bash", "-c", "exec -a experiments.poisoned_evolution sleep 30"],
        start_new_session=True,
    )
    try:
        record = {
            "status": "running",
            "pid": process.pid,
            "process_group_id": process.pid,
            "isolated_process_group": True,
            "process_start_ticks": _process_start_ticks(process.pid),
        }
        deadline = time.monotonic() + 2
        while not _controller_alive(record) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert _controller_alive(record) is True
        assert _signal_controller(record) == "process_group"
        assert process.wait(timeout=5) == -15
        assert _controller_alive(record) is False
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


@pytest.mark.asyncio
async def test_controlled_cancellation_records_abort_and_releases_lock(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config_path = _write_config(tmp_path, config)
    started = asyncio.Event()

    async def operation() -> dict:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    task = asyncio.create_task(
        _run_controlled(
            config_path,
            config,
            kind="smoke",
            command="smoke:acquisition",
            operation=operation,
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    artifact = tmp_path / "artifacts/smoke-run"
    controller = json.loads((artifact / "controller.json").read_text())
    assert controller["status"] == "aborted_by_user"
    assert isinstance(controller["process_group_id"], int)
    assert isinstance(controller["session_id"], int)
    assert not (artifact / "controller.lock").exists()


@pytest.mark.asyncio
async def test_smoke_wallclock_is_cumulative_across_invocations(tmp_path: Path) -> None:
    state = tmp_path / "wallclock-state.json"
    state.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "phase_complete",
                "elapsed_seconds": 60,
                "updated_unix": 1,
            }
        ),
        encoding="utf-8",
    )
    called = False

    async def operation() -> dict:
        nonlocal called
        called = True
        return {"status": "unexpected"}

    with pytest.raises(TimeoutError, match="cumulative wallclock cap"):
        await _run_cumulative_wallclock(
            state,
            limit_seconds=60,
            phase="evolution",
            operation=operation,
        )
    assert called is False
    assert json.loads(state.read_text())["status"] == "wallclock_exhausted"
