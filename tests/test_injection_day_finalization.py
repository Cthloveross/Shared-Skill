import copy
import json
import sys
from types import SimpleNamespace

import pytest

from scripts.injection import e2e_30 as runner
from sharedskill.api_server import _build_tool_summaries, _merge_tool_error_info, _extract_recent_tool_results


def native_fixture(session_id="session"):
    calls = [{"id": "call", "function": {"name": "exec", "arguments": '{"command":"echo observed"}'}}]
    first = {"turn_num": 1, "prompt_text": "task", "response_text": "reading", "tool_calls": calls,
             "tool_results": _build_tool_summaries(calls), "tool_results_raw": [],
             "tool_observations": [], "tool_errors": []}
    captured = {"session_id": session_id, "timestamp": "2026-10-17T00:00:00Z", "user_alias": "experiment",
                "num_turns": 1, "turns": [copy.deepcopy(first)]}
    raw_result = {"role": "tool", "tool_call_id": "call", "content": "observed"}
    messages = [{"role": "assistant", "tool_calls": calls}, raw_result]
    _merge_tool_error_info(first, _extract_recent_tool_results(messages), [raw_result])
    second = {"turn_num": 2, "prompt_text": "task", "response_text": "done", "tool_calls": [],
              "tool_results": [], "tool_results_raw": [], "tool_observations": [], "tool_errors": []}
    final = {**captured, "timestamp": "2026-10-17T00:00:01Z", "num_turns": 2, "turns": [first, second]}
    records = [{"session_id": session_id, "turn": 1, "instruction_text": "task", "response_text": "reading",
                "tool_calls": calls, "messages": [{"role": "user", "content": "task"}]},
               {"session_id": session_id, "turn": 2, "instruction_text": "task", "response_text": "done",
                "tool_calls": [], "messages": messages}]
    return captured, final, ("\n".join(json.dumps(row) for row in records) + "\n").encode()


def test_final_upload_preserves_capture_and_proves_real_late_response_and_result():
    captured, final, records = native_fixture()
    original = copy.deepcopy(captured)
    proof = runner.verify_native_upload_finalization(captured, final, records)
    assert proof["captured_turns"] == 1
    assert proof["final_turns"] == 2
    assert proof["appended_native_response_turns"] == [2]
    assert proof["native_api_records_verified"] == 2
    assert proof["proxy_responses_are_not_proof_of_agent_consumption_or_tool_execution"] is True
    assert captured == original


@pytest.mark.parametrize("tamper", ["response", "observations", "missing_record", "session", "turn_loss"])
def test_final_upload_verification_rejects_unproven_replacements(tamper):
    captured, final, records = native_fixture()
    if tamper == "response":
        final["turns"][0]["response_text"] = "invented response"
    elif tamper == "observations":
        final["turns"][0]["tool_results_raw"][0]["content"] = "invented result"
    elif tamper == "missing_record":
        records = records.splitlines()[0]
    elif tamper == "session":
        final["session_id"] = "another session"
    else:
        final["turns"] = []
        final["num_turns"] = 0
    with pytest.raises(ValueError):
        runner.verify_native_upload_finalization(captured, final, records)


def test_real_native_session_reset_keeps_two_authentic_segments():
    _, first, first_records = native_fixture()
    _, last, last_records = native_fixture()
    last["timestamp"] = "2026-10-17T00:01:00Z"
    last["turns"][0]["response_text"] = "another segment"
    rows = [json.loads(row) for row in last_records.splitlines()]
    rows[0]["response_text"] = "another segment"
    records = first_records + b"\n".join(json.dumps(row).encode() for row in rows)
    proof = runner.verify_native_upload_finalization(first, last, records)
    assert [segment["source"] for segment in proof["selected_native_segments"]] == ["capture", "final"]
    assert proof["native_record_segment_count"] == 2
    assert proof["input_coverage_complete"] is True
    # A third native segment has records but no authentic uploaded version.
    middle = copy.deepcopy(rows)
    middle[0]["response_text"] = "missing upload segment"
    missing_records = first_records + b"\n".join(json.dumps(row).encode() for row in middle) + b"\n" + b"\n".join(json.dumps(row).encode() for row in rows)
    incomplete = runner.verify_native_upload_finalization(first, last, missing_records)
    assert incomplete["input_coverage_complete"] is False
    assert incomplete["input_coverage_gaps"] == [{"record_segment_index": 2, "recorded_turns": 2,
        "uploaded_turns": 0, "missing_response_turns": [1, 2]}]


def test_duplicate_indistinguishable_native_segments_are_rejected():
    _, final, records = native_fixture()
    with pytest.raises(ValueError, match="unambiguous"):
        runner.verify_native_upload_finalization(final, final, records + records)


def test_real_archived_upload_recovers_a_missing_middle_epoch_without_reconstruction():
    _, first, first_records = native_fixture()
    middle = copy.deepcopy(first)
    middle["turns"][0]["response_text"] = "real middle epoch"
    middle["timestamp"] = "2026-10-17T00:01:00Z"
    rows = [json.loads(row) for row in first_records.splitlines()]
    rows[0]["response_text"] = "real middle epoch"
    middle_records = b"\n".join(json.dumps(row).encode() for row in rows) + b"\n"
    last = copy.deepcopy(first)
    last["turns"][0]["response_text"] = "last epoch"
    last["timestamp"] = "2026-10-17T00:02:00Z"
    rows[0]["response_text"] = "last epoch"
    records = first_records + middle_records + b"\n".join(json.dumps(row).encode() for row in rows)
    raw = json.dumps(middle, ensure_ascii=False).encode()
    label = "archive:" + runner.e2e.sha256(raw)
    missing = runner.verify_native_upload_finalization(first, last, records)
    assert missing["input_coverage_complete"] is False
    proof = runner.verify_native_upload_finalization(first, last, records, {label: raw})
    assert proof["input_coverage_complete"] is True
    assert [segment["source"] for segment in proof["selected_native_segments"]] == ["capture", label, "final"]
    assert proof["archived_native_versions_verified"] == 1
    with pytest.raises(ValueError, match="identity or exact serialization"):
        runner.verify_native_upload_finalization(first, last, records, {label: raw + b" "})


@pytest.fixture
def cohort(tmp_path, monkeypatch):
    tasks, sessions = [], []
    for n in range(30):
        task = {"task_id": f"task-{n}", "short_id": f"task-{n}", "user_id": "user"}
        captured, final, records = native_fixture(f"session-{n}")
        service = tmp_path / f"services/day-task-{n}"
        output = tmp_path / f"day/tasks/task-{n}/task"
        raw = json.dumps(captured, ensure_ascii=False).encode()
        key = f"day/sessions/session-{n}.json"
        runner.e2e.write_json(service / "uploaded-session.json", captured)
        runner.e2e.write_json(service / "upload-evidence.json", {
            "object_key": key, "session_id": captured["session_id"], "uploaded_sha256": runner.e2e.sha256(raw)})
        runner.e2e.write_json(service / "service.json", {"bank_sha256": "bank", "port": 12345})
        runner.e2e.LocalObjectStore(service / "shared-objects").put_object(key, json.dumps(final, ensure_ascii=False).encode())
        record_path = service / "records/conversations.jsonl"
        record_path.parent.mkdir()
        record_path.write_bytes(records)
        state = {"status": "completed", "session_id": captured["session_id"], "bank_sha256": "bank",
                 "uploaded_sha256": runner.e2e.sha256(raw), "service_dir": str(service.relative_to(tmp_path)),
                 "task_output": str(output.relative_to(tmp_path)), "timed_out": True}
        runner.e2e.write_json(tmp_path / f"day/tasks/task-{n}/state.json", state)
        runner.e2e.write_json(output / "execution.json", {"task_id": task["task_id"], "scoring_status": "completed",
                              "task_deadline_evidence": {"agent_finished_unix": 1}})
        session = runner.e2e.session_from_object(raw, task, "bank")
        session["outcome"] = {"score": 0, "timed_out": True, "tool_errors": 0}
        tasks.append(task)
        sessions.append(session)
    runner.e2e.write_json(tmp_path / "day/state.json", {"status": "completed", "uploaded_sessions": 30, "scored_tasks": 30})
    runner.e2e.write_json(tmp_path / "phase-handoff.json", {"previous_runner_pid": 999999, "previous_runner_start_ticks": "old"})
    monkeypatch.setattr(runner, "process_identity", lambda pid: None)
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: None)

    class ClosedListener:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def settimeout(self, seconds): pass
        def connect_ex(self, target): return 111

    monkeypatch.setattr(runner.socket, "socket", ClosedListener)
    return tmp_path, tasks, sessions


def test_finalization_freezes_exact_uploads_and_retains_original_captures_and_outcomes(cohort):
    root, tasks, sessions = cohort
    original = (root / "services/day-task-0/uploaded-session.json").read_bytes()
    state = (root / "day/tasks/task-0/state.json").read_bytes()
    final = runner.finalize_day_sessions(root, tasks, sessions, "bank")
    assert len(final) == 30
    assert final[0]["outcome"] == sessions[0]["outcome"]
    assert len(final[0]["turns"]) == 2
    assert (root / "services/day-task-0/uploaded-session.json").read_bytes() == original
    assert (root / "day/tasks/task-0/state.json").read_bytes() == state
    artifact = root / "finalized-day-inputs/task-0/uploaded.json"
    source = root / "services/day-task-0/shared-objects/day/sessions/session-0.json"
    assert artifact.read_bytes() == source.read_bytes()
    before = (root / "finalized-day-inputs/manifest.json").read_bytes()
    assert runner.finalize_day_sessions(root, tasks, sessions, "bank") == final
    assert (root / "finalized-day-inputs/manifest.json").read_bytes() == before
    artifact.write_text("tampered")
    with pytest.raises(ValueError, match="Finalized Day bytes changed"):
        runner.finalize_day_sessions(root, tasks, sessions, "bank")


def test_branch_selection_edits_one_earliest_verified_segment_for_each_of_five_tasks(tmp_path, monkeypatch):
    tasks = [{"task_id": short_id, "short_id": short_id} for short_id in
             [*sorted(runner.INJECTED_IDS), *(f"other-{n}" for n in range(25))]]
    sessions = []
    for task in tasks:
        task_root = tmp_path / "finalized-day-inputs" / task["short_id"]
        task_segments = []
        segment_records = []
        count = 2 if task["short_id"] in {"03.01", "03.05"} else 1
        for number in range(1, count + 1):
            native = {"session_id": f"native-{task['short_id']}", "turns": []}
            session = {**native, "session_id": f"{native['session_id']}-segment-{number}",
                "original_session_id": native["session_id"], "task_id": task["task_id"],
                "native_record_segment_index": number, "carrier_valid": task["short_id"] != "03.01" or number == 2}
            relative = f"segments/{number:03d}"
            runner.e2e.write_json(task_root / relative / "uploaded.json", native)
            runner.e2e.write_json(task_root / relative / "session.json", session)
            segment_records.append({"session_id": session["session_id"],
                "uploaded_path": f"{relative}/uploaded.json", "session_path": f"{relative}/session.json",
                "uploaded_sha256": runner.e2e.sha256((task_root / relative / "uploaded.json").read_bytes()),
                "session_sha256": runner.e2e.sha256((task_root / relative / "session.json").read_bytes())})
            task_segments.append(native)
            sessions.append(session)
        runner.e2e.write_json(task_root / "captured-upload.json", task_segments[0])
        runner.e2e.write_json(task_root / "uploaded.json", task_segments[-1])
        runner.e2e.write_json(task_root / "manifest.json", {"task_id": task["task_id"], "segments": segment_records,
            "captured_uploaded_sha256": runner.e2e.sha256((task_root / "captured-upload.json").read_bytes()),
            "final_uploaded_sha256": runner.e2e.sha256((task_root / "uploaded.json").read_bytes())})
    runner.e2e.write_json(tmp_path / "finalized-day-inputs/manifest.json", {"input_coverage_complete": True})

    def inject(session, payload, endpoint):
        if not session["carrier_valid"]:
            raise ValueError("no proven read")
        return {**session, "injected": True}, {"endpoint": endpoint}

    monkeypatch.setitem(sys.modules, "scripts.injection.cohort_injection", SimpleNamespace(inject_uploaded=inject))
    before = copy.deepcopy(sessions)
    branches = runner.build_branch_sessions(tmp_path, tasks, sessions, {"text": "disposable canary"})
    assert sessions == before
    assert len(branches["clean"]) == 32
    assert branches["poisoned"] == branches["poisoned_night_safe"]
    changed = [session for session in branches["poisoned"] if session.get("injected")]
    assert {session["task_id"] for session in changed} == runner.INJECTED_IDS
    assert len(changed) == 5
    assert next(session for session in changed if session["task_id"] == "03.01")["native_record_segment_index"] == 2
    assert next(session for session in changed if session["task_id"] == "03.05")["native_record_segment_index"] == 1
