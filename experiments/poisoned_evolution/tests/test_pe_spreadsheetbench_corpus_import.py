from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from experiments.poisoned_evolution.spreadsheetbench.corpus_import import import_completed_corpus
from experiments.poisoned_evolution.spreadsheetbench.runtime import ScoreResult

ROOT = Path(__file__).resolve().parents[3]
REAL_CONFIG = ROOT / "experiments/poisoned_evolution/benign/config-05.json"


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_offline_import_rescores_completed_only_and_preserves_raw_evidence(tmp_path: Path) -> None:
    dataset_root = tmp_path / "dataset"
    task_root = dataset_root / "spreadsheet/t1"
    task_root.mkdir(parents=True)
    for case_index in (1, 2, 3):
        (task_root / f"{case_index}_t1_input.xlsx").write_bytes(f"input-{case_index}".encode())
        (task_root / f"{case_index}_t1_answer.xlsx").write_bytes(f"answer-{case_index}".encode())
    dataset = dataset_root / "dataset.json"
    dataset.write_text(
        json.dumps(
            [
                {
                    "id": "t1",
                    "instruction": "edit workbook",
                    "instruction_type": "Cell-Level Manipulation",
                    "answer_position": "A1",
                    "answer_sheet": "Sheet1",
                    "data_position": "A1",
                    "spreadsheet_path": "spreadsheet/t1",
                }
            ]
        ),
        encoding="utf-8",
    )
    contract = tmp_path / "scoring-contract.json"
    _write_json(
        contract,
        {
            "schema_version": 1,
            "dataset_sha256": _sha(dataset),
            "default_answer_position_policy": (
                "official_first_sheet_for_unqualified_ranges_with_mechanical_repairs"
            ),
            "carrier_policy": {
                "require_output_pass": True,
                "require_baseline_input_failure": True,
                "exclude_unscorable": True,
            },
            "tasks": {},
        },
    )
    selection = tmp_path / "tasks.json"
    _write_json(selection, {"selected_ids": ["t1"]})
    evaluator = tmp_path / "evaluation.py"
    evaluator.write_text("# injected scorer does not load this\n", encoding="utf-8")
    skill = tmp_path / "SKILL.md"
    skill.write_text("# xlsx\n", encoding="utf-8")

    base = json.loads(REAL_CONFIG.read_text(encoding="utf-8"))
    base["sources"].update(
        dataset_path=str(dataset),
        dataset_sha256=_sha(dataset),
        selection_path=str(selection),
        selection_sha256=_sha(selection),
        evaluator_path=str(evaluator),
        evaluator_sha256=_sha(evaluator),
        xlsx_skill_path=str(skill),
        scoring_contract_path=str(contract),
        scoring_contract_sha256=_sha(contract),
    )
    base["corpus"].update(task_count=1, expected_tasks=1, cases_per_task=3, expected_case_logs=3)
    base["paths"]["usage_ledger"] = str(tmp_path / "campaign/ledger.json")

    source = copy.deepcopy(base)
    destination = copy.deepcopy(base)
    source_run = "source-corpus"
    destination_run = "destination-corpus"
    for config, run_id in ((source, source_run), (destination, destination_run)):
        config["run_ids"]["corpus"] = run_id
        config["corpus"]["run_id"] = run_id
        config["paths"]["corpus_artifacts"] = str(tmp_path / f"artifacts/{run_id}")
        config["paths"]["corpus_outputs"] = str(tmp_path / f"outputs/{run_id}")
    source_config = tmp_path / "source.json"
    destination_config = tmp_path / "destination.json"
    _write_json(source_config, source)
    _write_json(destination_config, destination)

    source_artifacts = tmp_path / f"artifacts/{source_run}"
    source_outputs = tmp_path / f"outputs/{source_run}"
    (source_artifacts / "frozen-config.json").parent.mkdir(parents=True)
    (source_artifacts / "frozen-config.json").write_bytes(source_config.read_bytes())
    _write_json(source_outputs / "corpus/state.json", {"status": "incomplete"})
    _write_json(source_outputs / "corpus/report.json", {"cases_completed": 2})

    solution = "print('solution')\n"
    solution_path = source_artifacts / "corpus/solutions/t1.py"
    solution_path.parent.mkdir(parents=True)
    solution_path.write_text(solution, encoding="utf-8")
    model = {
        "schema_version": 1,
        "task_id": "t1",
        "status": "generated",
        "model": {"id": "local"},
        "attempts": [{"status": "response", "content": "```python\nprint('solution')\n```"}],
        "messages": [
            {"role": "user", "content": "edit workbook"},
            {"role": "assistant", "content": "```python\nprint('solution')\n```"},
        ],
        "agent_steps": [
            {
                "response": "```python\nprint('solution')\n```",
                "solution": solution,
                "parse_error": None,
                "execution": {
                    "kind": "completed",
                    "attempts": [{"attempt": 1, "kind": "completed", "returncode": 0}],
                    "score": 0.0,
                    "passed": False,
                },
            }
        ],
        "solution_sha256": hashlib.sha256(solution.encode()).hexdigest(),
    }
    _write_json(source_outputs / "corpus/model/t1.json", model)

    copied_source = source_artifacts / "corpus/source/t1"
    copied_source.mkdir(parents=True)
    for path in task_root.iterdir():
        (copied_source / path.name).write_bytes(path.read_bytes())

    attempt = {"attempt": 1, "kind": "completed", "returncode": 0, "timed_out": False, "error": None}
    for case_index, status in ((1, "completed"), (2, "completed"), (3, "unknown")):
        execution = source_artifacts / f"corpus/executions/t1/case-{case_index}"
        execution.mkdir(parents=True)
        output = execution / "output.xlsx"
        output.write_bytes(f"output-{case_index}".encode())
        (execution / "solution.py").write_text(solution, encoding="utf-8")
        source_case = copied_source / f"{case_index}_t1_input.xlsx"
        source_answer = copied_source / f"{case_index}_t1_answer.xlsx"
        record = {
            "schema_version": 1,
            "case_id": f"t1:case-{case_index}",
            "task_id": "t1",
            "case_index": case_index,
            "status": status,
            "outcome": "task_failure" if status == "completed" else "infrastructure_error",
            "score": 0.0 if status == "completed" else None,
            "passed": False if status == "completed" else None,
            "attempt_count": 1,
            "attempts": [attempt],
            "error_class": "science_task" if status == "completed" else "infrastructure",
            "grading": {"status": "completed", "message": "", "normalized_answer_position": "'Sheet1'!A1"},
            "session_payload": {"timestamp": f"time-{case_index}"},
            "evidence": {
                "input_sha256": _sha(source_case),
                "answer_sha256": _sha(source_answer),
                "output_sha256": _sha(output),
                "output_path": str(output),
            },
        }
        _write_json(source_outputs / f"corpus/cases/t1/case-{case_index}.json", record)

    calls: list[tuple[int, bool]] = []

    def fake_scorer(*, task, case, output_path, evaluator_path):
        del task, evaluator_path
        baseline = output_path.name.endswith("_input.xlsx")
        passed = False if baseline else case.case_index == 1
        calls.append((case.case_index, baseline))
        return ScoreResult(passed, float(passed), "completed", "", "A1")

    result = import_completed_corpus(source_config, destination_config, scorer=fake_scorer)
    assert result["completed_cases_imported"] == 2
    assert result["excluded"] == {"unknown": ["t1:case-3"]}
    assert result["model_calls"] == result["executor_calls"] == 0
    assert "file_manifest" not in result
    assert "file_manifest_sha256" not in result
    assert calls == [(1, False), (1, True), (2, False), (2, True)]

    destination_outputs = tmp_path / f"outputs/{destination_run}"
    imported_one = json.loads((destination_outputs / "corpus/cases/t1/case-1.json").read_text())
    assert imported_one["score"] == 1.0
    assert imported_one["baseline_input_pass"] is False
    assert imported_one["carrier_eligible"] is True
    assert imported_one["session_payload"]["timestamp"] == "time-1"
    assert imported_one["session_payload"]["carrier_eligible"] is True
    assert Path(imported_one["evidence"]["output_path"]).is_file()
    assert not (destination_outputs / "corpus/cases/t1/case-3.json").exists()

    imported_model = json.loads((destination_outputs / "corpus/model/t1.json").read_text())
    assert imported_model["agent_steps"][-1]["execution"]["passed"] is True
    raw_model = tmp_path / (
        f"artifacts/{destination_run}/reuse-from-{source_run}/raw/outputs/corpus/model/t1.json"
    )
    assert raw_model.read_bytes() == (source_outputs / "corpus/model/t1.json").read_bytes()

    again = import_completed_corpus(source_config, destination_config, scorer=fake_scorer)
    assert again == result
    assert len(calls) == 4
