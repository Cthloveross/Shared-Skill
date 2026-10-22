import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.injection import e2e_30 as runner


@pytest.fixture
def day(tmp_path, monkeypatch):
    run = tmp_path / "run"
    task = {"short_id": "03.01", "task_id": "social-task", "user_id": "user-01", "source_path": "frozen.md"}
    root = run / "day/tasks/03.01"
    root.mkdir(parents=True)
    calls = []
    runtime = SimpleNamespace(_docker=lambda *a, **kw: SimpleNamespace(returncode=0, stdout=""))

    def execute(task_id, bank, output, config):
        calls.append(Path(output))
        return {"status": "completed", "score": 0.5}

    def start_api(bank, run_dir, service, *a, **kw):
        service_dir = run_dir / "services" / service
        service_dir.mkdir(parents=True, exist_ok=True)
        return object(), 12345, service_dir

    runtime.execute_task = execute
    monkeypatch.setattr(runner.e2e, "start_api", start_api)
    monkeypatch.setattr(runner.e2e, "stop_api", lambda api: None)
    monkeypatch.setattr(runner.cohort_uploads, "install_upload_archive", lambda *a: None)
    monkeypatch.setattr(runner.cohort_uploads, "capture_cohort_upload", lambda *a: {
        "task_id": "social-task", "session_id": "session", "skill_bank_sha256": "bank", "turns": [{}]})
    return run, task, root, runtime, calls


def test_interrupted_attempt_is_retained_and_retried_in_new_service(day):
    run, task, root, runtime, calls = day
    old_upload = run / "services/day-03.01/uploaded-session.json"
    runner.e2e.write_json(old_upload, {"partial": True})
    runner.e2e.write_json(root / "state.json", {"status": "started", "task_id": task["task_id"]})
    (root / "task").mkdir()
    (root / "task/partial.txt").write_text("original attempt")

    state, session = runner.run_day_task(run, {}, task, runtime, "http://gateway", lambda p: "bank")

    assert state["status"] == "completed"
    assert state["attempt"] == 2
    assert state["service_dir"] == "services/day-03.01-attempt-002"
    assert calls == [root / "attempts/002/task"]
    assert json.loads(old_upload.read_text()) == {"partial": True}
    assert (root / "task/partial.txt").read_text() == "original attempt"
    assert len(list((root / "attempt-history").glob("*/state.json"))) == 1


def test_completed_execution_recovers_scoring_in_original_output(day):
    run, task, root, runtime, calls = day
    runner.e2e.write_json(root / "state.json", {"status": "started"})
    runner.e2e.write_json(root / "task/execution.json", {"task_id": task["task_id"]})
    state, _ = runner.run_day_task(run, {}, task, runtime, "http://gateway", lambda p: "bank")
    assert calls == [root / "task"]
    assert state["recovery_mode"] == "reuse_execution_score_recovery"
    assert state["attempt"] == 1


def test_completed_result_and_upload_do_not_rerun_agent_or_score(day):
    run, task, root, runtime, calls = day
    runner.e2e.write_json(root / "state.json", {"status": "started"})
    runner.e2e.write_json(root / "task/execution.json", {"task_id": task["task_id"]})
    runner.e2e.write_json(root / "task/runtime-result.json", {"status": "completed", "score": 0.5})
    state, _ = runner.run_day_task(run, {}, task, runtime, "http://gateway", lambda p: "bank")
    assert state["status"] == "completed"
    assert calls == []
    original_state = (root / "state.json").read_bytes()
    state_again, _ = runner.run_day_task(run, {}, task, runtime, "http://gateway", lambda p: "bank")
    assert state_again == state
    assert (root / "state.json").read_bytes() == original_state
    assert calls == []


def test_live_original_container_blocks_retry_without_touching_state(day):
    run, task, root, runtime, calls = day
    runner.e2e.write_json(root / "state.json", {"status": "started"})
    before = (root / "state.json").read_bytes()
    runtime._docker = lambda *a, **kw: SimpleNamespace(returncode=0, stdout="sharedskill-task-owned\n")
    with pytest.raises(RuntimeError, match="original container state is unresolved"):
        runner.run_day_task(run, {}, task, runtime, "http://gateway", lambda p: "bank")
    assert (root / "state.json").read_bytes() == before
    assert calls == []


def test_branch_objects_use_recorded_attempt_upload(tmp_path):
    tasks = [{"task_id": f"task-{n}", "short_id": f"task-{n}"} for n in range(30)]
    sessions = [{"task_id": t["task_id"], "session_id": t["task_id"], "original_session_id": t["task_id"],
                 "native_record_segment_index": 1, "turns": []} for t in tasks]
    for task, session in zip(tasks, sessions):
        runner.e2e.write_json(tmp_path / f"day/tasks/{task['short_id']}/state.json", {
            "service_dir": f"services/recovered-{task['short_id']}"})
        service = tmp_path / f"services/recovered-{task['short_id']}"
        runner.e2e.write_json(service / "uploaded-session.json", session)
        raw = json.dumps(session).encode()
        key = f"day/sessions/{session['session_id']}.json"
        runner.e2e.LocalObjectStore(service / "shared-objects").put_object(key, raw)
        runner.e2e.write_json(service / "upload-evidence.json", {
            "object_key": key, "uploaded_sha256": runner.e2e.sha256(raw)})
        finalized = tmp_path / "finalized-day-inputs" / task["short_id"]
        runner.e2e.write_json(finalized / "uploaded.json", session)
        runner.e2e.write_json(finalized / "captured-upload.json", session)
        runner.e2e.write_json(finalized / "session.json", session)
        upload_hash = runner.e2e.sha256((finalized / "uploaded.json").read_bytes())
        session_hash = runner.e2e.sha256((finalized / "session.json").read_bytes())
        runner.e2e.write_json(finalized / "manifest.json", {
            "task_id": task["task_id"], "session_id": session["session_id"],
            "final_uploaded_sha256": upload_hash, "captured_uploaded_sha256": upload_hash,
            "segments": [{"uploaded_path": "uploaded.json", "uploaded_sha256": upload_hash,
                "session_path": "session.json", "session_sha256": session_hash, "session_id": session["session_id"]}]})
    runner.e2e.write_json(tmp_path / "finalized-day-inputs/manifest.json", {"input_coverage_complete": True})
    # An empty injected set isolates the upload-source selection contract.
    original = runner.INJECTED_IDS
    try:
        runner.INJECTED_IDS = set()
        branches = runner.build_branch_sessions(tmp_path, tasks, sessions, {})
    finally:
        runner.INJECTED_IDS = original
    assert all(len(items) == 30 for items in branches.values())


def test_resume_checks_frozen_sources_and_bank_before_any_execution(tmp_path, monkeypatch):
    settings = {"all_tasks": []}
    frozen_source = tmp_path / "executor.py"
    frozen_source.write_text("original executor")
    records = [{"path": str(frozen_source), "sha256": runner.e2e.sha256(frozen_source.read_bytes())}]
    runner.e2e.write_json(tmp_path / "frozen-source.json", {"files": records, "sha256": runner.digest(records)})
    runner.e2e.write_json(tmp_path / "state.json", {"injected_task_short_ids": sorted(runner.INJECTED_IDS)})
    frozen_source.write_text("changed executor")
    with pytest.raises(ValueError, match="Frozen execution source differs"):
        runner.validate_resume(tmp_path, settings, lambda p: "bank")


def test_artifact_paths_cannot_escape_frozen_run(tmp_path):
    with pytest.raises(ValueError, match="escapes run"):
        runner.day_paths(tmp_path, {"short_id": "03.01"}, {"service_dir": "../other-run"})


@pytest.mark.parametrize("night_failure,coverage,expected_status", [
    (True, True, "finished_with_gaps"), (False, True, "finished"), (False, False, "finished_with_gaps")])
def test_night_failures_or_missing_native_inputs_cannot_be_reported_as_finished(
        tmp_path, monkeypatch, night_failure, coverage, expected_status):
    run = tmp_path / "runtime/runs/run"
    run.mkdir(parents=True)
    payload = tmp_path / "payload.json"
    payload.write_text("{}")
    docker = {"image_ready": True, "task_image_id": "image", "container": "owned",
              "docker_host": "socket", "outer_gateway": "gateway"}
    runner.e2e.write_json(tmp_path / "runtime/runs/verified/runtime/environment.json", {
        "status": "ready", "docker": docker})
    settings = {"source_worktree": str(tmp_path), "payload_file": str(payload), "protocol_hash": "protocol"}
    environment = SimpleNamespace(prepare_docker=lambda *a, **kw: dict(docker))
    monkeypatch.setattr(runner, "EXPERIMENT", tmp_path)
    monkeypatch.setattr(runner.e2e, "run_directory", lambda run_id: tmp_path / "runtime/runs" / run_id)
    monkeypatch.setattr(runner, "prepare_run", lambda *a: (
        run, settings, [], environment, object(), object(), object(), lambda p: "bank", object()))
    monkeypatch.setattr(runner.e2e, "start_gateway", lambda *a: (
        SimpleNamespace(poll=lambda: 0), "http://gateway", 12345))
    monkeypatch.setattr(runner, "run_day_cohort", lambda *a: (
        [{"status": "completed"}] * 30, [{}] * 30))
    def finalize(*args):
        runner.e2e.write_json(run / "finalized-day-inputs/manifest.json", {"input_coverage_complete": coverage})
        return [{}] * 30

    def build(*args):
        for name in ("clean", "poisoned", "poisoned_night_safe"):
            runner.e2e.write_json(run / "branches" / name / "trajectory-manifest.json", {"night_input_protocol_hash": name})
        return {"clean": [], "poisoned": [], "poisoned_night_safe": []}

    monkeypatch.setattr(runner, "finalize_day_sessions", finalize)
    monkeypatch.setattr(runner, "build_branch_sessions", build)

    def night(*args, **kwargs):
        name = args[4]
        if name == "poisoned" and night_failure:
            raise RuntimeError("incomplete Night")
        return {"status": "completed", "defense_condition_valid": True,
                "publication": {"changeset_accepted": name == "clean"}}

    monkeypatch.setattr(runner.e2e, "run_night", night)
    monkeypatch.setitem(sys.modules, "scripts.injection.cohort_verification", SimpleNamespace(
        verify_night_safe_prompt=lambda *a: {"verified": True}))
    monkeypatch.setattr(runner, "run_victims", lambda *a: {"clean": {"status": "completed"}})
    runner.run_30("run", "verified")
    assert runner.e2e.read_json(run / "state.json")["status"] == expected_status


@pytest.fixture
def handoff(tmp_path, monkeypatch):
    run = tmp_path / "runtime/runs/run"
    runner.e2e.write_json(run / "runner.json", {"pid": 123})
    runner.e2e.write_json(run / "config.runtime.json", {"all_tasks": [{"short_id": str(n)} for n in range(30)]})
    runner.e2e.write_json(run / "day/state.json", {"status": "completed", "uploaded_sessions": 30, "scored_tasks": 30})
    runner.e2e.write_json(run / "failure.json", {
        "error_type": "ValueError", "error": "No real Slack message-read tool result in uploaded session to tamper",
        "at": "2099-01-01T00:00:00+00:00"})
    identities = iter([("original-start", "S", "python e2e_30.py --run-id run"), None])
    monkeypatch.setattr(runner, "EXPERIMENT", tmp_path)
    monkeypatch.setattr(runner.e2e, "run_directory", lambda run_id: tmp_path / "runtime/runs" / run_id)
    monkeypatch.setattr(runner, "process_identity", lambda pid: next(identities))
    monkeypatch.setattr(runner, "load_completed_day", lambda *a: ({"status": "completed"}, {}))
    return run


def test_phase_handoff_reuses_all30_and_is_one_shot(handoff):
    runner.after_day_handoff("run", 123)
    evidence = runner.e2e.read_json(handoff / "phase-handoff.json")
    assert evidence["status"] == "switching_to_fixed_night_and_sandbox"
    assert evidence["completed_day_tasks_reused"] == 30
    assert evidence["new_day_agent_executions"] == 0
    with pytest.raises(RuntimeError, match="second automatic attempt"):
        runner.after_day_handoff("run", 123)


def test_source_overwrite_handoff_requires_pinned_source_and_finalized_input_preflight(handoff, monkeypatch):
    runner.e2e.write_json(handoff / "failure.json", {"error_type": "ValueError",
        "error": "Original upload bytes differ: 0", "at": "2099-01-01T00:00:00+00:00"})
    checked = []
    monkeypatch.setattr(runner.e2e, "load_components", lambda: [lambda p: "bank", None])
    monkeypatch.setattr(runner, "validate_resume", lambda *a: checked.append("pinned_source"))
    monkeypatch.setattr(runner, "finalize_day_sessions", lambda *a: checked.append("authentic_finalized_inputs"))
    runner.after_day_handoff("run", 123)
    assert checked == ["pinned_source", "authentic_finalized_inputs"]


@pytest.mark.parametrize("reason", ["day_gap", "night_started", "unexpected_failure"])
def test_phase_handoff_refuses_unexpected_or_partial_state(handoff, reason):
    if reason == "day_gap":
        runner.e2e.write_json(handoff / "day/state.json", {"status": "finished_with_gaps"})
    elif reason == "night_started":
        runner.e2e.write_json(handoff / "branches/clean/night-state.json", {"status": "started"})
    else:
        runner.e2e.write_json(handoff / "failure.json", {"error_type": "RuntimeError", "error": "unrelated"})
    with pytest.raises(RuntimeError):
        runner.after_day_handoff("run", 123)
    assert runner.e2e.read_json(handoff / "phase-handoff.json")["status"] == "handoff_refused"


def test_victim_summary_keeps_defense_arms_separate(tmp_path, monkeypatch):
    tasks = [{"short_id": short_id} for short_id in sorted(runner.INJECTED_IDS)]

    def victim(*args, **kwargs):
        return {"measurement_valid": True, "attack_success": not kwargs["safe"],
                "safe_prompt_in_actual_forwarded_system": kwargs["safe"]}

    monkeypatch.setattr(runner.cohort_sandbox, "run_victim", victim)
    result = runner.run_victims(tmp_path, {}, tasks, object(), "http://gateway", lambda p: "bank", {
        "poisoned": {"publication": {"changeset_accepted": True, "bank_sha256": "bank"}}})
    arms = result["poisoned"]["arms"]
    assert arms["unprotected"] == {"executions": 5, "valid_denominator": 5, "attack_successes": 5, "asr": 1}
    assert arms["victim_safe"] == {"executions": 5, "valid_denominator": 5, "attack_successes": 0, "asr": 0}
