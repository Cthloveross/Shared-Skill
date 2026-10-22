"""Immutable byte snapshots and real score recovery branches; no model calls."""

import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from scripts.wildclawbench import alignment_runtime
from scripts.wildclawbench import alignment_snapshot as snapshot
import httpx
import requests
from scripts.wildclawbench import task_runtime as runtime
from . import test_runtime as fixtures
from scripts.wildclawbench.alignment_catalog import load_binding
from .test_runtime import MockExecution


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name)
        (self.output / "workspace/results").mkdir(parents=True)
        (self.output / "workspace/results/output.txt").write_bytes(b"original output")
        for name, value in {
            "chat.jsonl": {"message": "original"},
            "execution-binding.json": {"bank": "S6"},
            "agent-quiescence.json": {"quiescent": True, "surviving_writers": []},
        }.items():
            (self.output / name).write_text(json.dumps(value))
        self.identity = {"task_id": "03.01", "execution_id": "original", "bank_sha256": "S6"}

    def seal(self, cache=True):
        first = snapshot.freeze_agent_output(self.output, self.identity)
        if cache:
            (self.output / "mock-replay.json").write_text(
                json.dumps({"http://localhost:9100/gmail/audit": {"status": 200, "body": '{"messages":[]}'}})
            )
        second = snapshot.seal_grading_recovery(
            self.output, self.identity, 'url="http://localhost:9100/gmail/audit"', []
        )
        return first, second

    def test_two_seals_exclude_gt_and_grade_files_and_are_immutable(self):
        first, second = self.seal()
        (self.output / "workspace/gt").mkdir()
        (self.output / "workspace/gt/hidden.txt").write_text("hidden grader input")
        (self.output / "grade.stdout").write_text("new score attempt")
        self.assertEqual(snapshot.freeze_agent_output(self.output, self.identity), first)
        self.assertEqual(snapshot.verify_grading_recovery(self.output, self.identity), second)
        self.assertEqual(snapshot.seal_grading_recovery(self.output, self.identity, "changed checks", []), second)

    def test_changed_output_chat_path_or_identity_fails_before_recovery(self):
        self.seal()
        for name in ["workspace/results/output.txt", "chat.jsonl"]:
            path = self.output / name
            original = path.read_bytes()
            path.write_bytes(b"changed")
            with self.assertRaisesRegex(snapshot.SnapshotError, "output bytes"):
                snapshot.verify_grading_recovery(self.output, self.identity)
            path.write_bytes(original)
        with self.assertRaisesRegex(snapshot.SnapshotError, "identity differs"):
            snapshot.verify_grading_recovery(self.output, {**self.identity, "bank_sha256": "S0"})
        (self.output / "workspace/results/extra.txt").write_text("late writer")
        with self.assertRaisesRegex(snapshot.SnapshotError, "output bytes"):
            snapshot.verify_grading_recovery(self.output, self.identity)

    def test_missing_mock_is_explicitly_unrecoverable_and_never_recreated(self):
        _, sealed = self.seal(cache=False)
        self.assertFalse(sealed["recoverable"])
        self.assertIn("missing_original_mock_snapshot", sealed["unavailable_reasons"])
        with self.assertRaisesRegex(snapshot.SnapshotError, "mock state cannot be restored"):
            snapshot.verify_grading_recovery(self.output, self.identity)
        self.assertFalse((self.output / "mock-replay.json").exists())

    def test_changed_or_removed_mock_snapshot_is_rejected(self):
        self.seal()
        path = self.output / "mock-replay.json"
        path.write_text("{}")
        with self.assertRaisesRegex(snapshot.SnapshotError, "mock response snapshot changed"):
            snapshot.verify_grading_recovery(self.output, self.identity)
        path.unlink()
        with self.assertRaisesRegex(snapshot.SnapshotError, "mock response snapshot changed"):
            snapshot.verify_grading_recovery(self.output, self.identity)

    def test_existing_mock_is_archived_before_first_grade(self):
        replay = self.output / "mock-replay.json"
        replay.write_text("{}")
        snapshot.freeze_agent_output(self.output, self.identity)
        self.assertEqual((self.output / "snapshots/pre-grade-mock-replay.json").read_bytes(), b"{}")
        replay.write_text(json.dumps({"http://localhost:9100/gmail/audit": {"status": 200, "body": "{}"}}))
        snapshot.seal_grading_recovery(self.output, self.identity, "", [])
        snapshot.verify_grading_recovery(self.output, self.identity)

    def test_uncaptured_mock_or_cached_http_failure_is_not_recoverable(self):
        snapshot.freeze_agent_output(self.output, self.identity)
        (self.output / "mock-replay.json").write_text(
            json.dumps({"http://localhost:9100/gmail/audit": {"status": 503, "body": "unavailable"}})
        )
        sealed = snapshot.seal_grading_recovery(
            self.output,
            self.identity,
            "",
            [{"kind": "mock_api", "url": "http://localhost:9101/calendar/audit", "status": 503}],
        )
        self.assertFalse(sealed["recoverable"])
        self.assertIn("captured_mock_http_error", sealed["unavailable_reasons"])
        self.assertIn("uncaptured_observed_mock_urls", sealed["unavailable_reasons"])


class AlignmentExecution(MockExecution):
    def __init__(self, root):
        super().__init__(root)
        (self.bank / "alpha").mkdir()
        (self.bank / "alpha/SKILL.md").write_text(
            '---\nname: alpha\ndescription: "Frozen S6 skill."\n---\nOriginal procedure.\n'
        )
        self.config = {
            "alignment": {"version": "native-night-atomic-v1"},
            "protocol_hash": "frozen",
            "runtime_dir": str(root / "runtime"),
            "seed": 42,
            "user_id": "user-01",
            "phase": "endpoint",
            "round": 6,
            "task": {"input_sha256": "input", "source_sha256": "source"},
        }
        self.spec["automated_checks"] = (
            'def grade(**kwargs):\n    url="http://localhost:9100/gmail/audit"\n    return {}'
        )
        self.capture_mock = True

    def copy_out(self, name, source, path, config, **kwargs):
        if source == "/tmp/sharedskill-agent-quiescence.json":
            path.write_text(json.dumps({"quiescent": True, "surviving_writers": [], "deadline_expired": False}))
            return True
        return super().copy_out(name, source, path, config, **kwargs)

    def grade(self, name, spec, output, config, **kwargs):
        if self.capture_mock:
            (output / "mock-replay.json").write_text(
                json.dumps({"http://localhost:9100/gmail/audit": {"status": 200, "body": '{"messages":[]}'}})
            )
        return super().grade(name, spec, output, config, **kwargs)

    def execute(self, rescore=False, bank=None):
        with (
            self.context(),
            patch.object(alignment_runtime, "install_bank"),
            patch.object(alignment_runtime, "verify_deployed_bank", return_value={"passed": True}),
        ):
            return runtime._execute_child(self.spec["task_id"], bank or self.bank, self.output, self.config, rescore)


class ScoreRecoveryTests(unittest.TestCase):
    def pending(self, root):
        execution = AlignmentExecution(root)
        execution.grade_scores = {"overall_score": 0.0}
        execution.grade_events = [
            {
                "kind": "model_api",
                "fingerprint": "judge-request",
                "status": 503,
                "error": "judge temporarily unavailable",
            }
        ]
        return execution

    def test_503_recovers_from_original_s6_in_new_container_without_agent_rerun(self):
        with tempfile.TemporaryDirectory() as temporary:
            execution = self.pending(Path(temporary))
            first = execution.execute()
            self.assertEqual(first["status"], "pending_score")
            self.assertIsNone(first["score"])
            manifest = (execution.output / snapshot.AGENT_MANIFEST).read_bytes()
            binding = json.loads((execution.output / "execution-binding.json").read_text())
            s0 = Path(temporary) / "S0"
            s0.mkdir()
            execution.config["initial_skill_dir"] = str(s0)
            execution.grade_scores, execution.grade_events = {"overall_score": 0.8}, []
            recovered = execution.execute(rescore=True, bank=s0)
            self.assertEqual(recovered["status"], "completed", recovered)
            self.assertEqual(recovered["score"], 0.8)
            self.assertEqual(execution.agent_calls, 1)
            grades = [event for event in execution.events if event[0] == "grade"]
            self.assertNotEqual(grades[0][1], grades[1][1])
            self.assertTrue(grades[1][2]["replay_only"])
            self.assertEqual((execution.output / snapshot.AGENT_MANIFEST).read_bytes(), manifest)
            self.assertEqual(load_binding(execution.config, binding["run_id"], binding["execution_id"]), binding)

    def test_tampered_bytes_refuse_scoring_and_do_not_rerun_agent(self):
        with tempfile.TemporaryDirectory() as temporary:
            execution = self.pending(Path(temporary))
            self.assertEqual(execution.execute()["status"], "pending_score")
            (execution.output / "workspace/output.txt").write_text("different result")
            result = execution.execute(rescore=True)
            self.assertIsNone(result["score"])
            self.assertIn("output bytes", result["error"])
            self.assertEqual(execution.agent_calls, 1)
            self.assertEqual(len([event for event in execution.events if event[0] == "grade"]), 1)

    def test_missing_mock_refuses_recovery_instead_of_recreating_fresh_mock(self):
        with tempfile.TemporaryDirectory() as temporary:
            execution = self.pending(Path(temporary))
            execution.capture_mock = False
            self.assertEqual(execution.execute()["status"], "pending_score")
            result = execution.execute(rescore=True)
            self.assertIsNone(result["score"])
            self.assertIn("mock state cannot be restored", result["error"])
            self.assertEqual(execution.agent_calls, 1)
            self.assertEqual(len([event for event in execution.events if event[0] == "grade"]), 1)


class MockReplayTests(unittest.TestCase):
    def monitor(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        monitor, calls = fixtures.GraderHTTPTimeoutTests().monitor(directory)
        monitor.update(_httpx=httpx, _requests=requests, _urllib_error=urllib.error)
        return monitor, calls

    def invoke(self, monitor, client, url):
        if client == "httpx":
            return monitor["_httpx_send"](None, httpx.Request("GET", url))
        if client == "requests":
            return monitor["_requests_send"](None, requests.Request("GET", url).prepare())
        return monitor["_urlopen"](urllib.request.Request(url))

    def test_all_three_clients_reject_uncaptured_mock_in_replay_only_without_transport(self):
        with tempfile.TemporaryDirectory() as temporary:
            for client in ["httpx", "requests", "urllib"]:
                with self.subTest(client=client):
                    output = Path(temporary) / client
                    monitor, calls = self.monitor(output)
                    monitor["_sharedskill_replay_only"] = True
                    with self.assertRaisesRegex(RuntimeError, "Original mock state was not captured"):
                        self.invoke(monitor, client, "http://localhost:9100/gmail/audit")
                    self.assertFalse(calls)
                    event = json.loads((output / "judge-api.jsonl").read_text())
                    self.assertEqual(event["kind"], "mock_api")

    def test_mock_http_503_is_pending_even_if_official_checker_returns_zero(self):
        with tempfile.TemporaryDirectory() as temporary:
            for client in ["httpx", "requests", "urllib"]:
                with self.subTest(client=client):
                    output = Path(temporary) / client
                    monitor, _ = self.monitor(output)
                    url = "http://localhost:9100/gmail/audit"
                    if client == "httpx":
                        monitor["_original_httpx_send"] = lambda _client, request, **kw: httpx.Response(
                            503, text="unavailable", request=request
                        )
                    elif client == "requests":

                        def unavailable(*args, **kwargs):
                            response = requests.Response()
                            response.status_code, response._content = 503, b"unavailable"
                            return response

                        monitor["_original_requests_send"] = unavailable
                    else:

                        def unavailable(*args, **kwargs):
                            raise urllib.error.HTTPError(url, 503, "unavailable", {}, None)

                        monitor["_original_urlopen"] = unavailable
                    try:
                        self.invoke(monitor, client, url)
                    except urllib.error.HTTPError:
                        pass
                    events = [json.loads(line) for line in (output / "judge-api.jsonl").read_text().splitlines()]
                    self.assertEqual(events[-1]["kind"], "mock_api")
                    self.assertEqual(events[-1]["status"], 503)
                    execution = MockExecution(output / "task")
                    execution.grade_scores, execution.grade_events = {"overall_score": 0.0}, events
                    result = execution.execute()
                    self.assertEqual(result["status"], "pending_score")
                    self.assertIsNone(result["score"])

    def test_cached_success_preserves_body_and_does_not_query_fresh_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            for client in ["httpx", "requests", "urllib"]:
                with self.subTest(client=client):
                    output = Path(temporary) / client
                    monitor, calls = self.monitor(output)
                    url, body = "http://localhost:9100/gmail/audit", '{"messages":["原始状态"]}'
                    monitor.update(_sharedskill_replay_only=True, _replay={url: {"status": 200, "body": body}})
                    response = self.invoke(monitor, client, url)
                    actual = response.read().decode() if client == "urllib" else response.text
                    self.assertEqual(actual, body)
                    self.assertFalse(calls)


if __name__ == "__main__":
    unittest.main()
