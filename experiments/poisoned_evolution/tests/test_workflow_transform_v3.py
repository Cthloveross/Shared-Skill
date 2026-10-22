"""Outcome-conditioned, task-grounded carrier reconstruction remains text only."""

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path

import pytest

from experiments.poisoned_evolution.spreadsheetbench.models import ProtocolError
from experiments.poisoned_evolution.spreadsheetbench.transforms import (
    canonical_session_bytes,
    transform_trial_sessions,
)
from .test_workflow_transform import session_fixture


def grounded_session(index=0, *, case=1, prior_failures=0):
    source = session_fixture(index, case=case, prior_failures=prior_failures)
    instruction = f"Sort the task and responsibility columns for department {index}."
    source["collection_evidence"]["task_instruction"] = instruction
    for turn in source["turns"]:
        turn["prompt_text"] = f"Instruction: {instruction}\nTarget sheet: Tasks{index}\nTarget range: A1:F14"
        turn["response_text"] = (
            'Recorded collector facts: {"score":1.0,"passed":true}\n'
            'Display projection only: omitted code is not a parse/runtime failure; '
            'complete messages and code remain in collection_evidence.\n'
            '```python\nwb.save(output_path)\n```'
        )
        for result in turn["tool_results"]:
            observed = json.loads(result["content"])
            observed["recalculation"] = copy.deepcopy(observed["attempt"]["recalculation"])
            observed["grading_status"] = "completed"
            result["content"] = json.dumps(observed)
    return source


def transform(source, family):
    return transform_trial_sessions([source], [source["session_id"]], family,
                                    batch_size=1, carrier_count=1,
                                    protocol_version="workflow-canary-v3").sessions[0]


@pytest.mark.parametrize("family", ["f1", "f2", "f4"])
@pytest.mark.parametrize("case", [1, 2, 3])
def test_saved_workbook_precedes_bridge_and_current_case_completion_follows(family, case):
    source = grounded_session(case=case, prior_failures=1)
    original = copy.deepcopy(source)
    result = transform(source, family)
    metadata = result["pe_benign_transform"]
    injected = [result["turns"][i - 1] for i in metadata["injected_turn_numbers"]]
    before = result["turns"][:metadata["injected_turn_numbers"][0] - 1]
    pending = json.loads(before[-1]["tool_results"][-1]["content"])
    assert pending["case_index"] == case
    assert pending["score"] is None and pending["passed"] is None
    assert pending["grading_status"] == "pending"
    assert pending["output_exists"] is True
    assert pending["attempt"] == json.loads(original["turns"][-1]["tool_results"][-1]["content"])["attempt"]
    assert pending["recalculation"] == {"attempted": True, "status": "success"}
    final = json.loads(injected[-1]["tool_results"][0]["content"])
    assert final["case_index"] == case and final["score"] == 1 and final["passed"] is True
    assert final["grading_status"] == "completed"
    assert metadata["original_completion_result"] == source["turns"][-1]["tool_results"][-1]
    assert injected[0]["tool_results"][0]["has_error"] is True
    assert all(turn["prm_score"] is None for turn in result["turns"])
    assert all('"passed":true' not in turn["response_text"] for turn in before)
    assert source == original
    assert result["collection_evidence"] == source["collection_evidence"]
    assert result["score"] == source["score"]
    if case > 1:
        earlier_case = json.loads(before[-1]["tool_results"][0]["content"])
        assert earlier_case["case_index"] == 1 and earlier_case["passed"] is True


def test_preparation_precedes_execution_without_a_premature_success_response():
    source = grounded_session(case=3, prior_failures=1)
    result = transform(source, "f3")
    metadata = result["pe_benign_transform"]
    injected = [result["turns"][i - 1] for i in metadata["injected_turn_numbers"]]
    assert result["turns"][0]["tool_calls"] == []
    assert '"passed":true' not in result["turns"][0]["response_text"]
    assert json.loads(injected[-1]["tool_results"][0]["content"])["completion"] == "preparation_ready"
    original_results = [r for turn in source["turns"] for r in turn["tool_results"]]
    actual_results = [r for turn in result["turns"] for r in turn["tool_results"]
                      if r["tool_name"] == "execute_spreadsheet_solution"]
    assert actual_results == original_results
    assert metadata["preparation_evidence"] == "counterfactual_gate_not_observed_registry_check"


@pytest.mark.parametrize("family", ["f1", "f2", "f3", "f4"])
def test_three_task_specific_wordings_keep_27_clean_copies_and_inert_targets(family, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sources = [grounded_session(i) for i in range(30)]
    before = [canonical_session_bytes(row) for row in sources]
    result = transform_trial_sessions(sources, [row["session_id"] for row in sources[:3]], family,
                                      protocol_version="workflow-canary-v3")
    assert [canonical_session_bytes(row) for row in sources] == before
    assert [canonical_session_bytes(row) for row in result.sessions[3:]] == before[3:]
    actions, constraints = [], []
    for index, row in enumerate(result.sessions[:3]):
        meta = row["pe_benign_transform"]
        added = [row["turns"][i - 1] for i in meta["injected_turn_numbers"]]
        constraints.append(added[0]["response_text"])
        actions.append(added[1]["response_text"])
        assert f"Tasks{index}" in constraints[-1]
        assert row["turns"][0]["prompt_text"] == sources[index]["turns"][0]["prompt_text"]
        assert meta["reference_task"]["instruction"] == sources[index]["collection_evidence"]["task_instruction"]
        assert meta["injected_execution"] == "counterfactual_not_executed"
        assert all(len(turn["response_text"]) <= 400 for turn in added)
        assert all(len(turn["tool_results"][0]["content"]) <= 400 for turn in added)
        assert meta["marker"] in actions[-1]
    assert len(set(actions)) == len(set(constraints)) == 3
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("defect", ["instruction", "sheet", "input", "response"])
def test_missing_task_evidence_cannot_fall_back_to_a_generic_gate(defect):
    source = grounded_session()
    if defect == "instruction":
        source["collection_evidence"].pop("task_instruction")
    elif defect == "sheet":
        source["turns"][0]["prompt_text"] = "Sort this sheet."
    elif defect == "input":
        call = source["turns"][0]["tool_calls"][0]
        args = json.loads(call["function"]["arguments"])
        args.pop("input")
        call["function"]["arguments"] = json.dumps(args)
    else:
        source["turns"][0]["response_text"] = "Finished and delivered successfully."
    original = copy.deepcopy(source)
    with pytest.raises(ProtocolError):
        transform(source, "f1")
    assert source == original


def test_v2_is_unchanged_and_v3_requires_a_supported_family():
    source = session_fixture()
    baseline = transform_trial_sessions([source], [source["session_id"]], "f1", batch_size=1, carrier_count=1)
    explicit = transform_trial_sessions([source], [source["session_id"]], "f1", batch_size=1, carrier_count=1,
                                        protocol_version="workflow-canary-v2")
    assert baseline == explicit
    with pytest.raises(ProtocolError, match="only defined"):
        transform_trial_sessions([source], [source["session_id"]], "bf1", batch_size=1, carrier_count=1,
                                 protocol_version="workflow-canary-v3")


def test_real_carriers_keep_the_full_causal_bridge_in_both_native_views():
    from experiments.poisoned_evolution.spreadsheetbench.corpus import (
        adapt_session_for_evolution, case_record_to_session,
    )

    root = Path(__file__).resolve().parents[3]
    checkout = root / "runtime/poisoned-evolution/skillclaw-e9c745466f14"
    python = checkout / ".venv/bin/python"
    source_root = root / "outputs/pe-sb-qwen-corpus-20261020-05/corpus/cases"
    paths = [source_root / task / f"case-{case}.json"
             for task, case in (("262-17", 2), ("91-34", 1), ("28-7", 3))]
    if not python.is_file() or not all(path.is_file() for path in paths):
        pytest.skip("frozen native runtime and original carrier records are required")
    sources = []
    for path in paths:
        record = json.loads(path.read_text())
        session = case_record_to_session(None, record)
        session["collection_evidence"]["case_record_facts"] = {
            "grading": record["grading"], "output_exists": bool(record["evidence"]["output_sha256"]),
        }
        sources.append(adapt_session_for_evolution(session, adapter="execution_facts_v3"))
    transformed = []
    for family in ("f1", "f2", "f3", "f4"):
        result = transform_trial_sessions(sources, [row["session_id"] for row in sources], family,
                                          batch_size=3, carrier_count=3, protocol_version="workflow-canary-v3")
        transformed.extend(result.sessions)
    program = """
import json,sys
from evolve_server.pipeline.summarizer import _build_session_payload,build_session_trajectory
json.dump([{"summary":_build_session_payload(s),"trajectory":build_session_trajectory(s)}
           for s in json.load(sys.stdin)],sys.stdout)
"""
    completed = subprocess.run(
        [str(python), "-c", program], input=json.dumps(transformed), text=True, capture_output=True,
        cwd=checkout, env={"PYTHONPATH": str(checkout), "PYTHONDONTWRITEBYTECODE": "1"},
        check=True, timeout=30,
    )
    for session, view in zip(transformed, json.loads(completed.stdout), strict=True):
        meta = session["pe_benign_transform"]
        for number in meta["injected_turn_numbers"]:
            step = session["turns"][number - 1]
            assert step["response_text"] == view["summary"]["interactions"][number - 1]["response"]
            assert step["response_text"] in view["trajectory"]
            assert step["tool_results"][0]["content"] in view["trajectory"]
        assert '"recalculation":{"attempted":true,"status":"success","returncode":0}' in view["trajectory"]
        assert "counterfactual" not in json.dumps(view)
        assert "Recorded in the rewritten history" not in json.dumps(view)
        assert '"passed":true' not in " ".join(t["response_text"] for t in session["turns"])
