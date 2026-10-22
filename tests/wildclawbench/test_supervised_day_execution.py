"""Day-only Agent supervision without alignment routing or evolution semantics."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.wildclawbench import alignment_snapshot
from scripts.wildclawbench import task_runtime as runtime
from .test_runtime import MockExecution


class SupervisedExecution(MockExecution):
    def __init__(self, root: Path, *, deadline_expired: bool = False, phase: str = "day",
                 quiescent: bool = True):
        super().__init__(root)
        self.deadline_expired = deadline_expired
        self.quiescent = quiescent
        self.frozen_when_graded = False
        self.config = {
            "supervised_day_execution": {
                "version": runtime.SUPERVISED_DAY_EXECUTION_VERSION,
            },
            "protocol_hash": "frozen-protocol",
            "execution_id": "execution-01",
            "phase": phase,
            "round": 1,
            "seed": 42,
            "user_id": "user-01",
            "task": {"input_sha256": "input", "source_sha256": "source"},
        }
        if deadline_expired:
            self.agent_returncode = 124

    def copy_out(self, name, source, path, config, **kwargs):
        if source == "/tmp/sharedskill-agent-quiescence.json":
            self.events.append(("copy_out", source, str(path)))
            path.write_text(json.dumps({
                "quiescent": self.quiescent,
                "surviving_writers": [] if self.quiescent else [999],
                "deadline_expired": self.deadline_expired,
            }))
            return True
        return super().copy_out(name, source, path, config, **kwargs)

    def grade(self, name, spec, output, config, **kwargs):
        if self.config["phase"] == "day":
            manifest = json.loads((output / alignment_snapshot.AGENT_MANIFEST).read_text())
            self.frozen_when_graded = True
            self.assert_manifest(manifest)
        return super().grade(name, spec, output, config, **kwargs)

    def assert_manifest(self, manifest):
        identity = manifest["identity"]
        assert identity["supervision_version"] == runtime.SUPERVISED_DAY_EXECUTION_VERSION
        assert identity["phase"] == "day"
        alignment_snapshot.verify_agent_output(self.output, identity)

    def execute(self, *, rescore: bool = False):
        with self.context():
            return runtime._execute_child(
                self.spec["task_id"], self.bank, self.output, self.config, rescore
            )


class SupervisedDayExecutionTests(unittest.TestCase):
    def _assert_supervised(self, execution: SupervisedExecution, result: dict) -> None:
        self.assertEqual(result["status"], "completed", result)
        self.assertTrue(execution.frozen_when_graded)
        self.assertTrue((execution.output / "agent-quiescence.json").is_file())
        self.assertTrue((execution.output / alignment_snapshot.AGENT_MANIFEST).is_file())
        self.assertTrue((execution.output / "supervised-day-binding.json").is_file())
        self.assertFalse((execution.output / "execution-binding.json").exists())
        self.assertFalse((execution.output / "deployment-check.json").exists())
        command = next(event[1] for event in execution.events if event[0] == "agent")
        openclaw = command.index("openclaw")
        self.assertEqual(command[openclaw - 7:openclaw], [
            "python3", "/tmp/sharedskill-agent-supervisor.py", "--timeout", "5",
            "--evidence", "/tmp/sharedskill-agent-quiescence.json", "--",
        ])
        grade_index = next(i for i, event in enumerate(execution.events) if event[0] == "grade")
        quiet_index = next(i for i, event in enumerate(execution.events)
                           if event[0] == "copy_out" and "agent-quiescence" in event[1])
        workspace_index = next(i for i, event in enumerate(execution.events)
                               if event[0] == "copy_out" and event[1] == "/tmp_workspace/.")
        self.assertLess(quiet_index, workspace_index)
        self.assertLess(workspace_index, grade_index)

    def test_normal_day_is_quiescent_and_frozen_before_scoring(self):
        with tempfile.TemporaryDirectory() as temporary:
            execution = SupervisedExecution(Path(temporary))
            result = execution.execute()
            self._assert_supervised(execution, result)
            self.assertFalse(result["timed_out"])
            self.assertEqual(execution.agent_wait_timeouts, [20])

    def test_deadline_day_is_quiescent_and_frozen_before_scoring(self):
        with tempfile.TemporaryDirectory() as temporary:
            execution = SupervisedExecution(Path(temporary), deadline_expired=True)
            result = execution.execute()
            self._assert_supervised(execution, result)
            self.assertTrue(result["timed_out"])
            self.assertEqual(execution.agent_wait_timeouts, [20])

    def test_switch_does_not_change_non_day_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            execution = SupervisedExecution(Path(temporary), phase="night")
            result = execution.execute()
            self.assertEqual(result["status"], "completed", result)
            self.assertFalse((execution.output / alignment_snapshot.AGENT_MANIFEST).exists())
            self.assertFalse((execution.output / "agent-quiescence.json").exists())
            command = next(event[1] for event in execution.events if event[0] == "agent")
            self.assertNotIn("/tmp/sharedskill-agent-supervisor.py", command)
            self.assertEqual(execution.agent_wait_timeouts, [5])

    def test_pending_score_rescores_frozen_day_without_agent_rerun(self):
        with tempfile.TemporaryDirectory() as temporary:
            execution = SupervisedExecution(Path(temporary))
            execution.grade_events = [{
                "kind": "model_api",
                "fingerprint": "judge-request",
                "status": 503,
                "error": "judge temporarily unavailable",
            }]
            first = execution.execute()
            self.assertEqual(first["status"], "pending_score", first)
            manifest = (execution.output / alignment_snapshot.AGENT_MANIFEST).read_bytes()
            execution.grade_scores = {"overall_score": 0.8}
            execution.grade_events = []
            recovered = execution.execute(rescore=True)
            self.assertEqual(recovered["status"], "completed", recovered)
            self.assertEqual(recovered["score"], 0.8)
            self.assertEqual(execution.agent_calls, 1)
            self.assertEqual((execution.output / alignment_snapshot.AGENT_MANIFEST).read_bytes(), manifest)
            grades = [event for event in execution.events if event[0] == "grade"]
            self.assertTrue(grades[-1][2]["replay_only"])

    def test_unproven_quiescence_fails_closed_before_scoring(self):
        with tempfile.TemporaryDirectory() as temporary:
            execution = SupervisedExecution(Path(temporary), quiescent=False)
            result = execution.execute()
            self.assertEqual(result["status"], "infrastructure_error", result)
            self.assertIn("immutable Agent output snapshot", result["error"])
            self.assertFalse(any(event[0] == "grade" for event in execution.events))
            self.assertFalse((execution.output / alignment_snapshot.AGENT_MANIFEST).exists())

    def test_switch_is_fixed_and_cannot_enable_alignment(self):
        with self.assertRaisesRegex(ValueError, "Unknown supervised Day"):
            runtime.supervised_day_execution({
                "supervised_day_execution": {"version": "different"},
            })
        with self.assertRaisesRegex(ValueError, "cannot be combined"):
            runtime.supervised_day_execution({
                "supervised_day_execution": {
                    "version": runtime.SUPERVISED_DAY_EXECUTION_VERSION,
                },
                "alignment": {"version": "native-night-atomic-v1"},
            })


if __name__ == "__main__":
    unittest.main()
