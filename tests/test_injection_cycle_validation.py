import copy
from pathlib import Path
from types import SimpleNamespace
from types import FunctionType

import pytest

from scripts.injection import cycle_validation as validation


@pytest.fixture
def cohort(tmp_path):
    source = tmp_path / "cohort"
    source.mkdir()
    original = tmp_path / "original.md"
    original.write_text("# Official task\n\n## Workspace Path\n```text\nworkspace/task\n```\n")
    frozen = source / "frozen/tasks/03.01/task.md"
    workspace = frozen.parent / "task-workspace"
    workspace.mkdir(parents=True)
    (workspace / "input.txt").write_text("official input")
    frozen.write_text(original.read_text().replace("workspace/task", str(workspace)))
    manifest = validation.e2e.tree_manifest(workspace)
    task = {"task_id": "03_task_1", "short_id": "03.01", "user_id": "user-01",
            "source_path": str(frozen), "original_source_path": str(original),
            "source_sha256": validation._hash_file(original),
            "original_source_sha256": validation._hash_file(original),
            "input_sha256": manifest["sha256"], "input_manifest": manifest}
    validation.e2e.write_json(source / "config.runtime.json", {"all_tasks": [task]})
    validation.e2e.write_json(source / "frozen-tasks.json", [copy.deepcopy(task)])
    binding = validation.build_validation_binding(source)
    return source, task, binding


def test_cohort_inputs_validate_exact_original_and_frozen_tree(cohort):
    source, task, binding = cohort
    proof = validation.validate_cohort_inputs(binding, task)
    assert proof["validated"] is True
    assert proof["source_sha256"] == validation._hash_file(Path(task["source_path"]))
    assert proof["source_sha256"] != task["source_sha256"]
    assert proof["input_sha256"] == task["input_sha256"]
    (source / "frozen/tasks/03.01/task-workspace/input.txt").write_text("changed")
    with pytest.raises(ValueError, match="input tree changed"):
        validation.validate_cohort_inputs(binding, task)


def test_input_binding_does_not_bind_its_own_runtime_config(cohort):
    source, task, _ = cohort
    binding = validation.prepare_input_binding(source, [task])
    validation.e2e.write_json(source / "config.runtime.json", {
        "all_tasks": [task], validation.BINDING_KEY: binding})
    assert validation.validate_cohort_inputs(binding, task)["validated"] is True
    assert validation.prepare_input_binding(source, [task]) == binding
    with pytest.raises(ValueError, match="descriptors changed"):
        validation.prepare_input_binding(source, [{**task, "user_id": "other"}])


@pytest.mark.parametrize("mutation", ["original", "frozen", "descriptor", "missing"])
def test_invalid_cohort_inputs_fail_before_execution(cohort, mutation):
    source, task, binding = cohort
    if mutation == "original":
        Path(task["original_source_path"]).write_text("changed task")
    elif mutation == "frozen":
        Path(task["source_path"]).write_text("changed task")
    elif mutation == "descriptor":
        task["source_path"] = "/unrelated/task.md"
    else:
        (source / "frozen/tasks/03.01/task-workspace/input.txt").unlink()
    with pytest.raises(ValueError):
        validation.validate_cohort_inputs(binding, task)


def test_adapter_preserves_original_executor_arguments_and_official_fallback(cohort, monkeypatch):
    _source, task, binding = cohort
    validation.e2e.load_components()
    from scripts.wildclawbench import provenance
    calls = []
    official = lambda config, row: {"official": row["task_id"]}
    monkeypatch.setattr(provenance, "validate_task_inputs", official)

    def original(row, bank, root, config, phase, number, arm, candidate):
        calls.append((row, bank, root, config, phase, number, arm, candidate))
        assert provenance.validate_task_inputs(config, row)["validated"] is True
        # OCR scoring makes a config copy and must keep the cohort validation.
        assert provenance.validate_task_inputs(dict(config, task=row), row)["validated"] is True
        return {"score": 0.0, "status": "completed"}

    controller = SimpleNamespace(run_one=original)
    config = {validation.BINDING_KEY: binding, "protocol_hash": "new-protocol"}
    bank, root = Path("/bank"), _source / "revision"
    with validation.validation_adapter(controller, binding):
        result = controller.run_one(task, bank, root, config, "validation", 3, "old", "atomic-id")
        assert result == {"score": 0.0, "status": "completed"}
        assert calls == [(task, bank, root, config, "validation", 3, "old", "atomic-id")]
        assert provenance.validate_task_inputs({}, {"task_id": "official"}) == {"official": "official"}
        evidence = validation.e2e.read_json(root / "executions/validation/03/atomic-id/old/03.01/input-validation.json")
        assert evidence["validated"] is True
        with pytest.raises(ValueError, match="validation binding"):
            controller.run_one(task, bank, root, {}, "validation", 3)
    assert controller.run_one is original
    assert provenance.validate_task_inputs is official


def test_adapter_uses_real_controller_score_recovery_without_agent_rerun(cohort, monkeypatch):
    source, task, binding = cohort
    _, _, _, _, controller, runtime, _, _, _ = validation.e2e.load_components()
    from scripts.wildclawbench import provenance

    task.update(category="test")
    # Re-freeze the fixture descriptor before creating the protocol binding.
    validation.e2e.write_json(source / "config.runtime.json", {"all_tasks": [task]})
    validation.e2e.write_json(source / "frozen-tasks.json", [task])
    binding = validation.build_validation_binding(source)
    calls = []

    def execute(task_id, bank, directory, config):
        calls.append("agent")
        assert config["task"]["source_path"] == task["source_path"]
        assert config["round"] == 4 and config["execution_id"]
        return {"status": "pending_score", "score": None, "scoring_recovery_eligible": True}

    def rescore(task_id, directory, config):
        calls.append("score-recovery")
        assert provenance.validate_task_inputs(config, config["task"])["validated"] is True
        return {"status": "completed", "score": 0.0}

    monkeypatch.setattr(runtime, "execute_task", execute)
    monkeypatch.setattr(runtime, "rescore_task", rescore)
    config = {validation.BINDING_KEY: binding, "protocol_hash": "new-protocol", "seed": 42,
              "infrastructure_retries": 0}
    bank = source / "bank"
    bank.mkdir()
    (bank / "SKILL.md").write_text("unchanged")
    original = controller.run_one
    with validation.validation_adapter(controller, binding):
        first = controller.run_one(task, bank, source, config, "validation", 4, "candidate", "set")
        second = controller.run_one(task, bank, source, config, "validation", 4, "candidate", "set")
        third = controller.run_one(task, bank, source, config, "validation", 4, "candidate", "set")
    assert calls == ["agent", "score-recovery"]
    assert first["score"] is None and second["score"] == third["score"] == 0.0
    assert first["execution_hash"] == second["execution_hash"] == third["execution_hash"]
    assert controller.run_one is original


def test_fresh_runtime_child_checks_cohort_provenance_and_keeps_original_file(cohort):
    source, task, _binding = cohort
    _, _, _, _, _, actual_runtime, _, _, _ = validation.e2e.load_components()
    engine = source / "fixture-engine"
    engine.mkdir()
    runtime_path = engine / "task_runtime.py"
    (engine / "provenance.py").write_text(
        "def validate_task_inputs(config, task):\n"
        "    raise RuntimeError('relative-path official validator must not process cohort inputs')\n")
    runtime_path.write_text('''import argparse, json
from pathlib import Path
from scripts.wildclawbench.provenance import validate_task_inputs
p = argparse.ArgumentParser()
for name in ('task', 'skill-dir', 'output', 'config'):
    p.add_argument('--' + name)
p.add_argument('--rescore', action='store_true')
a = p.parse_args()
c = json.loads(Path(a.config).read_text())
# This is the OCR scorer's fresh-interpreter provenance call.
proof = validate_task_inputs(c, c['task'])
assert proof['validated']
Path(a.output, 'runtime-result.json').write_text(json.dumps({
    'status': 'completed', 'score': 0.0, 'runtime_file': __file__,
    'task_source': c['task']['source_path'], 'original_runtime_child': True}))
''')
    binding = validation.build_validation_binding(source, runtime_path)
    # Keep the real host execute_task bytecode, using the fixture child file.
    original = actual_runtime.execute_task
    namespace = dict(original.__globals__, __file__=str(runtime_path))
    fixture_execute = FunctionType(original.__code__, namespace, original.__name__,
                                   original.__defaults__, original.__closure__)
    runtime = SimpleNamespace(__file__=str(runtime_path), execute_task=fixture_execute)
    output = source / "fresh-child"
    config = {validation.BINDING_KEY: binding, "task": task, "user_id": task["user_id"]}
    with validation.execution_bridge(runtime, binding):
        result = runtime.execute_task(task["task_id"], source, output, config)
    assert result["status"] == "completed" and result["score"] == 0.0
    assert result["runtime_file"] == str(runtime_path)
    assert result["task_source"] == task["source_path"]
    proof = validation.e2e.read_json(output / "runtime-child-input-validation.json")
    assert proof["validated"] and proof["runtime"] == binding["runtime"]
    assert runtime.execute_task is fixture_execute


def test_execution_bridge_rejects_changed_frozen_runtime(cohort):
    source, _task, _binding = cohort
    runtime_path = source / "runtime.py"
    runtime_path.write_text("original")
    binding = validation.build_validation_binding(source, runtime_path)
    runtime_path.write_text("changed")
    with pytest.raises(ValueError, match="runtime source changed"):
        with validation.execution_bridge(SimpleNamespace(__file__=str(runtime_path)), binding):
            pass


def test_execution_bridge_separates_sandbox_contract_and_rejects_other_binding(cohort):
    source, task, binding = cohort
    calls = []

    def execute(task_id, bank, output, config):
        calls.append(config)
        return {"status": "completed", "own_sandbox_contract": True}

    runtime = SimpleNamespace(__file__=binding["runtime"]["path"], execute_task=execute)
    sandbox_config = {"purpose": "S6-canary-sandbox", "task": {"source_path": "new-sandbox-task.md"}}
    with validation.execution_bridge(runtime, binding):
        result = runtime.execute_task(task["task_id"], source, source, sandbox_config)
        assert result["own_sandbox_contract"] is True
        assert calls == [sandbox_config]
        with pytest.raises(ValueError, match="different cohort input binding"):
            runtime.execute_task(task["task_id"], source, source, {validation.BINDING_KEY: {"wrong": True}})


def test_fresh_two_task_day_to_native_candidate_validation_without_method_revision(cohort, monkeypatch):
    """Real scheduler, source resolver, provenance, gates and publish; model I/O is fake."""
    import shutil
    from scripts.injection import e2e_cycles as cycles, resilient_runner
    from scripts.wildclawbench import run as controller, evolution, environment, task_runtime, provenance
    from scripts.wildclawbench.protocol import tree_hash
    from sharedskill.skill_bundle import read_skill_bundle

    source, first, _ = cohort
    first["category"] = "test"
    second = copy.deepcopy(first)
    second.update(task_id="03_task_3", short_id="03.03", user_id="user-02")
    folder = source / "frozen/tasks/03.03"
    shutil.copytree(Path(first["source_path"]).parent, folder)
    second["source_path"] = str(folder / "task.md")
    Path(second["source_path"]).write_text(Path(first["source_path"]).read_text().replace(
        str(Path(first["source_path"]).parent), str(folder)))
    second["input_manifest"] = validation.e2e.tree_manifest(folder / "task-workspace")
    second["input_sha256"] = second["input_manifest"]["sha256"]
    tasks = [first, second]
    binding = validation.prepare_input_binding(source, tasks)
    settings = {"all_tasks": tasks, "rounds": 1, "task_count": 2, "users": 2,
        "model_id": "fixture-model", "model_revision": "fixture-revision",
        "protocol_hash": "day-protocol", "seed": 42, "infrastructure_retries": 0,
        "runtime_dir": str(source / "runtime"), "alignment_gateway_port": 19000,
        "local_api_base_url": "http://unused/v1", "score_tolerance": 0,
        "agent_prompt_sha256": "fixture-prompt", "runtime_contract_sha256": "fixture-runtime",
        validation.BINDING_KEY: binding,
        "alignment": {"version": "native-night-atomic-v1", "native_candidate_scope": "per-skill-v1",
            "source_evidence_revision": "native-history-frozen-alias-union-v3"},
        "execution_protocol": {"version": "resilient-v1", "conditions": ["clean"],
            "max_concurrency": 2, "max_transient_retries": 1,
            "circuit_breaker": {"same_signature_limit": 2}, "stage_budgets": {"night_seconds": 60}}}
    validation.e2e.write_json(source / "config.runtime.json", settings)
    bank = source / "banks/S0"
    (bank / "alpha").mkdir(parents=True)
    (bank / "alpha/SKILL.md").write_text("---\nname: alpha\ndescription: Procedure\n---\nOriginal procedure.\n")
    calls = []

    def execute(task_id, _bank, _directory, config):
        assert provenance.validate_task_inputs(config, config["task"])["validated"] is True
        calls.append((config["phase"], task_id))
        return {"status": "completed", "score": .5 if config["phase"] == "day" else .8,
            "session": {"turns": [{"prompt_text": "Task", "response_text": "Completed", "read_skills": []}]}}

    monkeypatch.setattr(task_runtime, "execute_task", execute)
    with validation.validation_adapter(controller, binding):
        rows = controller.run_stage(tasks, bank, source, settings, "day", 1)
    assert all(row["status"] == "completed" for row in rows)
    sessions = [row["session"] for row in rows]
    for session in sessions:
        session.update(original_session_id=session["session_id"],
                       native_uploaded_sha256="fixture-upload", native_record_segment_index=1)
    rows_by_task = {row["task_id"]: row for row in rows}
    importer = SimpleNamespace(directory=source, manifest_path=source / "fixture-import.json",
        manifest={"fixture": True}, sessions=lambda *_: copy.deepcopy(sessions),
        scored_row=lambda task, *_: (rows_by_task[task["task_id"]], {"reused_without_execution_or_rescoring": True}))
    validation.e2e.write_json(importer.manifest_path, importer.manifest)
    monkeypatch.setattr(resilient_runner, "baseline_import", lambda _: importer)
    # Full baseline-import identity/byte validation has dedicated tests; this
    # fixture exercises the new-run call chain using the scores just produced.
    monkeypatch.setattr(cycles.cohort, "validate_resume", lambda *_a, **_k: None)
    monkeypatch.setattr(cycles, "sources", lambda: [])
    monkeypatch.setattr(cycles, "start_gateway", lambda *_: (None, "http://unused", None))
    monkeypatch.setattr(cycles, "stop_gateway", lambda *_: None)
    monkeypatch.setattr(environment, "preflight_candidate_bank", lambda *_: {"passed": True})
    monkeypatch.setattr(controller, "outcome_preference", lambda *_: "candidate")
    collected = []

    def propose(directory, parent, day_sessions, config):
        attempt = directory / "evolution/fixture-model-output"
        workspace = attempt / "workspace/skills"
        shutil.copytree(parent, workspace)
        skill = workspace / "alpha/SKILL.md"
        skill.write_text(skill.read_text() + "Check the final artifact before delivery.\n")
        history = workspace / "alpha/history/v1_evidence.md"
        history.parent.mkdir()
        # Registered abbreviations must resolve to BOTH canonical task IDs,
        # identically when collecting the proposal and validating it later.
        history.write_text("Successful Day evidence: day-03.01/03.03\n")
        source_bank = {p.name: read_skill_bundle(p) for p in parent.iterdir()}
        normalized = [evolution._normalize_session(s, set(source_bank), False) for s in day_sessions]
        candidates = evolution._collect_atomic_candidate(attempt, parent, source_bank, normalized,
            config, {"source_bank_sha256": tree_hash(parent)})
        collected.extend(candidates)
        return candidates

    monkeypatch.setattr(evolution, "propose_candidates", propose)
    payload = source / "payload.json"
    validation.e2e.write_json(payload, {})
    config = {"settings": settings, "source_run": str(source), "sources": [], "rounds": 1,
        "run_id": "fixture-fresh", "source_protocol_hash": settings["protocol_hash"],
        "native_llm_request_timeout_seconds": 60, "bootstrap_manifest_sha256": "fixture-import",
        "frozen_payload": str(payload), "payload_sha256": validation.e2e.sha256(payload.read_bytes())}
    run = source / "cycle"
    result = cycles.run_stage(run, config, "clean", 1, bank)
    assert result["status"] == "completed" and result["changeset_accepted"] is True
    assert result["input_coverage_complete"] is True
    assert len(collected) == 1
    candidate = collected[0]
    assert candidate["source_task_ids"] == sorted(task["task_id"] for task in tasks)
    assert all(row["source_policy"] == "native-history-frozen-alias-union-v3"
               for row in candidate["source_evidence"])
    assert sorted(calls) == sorted((phase, task["task_id"]) for phase in ("day", "validation") for task in tasks)
    assert tree_hash(Path(result["published_bank"])) != tree_hash(bank)
    proofs = list((run / "rounds/01/clean/branches/clean/executions/validation").rglob("input-validation.json"))
    assert len(proofs) == 2 and all(validation.e2e.read_json(p)["validated"] for p in proofs)
    assert cycles.run_stage(run, config, "clean", 1, bank) == result
    assert len(calls) == 4
