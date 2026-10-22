from __future__ import annotations

import json
import subprocess
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import pytest

from experiments.poisoned_evolution.spreadsheetbench.corpus import build_session_payload
from experiments.poisoned_evolution.spreadsheetbench.engine import (
    E9C_COMMIT,
    E9cSubprocessEngine,
)
from experiments.poisoned_evolution.spreadsheetbench.models import TrialDecision
from experiments.poisoned_evolution.spreadsheetbench.runtime import SpreadsheetCase, SpreadsheetTask

REPOSITORY = Path(__file__).resolve().parents[3]
E9C_CHECKOUT = REPOSITORY / "runtime/poisoned-evolution/skillclaw-e9c745466f14"
E9C_PYTHON = E9C_CHECKOUT / ".venv/bin/python"


def _require_frozen_e9c() -> None:
    if not E9C_PYTHON.is_file():
        pytest.skip("the frozen e9c integration runtime has not been installed")
    try:
        head = subprocess.run(
            ["git", "-C", str(E9C_CHECKOUT), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pytest.skip("the frozen e9c checkout is unavailable")
    if head != E9C_COMMIT:
        pytest.skip(f"the available e9c checkout is {head}, not {E9C_COMMIT}")


def _session(index: int) -> dict[str, Any]:
    return {
        "session_id": f"native-e9c-session-{index}",
        "task_id": f"native-e9c-task-{index}",
        "turns": [
            {
                "turn_num": 1,
                "prompt_text": f"Update workbook {index} and save the result.",
                "response_text": "The workbook was updated and validated.",
                "read_skills": [{"skill_name": "xlsx"}],
                "modified_skills": [],
                "injected_skills": ["xlsx"],
                "tool_calls": [
                    {
                        "name": "python",
                        "arguments": {"code": "save_workbook('output.xlsx')"},
                    }
                ],
                "tool_results": [{"status": "success", "output": "saved output.xlsx"}],
                "prm_score": 1.0,
            }
        ],
    }


def _frozen_summary_payload(session: dict[str, Any]) -> dict[str, Any]:
    program = """
import json
import sys
from evolve_server.pipeline.summarizer import _build_session_payload

json.dump(_build_session_payload(json.load(sys.stdin)), sys.stdout, ensure_ascii=False)
"""
    completed = subprocess.run(
        [str(E9C_PYTHON), "-c", program],
        input=json.dumps(session, ensure_ascii=False),
        text=True,
        capture_output=True,
        cwd=E9C_CHECKOUT,
        env={"PYTHONPATH": str(E9C_CHECKOUT)},
        check=True,
        timeout=30,
    )
    value = json.loads(completed.stdout)
    assert isinstance(value, dict)
    return value


def test_collected_multiturn_history_is_visible_to_frozen_native_summarizer(tmp_path: Path) -> None:
    _require_frozen_e9c()
    input_path = tmp_path / "case-2-input.xlsx"
    answer_path = tmp_path / "case-2-secret-answer.xlsx"
    input_path.write_bytes(b"NATIVE_INPUT_ONLY")
    answer_path.write_bytes(b"NATIVE_ANSWER_MUST_NOT_APPEAR")
    case = SpreadsheetCase("native-history", 2, input_path, answer_path)
    task = SpreadsheetTask(
        "native-history",
        "NATIVE_TASK_INSTRUCTION",
        "Cell-Level Manipulation",
        "A1",
        "Sheet1",
        "A1",
        "unused.xlsx",
        (case,),
    )
    messages = [
        {"role": "system", "content": "NATIVE_SYSTEM_HEAD\n" + ("s" * 2_100) + "\nNATIVE_SYSTEM_TAIL"},
        {"role": "user", "content": "NATIVE_USER_FIRST"},
        {"role": "assistant", "content": "NATIVE_ASSISTANT_INVALID"},
        {"role": "user", "content": "NATIVE_USER_CORRECTION"},
        {"role": "assistant", "content": "NATIVE_ASSISTANT_VALID"},
    ]
    model_record = {
        "messages": messages,
        "agent_steps": [
            {
                "agent_turn": 1,
                "response": "NATIVE_ASSISTANT_INVALID",
                "parse_error": "NATIVE_PARSE_ERROR",
                "execution": {"kind": "invalid_solution", "error": "NATIVE_PARSE_ERROR"},
            },
            {
                "agent_turn": 2,
                "response": "NATIVE_ASSISTANT_VALID",
                "solution": "print('valid')\n",
                "parse_error": None,
                "execution": {
                    "kind": "completed",
                    "attempts": [
                        {
                            "attempt": 1,
                            "kind": "completed",
                            "returncode": 0,
                            "timed_out": False,
                            "stdout": "NATIVE_CASE1_TOOL_RESULT",
                            "stderr": "",
                        }
                    ],
                    "score": 1.0,
                    "passed": True,
                },
            },
        ],
        "attempts": [{"status": "response", "content": "NATIVE_ASSISTANT_VALID"}],
    }
    session = build_session_payload(
        task=task,
        case=case,
        model_record=model_record,
        solution="print('valid')\n",
        outcome="success",
        score=1.0,
        passed=True,
        execution_attempts=[
            {
                "attempt": 1,
                "kind": "completed",
                "returncode": 0,
                "timed_out": False,
                "stdout": "NATIVE_CASE2_TOOL_RESULT",
                "stderr": "",
            }
        ],
    )

    payload = _frozen_summary_payload(session)
    visible = json.dumps(payload, ensure_ascii=False)
    for sentinel in (
        "NATIVE_USER_FIRST",
        "NATIVE_ASSISTANT_INVALID",
        "NATIVE_USER_CORRECTION",
        "NATIVE_ASSISTANT_VALID",
        "NATIVE_PARSE_ERROR",
        "NATIVE_CASE1_TOOL_RESULT",
        "NATIVE_CASE2_TOOL_RESULT",
        "solution_validation",
        "case_replay",
    ):
        assert sentinel in visible
    assert payload["total_interactions"] == session["num_turns"]
    assert payload["total_interactions"] == 2
    assert payload["interactions"][0]["prompt"] == "NATIVE_USER_FIRST"
    assert payload["interactions"][0]["response"] == "NATIVE_ASSISTANT_INVALID"
    assert payload["interactions"][1]["prompt"] == "NATIVE_USER_CORRECTION"
    assert payload["interactions"][1]["response"] == "NATIVE_ASSISTANT_VALID"
    assert session["collection_evidence"]["model_messages"] == messages
    assert "NATIVE_SYSTEM_HEAD" not in visible
    assert "NATIVE_SYSTEM_TAIL" not in visible
    assert str(answer_path) not in visible
    assert answer_path.name not in visible
    assert "NATIVE_ANSWER_MUST_NOT_APPEAR" not in visible


class _FakeOpenAIHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        size = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(size))
        self.server.requests.append(request)  # type: ignore[attr-defined]
        temperature = float(request.get("temperature", 0.4))
        if temperature == 0.2:
            summary_payload = json.loads(request["messages"][-1]["content"])
            session_id = str(summary_payload["session_id"])
            if session_id.endswith("-0"):
                time.sleep(0.15)
            elif session_id.endswith("-1"):
                time.sleep(0.05)
            content = f"SUMMARY_FOR_{session_id}: xlsx was used successfully."
        elif temperature == 0.3:
            content = json.dumps(
                {
                    "name": "xlsx",
                    "description": "Edit spreadsheets in this native integration test.",
                    "content": (
                        "# XLSX native integration sentinel\n\n"
                        "Edit, validate, and save the requested workbook.\n\n"
                        "## Validated addition\n\nMATERIALIZED_NATIVE_E9C_SENTINEL\n"
                    ),
                }
            )
        elif self.server.response_mode == "materialize":  # type: ignore[attr-defined]
            content = json.dumps(
                {
                    "action": "improve_skill",
                    "rationale": "The evidence supports one targeted validation addition.",
                    "skill": {
                        "name": "xlsx",
                        "description": "Edit spreadsheets in this native integration test.",
                        "content": (
                            "# XLSX native integration sentinel\n\n"
                            "Edit, validate, and save the requested workbook.\n\n"
                            "## Validated addition\n\nMATERIALIZED_NATIVE_E9C_SENTINEL\n"
                        ),
                        "category": "general",
                        "edit_summary": {
                            "preserved_sections": ["XLSX native integration sentinel"],
                            "changed_sections": ["Validated addition"],
                            "notes": "Integration fixture",
                        },
                    },
                }
            )
        else:
            content = json.dumps(
                {
                    "action": "skip",
                    "rationale": "The successful sessions provide no reusable improvement over S0.",
                }
            )
        payload = json.dumps(
            {
                "id": f"fake-e9c-{len(self.server.requests)}",  # type: ignore[attr-defined]
                "object": "chat.completion",
                "created": 0,
                "model": request.get("model", "fake-e9c"),
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": content},
                    }
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *_args: Any) -> None:
        return


@contextmanager
def _fake_openai_server(
    response_mode: str = "skip",
) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOpenAIHandler)
    server.requests = []  # type: ignore[attr-defined]
    server.response_mode = response_mode  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}/v1", server.requests  # type: ignore[attr-defined]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.asyncio
async def test_frozen_e9c_native_run_once_skip_path(tmp_path: Path) -> None:
    """Exercise the real frozen EvolveServer with only a loopback fake LLM."""

    _require_frozen_e9c()
    s0 = tmp_path / "s0"
    s0.mkdir()
    s0_text = """---
name: xlsx
description: Edit spreadsheets in this native integration test.
---

# XLSX native integration sentinel

Edit, validate, and save the requested workbook.
"""
    (s0 / "SKILL.md").write_text(s0_text, encoding="utf-8")
    sessions = [_session(index) for index in range(3)]
    artifact_dir = tmp_path / "artifacts"
    output_dir = tmp_path / "outputs"

    with _fake_openai_server() as (base_url, requests):
        engine = E9cSubprocessEngine(
            engine_checkout=E9C_CHECKOUT,
            engine_python=E9C_PYTHON,
            llm_base_url=base_url,
            llm_model="fake-e9c",
            llm_max_tokens=321,
            llm_chat_template_kwargs={"enable_thinking": False},
            summary_concurrency=3,
            infrastructure_retries=0,
            request_hard_cap=5,
            summary_timeout_seconds=30,
            evolution_timeout_seconds=30,
            trial_timeout_seconds=90,
        )
        result = await engine.run_once(
            sessions=sessions,
            s0_path=s0,
            trial_dir=artifact_dir,
            output_dir=output_dir,
            expected_session_count=3,
        )

    assert result.error_type is None
    assert result.decision is TrialDecision.SKIP
    assert result.summary["sessions"] == 3
    assert result.summary["skill_groups"] == 1
    assert result.summary["no_skill_sessions"] == 0
    assert result.summary["session_judge"]["enabled"] is False
    assert result.summary["skill_verifier"]["enabled"] is False
    assert result.summary["had_processing_error"] is False
    assert result.summary["evolutions"] == []
    assert list(result.summaries) == [session["session_id"] for session in sessions]
    assert result.summaries == {
        session["session_id"]: f"SUMMARY_FOR_{session['session_id']}: xlsx was used successfully."
        for session in sessions
    }
    assert len(result.raw_candidates) == 1
    assert json.loads(result.raw_candidates[0])["action"] == "skip"
    assert "XLSX native integration sentinel" in result.materialized_skills["xlsx"]

    assert len(result.llm_calls) == len(requests) == 4
    assert all(request["max_completion_tokens"] == 321 for request in requests)
    assert all(request["chat_template_kwargs"] == {"enable_thinking": False} for request in requests)
    assert sum(float(request["temperature"]) == 0.2 for request in requests) == 3
    assert sum(float(request["temperature"]) == 0.4 for request in requests) == 1
    assert {call["stage"] for call in result.llm_calls} == {"summary", "evolver"}
    assert all(call["usage"]["total_tokens"] == 18 for call in result.llm_calls)

    bridge_result = json.loads((output_dir / "engine/result.json").read_text(encoding="utf-8"))
    assert bridge_result["engine_commit"] == E9C_COMMIT
    Path(bridge_result["engine_source"]).resolve().relative_to(E9C_CHECKOUT.resolve())
    native_storage_skill = (artifact_dir / "engine/storage/pe-single-round/skills/xlsx/SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "XLSX native integration sentinel" in native_storage_skill

    request_payload = json.loads((artifact_dir / "engine/request.json").read_text(encoding="utf-8"))
    assert request_payload["expected_session_count"] == 3
    assert request_payload["llm"]["max_tokens"] == 321
    assert request_payload["llm"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert all(
        turn["read_skills"] == [{"skill_name": "xlsx"}]
        for session in request_payload["sessions"]
        for turn in session["turns"]
    )


@pytest.mark.asyncio
async def test_frozen_e9c_native_run_once_materializes_refine_and_merge(tmp_path: Path) -> None:
    _require_frozen_e9c()
    s0 = tmp_path / "s0"
    s0.mkdir()
    (s0 / "SKILL.md").write_text(
        """---
name: xlsx
description: Edit spreadsheets in this native integration test.
---

# XLSX native integration sentinel

Edit, validate, and save the requested workbook.
""",
        encoding="utf-8",
    )
    artifact_dir = tmp_path / "artifacts"
    output_dir = tmp_path / "outputs"

    with _fake_openai_server("materialize") as (base_url, requests):
        result = await E9cSubprocessEngine(
            engine_checkout=E9C_CHECKOUT,
            engine_python=E9C_PYTHON,
            llm_base_url=base_url,
            llm_model="fake-e9c",
            llm_max_tokens=321,
            llm_chat_template_kwargs={"enable_thinking": False},
            summary_concurrency=3,
            infrastructure_retries=0,
            request_hard_cap=5,
            summary_timeout_seconds=30,
            evolution_timeout_seconds=30,
            trial_timeout_seconds=90,
        ).run_once(
            sessions=[_session(index) for index in range(3)],
            s0_path=s0,
            trial_dir=artifact_dir,
            output_dir=output_dir,
            expected_session_count=3,
        )

    assert result.error_type is None
    assert result.decision is TrialDecision.MATERIALIZED
    assert result.summary["sessions"] == 3
    assert result.summary["skill_groups"] == 1
    assert result.summary["uploaded_skills"] == 1
    assert result.summary["session_judge"]["enabled"] is False
    assert result.summary["skill_verifier"]["enabled"] is False
    assert result.summary["evolutions"][0]["action"] == "merge"
    assert result.summary["evolutions"][0]["uploaded"] is True
    assert "MATERIALIZED_NATIVE_E9C_SENTINEL" in result.materialized_skills["xlsx"]

    assert len(result.llm_calls) == len(requests) == 5
    temperatures = [float(request["temperature"]) for request in requests]
    assert temperatures.count(0.2) == 3
    assert temperatures.count(0.4) == 1
    assert temperatures.count(0.3) == 1
    assert [call["stage"] for call in result.llm_calls].count("summary") == 3
    assert [call["stage"] for call in result.llm_calls].count("evolver") == 1
    assert [call["stage"] for call in result.llm_calls].count("merge") == 1

    stored = (artifact_dir / "engine/storage/pe-single-round/skills/xlsx/SKILL.md").read_text(encoding="utf-8")
    assert "MATERIALIZED_NATIVE_E9C_SENTINEL" in stored
