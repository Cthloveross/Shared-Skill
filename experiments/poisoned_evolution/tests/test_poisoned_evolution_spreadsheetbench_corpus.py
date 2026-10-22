from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from experiments.poisoned_evolution.spreadsheetbench.corpus import (
    CorpusConfig,
    ModelResult,
    OpenAIModelClient,
    _apply_scoring_rule,
    _carrier_eligible,
    build_session_payload,
    build_solution_messages,
    case_record_to_session,
    collect,
    readiness,
)
from experiments.poisoned_evolution.spreadsheetbench.runtime import (
    DockerCaseExecutor,
    ExecutionResult,
    ScoreResult,
    SpreadsheetCase,
    SpreadsheetTask,
    docker_runtime_build_command,
    docker_runtime_readiness,
    load_tasks,
    normalize_answer_position,
    resolve_answer_position,
    score_workbook,
    sha256_file,
)

ROOT = Path(__file__).resolve().parents[3]
DATASET = ROOT / "reproduction/reference/spreadsheetbench_100/dataset.json"
SELECTION = ROOT / "reproduction/reference/spreadsheetbench_100/selection.json"
SKILL = ROOT / "reproduction/reference/xlsx/SKILL.md"
SCORING_CONTRACT = (
    ROOT / "experiments/poisoned_evolution/benign/scoring-contract.json"
)


class CountingModel:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def generate_solution(self, *, messages, task_id):
        self.calls.append(task_id)
        code = "import os, shutil\nshutil.copyfile(os.environ['INPUT_PATH'], os.environ['OUTPUT_PATH'])\n"
        return ModelResult(
            content=f"```python\n{code}```",
            usage={"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            response_id=f"response-{task_id}",
            request={
                "model": "fake",
                "max_tokens": 128,
                "chat_template_kwargs": {"enable_thinking": False},
            },
        )


class CountingExecutor:
    def __init__(
        self,
        *,
        first_infra: bool = False,
        always_infra: bool = False,
        first_task_failure: bool = False,
    ) -> None:
        self.calls: list[tuple[str, int, int]] = []
        self.first_infra = first_infra
        self.always_infra = always_infra
        self.first_task_failure = first_task_failure

    async def execute(self, *, task, case, solution, work_dir, timeout_seconds, attempt):
        del solution, timeout_seconds
        self.calls.append((task.task_id, case.case_index, attempt))
        if self.always_infra or (self.first_infra and attempt == 1):
            return ExecutionResult(
                kind="infrastructure_error",
                output_path=None,
                returncode=None,
                error="synthetic infrastructure error",
            )
        if self.first_task_failure and len(self.calls) == 1:
            return ExecutionResult(
                kind="task_failure",
                output_path=None,
                returncode=1,
                stderr="synthetic Python error",
                error="synthetic Python error",
                score=ScoreResult(False, 0.0, "task_failure", "synthetic Python error", ""),
            )
        work_dir.mkdir(parents=True, exist_ok=True)
        output = work_dir / "output.xlsx"
        output.write_bytes(case.input_path.read_bytes())
        score = ScoreResult(True, 1.0, "completed", "", f"'{task.answer_sheet}'!{task.answer_position}")
        return ExecutionResult(kind="completed", output_path=output, returncode=0, score=score)


def config(tmp: Path, **runtime_overrides) -> CorpusConfig:
    executor = {"backend": "fake", "concurrency": 4, "infra_retries": 1, **runtime_overrides}
    return CorpusConfig.from_mapping(
        {
            "run_id": "corpus-test",
            "sources": {
                "dataset_path": str(DATASET),
                "selection_path": str(SELECTION),
                "xlsx_skill_md_path": str(SKILL),
                "scoring_contract_path": str(SCORING_CONTRACT),
                "scoring_contract_sha256": sha256_file(SCORING_CONTRACT),
            },
            "paths": {"artifacts_dir": str(tmp / "artifacts"), "outputs_dir": str(tmp / "outputs")},
            "model": {
                "id": "fake",
                "revision": "test",
                "max_tokens": 128,
                "chat_template_kwargs": {"enable_thinking": False},
            },
            "corpus": {"executor": executor},
            "budgets": {
                "corpus_max_model_requests": 20,
                "max_tokens_total": 100_000,
                "corpus_wallclock_seconds": 60,
            },
        }
    )


def test_fixed_subset_is_exactly_100_tasks_and_300_cases() -> None:
    tasks = load_tasks(DATASET)
    assert len(tasks) == 100
    assert sum(len(task.cases) for task in tasks) == 300
    assert len({task.task_id for task in tasks}) == 100
    assert all(tuple(case.case_index for case in task.cases) == (1, 2, 3) for task in tasks)


def test_nested_config_aliases_and_fake_readiness() -> None:
    with tempfile.TemporaryDirectory() as directory:
        cfg = config(Path(directory))
        assert cfg.dataset_path == DATASET
        assert cfg.xlsx_skill_path == SKILL
        assert cfg.concurrency == 4
        assert cfg.max_model_requests == 20
        assert cfg.scoring_contract_sha256 == sha256_file(SCORING_CONTRACT)
        assert cfg.scoring_rule("13-1")["answer_position_override"] == "'LISTS'!A3:D32"
        assert cfg.scoring_rule("73-45")["carrier_excluded"] is True
        assert config(Path(directory), recalculate_with_libreoffice=False).require_recalculation is False
        report = readiness(cfg)
        assert report["ready"] is True
        assert report["checks"]["dataset"]["tasks"] == 100
        assert report["checks"]["dataset"]["cases"] == 300


def test_corpus_inference_controls_are_explicit_and_exact() -> None:
    with tempfile.TemporaryDirectory() as directory:
        base = {
            "run_id": "corpus-test",
            "sources": {
                "dataset_path": str(DATASET),
                "selection_path": str(SELECTION),
                "xlsx_skill_md_path": str(SKILL),
            },
            "paths": {"artifacts_dir": directory, "outputs_dir": directory},
            "model": {"id": "fake", "revision": "test"},
            "corpus": {"executor": {"backend": "fake"}},
        }
        with pytest.raises(ValueError, match="model.max_tokens is required"):
            CorpusConfig.from_mapping(base)
        base["model"]["max_tokens"] = 321
        with pytest.raises(ValueError, match="chat_template_kwargs"):
            CorpusConfig.from_mapping(base)
        base["model"]["chat_template_kwargs"] = {"enable_thinking": True}
        with pytest.raises(ValueError, match="chat_template_kwargs"):
            CorpusConfig.from_mapping(base)


def test_raw_corpus_request_applies_and_records_exact_inference_controls() -> None:
    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {
                "id": "response-1",
                "choices": [{"message": {"content": "```python\nprint('ok')\n```"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
            }

    class FakeHTTPClient:
        def __init__(self) -> None:
            self.requests = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, *, json, headers):
            self.requests.append({"url": url, "json": json, "headers": headers})
            return FakeResponse()

    with tempfile.TemporaryDirectory() as directory:
        cfg = replace(config(Path(directory)), model_max_tokens=321)
        client = OpenAIModelClient(cfg)
        fake_http = FakeHTTPClient()
        messages = [{"role": "user", "content": "make a workbook"}]
        with patch.object(httpx, "AsyncClient", return_value=fake_http):
            result = asyncio.run(client.generate_solution(messages=messages, task_id="task"))

        assert len(fake_http.requests) == 1
        sent = fake_http.requests[0]["json"]
        assert sent == {
            "model": "fake",
            "messages": messages,
            "temperature": 0.2,
            "top_p": 0.95,
            "max_tokens": 321,
            "chat_template_kwargs": {"enable_thinking": False},
            "stream": False,
        }
        assert result.request == sent


def test_prompt_contains_skill_and_never_answer_path() -> None:
    task = load_tasks(DATASET)[0]
    skill = SKILL.read_text(encoding="utf-8")
    messages = build_solution_messages(task, skill_text=skill, input_path=task.cases[0].input_path)
    serialized = json.dumps(messages, ensure_ascii=False)
    assert "<xlsx_skill>" in serialized
    assert "INPUT_PATH" in serialized
    assert str(task.cases[0].answer_path) not in serialized
    assert task.cases[0].answer_path.name not in serialized
    assert "_answer.xlsx" not in serialized


def test_collect_generates_one_solution_replays_three_cases_and_resumes() -> None:
    with tempfile.TemporaryDirectory() as directory:
        cfg = config(Path(directory))
        model = CountingModel()
        executor = CountingExecutor()
        first = asyncio.run(collect(cfg, task_limit=1, model_client=model, case_executor=executor))
        assert first["status"] == "smoke_complete"
        assert first["cases_completed"] == 3
        assert model.calls == ["13-1"]
        assert executor.calls == [("13-1", 1, 1), ("13-1", 2, 1), ("13-1", 3, 1)]

        second_model = CountingModel()
        second_executor = CountingExecutor()
        second = asyncio.run(collect(cfg, task_limit=1, model_client=second_model, case_executor=second_executor))
        assert second["cases_completed"] == 3
        assert second_model.calls == []
        assert second_executor.calls == []

        case_path = cfg.output_run_dir / "corpus/cases/13-1/case-1.json"
        case_record = json.loads(case_path.read_text(encoding="utf-8"))
        model_record = json.loads((cfg.output_run_dir / "corpus/model/13-1.json").read_text(encoding="utf-8"))
        assert model_record["inference"] == {
            "temperature": 0.2,
            "top_p": 0.95,
            "max_tokens": 128,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        assert model_record["attempts"][0]["request"] == {
            "model": "fake",
            "max_tokens": 128,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        session = case_record_to_session(model_record, case_record)
        assert session["session_id"] == "spreadsheetbench-13-1-case-1"
        assert session["task_id"] == "13-1"
        assert session["passed"] is True
        assert session["baseline_input_pass"] is False
        assert session["carrier_eligible"] is True
        assert session["num_turns"] == 1
        assert session["turns"][0]["prompt_text"].startswith("Task id: 13-1\nInstruction:")
        assert "<xlsx_skill>" not in session["turns"][0]["prompt_text"]
        assert session["turns"][0]["read_skills"] == [{"skill_name": "xlsx"}]
        assert session["collection_evidence"]["solution_replayed_from_case"] == 1
        assert "answer_path" not in json.dumps(session)

        case_two_record = json.loads(
            (cfg.output_run_dir / "corpus/cases/13-1/case-2.json").read_text(encoding="utf-8")
        )
        case_two = case_record_to_session(model_record, case_two_record)
        arguments = [
            json.loads(call["function"]["arguments"])
            for turn in case_two["turns"]
            for call in turn["tool_calls"]
            if call["function"]["name"] == "execute_spreadsheet_solution"
        ]
        assert [(item["phase"], item["case_index"]) for item in arguments] == [
            ("solution_validation", 1),
            ("case_replay", 2),
        ]


def test_native_turns_preserve_every_collected_message_and_real_tool_event(tmp_path: Path) -> None:
    input_path = tmp_path / "case-3-input.xlsx"
    answer_path = tmp_path / "case-3-private-answer.xlsx"
    input_path.write_bytes(b"INPUT_ONLY")
    answer_path.write_bytes(b"DO_NOT_LEAK_ANSWER_BYTES")
    case = SpreadsheetCase("task-with-history", 3, input_path, answer_path)
    task = SpreadsheetTask(
        "task-with-history",
        "Update the workbook.",
        "Cell-Level Manipulation",
        "A1",
        "Sheet1",
        "A1",
        "unused.xlsx",
        (case,),
    )
    messages = [
        {"role": "system", "content": "SYSTEM_HEAD\n" + ("x" * 2_100) + "\nSYSTEM_TAIL"},
        {"role": "user", "content": "FIRST_USER_INSTRUCTION"},
        {"role": "assistant", "content": "FIRST_INVALID_RESPONSE"},
        {"role": "user", "content": "PARSER_CORRECTION_MESSAGE"},
        {"role": "assistant", "content": "SECOND_VALID_RESPONSE"},
    ]
    model_record = {
        "model": {"id": "fake", "revision": "test"},
        "messages": messages,
        "agent_steps": [
            {
                "agent_turn": 1,
                "response": "FIRST_INVALID_RESPONSE",
                "solution": None,
                "parse_error": "PARSE_FAILURE_SENTINEL",
                "execution": {"kind": "invalid_solution", "error": "PARSE_FAILURE_SENTINEL"},
            },
            {
                "agent_turn": 2,
                "response": "SECOND_VALID_RESPONSE",
                "solution": "print('ok')\n",
                "parse_error": None,
                "execution": {
                    "kind": "completed",
                    "attempts": [
                        {
                            "attempt": 1,
                            "kind": "completed",
                            "returncode": 0,
                            "timed_out": False,
                            "stdout": "CASE1_VALIDATION_SENTINEL",
                            "stderr": "",
                        }
                    ],
                    "score": 1.0,
                    "passed": True,
                },
            },
        ],
        "attempts": [
            {
                "status": "response",
                "content": "SECOND_VALID_RESPONSE",
                "response_id": "response-2",
                "usage": {"total_tokens": 10},
            }
        ],
        "solution_sha256": "solution-digest",
    }
    session = build_session_payload(
        task=task,
        case=case,
        model_record=model_record,
        solution="print('ok')\n",
        outcome="success",
        score=1.0,
        passed=True,
        execution_attempts=[
            {
                "attempt": 1,
                "kind": "completed",
                "returncode": 0,
                "timed_out": False,
                "stdout": "CASE3_REPLAY_SENTINEL",
                "stderr": "",
            }
        ],
    )

    assert session["num_turns"] == len(session["turns"]) == 2
    assert session["turns"][0]["prompt_text"] == "FIRST_USER_INSTRUCTION"
    assert session["turns"][0]["response_text"] == "FIRST_INVALID_RESPONSE"
    assert session["turns"][1]["prompt_text"] == "PARSER_CORRECTION_MESSAGE"
    assert session["turns"][1]["response_text"] == "SECOND_VALID_RESPONSE"
    assert session["collection_evidence"]["model_messages"] == messages
    assert "SYSTEM_HEAD" not in json.dumps(session["turns"], ensure_ascii=False)

    serialized = json.dumps(session, ensure_ascii=False)
    for sentinel in (
        "PARSE_FAILURE_SENTINEL",
        "CASE1_VALIDATION_SENTINEL",
        "CASE3_REPLAY_SENTINEL",
    ):
        assert sentinel in serialized
    assert str(answer_path) not in serialized
    assert answer_path.name not in serialized
    assert "DO_NOT_LEAK_ANSWER_BYTES" not in serialized
    assert any(turn["read_skills"] == [{"skill_name": "xlsx"}] for turn in session["turns"])

    tool_results = [result for turn in session["turns"] for result in turn["tool_results"]]
    assert len(tool_results) == 3
    parse_result = next(result for result in tool_results if result["tool_name"] == "parse_python_solution")
    assert parse_result["has_error"] is True
    assert json.loads(parse_result["content"])["executed"] is False
    execution_payloads = [
        json.loads(result["content"])
        for result in tool_results
        if result["tool_name"] == "execute_spreadsheet_solution"
    ]
    assert {payload["phase"] for payload in execution_payloads} == {"solution_validation", "case_replay"}
    assert {payload["case_index"] for payload in execution_payloads} == {1, 3}

    stale_payload = json.loads(json.dumps(session))
    stale_payload["turns"] = [{"turn_num": 1, "prompt_text": "STALE_ONE_TURN"}]
    stale_payload["num_turns"] = 1
    rebuilt = case_record_to_session(
        model_record,
        {"task_id": task.task_id, "case_index": case.case_index, "session_payload": stale_payload},
    )
    assert rebuilt["num_turns"] == 2
    assert [turn["prompt_text"] for turn in rebuilt["turns"]] == [
        "FIRST_USER_INSTRUCTION",
        "PARSER_CORRECTION_MESSAGE",
    ]
    assert "CASE3_REPLAY_SENTINEL" in json.dumps(rebuilt, ensure_ascii=False)


def test_infrastructure_error_retries_once_per_case() -> None:
    with tempfile.TemporaryDirectory() as directory:
        cfg = config(Path(directory))
        executor = CountingExecutor(first_infra=True)
        report = asyncio.run(collect(cfg, task_limit=1, model_client=CountingModel(), case_executor=executor))
        assert report["cases_completed"] == 3
        assert report["cases_unknown"] == 0
        assert executor.calls == [
            ("13-1", 1, 1),
            ("13-1", 1, 2),
            ("13-1", 2, 1),
            ("13-1", 2, 2),
            ("13-1", 3, 1),
            ("13-1", 3, 2),
        ]


def test_agent_refines_case_one_then_replays_only_final_solution() -> None:
    with tempfile.TemporaryDirectory() as directory:
        cfg = config(Path(directory))
        model = CountingModel()
        executor = CountingExecutor(first_task_failure=True)
        report = asyncio.run(collect(cfg, task_limit=1, model_client=model, case_executor=executor))
        assert report["cases_completed"] == 3
        assert model.calls == ["13-1", "13-1"]
        assert executor.calls == [
            ("13-1", 1, 1),
            ("13-1", 1, 1),
            ("13-1", 2, 1),
            ("13-1", 3, 1),
        ]
        model_record = json.loads((cfg.output_run_dir / "corpus/model/13-1.json").read_text(encoding="utf-8"))
        assert len(model_record["agent_steps"]) == 2
        assert "synthetic Python error" in json.dumps(model_record["messages"])
        case_record = json.loads(
            (cfg.output_run_dir / "corpus/cases/13-1/case-3.json").read_text(encoding="utf-8")
        )
        session = case_record_to_session(model_record, case_record)
        assert session["num_turns"] == 2
        assert session["turns"][0]["response_text"] == model_record["agent_steps"][0]["response"]
        assert session["turns"][1]["response_text"] == model_record["agent_steps"][1]["response"]
        assert "synthetic Python error" in session["turns"][1]["prompt_text"]
        phases = [
            json.loads(call["function"]["arguments"])["phase"]
            for turn in session["turns"]
            for call in turn["tool_calls"]
            if call["function"]["name"] == "execute_spreadsheet_solution"
        ]
        assert phases == ["solution_validation", "solution_validation", "case_replay"]


def test_exhausted_unknown_is_not_retried_on_resume() -> None:
    with tempfile.TemporaryDirectory() as directory:
        cfg = config(Path(directory))
        executor = CountingExecutor(always_infra=True)
        first = asyncio.run(collect(cfg, task_limit=1, model_client=CountingModel(), case_executor=executor))
        assert first["status"] == "incomplete"
        assert first["cases_unknown"] == 3
        assert len(executor.calls) == 2
        assert first["stop_reason"].startswith("circuit_breaker:")

        resumed = CountingExecutor(always_infra=True)
        second = asyncio.run(collect(cfg, task_limit=1, model_client=CountingModel(), case_executor=resumed))
        assert second["cases_unknown"] == 3
        assert resumed.calls == []


def test_abort_request_stops_before_model_or_case_work() -> None:
    with tempfile.TemporaryDirectory() as directory:
        cfg = config(Path(directory))
        abort_path = cfg.output_run_dir / "abort-request.json"
        abort_path.parent.mkdir(parents=True)
        abort_path.write_text('{"requested": true}\n', encoding="utf-8")
        model = CountingModel()
        executor = CountingExecutor()
        report = asyncio.run(collect(cfg, task_limit=1, model_client=model, case_executor=executor))
        assert report["status"] == "aborted"
        assert report["stop_reason"] == "abort_requested"
        assert model.calls == []
        assert executor.calls == []


def test_docker_executor_cancellation_kills_cli_and_container(tmp_path: Path) -> None:
    input_path = tmp_path / "input.xlsx"
    answer_path = tmp_path / "answer.xlsx"
    input_path.write_bytes(b"input")
    answer_path.write_bytes(b"answer")
    case = SpreadsheetCase("task", 1, input_path, answer_path)
    task = SpreadsheetTask("task", "instruction", "Cell-Level Manipulation", "A1", "Sheet1", "A1", "x", (case,))
    began = asyncio.Event()

    class HangingProcess:
        pid = 999_999

        async def communicate(self):
            began.set()
            await asyncio.Future()

        async def wait(self):
            return -9

    async def fake_subprocess(*args, **kwargs):
        del args, kwargs
        return HangingProcess()

    async def exercise():
        executor = DockerCaseExecutor(image="unused", require_recalculation=False)
        remove = AsyncMock()
        with (
            patch(
                "experiments.poisoned_evolution.spreadsheetbench.runtime.asyncio.create_subprocess_exec",
                new=fake_subprocess,
            ),
            patch("experiments.poisoned_evolution.spreadsheetbench.runtime.os.killpg") as killpg,
            patch.object(executor, "_force_remove_container", remove),
        ):
            running = asyncio.create_task(
                executor.execute(
                    task=task,
                    case=case,
                    solution="import time; time.sleep(60)",
                    work_dir=tmp_path / "work",
                    timeout_seconds=60,
                    attempt=1,
                )
            )
            await began.wait()
            running.cancel()
            try:
                await running
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("executor did not propagate cancellation")
            killpg.assert_called_once()
            remove.assert_awaited_once()

    asyncio.run(exercise())


def test_docker_executor_records_and_classifies_recalculation_failure_as_infrastructure(tmp_path: Path) -> None:
    input_path = tmp_path / "input.xlsx"
    answer_path = tmp_path / "answer.xlsx"
    input_path.write_bytes(b"input")
    answer_path.write_bytes(b"answer")
    case = SpreadsheetCase("task", 1, input_path, answer_path)
    task = SpreadsheetTask("task", "instruction", "Cell-Level Manipulation", "A1", "Sheet1", "A1", "x", (case,))
    work_dir = tmp_path / "work"
    captured: dict[str, object] = {}

    class FailedRecalculation:
        pid = 123_456
        returncode = 45

        async def communicate(self):
            (work_dir / "runtime-recalc.status").write_text("soffice_failed\n", encoding="utf-8")
            (work_dir / "runtime-recalc.exitcode").write_text("77\n", encoding="utf-8")
            (work_dir / "runtime-recalc.stdout.log").write_text("conversion output\n", encoding="utf-8")
            (work_dir / "runtime-recalc.stderr.log").write_text(
                "Fatal Error: User installation could not be completed.\n",
                encoding="utf-8",
            )
            return b"conversion output\n", b"Fatal Error: User installation could not be completed.\n"

    async def fake_subprocess(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FailedRecalculation()

    executor = DockerCaseExecutor(image="test-image", require_recalculation=True)
    with patch(
        "experiments.poisoned_evolution.spreadsheetbench.runtime.asyncio.create_subprocess_exec",
        new=fake_subprocess,
    ):
        result = asyncio.run(
            executor.execute(
                task=task,
                case=case,
                solution="print('solution completed')",
                work_dir=work_dir,
                timeout_seconds=60,
                attempt=1,
            )
        )

    command = str(captured["args"][-1])
    assert "-env:UserInstallation=file:///tmp/libreoffice-profile" in command
    assert "/out/runtime-recalc.stdout.log" in command
    assert result.kind == "infrastructure_error"
    assert result.score is None
    assert result.error == "recalculation failed: status=soffice_failed, soffice_returncode=77"
    assert result.recalculation == {
        "attempted": True,
        "status": "soffice_failed",
        "returncode": 77,
        "stdout": "conversion output\n",
        "stderr": "Fatal Error: User installation could not be completed.\n",
        "user_installation": "file:///tmp/libreoffice-profile",
    }


def test_docker_executor_keeps_solution_failure_distinct_from_recalculation_failure(tmp_path: Path) -> None:
    input_path = tmp_path / "input.xlsx"
    answer_path = tmp_path / "answer.xlsx"
    input_path.write_bytes(b"input")
    answer_path.write_bytes(b"answer")
    case = SpreadsheetCase("task", 1, input_path, answer_path)
    task = SpreadsheetTask("task", "instruction", "Cell-Level Manipulation", "A1", "Sheet1", "A1", "x", (case,))

    class FailedSolution:
        pid = 123_457
        returncode = 1

        async def communicate(self):
            return b"", b"Traceback: bad generated program\n"

    async def fake_subprocess(*_args, **_kwargs):
        return FailedSolution()

    executor = DockerCaseExecutor(image="test-image", require_recalculation=True)
    with patch(
        "experiments.poisoned_evolution.spreadsheetbench.runtime.asyncio.create_subprocess_exec",
        new=fake_subprocess,
    ):
        result = asyncio.run(
            executor.execute(
                task=task,
                case=case,
                solution="raise RuntimeError('bad generated program')",
                work_dir=tmp_path / "work",
                timeout_seconds=60,
                attempt=1,
            )
        )

    assert result.kind == "task_failure"
    assert result.score is not None and result.score.score == 0.0
    assert result.recalculation is None


def test_real_docker_recalculation_works_under_exact_sandbox_profile(tmp_path: Path) -> None:
    image = "shared-skill-spreadsheetbench:20261020"
    if not shutil.which("docker"):
        pytest.skip("docker CLI unavailable")
    image_check = subprocess.run(
        ["docker", "image", "inspect", image],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if image_check.returncode != 0:
        pytest.skip(f"runtime image unavailable: {image}")

    runtime = docker_runtime_readiness(image=image, require_recalculation=True)
    assert runtime["ready"] is True
    assert runtime["recalculation_probe"]["ready"] is True
    assert runtime["recalculation_probe"]["read_only_root"] is True
    assert runtime["recalculation_probe"]["user_installation"] == "file:///tmp/libreoffice-profile"

    task = load_tasks(DATASET)[0]
    result = asyncio.run(
        DockerCaseExecutor(image=image, require_recalculation=True).execute(
            task=task,
            case=task.cases[0],
            solution=(
                "import os, shutil\n"
                "shutil.copyfile(os.environ['INPUT_PATH'], os.environ['OUTPUT_PATH'])\n"
            ),
            work_dir=tmp_path / "real-execution",
            timeout_seconds=60,
            attempt=1,
        )
    )
    assert result.kind == "completed"
    assert result.returncode == 0
    assert result.recalculation is not None
    assert result.recalculation["status"] == "success"
    assert result.recalculation["returncode"] == 0
    assert result.recalculation["user_installation"] == "file:///tmp/libreoffice-profile"


def test_dry_run_does_not_create_run_directories() -> None:
    with tempfile.TemporaryDirectory() as directory:
        cfg = config(Path(directory))
        report = asyncio.run(collect(cfg, dry_run=True, task_limit=1))
        assert report["status"] == "dry_run"
        assert report["evaluated"] is False
        assert not cfg.artifact_run_dir.exists()
        assert not cfg.output_run_dir.exists()


def test_scorer_uses_generated_output_and_preserves_official_first_sheet_semantics(tmp_path: Path) -> None:
    input_path = tmp_path / "input.xlsx"
    answer_path = tmp_path / "answer.xlsx"
    output_path = tmp_path / "output.xlsx"
    for path, value in ((input_path, b"input"), (answer_path, b"answer"), (output_path, b"output")):
        path.write_bytes(value)
    case = SpreadsheetCase("task", 1, input_path, answer_path)
    task = SpreadsheetTask("task", "instruction", "Cell-Level Manipulation", "B2:B3", "Results", "A1:B3", "x", (case,))
    seen = {}

    def comparator(answer, prediction, instruction_type, answer_position):
        seen.update(
            answer=answer, prediction=prediction, instruction_type=instruction_type, answer_position=answer_position
        )
        return True, ""

    score = score_workbook(task=task, case=case, output_path=output_path, comparator=comparator)
    assert score.passed is True
    assert seen["answer"] == str(answer_path)
    assert seen["prediction"] == str(output_path)
    assert seen["prediction"] != str(input_path)
    assert seen["answer_position"] == "B2:B3"


def test_task_60_7_preserves_official_first_workbook_sheet_selection() -> None:
    task = next(task for task in load_tasks(DATASET) if task.task_id == "60-7")

    assert task.answer_position == "A3:E11"
    assert task.answer_sheet == "Consolidated Tracker,Existing Task,Additions,Retired"
    assert normalize_answer_position(task.answer_position, task.answer_sheet) == "A3:E11"


def test_frozen_scoring_contract_overrides_13_1_and_preserves_53_14_first_sheet(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    tasks = {task.task_id: task for task in load_tasks(DATASET)}
    task_13 = _apply_scoring_rule(cfg, tasks["13-1"])
    task_53 = _apply_scoring_rule(cfg, tasks["53-14"])

    assert task_13.answer_position == "'LISTS'!A3:D32"
    assert task_53.answer_position == "B1:C5"
    for task in (task_13, task_53):
        baseline = score_workbook(
            task=task,
            case=task.cases[0],
            output_path=task.cases[0].input_path,
            evaluator_path=cfg.evaluator_path,
        )
        assert baseline.status == "completed"
        assert baseline.passed is False


def test_73_45_is_baseline_pass_and_explicitly_carrier_excluded(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    task = _apply_scoring_rule(cfg, next(task for task in load_tasks(DATASET) if task.task_id == "73-45"))
    baseline = score_workbook(
        task=task,
        case=task.cases[0],
        output_path=task.cases[0].input_path,
        evaluator_path=cfg.evaluator_path,
    )

    assert baseline.passed is True
    assert baseline.normalized_answer_position == "'Sheet1'!BD2:BD308"
    assert cfg.scoring_rule("73-45")["carrier_excluded"] is True
    assert _carrier_eligible(cfg, task, output_pass=True, baseline_input_pass=baseline.passed) is False


@pytest.mark.parametrize(
    ("answer_position", "answer_sheet", "expected"),
    [
        ("'Already Qualified'!A1:B2", "Ignored,Other", "'Already Qualified'!A1:B2"),
        ("A1:A2,B1:B2", "First,Second", "A1:A2,B1:B2"),
        ("Sheet'!A1:B2", "Sheet", "'Sheet'!A1:B2"),
        ("'Sheet!'A1:B2'", "Sheet", "'Sheet'!A1:B2"),
    ],
)
def test_answer_position_normalization_preserves_qualified_and_matched_ranges(
    answer_position: str, answer_sheet: str, expected: str
) -> None:
    assert normalize_answer_position(answer_position, answer_sheet) == expected


@pytest.mark.parametrize(
    ("task_id", "expected"),
    [
        ("53-14", "B1:C5"),
        ("60-7", "A3:E11"),
        ("73-45", "'Sheet1'!BD2:BD308"),
    ],
)
def test_dataset_answer_range_repairs_without_changing_first_sheet_selection(
    task_id: str, expected: str
) -> None:
    task = next(task for task in load_tasks(DATASET) if task.task_id == task_id)
    assert normalize_answer_position(task.answer_position, task.answer_sheet) == expected


@pytest.mark.parametrize(
    ("case_index", "expected"),
    [
        (1, "'Sheet3'!A1:G2,'Sheet4'!A1:G2"),
        (2, "'Sheet3'!A1:G1,'Sheet4'!A1:G2"),
        (3, "'Sheet3'!A1:G1,'Sheet4'!A1:G1"),
    ],
)
def test_task_283_32_expands_whole_columns_to_the_scored_workbook_rows(
    case_index: int, expected: str
) -> None:
    task = next(task for task in load_tasks(DATASET) if task.task_id == "283-32")
    case = task.cases[case_index - 1]
    assert (
        resolve_answer_position(
            task.answer_position,
            task.answer_sheet,
            answer_path=case.answer_path,
            output_path=case.answer_path,
        )
        == expected
    )


def test_whole_column_range_uses_maximum_answer_and_output_row(tmp_path: Path) -> None:
    from openpyxl import Workbook

    answer_path = tmp_path / "answer.xlsx"
    output_path = tmp_path / "output.xlsx"
    for path, row in ((answer_path, 2), (output_path, 5)):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Results"
        sheet.cell(row=row, column=1, value="x")
        workbook.save(path)
        workbook.close()

    assert (
        resolve_answer_position(
            "'Results'!A:G",
            "Results",
            answer_path=answer_path,
            output_path=output_path,
        )
        == "'Results'!A1:G5"
    )


@pytest.mark.parametrize("answer_position", ["", "A:", "A1:B", "B2:A1", "XFE1:XFE2"])
def test_unsupported_or_invalid_answer_ranges_fail_closed(tmp_path: Path, answer_position: str) -> None:
    answer_path = tmp_path / "answer.xlsx"
    output_path = tmp_path / "output.xlsx"
    answer_path.write_bytes(b"answer")
    output_path.write_bytes(b"output")
    case = SpreadsheetCase("task", 1, tmp_path / "input.xlsx", answer_path)
    task = SpreadsheetTask(
        "task",
        "instruction",
        "Cell-Level Manipulation",
        answer_position,
        "Sheet1",
        "A1",
        "x",
        (case,),
    )

    score = score_workbook(
        task=task,
        case=case,
        output_path=output_path,
        comparator=lambda *_args: (True, ""),
    )
    assert score.status == "infrastructure_error"
    assert score.passed is None
    assert score.score is None
    assert score.message.startswith("grader range resolution failed:")


def test_all_100_dataset_tasks_self_compare_with_non_vacuous_resolved_ranges() -> None:
    failures = []
    for task in load_tasks(DATASET):
        case = task.cases[0]
        score = score_workbook(task=task, case=case, output_path=case.answer_path)
        if score.passed is not True:
            failures.append((task.task_id, score.status, score.message, score.normalized_answer_position))

    assert failures == []


def test_runtime_bootstrap_is_explicit() -> None:
    command = docker_runtime_build_command(image="spreadsheetbench-runtime:test")
    assert command.startswith("docker build --pull=false")
    assert "Dockerfile.spreadsheet-runtime" in command


def test_single_task_timeout_covers_generation_and_all_three_cases() -> None:
    class HangingExecutor:
        def __init__(self) -> None:
            self.timeouts: list[float] = []
            self.cancelled = False

        async def execute(self, *, timeout_seconds, **_kwargs):
            self.timeouts.append(timeout_seconds)
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    with tempfile.TemporaryDirectory() as directory:
        cfg = config(Path(directory), task_timeout_seconds=0.05)
        executor = HangingExecutor()
        report = asyncio.run(collect(cfg, task_limit=1, model_client=CountingModel(), case_executor=executor))
        assert report["cases_unknown"] == 3
        assert executor.cancelled is True
        assert len(executor.timeouts) == 1
        assert 0 < executor.timeouts[0] <= cfg.task_timeout_seconds
        task_state = json.loads((cfg.output_run_dir / "corpus/tasks/13-1.json").read_text(encoding="utf-8"))
        assert task_state["reason"] == "task_timeout"


def test_provider_retry_time_is_split_across_the_single_task_deadline() -> None:
    class HangingModel:
        def __init__(self) -> None:
            self.calls = 0

        async def generate_solution(self, **_kwargs):
            self.calls += 1
            await asyncio.sleep(60)

    with tempfile.TemporaryDirectory() as directory:
        cfg = replace(
            config(Path(directory)),
            task_timeout_seconds=0.25,
            model_timeout_seconds=60,
        )
        model = HangingModel()
        report = asyncio.run(collect(cfg, task_limit=1, model_client=model, case_executor=CountingExecutor()))

        assert model.calls == 2
        assert report["cases_unknown"] == 3
        model_record = json.loads((cfg.output_run_dir / "corpus/model/13-1.json").read_text(encoding="utf-8"))
        assert model_record["status"] == "provider_unknown"
        assert len(model_record["attempts"]) == 2
        assert all(attempt["status"] == "provider_error" for attempt in model_record["attempts"])
        assert all("TimeoutError" in attempt["error"] for attempt in model_record["attempts"])


def test_cancelled_dispatched_provider_call_is_persisted_before_propagation() -> None:
    class HangingModel:
        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def generate_solution(self, **_kwargs):
            self.started.set()
            await asyncio.Future()

    async def exercise(cfg: CorpusConfig) -> None:
        model = HangingModel()
        running = asyncio.create_task(
            collect(cfg, task_limit=1, model_client=model, case_executor=CountingExecutor())
        )
        await model.started.wait()
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running

    with tempfile.TemporaryDirectory() as directory:
        cfg = replace(config(Path(directory)), task_timeout_seconds=60, model_timeout_seconds=60)
        asyncio.run(exercise(cfg))
        model_record = json.loads((cfg.output_run_dir / "corpus/model/13-1.json").read_text(encoding="utf-8"))
        assert model_record["status"] == "provider_unknown"
        assert len(model_record["attempts"]) == 1
        attempt = model_record["attempts"][0]
        assert attempt["status"] == "provider_error"
        assert attempt["cancelled"] is True
        assert "cancelled before a response" in attempt["error"]
        task_state = json.loads((cfg.output_run_dir / "corpus/tasks/13-1.json").read_text(encoding="utf-8"))
        assert task_state["status"] == "aborted"
        assert task_state["reason"] == "cancelled"
