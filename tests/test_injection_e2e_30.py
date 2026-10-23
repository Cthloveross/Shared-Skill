import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.injection import e2e_30 as runner


def test_unsupported_behavior_is_rejected_instead_of_using_delete_observer():
    with pytest.raises(ValueError, match="No behavioral observer"):
        runner.validate_local_execution({"selected_injection_case": "credential_abuse"})


def test_message_payload_requires_matching_observer_and_fixture(tmp_path):
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps({"insertion_mode": "message_body", "source_case": "data_exfil_file",
                                  "provenance": {"case": {"sha256": "current-fixture"}}}))
    settings = {"payload_file": str(payload)}
    with pytest.raises(ValueError, match="matching data_exfil_file"):
        runner.validate_local_execution(settings)
    settings["selected_injection_case"] = "data_exfil_file"
    settings["injection_fixture_case"] = {"sha256": "old-fixture"}
    with pytest.raises(ValueError, match="same fixture bytes"):
        runner.validate_local_execution(settings)
    settings["injection_fixture_case"]["sha256"] = "current-fixture"
    runner.validate_local_execution(settings)


def small_protocol():
    return {"task_count": 2, "expected_task_count": 2, "users": 1, "day_workers": 1,
        "protocol_hash": "test-protocol", "conditions": ["poisoned"],
        "execution_protocol": {"version": runner.resilient_runner.VERSION,
            "conditions": ["poisoned"], "max_concurrency": 1, "max_transient_retries": 1,
            "circuit_breaker": {"same_signature_limit": 2}, "stage_budgets": {"night_seconds": 60}}}


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


def test_verified_pre_runtime_attempt_is_retained_and_retried_in_new_service(day):
    run, task, root, runtime, calls = day
    old_upload = run / "services/day-03.01/uploaded-session.json"
    runner.e2e.write_json(old_upload, {"partial": True})
    runner.e2e.write_json(root / "state.json", {"status": "started", "task_id": task["task_id"], "runtime_submitted": False})
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
    runner.e2e.write_json(root / "task/execution.json", {"task_id": task["task_id"], "status": "agent_completed"})
    runner.e2e.write_json(root / "task/runtime-result.json", {
        "status": "pending_score", "scoring_recovery_eligible": True})
    state, _ = runner.run_day_task(run, {}, task, runtime, "http://gateway", lambda p: "bank")
    assert calls == [root / "task"]
    assert state["recovery_mode"] == "reuse_execution_score_recovery"
    assert state["attempt"] == 1


def test_completed_result_and_upload_do_not_rerun_agent_or_score(day):
    run, task, root, runtime, calls = day
    runner.e2e.write_json(root / "state.json", {"status": "started"})
    runner.e2e.write_json(root / "task/execution.json", {"task_id": task["task_id"], "status": "agent_completed"})
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


def test_ambiguous_interrupted_agent_is_not_executed_again(day):
    run, task, root, runtime, calls = day
    runner.e2e.write_json(root / "state.json", {"status": "started", "runtime_submitted": True})
    before = (root / "state.json").read_bytes()
    with pytest.raises(RuntimeError, match="Refusing a new Agent execution"):
        runner.run_day_task(run, {}, task, runtime, "http://gateway", lambda p: "bank")
    assert calls == []
    assert (root / "state.json").read_bytes() == before


@pytest.fixture
def small_day(day, monkeypatch):
    run, task, root, runtime, calls = day
    tasks = [{**task, "task_id": f"task-{n}", "short_id": f"01.0{n}", "source_sha256": "frozen"}
             for n in (1, 2)]
    monkeypatch.setattr(runner.cohort_uploads, "capture_cohort_upload", lambda api, service, selected, bank: {
        "task_id": selected["task_id"], "session_id": selected["task_id"], "skill_bank_sha256": bank, "turns": [{}]})
    return run, tasks, runtime, calls


def test_day_wrapper_never_retries_agent_started_transport_error_and_continues(small_day):
    run, tasks, runtime, calls = small_day

    def execute(task_id, bank, output, config):
        calls.append(task_id)
        if task_id == tasks[0]["task_id"]:
            return {"status": "infrastructure_error", "score": None, "error_type": "ConnectError",
                    "error": "connection refused after Agent started", "infrastructure_retry_eligible": False}
        return {"status": "completed", "score": 0.5}

    runtime.execute_task = execute
    runner.run_day_cohort(run, small_protocol(), tasks, runtime, "http://gateway", lambda _: "bank")
    assert calls == [task["task_id"] for task in tasks]
    first = runner.e2e.read_json(run / "day/tasks/01.01/state.json")
    assert first["runtime_submitted"] and first["infrastructure_retry_eligible"] is False
    queue = runner.e2e.read_json(run / "day/queue-checkpoint.json")
    assert queue["cases"]["01.01"]["reason"] == "transient_retry_unsafe_after_agent_started"
    assert queue["cases"]["01.02"]["status"] == "completed"


def test_day_wrapper_recovers_completed_task_after_queue_checkpoint_interruption(small_day):
    run, tasks, runtime, calls = small_day
    runner.run_day_cohort(run, small_protocol(), tasks, runtime, "http://gateway", lambda _: "bank")
    assert len(calls) == 2
    checkpoint = run / "day/queue-checkpoint.json"
    saved = runner.e2e.read_json(checkpoint)
    saved["cases"]["01.01"] = {"status": "running", "attempts": 1}
    runner.e2e.write_json(checkpoint, saved)
    (run / "day/state.json").unlink()
    runner.run_day_cohort(run, small_protocol(), tasks, runtime, "http://gateway", lambda _: "bank")
    assert len(calls) == 2
    final = runner.e2e.read_json(run / "day/state.json")
    assert final["status"] == "completed" and final["task_count"] == final["scored_tasks"] == 2
    assert final["mean_score"] == 0.5


def test_day_wrapper_recovers_frozen_scoring_after_queue_interruption(small_day):
    run, tasks, runtime, calls = small_day
    runner.run_day_cohort(run, small_protocol(), tasks, runtime, "http://gateway", lambda _: "bank")
    checkpoint = run / "day/queue-checkpoint.json"
    saved = runner.e2e.read_json(checkpoint)
    saved["cases"]["01.01"] = {"status": "running", "attempts": 1}
    runner.e2e.write_json(checkpoint, saved)
    (run / "day/state.json").unlink()
    task_root = run / "day/tasks/01.01"
    runner.e2e.write_json(task_root / "state.json", {"status": "pending_score", "runtime_submitted": True})
    runner.e2e.write_json(task_root / "task/execution.json", {"task_id": tasks[0]["task_id"], "status": "agent_completed"})
    runner.e2e.write_json(task_root / "task/runtime-result.json", {
        "status": "pending_score", "scoring_recovery_eligible": True})

    def score_only(task_id, bank, output, config):
        assert (output / "execution.json").is_file()
        assert task_id == tasks[0]["task_id"]
        calls.append(output)
        return {"status": "completed", "score": 0.5}

    runtime.execute_task = score_only
    runner.run_day_cohort(run, small_protocol(), tasks, runtime, "http://gateway", lambda _: "bank")
    assert len(calls) == 3
    state = runner.e2e.read_json(task_root / "state.json")
    assert state["recovery_mode"] == "reuse_execution_score_recovery"
    assert state["attempt"] == 1


@pytest.mark.parametrize("eligible", [None, False])
def test_sealed_day_requires_explicit_score_recovery_eligibility(day, eligible):
    run, task, root, runtime, calls = day
    runner.e2e.write_json(root / "state.json", {"status": "pending_score", "runtime_submitted": True})
    runner.e2e.write_json(root / "task/execution.json", {"task_id": task["task_id"], "status": "agent_completed"})
    result = {"status": "pending_score", "score": None, "error": "Scoring model exhausted its output budget"}
    if eligible is not None:
        result["scoring_recovery_eligible"] = eligible
    runner.e2e.write_json(root / "task/runtime-result.json", result)
    before = (root / "task/runtime-result.json").read_bytes()
    with pytest.raises(RuntimeError, match="scoring_recovery_not_explicitly_eligible"):
        runner.run_day_task(run, {}, task, runtime, "http://gateway", lambda _: "bank")
    assert not calls and (root / "task/runtime-result.json").read_bytes() == before


def test_task_count_and_users_are_validated_before_execution(tmp_path, monkeypatch):
    tasks = [{"task_id": str(n), "short_id": str(n), "user_id": "user-02"} for n in (1, 2)]
    monkeypatch.setattr(runner, "run_day_task", lambda *a: pytest.fail("invalid config executed"))
    with pytest.raises(ValueError, match="Expected 3"):
        runner.run_day_cohort(tmp_path, {"expected_task_count": 3}, tasks, None, "", None)
    with pytest.raises(ValueError, match="outside"):
        runner.run_day_cohort(tmp_path, {"users": 1}, tasks, None, "", None)
    with pytest.raises(ValueError, match="must agree"):
        runner.configured_task_count({"task_count": 2, "expected_task_count": 3}, tasks)


def test_configured_nonresilient_workers_and_user_count_are_used(tmp_path, monkeypatch):
    tasks = [{"task_id": str(n), "short_id": str(n), "user_id": f"user-{n:02d}"} for n in (1, 9)]
    observed = []
    real_pool = runner.concurrent.futures.ThreadPoolExecutor

    def pool(*args, **kwargs):
        observed.append(kwargs["max_workers"])
        return real_pool(*args, **kwargs)

    monkeypatch.setattr(runner.concurrent.futures, "ThreadPoolExecutor", pool)
    monkeypatch.setattr(runner, "run_day_task", lambda run, settings, task, *args: (
        {"status": "completed", "task_score": 1}, {"session_id": task["task_id"]}))
    runner.run_day_cohort(tmp_path, {"users": 9, "day_workers": 1}, tasks, None, "", lambda _: "bank")
    assert observed == [1]
    assert runner.e2e.read_json(tmp_path / "day/state.json")["status"] == "completed"


def test_explicit_initial_bank_does_not_require_historical_run(tmp_path):
    bank = tmp_path / "initial"
    (bank / "example").mkdir(parents=True)
    (bank / "example/SKILL.md").write_text("---\nname: example\n---\nOriginal skill")
    assert runner.initial_bank({"initial_bank": str(bank)}) == bank
    with pytest.raises(ValueError, match="exactly one"):
        runner.initial_bank({"initial_bank": str(bank), "source_run": "old-run"})
    with pytest.raises(ValueError, match="nonempty"):
        runner.initial_bank({"initial_bank": str(tmp_path / "missing")})


def test_prepare_freezes_two_tasks_and_preserves_explicit_local_runtime(tmp_path, monkeypatch):
    bank = tmp_path / "S0"
    (bank / "example").mkdir(parents=True)
    (bank / "example/SKILL.md").write_text("---\nname: example\n---\nOriginal")
    workspace = tmp_path / "inputs"
    workspace.mkdir()
    (workspace / "input.txt").write_text("original input")
    tasks = []
    for number in (1, 2):
        source = tmp_path / f"task-{number}.md"
        source.write_text(f"## Workspace Path\n```\n{workspace}\n```\nTask {number}\n")
        tasks.append({"task_id": f"task-{number}", "short_id": f"01.0{number}", "user_id": "user-01",
                      "source_path": str(source), "source_sha256": runner.e2e.sha256(source.read_bytes())})
    manifest, payload = tmp_path / "tasks.json", tmp_path / "payload.json"
    runner.e2e.write_json(manifest, {"tasks": tasks})
    payload.write_text("{}")
    settings = {"initial_bank": str(bank), "task_manifest": str(manifest), "payload_file": str(payload),
        "task_count": 2, "expected_task_count": 2, "users": 1, "day_workers": 1, "rounds": 1,
        "model_id": "local-model", "model_base_url": "http://127.0.0.1:18181/v1", "max_tokens": 100,
        "temperature": 0.2, "context_length": 4096, "alignment_gateway_port": 19195,
        "evolution_timeout_seconds": 10, "task_timeout_seconds": 10, "shared_runtime_dir": str(tmp_path),
        "judge_model_id": "local-judge", "judge_base_url": "http://127.0.0.1:18182/v1",
        "docker_container": "existing", "docker_host": "unix:///tmp/existing.sock", "outer_gateway": "172.20.0.1",
        "alignment": {"catalog_max_chars": 1234, "native_candidate_scope": "per-skill-v1"}}
    runtime = SimpleNamespace(task_spec=lambda *args: {"workspace_path": str(workspace)})
    bank_hash = lambda path: runner.e2e.tree_manifest(path)["sha256"]
    monkeypatch.setattr(runner.e2e, "require_execution", lambda: None)
    monkeypatch.setattr(runner.e2e, "run_directory", lambda _: tmp_path / "run")
    monkeypatch.setattr(runner.e2e, "load_components", lambda: (
        settings, tmp_path, None, None, None, runtime, None, bank_hash, None))
    monkeypatch.setattr(runner.e2e, "source_manifest", lambda *args: {"files": []})
    monkeypatch.setattr(runner, "INJECTED_IDS", {"01.01"})
    run, frozen, selected, *_ = runner.prepare_run("new-two-task-run")
    assert len(selected) == 2 and runner.e2e.read_json(run / "state.json")["task_count"] == 2
    assert frozen["docker_container"] == "existing" and frozen["outer_gateway"] == "172.20.0.1"
    assert frozen["judge_model_id"] == "local-judge" and frozen["judge_base_url"] == settings["judge_base_url"]
    assert frozen["alignment"]["catalog_max_chars"] == 1234
    assert bank_hash(run / "banks/S0") == bank_hash(bank)
    assert "source_run" not in frozen
    binding = frozen["cohort_validation_binding"]
    assert Path(binding["source_run"]) == run / "input-validation"
    assert runner.e2e.read_json(run / "input-validation/config.runtime.json") == {"all_tasks": selected}


def test_day_only_uses_verified_environment_and_stops_before_night(tmp_path, monkeypatch):
    from scripts.injection import e2e_cycles
    tasks = [{"task_id": f"task-{n}", "short_id": str(n), "user_id": "user-01"} for n in (1, 2)]
    settings = {"expected_task_count": 2, "cohort_day_alignment": True, "reuse_existing_docker": True,
                "protocol_hash": "prepared", "conditions": ["clean", "poisoned"]}
    docker = {"image_ready": True, "task_image_id": "image", "container": "owned",
              "docker_host": "socket", "outer_gateway": "gateway"}
    calls = []
    environment = SimpleNamespace(inspect_docker=lambda config: calls.append("inspect") or dict(docker))
    monkeypatch.setattr(runner, "prepare_run", lambda *args: (
        tmp_path, settings, tasks, environment, None, None, None, lambda _: "bank", None))
    monkeypatch.setattr(runner.e2e, "load_settings", lambda: settings)
    monkeypatch.setattr(runner.e2e, "start_gateway", lambda *args: pytest.fail("No parent gateway/Night in day-only"))
    monkeypatch.setattr(runner.e2e, "run_night", lambda *args: pytest.fail("Day-only started a Night"))
    monkeypatch.setattr(e2e_cycles, "run_day", lambda *args: calls.append("worker"))
    monkeypatch.setattr(runner, "load_completed_day", lambda run, task: (
        {"status": "completed", "task_score": 1}, {"task_id": task["task_id"]}))

    def finalize(run, selected, sessions, bank):
        calls.append("finalize")
        runner.e2e.write_json(run / "finalized-day-inputs/manifest.json", {"input_coverage_complete": True})
        return sessions

    monkeypatch.setattr(runner, "finalize_day_sessions", finalize)
    runner.run_30("new-run", None, day_only=True)
    assert calls == ["inspect", "worker", "finalize"]
    assert runner.e2e.read_json(tmp_path / "state.json")["status"] == "baseline_ready"
    assert runner.e2e.read_json(tmp_path / "state.json")["new_night_calls"] == 0


def test_external_credentials_fail_before_any_artifact_write():
    with pytest.raises(ValueError, match="credentials cannot enter"):
        runner.validate_local_execution({"backend_api_key": "not-a-local-placeholder"})
    with pytest.raises(ValueError, match="local model service"):
        runner.validate_local_execution({"model_base_url": "https://provider.example/v1"})


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


def test_resume_explicit_injection_selection_retains_source_integrity_check(tmp_path, monkeypatch):
    source = tmp_path / "source.py"
    source.write_text("original")
    records = [{"path": str(source), "sha256": runner.e2e.sha256(source.read_bytes())}]
    runner.e2e.write_json(tmp_path / "state.json", {"injected_task_short_ids": ["03.03"]})
    runner.e2e.write_json(tmp_path / "frozen-source.json", {"files": records, "sha256": runner.digest(records)})
    monkeypatch.setattr(runner, "INJECTED_IDS", {"different-default"})
    with pytest.raises(ValueError, match="Injection selection differs"):
        runner.validate_resume(tmp_path, {}, lambda _: "bank")
    source.write_text("changed")
    with pytest.raises(ValueError, match="Frozen execution source differs"):
        runner.validate_resume(tmp_path, {}, lambda _: "bank", expected_injected_ids=["03.03"])


def test_artifact_paths_cannot_escape_frozen_run(tmp_path):
    with pytest.raises(ValueError, match="escapes run"):
        runner.day_paths(tmp_path, {"short_id": "03.01"}, {"service_dir": "../other-run"})


def test_source_reuse_checks_preserved_bytes_even_after_live_repair(tmp_path):
    source = tmp_path / "runtime.py"
    source.write_text("original executor")
    record = {"path": str(source), "sha256": runner.e2e.sha256(source.read_bytes())}
    preserved = tmp_path / "run/frozen/source/0000-runtime.py"
    preserved.parent.mkdir(parents=True)
    preserved.write_bytes(source.read_bytes())
    runner.e2e.write_json(tmp_path / "run/frozen-source.json", {"files": [record], "sha256": runner.digest([record])})
    source.write_text("repaired grader")
    assert runner.verify_frozen_sources(tmp_path / "run", require_live=False) == [record]
    with pytest.raises(ValueError, match="Frozen execution source differs"):
        runner.verify_frozen_sources(tmp_path / "run")
    preserved.write_text("mutated original")
    with pytest.raises(ValueError, match="Preserved execution source differs"):
        runner.verify_frozen_sources(tmp_path / "run", require_live=False)


@pytest.mark.parametrize("night_failure,coverage,expected_status", [
    (True, True, "finished_with_gaps"), (False, True, "finished"), (False, False, "finished_with_gaps")])
@pytest.mark.parametrize("selected_case", [None, "data_exfil_file"])
def test_night_failures_or_missing_native_inputs_cannot_be_reported_as_finished(
        tmp_path, monkeypatch, night_failure, coverage, expected_status, selected_case):
    run = tmp_path / "runtime/runs/run"
    run.mkdir(parents=True)
    payload = tmp_path / "payload.json"
    payload.write_text("{}")
    docker = {"image_ready": True, "task_image_id": "image", "container": "owned",
              "docker_host": "socket", "outer_gateway": "gateway"}
    runner.e2e.write_json(tmp_path / "runtime/runs/verified/runtime/environment.json", {
        "status": "ready", "docker": docker})
    settings = {"source_worktree": str(tmp_path), "payload_file": str(payload), "protocol_hash": "protocol",
                "conditions": ["clean", "poisoned"], "selected_injection_case": selected_case}
    tasks = [{"task_id": f"task-{n}", "short_id": str(n)} for n in range(30)]
    environment = SimpleNamespace(prepare_docker=lambda *a, **kw: dict(docker))
    monkeypatch.setattr(runner, "EXPERIMENT", tmp_path)
    monkeypatch.setattr(runner.e2e, "run_directory", lambda run_id: tmp_path / "runtime/runs" / run_id)
    monkeypatch.setattr(runner, "prepare_run", lambda *a: (
        run, settings, tasks, environment, object(), object(), object(), lambda p: "bank", object()))
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
    definition = runner.e2e.read_json(run / "state.json")["asr_definition"]
    assert ("synthetic local invocation-audit write" in definition) == (selected_case == "data_exfil_file")
    assert ("deletion" in definition) == (selected_case is None)
    assert set(runner.e2e.read_json(run / "state.json")["night_branches"]) == {"clean", "poisoned"}


@pytest.fixture
def handoff(tmp_path, monkeypatch):
    run = tmp_path / "runtime/runs/run"
    runner.e2e.write_json(run / "runner.json", {"pid": 123})
    runner.e2e.write_json(run / "config.runtime.json", {"all_tasks": [{"short_id": str(n), "task_id": f"task-{n}"} for n in range(30)]})
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
    result = runner.run_victims(tmp_path, {"victim_safe_enabled": True}, tasks, object(), "http://gateway", lambda p: "bank", {
        "poisoned": {"publication": {"changeset_accepted": True, "bank_sha256": "bank"}}})
    arms = result["poisoned"]["arms"]
    assert arms["unprotected"] == {"executions": 5, "valid_denominator": 5, "attack_successes": 5, "asr": 1}
    assert arms["victim_safe"] == {"executions": 5, "valid_denominator": 5, "attack_successes": 0, "asr": 0}


def test_aligned_victim_defaults_to_only_requested_unprotected_arm(tmp_path, monkeypatch):
    tasks = [{"short_id": short_id} for short_id in sorted(runner.INJECTED_IDS)]
    calls = []

    def victim(*args, **kwargs):
        calls.append(kwargs["safe"])
        return {"measurement_valid": True, "attack_success": False}

    monkeypatch.setattr(runner.cohort_sandbox, "run_victim", victim)
    settings = {"cohort_day_alignment": True}
    result = runner.run_victims(tmp_path, settings, tasks, object(), "http://gateway", lambda _: "bank", {
        "poisoned": {"publication": {"changeset_accepted": True, "bank_sha256": "bank"}}})
    assert calls == [False] * len(tasks)
    assert result["poisoned"]["status"] == "completed"
    assert set(result["poisoned"]["arms"]) == {"unprotected"}
    with pytest.raises(ValueError, match="victim_safe_enabled"):
        runner.validate_local_execution({**settings, "victim_safe_enabled": True})
    with pytest.raises(ValueError, match="explicit legacy"):
        runner.run_victims(tmp_path, {**settings, "victim_safe_enabled": True}, tasks, object(), "", None, {})
