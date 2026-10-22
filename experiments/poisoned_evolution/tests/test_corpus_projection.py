from __future__ import annotations

import asyncio
import copy
import json
import subprocess
from pathlib import Path

import pytest

from experiments.poisoned_evolution.spreadsheetbench.corpus import (
    adapt_session_for_evolution,
    case_record_to_session,
    load_case_logs,
)
from experiments.poisoned_evolution.spreadsheetbench.corpus_import import derive_completed_corpus


def _session(*, passed: bool = False, case_index: int = 1, parse_error: str | None = None) -> dict:
    response = "```python\n" + "# valid, lengthy program\n" * 180 + "wb.save(output_path)\n```"
    attempt = {
        "attempt": 1, "kind": "completed", "returncode": 0, "timed_out": False,
        "stdout": "long log\n" * 500, "stderr": "", "error": None,
    }
    step = {
        "agent_turn": 1, "response": response, "solution": response[10:-3], "parse_error": parse_error,
        "execution": {"kind": "completed", "attempts": [attempt], "score": float(passed), "passed": passed},
    }
    return {
        "session_id": f"spreadsheetbench-t1-case-{case_index}", "task_id": "t1",
        "passed": passed, "success": passed, "score": float(passed), "carrier_eligible": passed,
        "baseline_input_pass": False, "outcome": "success" if passed else "failure",
        "turns": [{"response_text": response, "tool_results": [{"has_error": not passed}]}],
        "collection_evidence": {
            "case_index": case_index, "task_instruction": "edit workbook",
            "model_messages": [{"role": "user", "content": "edit workbook"},
                               {"role": "assistant", "content": response}],
            "agent_steps": [step], "execution_attempts": [attempt],
            "case_record_facts": {"output_exists": True},
        },
    }


def test_projection_separates_zero_reward_from_tool_failure_and_preserves_raw_evidence() -> None:
    source = _session()
    before = copy.deepcopy(source)
    projected = adapt_session_for_evolution(source)
    assert source == before
    assert projected["collection_evidence"] == source["collection_evidence"]
    turn = projected["turns"][0]
    result = turn["tool_results"][0]
    assert result["has_error"] is False
    assert result["error_type"] == ""
    facts = json.loads(result["content"])
    assert facts["returncode"] == 0 and facts["output_exists"] is True
    assert facts["score"] == 0 and facts["passed"] is False
    assert len(result["content"]) < 400
    assert '"parse_status":"ok"' in turn["response_text"][:400]
    assert '"score":0.0' in turn["response_text"][:400]
    assert "not a parse/runtime failure" in turn["response_text"][:400]
    assert "middle omitted" in turn["response_text"]
    assert "wb.save(output_path)" in turn["response_text"]
    assert len(turn["response_text"]) < 2000
    assert len(projected["turns"]) == 1
    assert adapt_session_for_evolution(projected) == projected
    assert adapt_session_for_evolution(source, adapter="legacy") == source
    with pytest.raises(ValueError, match="unsupported"):
        adapt_session_for_evolution(source, adapter="invented")


def test_projection_preserves_real_parse_and_runtime_errors_and_unknown_output() -> None:
    parsed = adapt_session_for_evolution(_session(parse_error="SyntaxError: unterminated string"))
    result = parsed["turns"][0]["tool_results"][0]
    assert result["tool_name"] == "parse_python_solution"
    assert result["has_error"] is True
    assert json.loads(result["content"])["executed"] is False
    source = _session()
    evidence = source["collection_evidence"]
    evidence.pop("case_record_facts")
    attempt = evidence["agent_steps"][0]["execution"]["attempts"][0]
    attempt.update(kind="task_failure", returncode=1, error="IndexError")
    result = adapt_session_for_evolution(source)["turns"][0]["tool_results"][0]
    assert result["has_error"] is True and result["error_type"] == "task_failure"
    assert json.loads(result["content"])["output_exists"] is None


def test_replayed_case_keeps_generation_and_replay_distinct() -> None:
    source = _session(passed=True, case_index=2)
    source["collection_evidence"]["agent_steps"][0]["execution"].update(score=0.0, passed=False)
    projected = adapt_session_for_evolution(source)
    results = projected["turns"][0]["tool_results"]
    assert len(results) == 2
    first, replay = [json.loads(result["content"]) for result in results]
    assert (first["case_index"], first["phase"], first["score"]) == (1, "solution_validation", 0.0)
    assert (replay["case_index"], replay["phase"], replay["score"]) == (2, "case_replay", 1.0)
    assert not any(result["has_error"] for result in results)


@pytest.mark.parametrize("recalculation", [
    {"attempted": True, "status": "success", "returncode": 0, "stdout": "full raw log" * 500},
    {"attempted": True, "status": "soffice_failed", "returncode": 77},
    {"attempted": False, "status": "disabled", "returncode": None},
    None,
])
def test_v3_preserves_recalculation_observations_without_diagnosing_zero_reward(recalculation) -> None:
    source = _session()
    evidence = source["collection_evidence"]
    evidence["agent_steps"][0]["execution"]["attempts"][0]["recalculation"] = recalculation
    evidence["case_record_facts"]["grading"] = {"status": "completed", "message": ""}
    before = copy.deepcopy(source)
    projected = adapt_session_for_evolution(source, adapter="execution_facts_v3")
    assert source == before
    assert projected["collection_evidence"] == evidence
    assert projected["score"] == 0 and projected["passed"] is False
    turn = projected["turns"][0]
    facts = json.loads(turn["tool_results"][0]["content"])
    expected = {key: (recalculation or {}).get(key) for key in ("attempted", "status", "returncode")}
    assert facts["recalculation"] == facts["attempt"]["recalculation"] == expected
    assert facts["grading_status"] == "completed"
    encoded = json.dumps(expected, separators=(",", ":"))
    for window in (turn["response_text"][:400], turn["tool_results"][0]["content"][:400]):
        assert f'"recalculation":{encoded}' in window
        assert '"score":0.0,"passed":false' in window
        assert '"grading_status":"completed"' in window
    assert len(turn["response_text"]) < 2000
    assert adapt_session_for_evolution(projected, adapter="execution_facts_v3") == projected
    # The historical adapter deliberately retains its old omission for audit.
    old = adapt_session_for_evolution(source, adapter="execution_facts_v2")
    assert "recalculation" not in old["turns"][0]["tool_results"][0]["content"]


def test_v3_actual_zero_reward_cases_keep_recalculation_in_both_native_views() -> None:
    root = Path(__file__).resolve().parents[3]
    checkout = root / "runtime/poisoned-evolution/skillclaw-e9c745466f14"
    python = checkout / ".venv/bin/python"
    source_root = root / "outputs/pe-sb-qwen-corpus-20261020-05/corpus/cases"
    paths = [source_root / task / f"case-{case}.json"
             for task in ("263-1", "267-21") for case in (1, 2, 3)]
    if not python.is_file() or not all(path.is_file() for path in paths):
        pytest.skip("frozen native runtime and six original case records are required")
    sessions = []
    for path in paths:
        record = json.loads(path.read_text())
        assert record["score"] == 0 and record["grading"]["status"] == "completed"
        session = case_record_to_session(None, record)
        session["collection_evidence"]["case_record_facts"] = {
            "grading": record["grading"], "output_exists": bool(record["evidence"]["output_sha256"]),
        }
        sessions.append(adapt_session_for_evolution(session, adapter="execution_facts_v3"))
    program = """
import json, sys
from evolve_server.pipeline.summarizer import _build_session_payload, build_session_trajectory
json.dump([{"summary": _build_session_payload(s), "trajectory": build_session_trajectory(s)}
           for s in json.load(sys.stdin)], sys.stdout)
"""
    result = subprocess.run(
        [str(python), "-c", program], input=json.dumps(sessions), text=True, capture_output=True,
        cwd=checkout, env={"PYTHONPATH": str(checkout), "PYTHONDONTWRITEBYTECODE": "1"},
        check=True, timeout=30,
    )
    for session, view in zip(sessions, json.loads(result.stdout), strict=True):
        assert session["score"] == 0 and session["passed"] is False
        for interaction in view["summary"]["interactions"]:
            assert len(interaction["response"]) <= 2000
            for text in (interaction["response"][:400], view["trajectory"]):
                assert '"recalculation":{"attempted":true,"status":"success","returncode":0}' in text
                assert '"score":0.0,"passed":false' in text
                assert '"grading_status":"completed"' in text
        assert "full raw log" not in view["trajectory"]


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n")


def _derive_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    source, destination = {}, {}
    for target, run_id in ((source, "source"), (destination, "destination")):
        target.update(
            run_ids={"corpus": run_id}, corpus={"task_count": 1, "session_adapter": "execution_facts_v2"},
            sources={key: "recorded-digest" for key in (
                "dataset_sha256", "selection_sha256", "evaluator_sha256", "scoring_contract_sha256"
            )},
            paths={"corpus_artifacts": str(tmp_path / "artifacts" / run_id),
                   "corpus_outputs": str(tmp_path / "outputs" / run_id)},
        )
    source_config, destination_config = tmp_path / "source.json", tmp_path / "destination.json"
    _write(source_config, source)
    _write(destination_config, destination)
    source_artifact = tmp_path / "artifacts/source"
    source_artifact.mkdir(parents=True)
    (source_artifact / "frozen-config.json").write_bytes(source_config.read_bytes())
    output = tmp_path / "outputs/source/corpus"
    _write(output / "state.json", {"status": "complete"})
    _write(output / "report.json", {
        "status": "complete", "cases_completed": 3, "cases_unknown": 0, "ready_for_trials": True,
        "cases_passed": 1, "carrier_eligible_case_count": 1, "elapsed_seconds": 10,
    })
    for index in (1, 2, 3):
        passed = index == 3
        record = {
            "case_id": f"t1:case-{index}", "task_id": "t1", "case_index": index,
            "status": "completed", "score": float(passed), "passed": passed, "carrier_eligible": passed,
            "evidence": {"output_path": str(source_artifact / f"output-{index}.xlsx"),
                         "output_sha256": "recorded-output-digest"},
            "grading": {"status": "completed", "message": "frozen scoring evidence"},
            "session_payload": _session(passed=passed, case_index=index),
        }
        _write(output / f"cases/t1/case-{index}.json", record)
    return source_config, destination_config, output, tmp_path / "outputs/destination/corpus"


@pytest.mark.parametrize("adapter", ["execution_facts_v2", "execution_facts_v3"])
def test_offline_derivation_preserves_grades_and_sources_without_execution_or_rescoring(
    tmp_path, monkeypatch, adapter,
) -> None:
    source, destination, source_output, destination_output = _derive_fixture(tmp_path)
    config = json.loads(destination.read_text())
    config["corpus"]["session_adapter"] = adapter
    _write(destination, config)
    before = {path: path.read_bytes() for path in source_output.rglob("*") if path.is_file()}
    import experiments.poisoned_evolution.spreadsheetbench.corpus_import as module

    def unexpected(*args, **kwargs):
        pytest.fail("offline projection must not invoke scorer, model, or executor")

    monkeypatch.setattr(module, "score_workbook", unexpected)
    monkeypatch.setattr(module, "import_completed_corpus", unexpected)
    summary = derive_completed_corpus(source, destination)
    assert summary["ready_for_trials"] is True
    assert summary["completed_cases_imported"] == 3
    assert summary["model_calls"] == summary["executor_calls"] == summary["scorer_calls"] == 0
    assert summary["workbooks_copied"] == 0
    assert all(path.read_bytes() == data for path, data in before.items())
    for original, derived in zip(load_case_logs(source_output), load_case_logs(destination_output)):
        for key in ("score", "passed", "carrier_eligible", "grading", "evidence"):
            assert original[key] == derived[key]
        session = case_record_to_session(None, derived)
        assert session["session_adapter"] == adapter
        if adapter == "execution_facts_v3":
            facts = json.loads(session["turns"][-1]["tool_results"][-1]["content"])
            assert facts["grading_status"] == original["grading"]["status"]
            assert session["collection_evidence"]["case_record_facts"]["grading"] == original["grading"]
    assert not list((tmp_path / "artifacts/destination").rglob("*.xlsx"))
    assert derive_completed_corpus(source, destination) == summary
    # A source snapshot must never be silently repurposed as a new derivation.
    changed = json.loads(destination.read_text())
    changed["sources"]["scoring_contract_sha256"] = "different-scoring"
    _write(destination, changed)
    with pytest.raises(ValueError, match="scoring identity"):
        derive_completed_corpus(source, destination)


def test_derivation_refuses_unknown_source_cases(tmp_path) -> None:
    source, destination, source_output, _ = _derive_fixture(tmp_path)
    case = source_output / "cases/t1/case-1.json"
    record = json.loads(case.read_text())
    record.update(status="unknown", passed=None, score=None)
    _write(case, record)
    with pytest.raises(RuntimeError, match="not determinate"):
        derive_completed_corpus(source, destination)
    assert not (tmp_path / "artifacts/destination").exists()


def test_v2_acquisition_reuse_binds_nine_cases_and_rejects_source_drift(tmp_path, monkeypatch) -> None:
    from experiments.poisoned_evolution.spreadsheetbench import cli

    config = {
        "protocol_version": "workflow-canary-v2", "run_ids": {"smoke": "smoke"},
        "corpus": {"source_config_path": "frozen-source-config.json"},
        "paths": {"artifacts_dir": str(tmp_path / "artifacts"), "outputs_dir": str(tmp_path / "outputs")},
    }
    sessions = [
        {"task_id": f"t{task:03}", "session_id": f"t{task:03}-case-{case}",
         "passed": task % 2 == 0, "score": float(task % 2 == 0), "session_adapter": "execution_facts_v2"}
        for task in range(100) for case in (1, 2, 3)
    ]
    monkeypatch.setattr(cli, "_corpus_report", lambda _: {"ready_for_trials": True})
    monkeypatch.setattr(cli, "_load_corpus_sessions", lambda _: sessions)
    freezes = []
    monkeypatch.setattr(cli, "_freeze_sources", lambda *args: freezes.append(args))
    with pytest.raises(RuntimeError, match="300 uniquely"):
        cli._corpus_session_binding(sessions[:9])
    report = asyncio.run(cli._acquisition_smoke(tmp_path / "config.json", config))
    assert len(freezes) == 1
    assert report["reexecuted"] is False
    assert report["sessions"]["session_count"] == 9
    assert report["session_ids"] == [row["session_id"] for row in sessions[:9]]
    assert cli._validated_acquisition_smoke(config)["status"] == "9/9 determinate"
    frozen = tmp_path / "artifacts/smoke"
    binding = cli._validated_acquisition_smoke(config, freeze_into=frozen)
    assert binding["report_sha256"]
    assert json.loads((frozen / "acquisition-smoke-report.json").read_text()) == report
    sessions[0]["score"] = 0.25
    with pytest.raises(RuntimeError, match="current nine adapted"):
        cli._validated_acquisition_smoke(config)
    sessions[0]["score"] = 1.0
    sessions[-1]["session_adapter"] = "legacy"
    with pytest.raises(RuntimeError, match="uniformly adapted"):
        asyncio.run(cli._acquisition_smoke(tmp_path / "config.json", config))
