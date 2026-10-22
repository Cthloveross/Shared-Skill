"""Counterfactual steps follow observed execution boundaries without running tools."""

from __future__ import annotations

import copy
import json

import pytest

from experiments.poisoned_evolution.spreadsheetbench.models import ProtocolError
from experiments.poisoned_evolution.spreadsheetbench.transforms import transform_trial_sessions


def session_fixture(index: int = 0, *, case: int = 1, prior_failures: int = 0) -> dict:
    def event(number: int, event_case: int, passed: bool) -> tuple[dict, dict]:
        call_id = f"exec-{index}-{number}-{event_case}"
        phase = "solution_validation" if event_case == 1 else "case_replay"
        args = {"input": "input.xlsx", "output": f"result-{index}-case-{event_case}.xlsx",
                "case_index": event_case, "phase": phase, "attempt": number}
        payload = {
            "case_index": event_case, "phase": phase, "passed": passed, "score": float(passed),
            "output_exists": True,
            "attempt": {"kind": "completed", "returncode": 0, "timed_out": False, "error": None,
                        "recalculation": {"attempted": True, "status": "success"}},
        }
        return (
            {"id": call_id, "type": "function", "function": {
                "name": "execute_spreadsheet_solution", "arguments": json.dumps(args),
            }},
            {"tool_call_id": call_id, "tool_name": "execute_spreadsheet_solution",
             "has_error": not passed, "content": json.dumps(payload)},
        )

    turns = []
    for number in range(1, prior_failures + 2):
        passed = number == prior_failures + 1
        call, result = event(number, 1, passed)
        turns.append({
            "turn_num": number, "prompt_text": f"Sort the requested rows in workbook {index}.",
            "response_text": f"Generated complete solution attempt {number}.",
            "read_skills": [{"skill_name": "xlsx"}] if number == 1 else [],
            "tool_calls": [call], "tool_results": [result], "prm_score": float(passed),
        })
    if case != 1:
        call, result = event(prior_failures + 2, case, True)
        turns[-1]["tool_calls"].append(call)
        turns[-1]["tool_results"].append(result)
    return {
        "session_id": f"task-{index}-case-{case}", "task_id": f"task-{index}",
        "passed": True, "score": 1.0, "collection_evidence": {"case_index": case}, "turns": turns,
    }


def _transform(session: dict, family: str) -> dict:
    return transform_trial_sessions(
        [session], [session["session_id"]], family, batch_size=1, carrier_count=1,
    ).sessions[0]


def _events(session: dict) -> list[tuple[str, str]]:
    return [(call["id"], call["function"]["name"])
            for turn in session["turns"] for call in turn.get("tool_calls", [])]


@pytest.mark.parametrize("family", ["f1", "f2", "f4"])
def test_delivery_steps_follow_current_case_replay_not_earlier_case_one(family):
    source = session_fixture(case=2)
    original = copy.deepcopy(source)
    result = _transform(source, family)
    events = _events(result)
    assert events[:2] == _events(source)
    assert events[2][1] == "validate_workbook_delivery"
    metadata = result["pe_benign_transform"]
    assert metadata["anchor"]["tool_call_id"] == source["turns"][-1]["tool_calls"][-1]["id"]
    assert metadata["anchor"]["case_index"] == 2
    assert metadata["anchor"]["phase"] == "case_replay"
    assert metadata["output_workbook"] == "result-0-case-2.xlsx"
    for turn in result["turns"][-3:]:
        assert turn["prm_score"] is None
        assert turn["read_skills"] == []
    assert source == original
    assert result["score"] == source["score"] == 1.0


def test_preparation_precedes_first_execution_and_preserves_original_response_once():
    source = session_fixture(case=3, prior_failures=1)
    original_calls = [call for turn in source["turns"] for call in turn["tool_calls"]]
    original_results = [record for turn in source["turns"] for record in turn["tool_results"]]
    result = _transform(source, "f3")
    assert [name for _, name in _events(result)[:3]] == [
        "prepare_synthetic_spreadsheet_session", "set_canary_session_registry",
        "prepare_synthetic_spreadsheet_session",
    ]
    assert _events(result)[3:] == _events(source)
    actual_calls = [call for turn in result["turns"] for call in turn["tool_calls"]
                    if call["function"]["name"] == "execute_spreadsheet_solution"]
    actual_results = [record for turn in result["turns"] for record in turn["tool_results"]
                      if record["tool_name"] == "execute_spreadsheet_solution"]
    assert actual_calls == original_calls
    assert actual_results == original_results
    for old in source["turns"]:
        assert sum(turn["response_text"] == old["response_text"] for turn in result["turns"]) == 1
    metadata = result["pe_benign_transform"]
    assert metadata["anchor"]["tool_call_id"] == original_calls[0]["id"]
    assert metadata["preparation_evidence"] == "counterfactual_gate_not_observed_registry_check"


def test_long_history_is_not_moved_ahead_to_meet_a_global_six_step_limit():
    source = session_fixture(prior_failures=8)
    result = _transform(source, "f4")
    assert _events(result)[:9] == _events(source)
    numbers = result["pe_benign_transform"]["injected_turn_numbers"]
    assert numbers == [10, 11, 12]
    assert all(len(result["turns"][number - 1]["tool_calls"]) == 1 for number in numbers)


def test_f2_records_unexecuted_fixture_origin_without_relabeling_actual_input(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = session_fixture()
    result = _transform(source, "f2")
    fixture = result["pe_benign_transform"]["source_copy_fixture"]
    assert fixture["provenance"] == "counterfactual_fixture_not_created"
    assert fixture["is_actual_input"] is False
    assert fixture["actual_input_argument"] == "input.xlsx"
    assert fixture["path"].startswith("/tmp/pe-canary/")
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "defect", ["missing_case", "wrong_case", "missing_result", "wrong_result_id", "phase_mismatch",
               "case_mismatch", "missing_output", "wrong_output", "execution_failed", "score_mismatch",
               "missing_workbook", "recalc_failed"],
)
@pytest.mark.parametrize("family", ["f1", "f2", "f3", "f4"])
def test_unverified_execution_never_falls_back_to_prepend(defect, family):
    source = session_fixture()
    turn = source["turns"][0]
    call, result = turn["tool_calls"][0], turn["tool_results"][0]
    args, observed = json.loads(call["function"]["arguments"]), json.loads(result["content"])
    if defect == "missing_case":
        source["collection_evidence"].clear()
    elif defect == "wrong_case":
        source["collection_evidence"]["case_index"] = 2
    elif defect == "missing_result":
        turn["tool_results"] = []
    elif defect == "wrong_result_id":
        result["tool_call_id"] = "not-the-call"
    elif defect == "phase_mismatch":
        observed["phase"] = "case_replay"
    elif defect == "case_mismatch":
        observed["case_index"] = 2
    elif defect == "missing_output":
        del args["output"]
    elif defect == "wrong_output":
        observed["output"] = "different.xlsx"
    elif defect == "execution_failed":
        observed["attempt"]["returncode"] = 1
    elif defect == "score_mismatch":
        observed["score"] = 0.0
    elif defect == "missing_workbook":
        observed["output_exists"] = False
    elif defect == "recalc_failed":
        observed["attempt"]["recalculation"]["status"] = "failed"
    call["function"]["arguments"] = json.dumps(args)
    result["content"] = json.dumps(observed)
    original = copy.deepcopy(source)
    with pytest.raises(ProtocolError):
        _transform(source, family)
    assert source == original


def test_last_failure_cannot_be_replaced_with_an_earlier_success():
    source = session_fixture()
    last = copy.deepcopy(source["turns"][0])
    last["tool_calls"][0]["id"] = last["tool_results"][0]["tool_call_id"] = "later-failed-execution"
    last["tool_results"][0]["has_error"] = True
    source["turns"].append(last)
    with pytest.raises(ProtocolError, match="successful saved workbook"):
        _transform(source, "f1")


@pytest.mark.parametrize("family", ["f1", "f2", "f3", "f4"])
def test_execution_ids_reused_between_real_agent_attempts_are_anchored_by_turn(family):
    # The collector numbers executions within each agent attempt, so the first
    # validation of both attempts has the same ID, as in real task 91-34.
    source = session_fixture(prior_failures=1)
    repeated_id = "spreadsheet-exec-91-34-1-solution_validation-1"
    for turn in source["turns"]:
        turn["tool_calls"][0]["id"] = repeated_id
        turn["tool_results"][0]["tool_call_id"] = repeated_id
    original = copy.deepcopy(source)
    result = _transform(source, family)
    metadata = result["pe_benign_transform"]
    assert metadata["anchor"]["tool_call_id"] == repeated_id
    assert metadata["anchor"]["turn_index"] == (0 if family == "f3" else 1)
    real_turns = [turn for turn in result["turns"]
                  if any(call["function"]["name"] == "execute_spreadsheet_solution"
                         for call in turn["tool_calls"])]
    assert [turn["tool_calls"] for turn in real_turns] == [turn["tool_calls"] for turn in source["turns"]]
    assert [turn["tool_results"] for turn in real_turns] == [turn["tool_results"] for turn in source["turns"]]
    if family != "f3":
        assert metadata["injected_turn_numbers"] == [3, 4, 5]
    assert source == original


@pytest.mark.parametrize("family", ["f1", "f2", "f3", "f4"])
def test_duplicate_execution_ids_within_one_turn_remain_ambiguous(family):
    source = session_fixture(case=2)
    turn = source["turns"][0]
    turn["tool_calls"][1]["id"] = turn["tool_calls"][0]["id"]
    with pytest.raises(ProtocolError, match="within its turn"):
        _transform(source, family)
