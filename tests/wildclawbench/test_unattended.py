"""Exercise unattended recovery with native task identities and no model calls."""

from __future__ import annotations

import collections
import contextlib
import csv
import json
import os
import random
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
from scripts.wildclawbench import reports
from scripts.wildclawbench import run
from . import test_protocol as protocol_tests
from scripts.wildclawbench.operations import unattended
from scripts.wildclawbench.protocol import canonical_hash, tree_hash, write_json
from sharedskill.skill_bundle import read_skill_bundle_with_meta


AUTHORIZED_AT = "2026-10-13T00:00:00+00:00"


class UnattendedControllerTests(unittest.TestCase):
    def setUp(self):
        protocol_tests.ControllerTests.setUp(self)
        self.config["infrastructure_retries"] = 1
        write_json(self.directory / "pilot-gate.json", {"passed": True})
        frozen = {"tasks": self.tasks, "config": {"seed": self.config["seed"]}}
        write_json(self.directory / "frozen-protocol.json", frozen)
        self.config["protocol_hash"] = canonical_hash(frozen)
        self.proposal_rounds = []

    def tearDown(self):
        protocol_tests.ControllerTests.tearDown(self)

    def execute(self, task_id, bank, output, config):
        version = int((Path(bank) / "skill/SKILL.md").read_text())
        self.calls.append((config["phase"], config["round"], task_id, version, Path(output).parent.name))
        return {"status": "completed", "score": 0.2 + 0.1 * version,
                "timed_out": False, "tool_errors": 0, "session": {"turns": []}}

    def propose(self, proposal_dir, bank, sessions, config):
        number = int(Path(proposal_dir).parent.name)
        self.proposal_rounds.append(number)
        self.assertTrue(all(session["phase"] == "day" and session["round"] == number for session in sessions))
        self.assertTrue(all(session["outcome"]["score"] is not None for session in sessions))
        bundle = Path(proposal_dir) / "skill"
        shutil.copytree(Path(bank) / "skill", bundle)
        (bundle / "SKILL.md").write_text(str(number))
        (bundle / "scripts/check.py").write_text(f"print({number})")
        return [{"name": "skill", "bundle_dir": str(bundle), "bundle_sha256": tree_hash(bundle),
                 "native_tree_sha256": read_skill_bundle_with_meta(bundle)[2],
                 "source_task_ids": [self.tasks[-1]["task_id"]]}]

    @contextlib.contextmanager
    def native_mocks(self, execute=None, propose=None, compare=None, rescore=None):
        adapter = types.SimpleNamespace(execute_task=Mock(side_effect=execute or self.execute),
                                        rescore_task=Mock(side_effect=rescore or self.execute))
        evolution = types.SimpleNamespace(propose_candidates=Mock(side_effect=propose or self.propose))
        with (patch.dict(sys.modules, {"scripts.wildclawbench.task_runtime": adapter, "scripts.wildclawbench.evolution": evolution}),
              patch.object(run, "check_services"), patch.object(run, "generate", return_value={}),
              patch.object(run, "event"),
              patch.object(run, "outcome_preference", side_effect=compare or (lambda *args: "candidate"))):
            yield adapter, evolution

    def assert_rejection_hashes(self, number, parent):
        directory = self.directory / "nights" / f"{number:02d}"
        candidate = json.loads((directory / "candidates.json").read_text())[0]
        validation = directory / "validation/001-skill"
        decision = json.loads((validation / "decision.json").read_text())
        self.assertFalse(decision["accepted"])
        self.assertFalse(decision["pending"])
        self.assertTrue(decision["skipped"])
        self.assertEqual(decision["name"], candidate["name"])
        self.assertEqual(decision["candidate_id"], validation.name)
        self.assertEqual(decision["source_task_ids"], candidate["source_task_ids"])
        self.assertEqual(decision["parent_sha256"], tree_hash(parent))
        self.assertEqual(decision["candidate_bank_sha256"], tree_hash(validation / "candidate-bank"))
        self.assertEqual(decision["bundle_tree_sha256"], tree_hash(Path(candidate["bundle_dir"])))
        self.assertEqual(decision["bundle_tree_sha256"], candidate["bundle_sha256"])
        return decision

    def test_fault_chain_finishes_all_rounds_and_endpoint_then_reuses_every_skip(self):
        def execute(task_id, bank, output, config):
            result = self.execute(task_id, bank, output, config)
            phase, number = config["phase"], config["round"]
            arm = Path(output).parent.name
            pending = ((phase, number, task_id) == ("day", 1, self.tasks[0]["task_id"])
                       or (phase == "validation" and number == 1 and arm == "candidate")
                       or (phase == "endpoint" and task_id == self.tasks[2]["task_id"] and arm == "S0"))
            if pending:
                result.update(status="pending_score", score=None, error="Mock unavailable judge")
            if (phase, number, task_id) == ("day", 2, self.tasks[1]["task_id"]):
                result.update(status="infrastructure_error", score=None, error="Mock unavailable infrastructure")
            return result

        def propose(directory, bank, sessions, config):
            number = int(Path(directory).parent.name)
            self.assertEqual(len(sessions), 59 if number in {1, 2} else 60)
            if number == 3:
                self.proposal_rounds.append(number)
                raise httpx.ConnectError("Mock proposal service unavailable")
            return self.propose(directory, bank, sessions, config)

        def compare(old, new, directory, config):
            if int(Path(directory).parents[2].name) == 2:
                raise httpx.ConnectError("Mock comparison service unavailable")
            return "candidate"

        with self.native_mocks(execute=execute, propose=propose, compare=compare) as (adapter, evolution):
            with unattended.Policy(self.directory, AUTHORIZED_AT).install(evolution):
                run.run_experiment(self.tasks, self.bank, self.directory, self.config)
            initial_calls = list(self.calls)
            initial_proposals = list(self.proposal_rounds)
            with unattended.Policy(self.directory, AUTHORIZED_AT).install(evolution):
                run.run_experiment(self.tasks, self.bank, self.directory, self.config)
            self.assertEqual(self.calls, initial_calls)
            self.assertEqual(self.proposal_rounds, initial_proposals)
            adapter.rescore_task.assert_not_called()

        state = json.loads((self.directory / "state.json").read_text())
        self.assertEqual(state["status"], "finished_with_gaps")
        self.assertTrue(state["finished"])
        self.assertEqual(state["fixed_execution_coverage"]["expected"], 480)
        self.assertEqual(state["fixed_execution_coverage"]["graded"], 477)
        self.assertTrue(unattended.terminal(self.directory))
        self.assertEqual(self.proposal_rounds, list(range(1, 7)))
        self.assertEqual(len(self.calls), 491)  # 480 fixed, ten A/B executions, one infrastructure retry.
        day_versions = {1: 0, 2: 0, 3: 0, 4: 0, 5: 4, 6: 5}
        days = [call for call in self.calls if call[0] == "day"]
        self.assertTrue(all(version == day_versions[number] for _, number, _, version, _ in days))
        self.assertEqual(collections.Counter(call[1] for call in days), {1: 60, 2: 61, 3: 60, 4: 60, 5: 60, 6: 60})
        endpoint = [call for call in self.calls if call[0] == "endpoint"]
        self.assertEqual(collections.Counter(call[4] for call in endpoint), {"S0": 60, "S6": 60})
        for task in self.tasks:
            expected_arms = ["S0", "S6"]
            random.Random(self.config["seed"] + int(task["short_id"].replace(".", ""))).shuffle(expected_arms)
            self.assertEqual([call[4] for call in endpoint if call[2] == task["task_id"]], expected_arms)
        for number in range(1, 7):
            publication = json.loads((self.directory / "nights" / f"{number:02d}" / "publication.json").read_text())
            self.assertEqual(publication["parent_sha256"], tree_hash(self.directory / "banks" / f"S{number - 1}"))
            self.assertEqual(publication["bank_sha256"], tree_hash(self.directory / "banks" / f"S{number}"))
        self.assertIn("validation_unavailable", self.assert_rejection_hashes(1, self.bank)["reason"])
        self.assertIn("comparison_unavailable", self.assert_rejection_hashes(2, self.directory / "banks/S1")["reason"])
        self.assertEqual(tree_hash(self.directory / "banks/S3"), tree_hash(self.directory / "banks/S2"))
        self.assertEqual((self.directory / "banks/S6/skill/SKILL.md").read_text(), "6")

    def test_durable_skip_preserves_native_identity_and_rejects_changed_inputs(self):
        pending = lambda *args: {"status": "pending_score", "score": None, "session": {"turns": []}}
        task = self.tasks[0]
        with self.native_mocks(execute=pending) as (adapter, evolution):
            with unattended.Policy(self.directory, AUTHORIZED_AT).install(evolution):
                first = run.run_one(task, self.bank, self.directory, self.config, "day", 1)
            expected_hash = canonical_hash({"phase": "day", "round": 1, "arm": "", "candidate_id": "",
                                            "task_id": task["task_id"], "bank": tree_hash(self.bank),
                                            "config": self.config["protocol_hash"], "input": task["source_sha256"],
                                            "seed": self.config["seed"]})
            self.assertEqual(first["execution_hash"], expected_hash)
            result_path = self.directory / "executions/day/01/main/shared" / task["short_id"] / "result.json"
            original_bytes = result_path.read_bytes()
            skip_path = self.directory / "operations/unattended/skips" / (expected_hash + ".json")
            skip_bytes = skip_path.read_bytes()
            with unattended.Policy(self.directory, AUTHORIZED_AT).install(evolution):
                self.assertEqual(run.run_one(task, self.bank, self.directory, self.config, "day", 1), first)
                for changed_task, changed_config in (
                    ({**task, "source_sha256": "changed"}, self.config),
                    (task, {**self.config, "protocol_hash": "changed"}),
                    (task, {**self.config, "seed": self.config["seed"] + 1}),
                ):
                    with self.assertRaisesRegex(ValueError, "cross-bank/config"):
                        run.run_one(changed_task, self.bank, self.directory, changed_config, "day", 1)
                (self.bank / "skill/SKILL.md").write_text("1")
                with self.assertRaisesRegex(ValueError, "cross-bank/config"):
                    run.run_one(task, self.bank, self.directory, self.config, "day", 1)
            self.assertEqual(adapter.execute_task.call_count, 1)
            adapter.rescore_task.assert_not_called()
            self.assertEqual(result_path.read_bytes(), original_bytes)
            self.assertEqual(skip_path.read_bytes(), skip_bytes)
            self.assertTrue(first["experiment_record"])
            self.assertIsNone(first["score"])

    def test_infrastructure_retries_can_recover_before_declaring_skip(self):
        attempts = 0

        def execute(*args):
            nonlocal attempts
            attempts += 1
            if attempts < 2:
                return {"status": "infrastructure_error", "score": None, "error": "Mock transient failure"}
            return self.execute(*args)

        with self.native_mocks(execute=execute) as (adapter, evolution):
            with unattended.Policy(self.directory, AUTHORIZED_AT).install(evolution):
                result = run.run_one(self.tasks[0], self.bank, self.directory, self.config, "day", 1)
            self.assertEqual(adapter.execute_task.call_count, 2)
        self.assertTrue(unattended.graded(result))
        self.assertEqual(list((self.directory / "operations/unattended/skips").glob("*.json")), [])

    def test_existing_exhausted_failure_is_adopted_without_reexecuting(self):
        failed = lambda *args: {"status": "infrastructure_error", "score": None, "error": "Still unavailable"}
        with self.native_mocks(execute=failed) as (adapter, evolution):
            first = run.run_one(self.tasks[0], self.bank, self.directory, self.config, "day", 1)
            policy = unattended.Policy(self.directory, AUTHORIZED_AT)
            path = policy.execution(self.tasks[0], self.bank, self.config, "day", 1)[1]
            before = path.read_bytes()
            with policy.install(evolution):
                result = run.run_one(self.tasks[0], self.bank, self.directory, self.config, "day", 1)
            self.assertEqual(adapter.execute_task.call_count, 2)
            self.assertEqual(result, first)
            self.assertEqual(path.read_bytes(), before)
            self.assertTrue((policy.operations / "skips" / (first["execution_hash"] + ".json")).exists())

    def test_existing_pending_score_resumes_only_the_native_grader(self):
        # Explicit runtime permission covers a transient-only grader attempt.
        pending = lambda *args: {"status": "pending_score", "score": None, "session": {"turns": []},
                                 "error": "Judge HTTP 503", "scoring_recovery_eligible": True}
        scored = lambda *args: {"status": "completed", "score": 0.4, "session": {"turns": []}}
        with self.native_mocks(execute=pending, rescore=scored) as (adapter, evolution):
            first = run.run_one(self.tasks[0], self.bank, self.directory, self.config, "day", 1)
            with unattended.Policy(self.directory, AUTHORIZED_AT).install(evolution):
                result = run.run_one(self.tasks[0], self.bank, self.directory, self.config, "day", 1)
            self.assertEqual(first["execution_hash"], result["execution_hash"])
            self.assertEqual(adapter.execute_task.call_count, 1)
            self.assertEqual(adapter.rescore_task.call_count, 1)
        self.assertTrue(unattended.graded(result))

    def test_unexpected_task_failure_keeps_later_tasks_for_the_same_user_running(self):
        selected = [self.tasks[0], self.tasks[8]]
        self.assertEqual(selected[0]["user_id"], selected[1]["user_id"])

        def execute(task_id, *args):
            result = self.execute(task_id, *args)
            if task_id == selected[0]["task_id"]:
                result["score"] = "malformed numerical grade"
            return result

        with self.native_mocks(execute=execute) as (adapter, evolution):
            with unattended.Policy(self.directory, AUTHORIZED_AT).install(evolution):
                rows = run.run_stage(selected, self.bank, self.directory, self.config, "day", 1)
            self.assertFalse(unattended.graded(rows[0]))
            self.assertIsNone(rows[0]["score"])
            self.assertTrue(unattended.graded(rows[1]))
            self.assertEqual(adapter.execute_task.call_count, 2)
            with unattended.Policy(self.directory, AUTHORIZED_AT).install(evolution):
                run.run_stage(selected, self.bank, self.directory, self.config, "day", 1)
            self.assertEqual(adapter.execute_task.call_count, 2)

    def test_bank_mutation_during_task_failure_is_fatal_and_never_skipped(self):
        def execute(*args):
            result = self.execute(*args)
            (self.bank / "skill/SKILL.md").write_text("tampered")
            return result

        with self.native_mocks(execute=execute) as (_, evolution):
            with unattended.Policy(self.directory, AUTHORIZED_AT).install(evolution):
                with self.assertRaisesRegex(RuntimeError, "Frozen bank changed during failed task"):
                    run.run_one(self.tasks[0], self.bank, self.directory, self.config, "day", 1)
        self.assertEqual(list((self.directory / "operations/unattended/skips").glob("*.json")), [])

    def test_native_report_preserves_missing_scores_and_fixed_denominators(self):
        selected = self.tasks[:2]
        (self.directory / "pilot-gate.json").unlink()

        def execute(task_id, *args):
            result = self.execute(task_id, *args)
            if task_id == selected[1]["task_id"]:
                result.update(status="pending_score", score=None, error="Mock unavailable grader")
            return result

        adapter = types.SimpleNamespace(execute_task=Mock(side_effect=execute), rescore_task=Mock())
        evolution = types.SimpleNamespace(propose_candidates=Mock(return_value=[]))
        with (patch.dict(sys.modules, {"scripts.wildclawbench.task_runtime": adapter, "scripts.wildclawbench.evolution": evolution}),
              patch.object(reports, "update_experiment_status") as update_status,
              patch.object(run, "event")):
            policy = unattended.Policy(self.directory, AUTHORIZED_AT)
            with policy.install(evolution):
                rows = run.run_stage(selected, self.bank, self.directory, self.config, "day", 1)
                summary = policy.report(self.directory, self.tasks)
            self.assertTrue(update_status.called)

        day = next(stage for stage in summary["stages"] if stage["phase"] == "day" and stage["round"] == 1)
        self.assertEqual(day["expected"], 60)
        self.assertEqual(day["attempted"], 2)
        self.assertEqual(day["graded"], 1)
        self.assertEqual(day["score"], 0.2)
        self.assertEqual(day["grading_coverage"], 1 / 60)
        fixed = summary["fixed_execution_coverage"]
        self.assertEqual(fixed["expected"], 480)
        self.assertEqual(fixed["graded"], 1)
        self.assertEqual(fixed["stages"][0]["grading_coverage"], 1 / 60)
        with (self.directory / "report/tasks.csv").open(newline="") as stream:
            records = {record["task_id"]: record for record in csv.DictReader(stream)}
        self.assertEqual(records[selected[0]["task_id"]]["score"], "0.2")
        self.assertEqual(records[selected[1]["task_id"]]["status"], "pending_score")
        self.assertEqual(records[selected[1]["task_id"]]["score"], "")
        self.assertIsNone(rows[1]["score"])
        self.assertEqual(rows[1]["status"], "pending_score")
        skips = summary["unattended_skips"]
        self.assertEqual(len(skips), 1)
        self.assertEqual(skips[0]["kind"], "task")
        self.assertEqual(skips[0]["task_id"], selected[1]["task_id"])
        self.assertEqual(skips[0]["status"], "pending_score")
        self.assertFalse(skips[0]["score_available"])
        report = (self.directory / "report/REPORT.md").read_text()
        self.assertIn("1/60", report)
        self.assertIn("Mock unavailable grader", report)

    def test_comparison_failure_records_exact_rejection_and_does_not_repeat(self):
        compare = Mock(side_effect=httpx.ConnectError("Mock comparison service unavailable"))
        with self.native_mocks(compare=compare) as (adapter, evolution):
            with unattended.Policy(self.directory, AUTHORIZED_AT).install(evolution):
                published = run.night(1, self.bank, [], self.tasks, self.directory, self.config)
            self.assertEqual(adapter.execute_task.call_count, 2)
            self.assertEqual(compare.call_count, 1)
            before = (self.directory / "nights/01/validation/001-skill/decision.json").read_bytes()
            with unattended.Policy(self.directory, AUTHORIZED_AT).install(evolution):
                self.assertEqual(run.night(1, self.bank, [], self.tasks, self.directory, self.config), published)
            self.assertEqual(adapter.execute_task.call_count, 2)
            self.assertEqual(compare.call_count, 1)
        self.assertEqual(tree_hash(published), tree_hash(self.bank))
        self.assert_rejection_hashes(1, self.bank)
        self.assertEqual((self.directory / "nights/01/validation/001-skill/decision.json").read_bytes(), before)
        failure = json.loads((self.directory / "nights/01/validation/001-skill/comparison-failure.json").read_text())
        self.assertEqual(failure["baseline_bank"], tree_hash(self.bank))
        self.assertEqual(failure["candidate_bank"], tree_hash(self.directory / "nights/01/validation/001-skill/candidate-bank"))

    def test_proposal_failure_publishes_exact_parent_snapshot(self):
        failure = Mock(side_effect=httpx.ConnectError("Mock proposal service unavailable"))
        with self.native_mocks(propose=failure) as (adapter, evolution):
            with unattended.Policy(self.directory, AUTHORIZED_AT).install(evolution):
                published = run.night(1, self.bank, [], self.tasks, self.directory, self.config)
            self.assertEqual(tree_hash(published), tree_hash(self.bank))
            with unattended.Policy(self.directory, AUTHORIZED_AT).install(evolution):
                run.night(1, self.bank, [], self.tasks, self.directory, self.config)
            self.assertEqual(failure.call_count, 1)
            adapter.execute_task.assert_not_called()
        publication = json.loads((self.directory / "nights/01/publication.json").read_text())
        self.assertTrue(publication["skipped"])
        self.assertEqual(publication["candidate_count"], 0)
        self.assertEqual(publication["parent_sha256"], tree_hash(self.bank))
        self.assertEqual(publication["bank_sha256"], tree_hash(self.bank))
        self.assertFalse((self.directory / "nights/01/candidates.json").exists())

    def test_candidate_bundle_integrity_failure_remains_fatal(self):
        def propose(*args):
            candidates = self.propose(*args)
            (Path(candidates[0]["bundle_dir"]) / "SKILL.md").write_text("tampered")
            return candidates

        with self.native_mocks(propose=propose) as (adapter, evolution):
            with unattended.Policy(self.directory, AUTHORIZED_AT).install(evolution):
                with self.assertRaisesRegex(ValueError, "bundle changed after proposal"):
                    run.night(1, self.bank, [], self.tasks, self.directory, self.config)
            adapter.execute_task.assert_not_called()
        self.assertFalse((self.directory / "nights/01/publication.json").exists())
        self.assertFalse((self.directory / "nights/01/validation/001-skill/decision.json").exists())


class ProcessAndLockTests(unittest.TestCase):
    def test_pid_reuse_and_uid_change_do_not_keep_original_process_alive(self):
        original = {"pid": 1234, "uid": 1000, "start_ticks": "99"}
        for identity, expected in ((original, True), ({**original, "start_ticks": "100"}, False),
                                   ({**original, "uid": 1001}, False), (None, False)):
            with self.subTest(identity=identity), patch.object(unattended, "process_identity", return_value=identity):
                self.assertEqual(unattended.alive(original), expected)
        with self.assertRaises(ValueError):
            unattended.alive({"pid": 1234})
        with patch.object(unattended, "process_identity", side_effect=PermissionError("Mock inaccessible proc")):
            with self.assertRaises(PermissionError):
                unattended.alive(original)

    def test_proc_identity_parses_parentheses_and_treats_zombies_as_exited(self):
        fields = ["S"] + ["0"] * 18 + ["987654321"] + ["0"] * 4
        stat = "1234 (python (worker) name) " + " ".join(fields)
        with patch.object(Path, "read_text", return_value=stat), patch.object(Path, "stat", return_value=types.SimpleNamespace(st_uid=1000)):
            self.assertEqual(unattended.process_identity(1234), {"pid": 1234, "uid": 1000, "start_ticks": "987654321"})
        fields[0] = "Z"
        with patch.object(Path, "read_text", return_value="1234 (worker) " + " ".join(fields)):
            self.assertIsNone(unattended.process_identity(1234))
        with patch.object(Path, "read_text", side_effect=PermissionError("Mock inaccessible proc")):
            with self.assertRaises(PermissionError):
                unattended.process_identity(1234)
        self.assertTrue(unattended.alive(unattended.process_identity(os.getpid())))

    def test_native_controller_lock_blocks_a_second_controller_then_releases(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with run.exclusive_run(directory):
                with self.assertRaisesRegex(RuntimeError, "controller is already running"):
                    with run.exclusive_run(directory):
                        self.fail("Second native controller acquired the lock")
            with run.exclusive_run(directory):
                pass

    def test_supervisor_lock_blocks_a_second_supervisor_then_releases(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            operations = directory / "operations/unattended"
            operations.mkdir(parents=True)
            args = types.SimpleNamespace(run_dir=directory, authorized_at=AUTHORIZED_AT,
                                         config=directory / "config.json", poll_seconds=1, stale_seconds=7200)
            identity = unattended.process_identity(os.getpid())
            write_json(directory / "controller-process.json", identity)
            with (operations / "supervisor.lock").open("a") as lock:
                run.fcntl.flock(lock, run.fcntl.LOCK_EX | run.fcntl.LOCK_NB)
                with patch.object(unattended.subprocess, "Popen") as launch:
                    with self.assertRaises(BlockingIOError):
                        unattended.supervisor(args)
                    launch.assert_not_called()
            with (patch.object(unattended, "alive", return_value=False),
                  patch.object(unattended, "terminal", return_value=True), patch.object(run, "event"),
                  patch.object(run, "load_config", return_value={}),
                  patch.object(unattended, "model_progress", return_value=None),
                  patch.object(unattended.subprocess, "Popen") as launch):
                unattended.supervisor(args)
                launch.assert_not_called()

    def test_supervisor_launches_worker_only_after_original_identity_exits(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            original = {"pid": 1234, "uid": 1000, "start_ticks": "99"}
            write_json(directory / "controller-process.json", original)
            args = types.SimpleNamespace(run_dir=directory, authorized_at=AUTHORIZED_AT,
                                         config=directory / "config.json", poll_seconds=1, stale_seconds=7200)
            transitions = []
            process = types.SimpleNamespace(pid=1235, returncode=0, poll=lambda: 0, wait=lambda: 0)
            supervisor_identity = unattended.process_identity(os.getpid())

            def launch(*args, **kwargs):
                transitions.append("launch")
                return process

            with (patch.object(unattended, "alive", side_effect=[True, True, False]) as alive,
                  patch.object(unattended, "terminal", side_effect=[False, True]),
                  patch.object(unattended.time, "sleep", side_effect=lambda seconds: transitions.append("wait")),
                  patch.object(unattended.subprocess, "Popen", side_effect=launch) as popen,
                  patch.object(unattended, "process_identity", side_effect=lambda pid: supervisor_identity if pid == os.getpid() else None),
                  patch.object(run, "load_config", return_value={}),
                  patch.object(unattended, "model_progress", return_value=None),
                  patch.object(run, "event")):
                unattended.supervisor(args)
            self.assertEqual(transitions, ["wait", "wait", "launch"])
            self.assertTrue(all(call.args == (original,) for call in alive.call_args_list))
            command = popen.call_args.args[0]
            self.assertIn("worker", command)
            self.assertIn("--source-sha256", command)
            self.assertTrue(popen.call_args.kwargs["start_new_session"])


if __name__ == "__main__":
    unittest.main()
