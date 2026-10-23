import hashlib
import json
import threading
from pathlib import Path

import pytest

from scripts.injection import resilient_runner as runner
from scripts.wildclawbench import run as controller


def protocol(**updates):
    value = {
        "version": runner.VERSION,
        "conditions": ["poisoned"],
        "max_concurrency": 2,
        "max_transient_retries": 1,
        "circuit_breaker": {"same_signature_limit": 2},
        "stage_budgets": {"night_seconds": 60},
    }
    value.update(updates)
    return {"execution_protocol": value, "conditions": ["poisoned"], "infrastructure_retries": 0}


def test_protocol_is_explicit_and_fixes_retry_and_circuit_policy():
    assert runner.execution_protocol({}) is None
    assert runner.execution_protocol(protocol())["max_transient_retries"] == 1
    with pytest.raises(runner.ProtocolError, match="exactly one"):
        runner.execution_protocol(protocol(max_transient_retries=2))
    value = protocol()
    value["infrastructure_retries"] = 1
    with pytest.raises(runner.ProtocolError, match="owns retries"):
        runner.execution_protocol(value)


def test_repository_config_aliases_normalize_to_one_resilient_policy():
    config = {"execution_protocol": {"version": runner.VERSION, "conditions": ["poisoned"], "day_workers": 8,
        "validation_workers": 8, "infrastructure_retries_per_case": 1,
        "identical_infrastructure_error_circuit_breaker": 2},
        "conditions": ["poisoned"],
        "budgets": {"round_wallclock_hard_cap_seconds": 14400,
                    "final_utility_wallclock_hard_cap_seconds": 7200}}
    normalized = runner.execution_protocol(config)
    assert normalized["max_concurrency"] == 8
    assert normalized["stage_budgets"] == {"night_seconds": 14400, "endpoint_seconds": 7200}


def test_dynamic_case_queue_overlaps_same_user_cases_and_checkpoints(tmp_path):
    both_started = threading.Barrier(2)
    calls = []
    cases = [runner.Case(str(index), {"task": index, "user": "same"}, index) for index in (1, 2)]

    def execute(case):
        calls.append(case.key)
        both_started.wait(timeout=2)
        return {"status": "completed", "score": 0.5, "task": case.payload}

    checkpoint = tmp_path / "queue.json"
    first = runner.run_case_queue(cases, execute, checkpoint, max_workers=2)
    assert all(row["status"] == "completed" for row in first.values())
    assert sorted(calls) == ["1", "2"]
    second = runner.run_case_queue(cases, lambda case: pytest.fail("completed case reran"), checkpoint, max_workers=2)
    assert second == first


def test_checkpoint_rejects_changed_case_identity(tmp_path):
    checkpoint = tmp_path / "queue.json"
    runner.run_case_queue([runner.Case("one", {"task": 1})],
                          lambda case: {"status": "completed", "score": 0}, checkpoint, max_workers=1)
    with pytest.raises(runner.ProtocolError, match="different case identities"):
        runner.run_case_queue([runner.Case("one", {"task": 2})],
                              lambda case: {"status": "completed", "score": 0}, checkpoint, max_workers=1)


def test_one_transient_retry_then_same_signature_circuit_stops_queue(tmp_path):
    calls = []
    cases = [runner.Case(str(index), {"task": index}) for index in (1, 2, 3)]

    def execute(case):
        calls.append(case.key)
        return {"status": "infrastructure_error", "score": None,
                "error_type": "ConnectError", "error": "connection refused on port 12345"}

    outcomes = runner.run_case_queue(cases, execute, tmp_path / "queue.json", max_workers=1)
    assert calls == ["1", "2"]
    assert outcomes["1"]["attempts"] == outcomes["2"]["attempts"] == 1
    assert outcomes["1"]["status"] == outcomes["3"]["status"] == "circuit_open"
    saved = json.loads((tmp_path / "queue.json").read_text())
    assert saved["circuit"]["consecutive_failures"] == 2


def test_agent_started_transient_is_sealed_without_retry_and_later_case_runs(tmp_path):
    calls = []
    cases = [runner.Case(key, {"task": key}) for key in ("started", "later")]

    def execute(case):
        calls.append(case.key)
        if case.key == "started":
            return {"status": "infrastructure_error", "score": None,
                    "error_type": "ConnectError", "error": "connection refused",
                    "infrastructure_retry_eligible": False}
        return {"status": "completed", "score": 0.5}

    def classify(result):
        value = runner.classify_result(result)
        if value.status == "transient" and result.get("infrastructure_retry_eligible") is False:
            return runner.Classification("terminal_transient", value.error_type, value.error)
        return value

    outcomes = runner.run_case_queue(
        cases, execute, tmp_path / "queue.json", max_workers=1, classify=classify)
    assert calls == ["started", "later"]
    assert outcomes["started"]["status"] == "failed"
    assert outcomes["started"]["attempts"] == 1
    assert outcomes["started"]["reason"] == "transient_retry_unsafe_after_agent_started"
    assert outcomes["later"]["status"] == "completed"


def test_two_agent_started_transients_still_open_stage_circuit(tmp_path):
    cases = [runner.Case(str(index), {"task": index}) for index in (1, 2, 3)]

    def execute(_case):
        return {"status": "infrastructure_error", "score": None,
                "error_type": "ConnectError", "error": "connection refused",
                "infrastructure_retry_eligible": False}

    def classify(result):
        value = runner.classify_result(result)
        return runner.Classification("terminal_transient", value.error_type, value.error)

    outcomes = runner.run_case_queue(
        cases, execute, tmp_path / "queue.json", max_workers=1, classify=classify)
    assert outcomes["1"]["status"] == "failed"
    assert outcomes["2"]["status"] == "failed"
    assert outcomes["3"]["status"] == "circuit_open"
    assert all(outcomes[key]["attempts"] == 1 for key in ("1", "2"))


def test_resume_runs_only_never_started_or_caller_verified_cases(tmp_path):
    checkpoint = tmp_path / "queue.json"
    cases = [runner.Case(key, {"task": key}) for key in (
        "terminal", "never-started", "frozen-score", "interrupted-frozen",
        "ambiguous-started")]
    identities = {case.key: runner._canonical(case.identity) for case in cases}
    signature = runner.infrastructure_signature("ConnectError", "connection refused")
    checkpoint.write_text(json.dumps({
        "schema": runner.QUEUE_SCHEMA,
        "protocol_version": runner.VERSION,
        "identities": identities,
        "cases": {
            "terminal": {"status": "failed", "attempts": 1,
                         "identity_sha256": identities["terminal"],
                         "reason": "transient_retry_unsafe_after_agent_started"},
            "never-started": {"status": "circuit_open", "attempts": 0,
                              "identity_sha256": identities["never-started"]},
            "frozen-score": {"status": "failed", "attempts": 1,
                             "identity_sha256": identities["frozen-score"],
                             "reason": "case_failure",
                             "result": {"status": "pending_score", "score": None}},
            "interrupted-frozen": {"status": "running", "attempts": 1,
                                   "identity_sha256": identities["interrupted-frozen"]},
            "ambiguous-started": {"status": "running", "attempts": 1,
                                  "identity_sha256": identities["ambiguous-started"]},
        },
        "infrastructure_streak": {"signature": signature, "consecutive_failures": 2},
        "circuit": {"status": "open", "signature": signature,
                    "consecutive_failures": 2, "opened_by": "terminal"},
        "updated_at": "2026-10-20T00:00:00+00:00",
    }))
    calls = []

    outcomes = runner.run_case_queue(
        cases,
        lambda case: calls.append(case.key) or {"status": "completed", "score": 0.5},
        checkpoint,
        max_workers=1,
        recover_interrupted=lambda case, _record: case.key in {"frozen-score", "interrupted-frozen"},
    )

    assert calls == ["never-started", "frozen-score", "interrupted-frozen"]
    assert outcomes["terminal"]["status"] == "failed"
    assert outcomes["ambiguous-started"]["status"] == "failed"
    assert outcomes["ambiguous-started"]["reason"].endswith("agent_reexecution_forbidden")
    assert outcomes["never-started"]["attempts"] == 1
    assert outcomes["frozen-score"]["attempts"] == 2
    assert outcomes["interrupted-frozen"]["attempts"] == 2
    saved = json.loads(checkpoint.read_text())
    assert len(saved["circuit_history"]) == 1


def test_infrastructure_streak_survives_checkpoint_resume(tmp_path):
    checkpoint = tmp_path / "queue.json"
    cases = [runner.Case("one", {"task": 1}), runner.Case("two", {"task": 2})]
    identity = {case.key: runner._canonical(case.identity) for case in cases}
    checkpoint.write_text(json.dumps({"schema": runner.QUEUE_SCHEMA,
        "protocol_version": runner.VERSION, "identities": identity,
        "cases": {"one": {"status": "failed", "attempts": 2,
                            "identity_sha256": identity["one"]}},
        "infrastructure_streak": {"signature": runner.infrastructure_signature(
            "ConnectError", "connection refused on port 12345"), "consecutive_failures": 1},
        "circuit": None, "updated_at": None}))

    outcomes = runner.run_case_queue(cases, lambda case: {
        "status": "infrastructure_error", "score": None,
        "error_type": "ConnectError", "error": "connection refused on port 54321",
    }, checkpoint, max_workers=1)
    assert outcomes["two"]["status"] == "circuit_open"
    assert json.loads(checkpoint.read_text())["circuit"]["consecutive_failures"] == 2


def test_stage_wide_infrastructure_observer_is_idempotent_and_opens_on_second_unit(tmp_path):
    checkpoint = tmp_path / "infrastructure-circuit.json"
    first = runner.observe_infrastructure_result(
        checkpoint, "candidate-one", error_type="ConnectError",
        error="connection refused on port 12345")
    repeated = runner.observe_infrastructure_result(
        checkpoint, "candidate-one", error_type="ConnectError",
        error="connection refused on port 12345")
    assert repeated == first
    with pytest.raises(runner.CircuitBreakerOpen, match="opened"):
        runner.observe_infrastructure_result(
            checkpoint, "candidate-two", error_type="ConnectError",
            error="connection refused on port 54321")
    saved = json.loads(checkpoint.read_text())
    assert saved["circuit"]["consecutive_failures"] == 2


def test_candidate_failure_is_isolated_and_later_case_runs(tmp_path):
    def execute(case):
        if case.key == "bad":
            raise ValueError("candidate bundle is malformed")
        return {"status": "completed", "score": 0.7}

    outcomes = runner.run_case_queue([
        runner.Case("bad", {"candidate": "bad"}), runner.Case("good", {"candidate": "good"})
    ], execute, tmp_path / "queue.json", max_workers=1)
    assert outcomes["bad"]["status"] == "failed"
    assert outcomes["good"]["status"] == "completed"


def test_completed_result_without_a_numeric_score_is_not_completed():
    classification = runner.classify_result({"status": "completed"})
    assert classification.status == "failed"


def test_stage_budget_persists_one_deadline_and_caps_all_timeouts(tmp_path):
    moments = iter([100.0, 110.0, 121.0])
    budget = runner.StageBudget.open(tmp_path / "budget.json", "night", 20, clock=lambda: next(moments))
    capped = budget.cap_config({"agent_timeout": 99, "grading_timeout_seconds": 99}, "summary",
                               clock=lambda: next(moments))
    assert capped["agent_timeout"] == capped["grading_timeout_seconds"] == 10
    resumed = runner.StageBudget.open(tmp_path / "budget.json", "night", 20, clock=lambda: 999)
    assert resumed.deadline_epoch == 120
    with pytest.raises(runner.StageBudgetExceeded):
        resumed.require_remaining("validation", clock=lambda: next(moments))


def test_typed_sources_are_canonical_and_detect_ambiguous_ids():
    refs = runner.typed_source_refs(["task-b", "task-a"], ["session-b", "session-a"])
    assert refs == [{"kind": "task", "id": "task-a"}, {"kind": "task", "id": "task-b"},
                    {"kind": "session", "id": "session-a"}, {"kind": "session", "id": "session-b"}]
    assert runner.validate_typed_sources({"source_task_ids": ["task-b", "task-a"],
                                         "source_session_ids": ["session-a", "session-b"],
                                         "source_refs": refs}) == refs
    with pytest.raises(runner.ProtocolError, match="both a task and a session"):
        runner.typed_source_refs(["same"], ["same"])


def _reference(root: Path, name: str, value: dict) -> dict:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return {"path": name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _archived_day_fixture(repository: Path) -> tuple[str, Path, list[dict], dict, Path, Path]:
    run_id = "archived-day-fixture"
    source = Path("/retired/shared-skill/runs/poisoned/day-01")
    mirror = repository / "artifacts" / run_id / "source"
    evidence = []

    def write(relative: str, value) -> Path:
        path = mirror / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(value, str):
            path.write_text(value)
        else:
            path.write_text(json.dumps(value))
        evidence.append({"path": str(source / relative),
                         "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        return path

    tasks = [{"task_id": f"task-{index:02d}", "short_id": f"01.{index:02d}",
              "user_id": f"user-{index:02d}", "source_sha256": f"source-{index:02d}",
              "input_sha256": f"input-{index:02d}"}
             for index in range(1, 31)]
    bank_sha256 = "b" * 64
    prompts, contracts = [], []
    contract_fields = ("approved_variant", "execution_preamble_source", "timeout_seconds",
                       "judge_model", "audio_judge_route", "shared_skill_bank", "search",
                       "local_image_generation", "modified_checks_sha256")
    runtime_fields = ("benchmark_revision", "dataset_revision", "model_revision",
                      "audio_model_revision", "image_model_revision", "task_image_id",
                      "external_judge", "judge_model_id", "judge_reasoning_effort")
    for task in tasks:
        short = task["short_id"]
        task_output = f"day/tasks/{short}/task"
        service_dir = f"services/day-{short}"
        session_id = f"session-{short}"
        uploaded_sha256 = hashlib.sha256(session_id.encode()).hexdigest()
        write(f"day/tasks/{short}/state.json", {
            "status": "completed", "task_id": task["task_id"], "short_id": short,
            "user_id": task["user_id"], "bank_sha256": bank_sha256,
            "task_score": 0.5, "timed_out": False, "task_output": task_output,
            "service_dir": service_dir, "uploaded_sha256": uploaded_sha256,
        })
        write(f"{task_output}/experiment-result.json", {
            "status": "completed", "task_id": task["task_id"], "score": 0.5,
            "timed_out": False, "run_id": "archived-source",
        })
        write(f"{service_dir}/native-day-session.json", {
            "task_id": task["task_id"], "session_id": session_id,
            "user_id": task["user_id"], "phase": "day",
            "skill_bank_sha256": bank_sha256,
            "outcome": {"score": 0.5, "timed_out": False}, "turns": [],
        })
        write(f"{service_dir}/upload-evidence.json", {
            "session_id": session_id, "uploaded_sha256": uploaded_sha256,
        })
        prompt_path = write(f"{task_output}/execution-prompt.txt", f"prompt for {short}\n")
        variant_path = write(f"{task_output}/task-variant.json", {})
        runtime_path = write(f"{task_output}/runtime-config.json", {})
        prompts.append({"task_id": task["task_id"],
                        "sha256": hashlib.sha256(prompt_path.read_bytes()).hexdigest()})
        variant, runtime = json.loads(variant_path.read_text()), json.loads(runtime_path.read_text())
        contracts.append({"task_id": task["task_id"],
                          "variant": {field: variant.get(field) for field in contract_fields},
                          "runtime": {field: runtime.get(field) for field in runtime_fields}})

    config = {"model_id": "model", "model_revision": "revision", "temperature": 0,
              "reasoning_effort": "high", "max_tokens": 10, "context_length": 100,
              "seed": 42, "all_tasks": tasks}
    write("config.runtime.json", config)
    write("day/state.json", {"status": "completed", "scored_tasks": 30})
    identity_config = {**config, "agent_prompt_sha256": runner._canonical(prompts),
                       "runtime_contract_sha256": runner._canonical(contracts)}
    identity = runner.baseline_execution_identity(identity_config, tasks, bank_sha256)

    audit_path = mirror / "audit.json"
    audit_path.write_text(json.dumps({"proof": {"evidence": evidence}}))
    index_path = repository / "artifacts" / run_id / "archive.json"
    index_path.write_text(json.dumps({
        "schema": "sharedskill-experiment-archive-v1", "run_id": run_id,
        "roots": [{"original": str(source),
                   "mirrors": [str(mirror.relative_to(repository))]}],
        "audit": {"path": str(source / "audit.json"),
                  "sha256": hashlib.sha256(audit_path.read_bytes()).hexdigest()},
    }))
    return run_id, source, tasks, identity, audit_path, index_path


def _rewrite_archive_audit(audit_path: Path, index_path: Path, audit: dict) -> None:
    audit_path.write_text(json.dumps(audit))
    index = json.loads(index_path.read_text())
    index["audit"]["sha256"] = hashlib.sha256(audit_path.read_bytes()).hexdigest()
    index_path.write_text(json.dumps(index))


def test_baseline_bundle_requires_exact_execution_and_task_identity(tmp_path):
    tasks = [{"task_id": f"task-{index}", "short_id": f"01.{index:02d}", "user_id": "user-01",
              "source_sha256": f"source-{index}", "input_sha256": f"input-{index}"}
             for index in range(1, 31)]
    config = {"model_id": "model", "model_revision": "revision", "temperature": 0,
              "reasoning_effort": "high", "max_tokens": 10, "context_length": 100,
              "agent_prompt_sha256": "prompt", "runtime_contract_sha256": "runtime", "seed": 42}
    identity = runner.baseline_execution_identity(config, tasks, "bank")
    records = []
    for task in tasks:
        result = _reference(tmp_path, f"results/{task['short_id']}.json",
                            {"status": "completed", "score": 0.5, "timed_out": False,
                             "task_id": task["task_id"], "run_id": "source"})
        session = _reference(tmp_path, f"sessions/{task['short_id']}.json",
                             {"task_id": task["task_id"], "session_id": "session-" + task["short_id"],
                              "user_id": task["user_id"], "phase": "day", "skill_bank_sha256": "bank",
                              "original_session_id": "session-" + task["short_id"],
                              "native_uploaded_sha256": "a" * 64,
                              "native_record_segment_index": 0, "native_record_segment_count": 1,
                              "outcome": {"score": 0.5, "timed_out": False}, "turns": []})
        records.append({**task, "score": 0.5, "timed_out": False, "result": result, "session": session})
    (tmp_path / "manifest.json").write_text(json.dumps({"schema": runner.BASELINE_SCHEMA,
        "task_count": 30, "execution_identity": identity, "tasks": records}))
    imported = runner.BaselineImport(tmp_path)
    row, proof = imported.scored_row(tasks[0], identity, "bank")
    assert row["score"] == 0.5 and proof["new_model_calls"] == 0
    assert len(imported.sessions(tasks, identity)) == 30
    changed = dict(identity, temperature=1)
    with pytest.raises(runner.ProtocolError, match="incompatible"):
        imported.scored_row(tasks[0], changed, "bank")


def test_archive_baseline_builder_requires_every_source_in_authenticated_audit(tmp_path):
    run_id, source, tasks, identity, audit_path, index_path = _archived_day_fixture(tmp_path)
    imported = runner.build_baseline_import_from_archive(
        tmp_path, run_id, str(source), tmp_path / "baseline", tasks, identity)
    row, proof = imported.scored_row(tasks[0], identity, identity["s0_bank_sha256"])
    assert row["score"] == 0.5
    assert proof["reused_without_execution_or_rescoring"] is True

    audit = json.loads(audit_path.read_text())
    omitted = str(source / "day/tasks/01.01/task/execution-prompt.txt")
    audit["proof"]["evidence"] = [reference for reference in audit["proof"]["evidence"]
                                      if reference["path"] != omitted]
    _rewrite_archive_audit(audit_path, index_path, audit)
    with pytest.raises(runner.ProtocolError, match="absent from the authenticated audit"):
        runner.build_baseline_import_from_archive(
            tmp_path, run_id, str(source), tmp_path / "baseline-with-gap", tasks, identity)


def test_archive_baseline_builder_rejects_authenticated_task_output_escape(tmp_path):
    run_id, source, tasks, identity, audit_path, index_path = _archived_day_fixture(tmp_path)
    state_relative = "day/tasks/01.01/state.json"
    state_path = tmp_path / "artifacts" / run_id / "source" / state_relative
    state = json.loads(state_path.read_text())
    state["task_output"] = "../outside-day"
    state_path.write_text(json.dumps(state))

    audit = json.loads(audit_path.read_text())
    original = str(source / state_relative)
    for reference in audit["proof"]["evidence"]:
        if reference["path"] == original:
            reference["sha256"] = hashlib.sha256(state_path.read_bytes()).hexdigest()
            break
    _rewrite_archive_audit(audit_path, index_path, audit)

    with pytest.raises(runner.ProtocolError, match="task_output escapes"):
        runner.build_baseline_import_from_archive(
            tmp_path, run_id, str(source), tmp_path / "baseline", tasks, identity)


def test_alignment_candidate_error_is_recorded_and_publication_continues(tmp_path, monkeypatch):
    bank = tmp_path / "S0"
    (bank / "skill").mkdir(parents=True)
    (bank / "skill/SKILL.md").write_text("parent")
    run_dir = tmp_path / "branch"
    directory = run_dir / "nights/01"
    config = {**protocol(), "protocol_hash": "protocol", "score_tolerance": 0,
              "alignment": {"version": "native-night-atomic-v1"}}
    candidate = {"name": "broken", "candidate_id": "broken-id", "bundles": [{"name": "broken"}]}
    monkeypatch.setattr(controller, "alignment_settings", lambda cfg: {"native_candidate_scope": "per-skill-v1"})
    monkeypatch.setattr(controller, "_prepare_alignment_candidate",
                        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("bad source")))
    published = controller._alignment_night(1, bank, [], [], run_dir, config, directory, [candidate])
    publication = json.loads((directory / "publication.json").read_text())
    assert controller.tree_hash(published) == controller.tree_hash(bank)
    assert publication["per_skill_decisions"][0]["individual_reason"] == "candidate_validation_error"
    assert publication["validation_gaps"][0]["phase"] == "candidate_preparation"
    assert publication["validation_gaps"][0]["error"] == "bad source"
    assert json.loads(next((run_dir / "operations/unattended/skips").glob("*.json")).read_text())["score_available"] is False


@pytest.mark.parametrize("marker,runtime_forbids", [(False, True), (True, False)])
def test_real_validation_queue_never_repeats_unsealed_agent(tmp_path, monkeypatch, marker, runtime_forbids):
    from scripts.wildclawbench import task_runtime
    bank = tmp_path / "bank"
    bank.mkdir()
    (bank / "SKILL.md").write_text("unchanged")
    tasks = [{"task_id": name, "short_id": short, "user_id": "user-01", "category": "test",
              "source_sha256": "fixture"} for name, short in (("started", "03.01"), ("healthy", "03.03"))]
    config = {**protocol(max_concurrency=1), "protocol_hash": "fixture", "seed": 42,
        "all_tasks": tasks, "execution_plan": "longitudinal-day-night-text-endpoint-v1"}
    calls = []

    def execute(task_id, _bank, output, _config):
        calls.append(task_id)
        if task_id == "healthy":
            return {"status": "completed", "score": .5}
        if marker:
            (output / "agent-started.json").write_text('{"started":true}')
        return {"status": "infrastructure_error", "score": None, "error": "Runtime child exited 124",
                "infrastructure_retry_eligible": not runtime_forbids}

    monkeypatch.setattr(task_runtime, "execute_task", execute)
    with pytest.raises(RuntimeError, match="isolated cases incomplete"):
        controller.run_stage(tasks, bank, tmp_path, config, "validation", 1, "candidate", "fixture")
    assert calls == ["started", "healthy"]
    checkpoint = json.loads(next((tmp_path / "operations/resilient-queues").glob("*.json")).read_text())
    assert checkpoint["cases"]["03.01"]["attempts"] == 1
    assert checkpoint["cases"]["03.03"]["status"] == "completed"


@pytest.mark.parametrize("field", [
    "top_p", "top_k", "min_p", "presence_penalty", "repetition_penalty",
    "grading_timeout_seconds", "task_timeout_seconds", "native_llm_request_timeout_seconds",
    "judge_max_tokens", "judge_reasoning_effort", "local_judge_max_tokens", "local_judge_enable_thinking",
])
def test_baseline_identity_binds_sampling_and_budget_fields(field):
    config = {"task_manifest_sha256": "manifest", "model_id": "model", "model_revision": "revision",
              "agent_prompt_sha256": "prompt", "runtime_contract_sha256": "runtime", field: 1}
    before = runner.baseline_execution_identity(config, [], "bank")
    after = runner.baseline_execution_identity({**config, field: 2}, [], "bank")
    assert before != after


def test_baseline_task_timeout_is_bound_even_with_same_declared_manifest():
    config = {"task_manifest_sha256": "manifest", "model_id": "model", "model_revision": "revision",
              "agent_prompt_sha256": "prompt", "runtime_contract_sha256": "runtime"}
    a = runner.baseline_execution_identity(config, [{"task_id": "task", "timeout_seconds": 10}], "bank")
    b = runner.baseline_execution_identity(config, [{"task_id": "task", "timeout_seconds": 20}], "bank")
    assert a["task_conditions_sha256"] != b["task_conditions_sha256"]


def test_missing_local_judge_override_is_distinct_from_explicit_false():
    config = {"model_id": "model", "model_revision": "revision",
              "agent_prompt_sha256": "prompt", "runtime_contract_sha256": "runtime"}
    before = runner.baseline_execution_identity(config, [], "bank")
    after = runner.baseline_execution_identity({**config, "local_judge_enable_thinking": False}, [], "bank")
    assert before != after


def test_required_skill_prompt_change_cannot_reuse_previous_day_baseline():
    from scripts.injection.e2e_cycles import cycle_settings

    source = {"all_tasks": [], "model_id": "local", "model_revision": "fixed",
              "agent_prompt_sha256": "actual-old-prompt", "runtime_contract_sha256": "actual-runtime"}
    identity = runner.baseline_execution_identity(source, [], "bank")
    imported = object.__new__(runner.BaselineImport)
    imported.manifest = {"schema": runner.BASELINE_SCHEMA, "execution_identity": identity}
    with pytest.raises(ValueError, match="required_shared_skill"):
        cycle_settings(source, {"required_shared_skill": "xlsx"})
    changed = runner.baseline_execution_identity({**source, "required_shared_skill": "xlsx"}, [], "bank")
    with pytest.raises(runner.ProtocolError, match="incompatible"):
        imported.verify_identity(changed)
    # Earlier v2 imports never attested to this prompt condition.
    imported.manifest["execution_identity"] = {
        key: value for key, value in identity.items() if key != "required_shared_skill"}
    with pytest.raises(runner.ProtocolError, match="incompatible"):
        imported.verify_identity(identity)


def test_legacy_baseline_is_read_only_and_cannot_pass_new_reuse_contract():
    imported = object.__new__(runner.BaselineImport)
    imported.manifest = {"schema": runner.LEGACY_BASELINE_SCHEMA, "execution_identity": {}}
    with pytest.raises(runner.ProtocolError, match="read-only"):
        imported.verify_identity({"identity_version": "baseline-execution-v2"})


def fresh_day_fixture(tmp_path, monkeypatch):
    from scripts.injection import e2e_30 as cohort
    from scripts.wildclawbench import alignment_snapshot
    from scripts.wildclawbench.protocol import tree_hash
    source = tmp_path / "fresh-day"
    bank = source / "banks/S0/skill"
    bank.mkdir(parents=True)
    (bank / "SKILL.md").write_text("original skill")
    bank_hash = tree_hash(bank.parent)
    tasks = [{"task_id": f"task-{i}", "short_id": f"03.0{i}", "user_id": f"user-{i}",
              "source_path": str(source / f"task-{i}.md"), "source_sha256": f"source-{i}",
              "input_sha256": f"input-{i}", "timeout_seconds": 240} for i in (1, 2)]
    config = {"all_tasks": tasks, "injected_task_short_ids": ["03.02"],
              "model_id": "local-model", "model_revision": "revision",
              "agent_timeout": 900, "evolution_timeout_seconds": 900,
              "top_p": .95, "task_timeout_seconds": 240, "grading_timeout_seconds": 180,
              "protocol_hash": "protocol"}
    def write(relative, value):
        path = source / relative
        runner._write_json(path, value)
        return path
    write("config.runtime.json", config)
    write("day/state.json", {"status": "completed", "scored_tasks": 2})
    write("finalized-day-inputs/manifest.json", {"task_count": 2,
        "input_coverage_complete": True, "bank_sha256": bank_hash})
    upload = b"{}"
    uploaded_hash = hashlib.sha256(upload).hexdigest()
    for i, task in enumerate(tasks, 1):
        short, task_id = task["short_id"], task["task_id"]
        output = f"day/tasks/{short}/task"
        write(f"day/tasks/{short}/state.json", {"status": "completed", "task_id": task_id,
            "task_score": .5, "bank_sha256": bank_hash, "session_id": f"native-{i}", "timed_out": False})
        write(f"services/day-{short}/native-day-session.json", {
            "task_id": task_id, "session_id": f"native-{i}", "skill_bank_sha256": bank_hash})
        write(f"{output}/experiment-result.json", {"status": "completed", "score": .5,
            "task_id": task_id, "run_id": f"run-{i}", "timed_out": False})
        write(f"{output}/runtime-config.json", {**config, "task": {"source_path": task["source_path"]},
            "user_id": task["user_id"]})
        write(f"{output}/execution-binding.json", {"bank_sha256": bank_hash,
            "protocol_hash": "protocol", "run_id": f"run-{i}"})
        write(f"{output}/execution.json", {"task_id": task_id,
            "scoring_status": "completed", "output_snapshot_sha256": "sealed"})
        prompt = source / output / "execution-prompt.txt"
        prompt.write_text(f"actual prompt for {task_id}")
        write(f"{output}/task-variant.json", {
            "execution_prompt_sha256": hashlib.sha256(prompt.read_bytes()).hexdigest()})
        base = source / "finalized-day-inputs" / short
        base.mkdir(parents=True)
        (base / "uploaded.json").write_bytes(upload)
        (base / "captured-upload.json").write_bytes(upload)
        segments = []
        for number in range(1, (2 if i == 1 else 1) + 1):
            relative = Path("segments") / f"{number:03d}"
            (base / relative).mkdir(parents=True)
            (base / relative / "uploaded.json").write_bytes(upload)
            session = {"task_id": task_id, "session_id": f"native-{i}-segment-{number}",
                "original_session_id": f"native-{i}", "native_record_segment_index": number,
                "native_uploaded_sha256": uploaded_hash, "user_id": task["user_id"], "phase": "day",
                "skill_bank_sha256": bank_hash, "outcome": {"score": .5, "timed_out": False}, "turns": []}
            runner._write_json(base / relative / "session.json", session)
            segments.append({"session_id": session["session_id"], "uploaded_path": str(relative / "uploaded.json"),
                "uploaded_sha256": uploaded_hash, "session_path": str(relative / "session.json"),
                "session_sha256": hashlib.sha256((base / relative / "session.json").read_bytes()).hexdigest()})
        runner._write_json(base / "manifest.json", {"task_id": task_id, "input_coverage_complete": True,
            "final_uploaded_sha256": uploaded_hash, "captured_uploaded_sha256": uploaded_hash, "segments": segments})
    # The builder is tested against real score/session fixtures; outer source-tree
    # and Agent seal verification are separate suites, with no container/model calls.
    monkeypatch.setattr(cohort, "validate_resume", lambda *a, **k: None)
    monkeypatch.setattr(alignment_snapshot, "execution_identity", lambda *a: {})
    monkeypatch.setattr(alignment_snapshot, "verify_agent_output", lambda *a: {"manifest_sha256": "sealed"})
    return source, tasks, config, bank_hash


def test_night_evolver_cap_does_not_change_day_baseline_identity(tmp_path, monkeypatch):
    source, tasks, settings, bank_hash = fresh_day_fixture(tmp_path, monkeypatch)
    imported = runner.build_baseline_import_from_run(source, tmp_path / "baseline")
    identity = imported.manifest["execution_identity"]
    settings.update({key: identity[key] for key in ("agent_prompt_sha256", "runtime_contract_sha256")})
    budget = runner.StageBudget.open(tmp_path / "night-budget.json", "night", 900, clock=lambda: 100)
    capped = budget.cap_config(settings, "native-evolver", clock=lambda: 101)
    assert capped["agent_timeout"] == 899
    imported.verify_identity(runner.baseline_execution_identity(capped, tasks, bank_hash))
    # A cap that actually changes the task/grading budget remains incompatible.
    capped = budget.cap_config(settings, "too-late", clock=lambda: 900)
    with pytest.raises(runner.ProtocolError, match="identity"):
        imported.verify_identity(runner.baseline_execution_identity(capped, tasks, bank_hash))


def test_fresh_two_task_import_preserves_all_native_segments_and_is_idempotent(tmp_path, monkeypatch):
    source, tasks, config, bank_hash = fresh_day_fixture(tmp_path, monkeypatch)
    original = (source / "config.runtime.json").read_bytes()
    bundle = runner.build_baseline_import_from_run(source, tmp_path / "import")
    identity = bundle.manifest["execution_identity"]
    assert bundle.manifest["task_count"] == 2
    assert len(bundle.sessions(tasks, identity)) == 3
    assert bundle.scored_row(tasks[0], identity, bank_hash)[0]["score"] == .5
    assert (source / "config.runtime.json").read_bytes() == original
    assert runner.build_baseline_import_from_run(source, tmp_path / "import").manifest == bundle.manifest
    reference = bundle.manifest["tasks"][0]["sessions"][0]
    (bundle.directory / reference["path"]).write_text("{}")
    with pytest.raises(runner.ProtocolError, match="digest changed"):
        bundle.sessions(tasks, identity)


def test_fresh_import_reads_native_finalizer_nested_coverage(tmp_path, monkeypatch):
    source, tasks, _, _ = fresh_day_fixture(tmp_path, monkeypatch)
    for task in tasks:
        path = source / "finalized-day-inputs" / task["short_id"] / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["native_finalization"] = {"input_coverage_complete": manifest.pop("input_coverage_complete")}
        runner._write_json(path, manifest)
    bundle = runner.build_baseline_import_from_run(source, tmp_path / "import")
    assert len(bundle.sessions(tasks, bundle.manifest["execution_identity"])) == 3


def test_fresh_import_rejects_changed_effective_sampling(tmp_path, monkeypatch):
    source, tasks, config, bank_hash = fresh_day_fixture(tmp_path, monkeypatch)
    path = source / "day/tasks/03.01/task/runtime-config.json"
    runtime = json.loads(path.read_text())
    runner._write_json(path, {**runtime, "top_p": .1})
    with pytest.raises(runner.ProtocolError, match="effective sampling"):
        runner.build_baseline_import_from_run(source, tmp_path / "import")
    assert not (tmp_path / "import").exists()


def test_fresh_import_rejects_incomplete_day_instead_of_partial_denominator(tmp_path, monkeypatch):
    source, tasks, config, bank_hash = fresh_day_fixture(tmp_path, monkeypatch)
    runner._write_json(source / "day/state.json", {"status": "completed", "scored_tasks": 1})
    with pytest.raises(runner.ProtocolError, match="complete scored Day"):
        runner.build_baseline_import_from_run(source, tmp_path / "import")


def test_source_compatibility_requires_exact_reviewed_digest_pairs():
    old = [{"path": "/runtime.py", "sha256": "old"}]
    new = [{"path": "/runtime.py", "sha256": "new"}]
    review = {"path": "/runtime.py", "before_sha256": "old", "after_sha256": "new",
              "scope": "grading_only", "reason": "separate local judge policy; Agent route unchanged"}
    runner._verify_source_changes(old, new, [review])
    for reviews in ([], [{**review, "scope": "agent_execution"}], [{**review, "after_sha256": "another"}]):
        with pytest.raises(runner.ProtocolError, match="compatibility review"):
            runner._verify_source_changes(old, new, reviews)


def test_reused_agent_keeps_sampling_and_renders_identical_prompt(tmp_path, monkeypatch):
    from scripts.wildclawbench import alignment_snapshot, task_runtime
    source = tmp_path / "task.md"
    source.write_text("official task")
    settings = {"model_id": "fixed", "top_p": .95, "task_timeout_seconds": 240}
    task = {"task_id": "task", "user_id": "user-01", "source_path": str(source)}
    output = tmp_path / "output"
    runner._write_json(output / "runtime-config.json", settings)
    runner._write_json(output / "execution.json", {"status": "agent_completed", "task_id": "task",
                                                  "output_snapshot_sha256": "sealed"})
    runner._write_json(output / "execution-binding.json", {"run_id": "original"})
    prompt = task_runtime._execution_preamble(240) + "Complete the task."
    (output / "execution-prompt.txt").write_text(prompt)
    monkeypatch.setattr(alignment_snapshot, "execution_identity", lambda *a: {})
    monkeypatch.setattr(alignment_snapshot, "verify_agent_output", lambda *a: {"manifest_sha256": "sealed"})
    monkeypatch.setattr(task_runtime, "task_spec", lambda *a: {"task_id": "task", "timeout_seconds": 240,
        "prompt": "Complete the task.", "file_path": str(source)})
    # The actual variants/preamble renderer runs; only the upstream parser and seal fixture are isolated.
    changed_judge = {**settings, "local_judge_enable_thinking": False, "local_judge_max_tokens": 4096}
    assert runner._check_reused_agent(output, task, settings, changed_judge)["source_run_id"] == "original"
    with pytest.raises(runner.ProtocolError, match="Changed Agent sampling"):
        runner._check_reused_agent(output, task, settings, {**changed_judge, "top_p": .1})
    (output / "execution-prompt.txt").write_text("different original prompt")
    with pytest.raises(runner.ProtocolError, match="changes the original Agent prompt"):
        runner._check_reused_agent(output, task, settings, changed_judge)


def test_derived_baseline_retains_old_agent_binding_and_new_score_identity(tmp_path, monkeypatch):
    source, tasks, config, _ = fresh_day_fixture(tmp_path, monkeypatch)
    config.update(protocol_hash="derived-protocol", local_judge_enable_thinking=False, local_judge_max_tokens=4096)
    runner._write_json(source / "config.runtime.json", config)
    proof = {"source_protocol_hash": "protocol", "tasks": {}}
    for task in tasks:
        output = source / "day/tasks" / task["short_id"] / "task"
        result = json.loads((output / "experiment-result.json").read_text())
        result["run_id"] = "new-grading-" + task["short_id"]
        runner._write_json(output / "experiment-result.json", result)
        proof["tasks"][task["short_id"]] = {"scoring_run_id": result["run_id"]}
    runner._write_json(source / "derived-source.json", proof)
    bundle = runner.build_baseline_import_from_run(source, tmp_path / "import")
    assert bundle.manifest["execution_identity"]["local_judge_enable_thinking"] is False
    assert bundle.manifest["source"]["derived_source_sha256"] == hashlib.sha256(
        (source / "derived-source.json").read_bytes()).hexdigest()
    assert len(bundle.sessions(tasks, bundle.manifest["execution_identity"])) == 3


@pytest.mark.parametrize("eligible", [None, False, True])
def test_validation_score_recovery_requires_explicit_runtime_permission(tmp_path, monkeypatch, eligible):
    from scripts.wildclawbench import task_runtime
    bank = tmp_path / "bank"
    bank.mkdir()
    (bank / "SKILL.md").write_text("unchanged")
    tasks = [{"task_id": name, "short_id": short, "user_id": "user-01", "category": "test",
              "source_sha256": "fixture"} for name, short in (("pending", "03.01"), ("healthy", "03.03"))]
    config = {**protocol(max_concurrency=1), "protocol_hash": "fixture", "seed": 42,
        "all_tasks": tasks, "execution_plan": "longitudinal-day-night-text-endpoint-v1"}
    calls = []

    def execute(task_id, _bank, output, _config):
        calls.append(("agent", task_id))
        if task_id == "healthy":
            return {"status": "completed", "score": .5}
        runner._write_json(output / "execution.json", {"task_id": task_id, "status": "agent_completed"})
        result = {"status": "pending_score", "score": None, "error": "ReadTimeout from grader"}
        if eligible is not None:
            result["scoring_recovery_eligible"] = eligible
        return result

    def rescore(task_id, _output, _config):
        calls.append(("judge", task_id))
        return {"status": "completed", "score": .6}

    monkeypatch.setattr(task_runtime, "execute_task", execute)
    monkeypatch.setattr(task_runtime, "rescore_task", rescore)
    invoke = lambda: controller.run_stage(tasks, bank, tmp_path, config, "validation", 1, "candidate", "fixture")
    if eligible is True:
        rows = invoke()
        assert all(row["status"] == "completed" for row in rows)
        assert calls.count(("judge", "pending")) == 1
    else:
        with pytest.raises(RuntimeError, match="isolated cases incomplete"):
            invoke()
        # Neither a direct invocation nor queue resume grants permission to
        # repeat prior successful or deterministically failed judge responses.
        prior = controller.run_one(tasks[0], bank, tmp_path, config, "validation", 1, "candidate", "fixture")
        assert prior["status"] == "pending_score" and prior.get("scoring_recovery_eligible") is eligible
        with pytest.raises(RuntimeError, match="isolated cases incomplete"):
            invoke()
        assert calls == [("agent", "pending"), ("agent", "healthy")]
    assert calls.count(("agent", "pending")) == 1
