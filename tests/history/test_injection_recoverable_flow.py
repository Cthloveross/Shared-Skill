"""Exercise the isolated evidence-driven controller without models or live files."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
from threading import Event

import pytest


ROOT = Path(__file__).resolve().parents[2]
if os.environ.get("SHAREDSKILL_HISTORICAL_TESTS") != "1":
    pytest.skip("Explicit historical controller check; use SHAREDSKILL_HISTORICAL_TESTS=1", allow_module_level=True)
from scripts.archive import Archive
archive = Archive(ROOT / "artifacts/30x5-sixcycles-20261016-01/archive.json")
SOURCE = archive.resolve(str(ROOT / (
    "experiment/reproduction/dymalskill-trajectory-e2e/runtime/runs/"
    "30x5-sixcycles-20261016-01/operations/flow-fix-20261017/cycle_flow.py")))
spec = importlib.util.spec_from_file_location("isolated_recoverable_cycle_flow", SOURCE)
flow = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = flow
spec.loader.exec_module(flow)


@pytest.fixture
def stage(tmp_path):
    root = tmp_path / "revision/rounds/02/poisoned"
    parent = tmp_path / "published/S1"
    for bank in (root / "banks/S0", parent):
        (bank / "alpha").mkdir(parents=True)
        (bank / "alpha/SKILL.md").write_text("---\nname: alpha\ndescription: Existing\n---\nCurrent skill\n")
    source = tmp_path / "frozen-controller.py"
    source.write_text("# frozen dependency\n")
    sources = [{"path": str(source), "sha256": flow.file_hash(source)}]
    flow.write(root / "config.runtime.json", {
        "cycle_condition": "poisoned", "cycle_round": 2, "protocol_hash": "protocol-v3",
        "cohort_validation_binding": {"frozen": "inputs"},
        "all_tasks": [{"short_id": "02.03", "task_id": "actual-task"}],
    })
    flow.write(root / "stage-binding.json", {"catalog_sha256": "actual-catalog"})
    return root, parent, sources


def test_completed_candidate_cache_recovers_validation_without_another_evolution(stage):
    root, parent, sources = stage
    cache = root / "branches/poisoned/nights/02/candidates.json"
    flow.write(cache, [{"candidate_id": "actual-export"}])
    before = cache.read_bytes()
    action = flow.inspect_stage(root, parent, sources)
    assert action.kind == "resume_validation"
    assert action.evidence["candidate_cache_sha256"] == flow.file_hash(cache)
    assert cache.read_bytes() == before


def test_completed_proposal_without_controller_cache_is_sealed_not_regenerated(stage):
    root, parent, sources = stage
    flow.write(root / "branches/poisoned/nights/02/proposals/evolution/proposal-summary.json",
               {"status": "completed", "candidates": []})
    action = flow.inspect_stage(root, parent, sources)
    assert action.kind == "seal_completed_proposal"
    assert action.evidence["new_native_calls"] == 0


def test_failed_night_requires_original_session_and_blocks_ancestor_retcon(stage):
    root, parent, sources = stage
    flow.write(root / "branches/poisoned/nights/02/proposals/evolution/proposal-summary.json", {
        "status": "failed", "attempt_dir": "/preserved/attempt", "input_fingerprint": "actual-inputs",
    })
    action = flow.inspect_stage(root, parent, sources)
    assert action.kind == "resume_native_session"
    assert action.evidence["fresh_run_once_allowed"] is False
    fork = flow.inspect_stage(root, parent, sources, descendants=[root.parent.parent / "03/poisoned"])
    assert fork.kind == "fork_required" and not fork.terminal


def test_context_overflow_never_restarts_agent_or_invents_score(stage):
    root, parent, sources = stage
    state_path = root / "day/tasks/02.03/state.json"
    flow.write(state_path, {"status": "finished_with_gaps", "task_score": None})
    result_path = root / "day/tasks/02.03/task/runtime-result.json"
    flow.write(result_path, {"status": "infrastructure_error", "score": None,
        "gateway_main_errors": [{"context_budget": {"remaining_tokens": -23749}}],
        "task_deadline_evidence": {"deadline_not_before_unix": 1000},
    })
    before = state_path.read_bytes(), result_path.read_bytes()
    action = flow.inspect_stage(root, parent, sources)
    assert action.kind == "compact_and_resume_day"
    assert action.evidence["new_Agent_execution_allowed"] is False
    assert action.evidence["requires_native_compaction_evidence"] is True
    assert action.evidence["original_deadline"] == 1000
    assert (state_path.read_bytes(), result_path.read_bytes()) == before


def test_existing_execution_recovers_score_only_and_zero_is_valid(stage):
    root, parent, sources = stage
    output = root / "day/tasks/02.03/task"
    flow.write(root / "day/tasks/02.03/state.json", {"status": "finished_with_gaps", "task_score": None})
    for name in ("execution.json", "agent-output-manifest.json", "grading-recovery-manifest.json",
                 "execution-binding.json"):
        flow.write(output / name, {"sealed": name})
    action = flow.inspect_stage(root, parent, sources)
    assert action.kind == "rescore_only" and action.evidence["new_Agent_calls"] == 0
    flow.write(root / "day/tasks/02.03/state.json", {"status": "completed", "task_score": 0.0})
    assert flow.inspect_stage(root, parent, sources).kind == "finalize_native_uploads"


def test_live_worker_prevents_second_day_worker_and_stale_pid_does_not(stage):
    root, parent, sources = stage
    flow.write(root / "day-workers/01.json", {"pid": 123, "start_ticks": "actual-start"})
    assert flow.inspect_stage(root, parent, sources, alive=lambda _row: True).kind == "wait_existing_worker"
    assert flow.inspect_stage(root, parent, sources, alive=lambda _row: False).kind == "execute_unstarted_day"


def test_changed_dependency_or_bank_mode_is_rejected_before_dispatch(stage):
    root, parent, sources = stage
    (root / "banks/S0/alpha/SKILL.md").chmod(0o400)
    with pytest.raises(ValueError, match="actually deployed parent"):
        flow.inspect_stage(root, parent, sources)
    (root / "banks/S0/alpha/SKILL.md").chmod(0o644)
    Path(sources[0]["path"]).write_text("changed source")
    with pytest.raises(ValueError, match="dependency closure changed"):
        flow.inspect_stage(root, parent, sources)


def test_unknown_error_is_durable_block_and_does_not_repeat_on_next_poll(tmp_path):
    action = flow.Action("resume_native_session", "phase", {"same": "checkpoint"})
    calls = []

    def fail(_action):
        calls.append(True)
        raise ValueError("unknown native failure")

    journal = flow.FlowJournal(tmp_path)
    assert journal.dispatch(action, {action.kind: fail})["status"] == "blocked"
    assert flow.FlowJournal(tmp_path).dispatch(action, {action.kind: fail})["status"] == "blocked"
    assert calls == [True]


def test_interrupted_action_requires_reconciliation_not_blind_replay(tmp_path):
    action = flow.Action("start_native_night", "phase", {"input": "same"})
    flow.write(tmp_path / "phase.json", {"status": "action_running", "attempts": 1,
               "evidence_sha256": flow.digest(flow.asdict(action))})
    journal = flow.FlowJournal(tmp_path)
    result = journal.dispatch(action, {action.kind: lambda _action: pytest.fail("Interrupted action must reconcile")})
    assert result["status"] == "reconcile_interrupted_action" and result["replay_allowed"] is False


def test_successful_handler_return_is_not_completion_without_artifact(tmp_path):
    action = flow.Action("resume_validation", "phase", {})
    calls = []
    handler = lambda _action: calls.append(True)
    journal = flow.FlowJournal(tmp_path)
    assert journal.dispatch(action, {action.kind: handler})["status"] == "reconcile_required"
    assert journal.dispatch(action, {action.kind: handler})["status"] == "reconcile_required"
    assert calls == [True]


def test_only_explicit_transient_errors_get_bounded_backoff_and_budget(tmp_path):
    clock = [0.0]
    action = flow.Action("rescore_only", "phase", {})
    calls = []

    def transient(_action):
        calls.append(True)
        raise flow.RetryableFlowError("Verified judge API HTTP 503")

    journal = flow.FlowJournal(tmp_path, clock=lambda: clock[0])
    first = journal.dispatch(action, {action.kind: transient}, attempts=2)
    assert first["status"] == "retry_wait" and first["retry_at"] == 15
    assert journal.dispatch(action, {action.kind: transient}, attempts=2) == first
    clock[0] = 15
    assert journal.dispatch(action, {action.kind: transient}, attempts=2)["retry_at"] == 45
    clock[0] = 45
    assert journal.dispatch(action, {action.kind: transient}, attempts=2)["status"] == "blocked"
    assert calls == [True, True]


def test_service_keeps_heartbeat_alive_when_safe_recovery_adapter_is_missing(tmp_path):
    stop, beats = Event(), []

    def heartbeat(state):
        beats.append(state["status"])
        if len(beats) == 3:
            stop.set()

    action = flow.Action("compact_and_resume_day", "phase", {})
    final = flow.run_service(lambda: action, {}, flow.FlowJournal(tmp_path), stop, heartbeat, poll_seconds=0)
    assert final["status"] == "blocked" and beats == ["blocked"] * 3


def test_published_bank_is_terminal_but_missing_measurements_keep_gap_status(stage, tmp_path):
    root, parent, sources = stage
    deployed = root / "branches/poisoned/banks/S2"
    (deployed / "alpha").mkdir(parents=True)
    (deployed / "alpha/SKILL.md").write_bytes((parent / "alpha/SKILL.md").read_bytes())
    flow.write(root / "branches/poisoned/nights/02/publication.json", {
        "round": 2, "bank_sha256": flow.bank_identity(deployed)["bank_sha256"],
        "parent_sha256": flow.bank_identity(parent)["bank_sha256"],
    })
    action = flow.inspect_stage(root, parent, sources)
    assert action.terminal
    assert flow.FlowJournal(tmp_path / "journal").dispatch(action, {})["status"] == "finished_with_gaps"
