"""Runtime contract checks. Never start Docker or call a model/service.

Run with the project's Python from any working directory. Corpus checks use the
prepared, pinned upstream checkout and skip when sources have not been prepared.
"""

from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
import urllib.parse
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import environment  # noqa: E402 - standalone experiment directory is not a package
import task_runtime as runtime  # noqa: E402

SOURCE = ROOT / "runtime" / "upstream" / "WildClawBench"
HAS_CORPUS = (SOURCE / "src" / "utils" / "task_parser.py").is_file()


def completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


@unittest.skipUnless(HAS_CORPUS, "Prepare pinned benchmark sources first")
class OfficialCorpusTests(unittest.TestCase):
    def test_all_60_parse_and_accept_original_grader_keywords(self):
        config = {"experiment_root": str(ROOT)}
        files = sorted((SOURCE / "tasks").glob("*/*task_*.md"))
        self.assertEqual(len(files), 60)
        ids, categories = set(), set()
        for file in files:
            with self.subTest(task=file.name):
                spec = runtime.task_spec(file.stem, config)
                ids.add(spec["task_id"])
                categories.add(spec["category"])
                self.assertEqual(Path(spec["file_path"]), file.resolve())
                self.assertTrue(spec["prompt"].strip())
                self.assertTrue(Path(spec["workspace_path"]).is_relative_to((SOURCE / "workspace").resolve()))
                tree = ast.parse(spec["automated_checks"])
                grade = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "grade")
                self.assertIsNotNone(grade.args.kwarg, "Original grade must accept transcript/workspace_path")
        self.assertEqual(len(ids), 60)
        self.assertEqual(len(categories), 6)

    def test_59_prompts_unchanged_and_only_ocr_is_documented_variant(self):
        config = {"experiment_root": str(ROOT), "model_id": "qwen3.8-27b"}
        unchanged, ocr = 0, 0
        with tempfile.TemporaryDirectory() as temporary:
            for file in sorted((SOURCE / "tasks").glob("*/*task_*.md")):
                with self.subTest(task=file.name):
                    spec = runtime.task_spec(file.stem, config)
                    output = Path(temporary) / file.stem
                    output.mkdir()
                    modified = runtime._variants(spec, config, output, "http://local-gateway:18182/runs/test/v1")
                    manifest = json.loads((output / "task-variant.json").read_text())
                    self.assertEqual((output / "official-task.md").read_bytes(), file.read_bytes())
                    self.assertEqual(manifest["source_sha256"], hashlib.sha256(file.read_bytes()).hexdigest())
                    self.assertEqual(
                        manifest["modified_prompt_sha256"], hashlib.sha256(modified["prompt"].encode()).hexdigest()
                    )
                    if "ocrbench" in spec["task_id"].lower():
                        ocr += 1
                        self.assertNotEqual(modified["prompt"], spec["prompt"])
                        self.assertIn("1000", modified["prompt"])
                        self.assertIn("qwen3.8-27b", modified["prompt"])
                        self.assertTrue(manifest["ocr_task_completion_is_not_model_accuracy"])
                    else:
                        unchanged += 1
                        self.assertEqual(modified["prompt"], spec["prompt"])
                        self.assertFalse(any(s["field"] == "prompt" for s in manifest["substitutions"]))
        self.assertEqual((unchanged, ocr), (59, 1))


class GraderTests(unittest.TestCase):
    def test_process_failure_timeout_and_invalid_output_are_errors_not_zero(self):
        cases = [
            completed("", "scorer startup failed", 1),
            completed("not scores\n"),
            subprocess.TimeoutExpired("mock grade", 1),
        ]
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary)

                def docker(args, config, **kwargs):
                    if "python3" not in args:
                        return completed()
                    if isinstance(case, Exception):
                        raise case
                    return case

                with (
                    patch.object(runtime, "_docker", side_effect=docker),
                    patch.object(runtime, "_copy_in"),
                    patch.object(runtime, "_copy_out", return_value=False),
                    patch.object(environment, "container_api_url", return_value="http://mock-only/v1"),
                ):
                    scores, events = runtime._grade(
                        "mock-only",
                        {"automated_checks": "def grade(**kwargs): return {'overall_score': 0}"},
                        output,
                        {},
                    )
                self.assertIn("error", scores)
                self.assertTrue(scores["_grading_infrastructure_error"])
                self.assertNotIn("overall_score", scores)
                self.assertEqual(events, [])
                code = (output / "grade-runner.py").read_text()
                ast.parse(code)
                self.assertIn("grade(transcript=_transcript,workspace_path='/tmp_workspace')", code)

    def test_legitimate_zero_survives_stdout_logs_and_api_events(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            event = {"fingerprint": "f", "kind": "model_api", "status": 200}

            def copy_out(name, source, path, config, **kwargs):
                if source.endswith("judge-api.jsonl"):
                    path.write_text(json.dumps(event) + "\n")
                    return True
                return False

            with (
                patch.object(runtime, "_docker", return_value=completed('debug line\n{"overall_score": 0.0}\n')),
                patch.object(runtime, "_copy_in"),
                patch.object(runtime, "_copy_out", side_effect=copy_out),
                patch.object(environment, "container_api_url", return_value="http://mock-only/v1"),
            ):
                scores, events = runtime._grade(
                    "mock-only", {"automated_checks": "def grade(**kwargs): return {'overall_score': 0}"}, output, {}
                )
            self.assertEqual(scores, {"overall_score": 0.0})
            self.assertEqual(events, [event])

    def test_scoring_completion_exhaustion_and_empty_text_are_diagnostic_errors(self):
        # Extract only the pure function; never execute the HTTP monkeypatches.
        tree = ast.parse(runtime._GRADE_MONITOR)
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_completion_issue")
        namespace = {}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "mock-grade-pure-function", "exec"), namespace)
        issue = namespace["_completion_issue"]
        self.assertIsNotNone(issue({"choices": [{"finish_reason": "length", "message": {"content": "partial"}}]}))
        self.assertIsNotNone(issue({"choices": [{"finish_reason": "stop", "message": {"content": ""}}]}))
        self.assertIsNone(issue({"choices": [{"finish_reason": "stop", "message": {"content": "valid judgment"}}]}))


class GraderHTTPTimeoutTests(unittest.TestCase):
    BASE_URL = "http://local-gateway:18182/runs/this-run/v1"

    def monitor(self, output: Path):
        """Load the actual wrapper functions with stub transports, without imports or monkeypatches."""
        namespace = {
            "_urlparse": urllib.parse,
            "_hashlib": hashlib,
            "_json": json,
            "_io": io,
            "_api_log": output / "judge-api.jsonl",
            "_mock_log": output / "mock-replay.json",
            "_replay": {},
            "_sharedskill_model_api_base_url": self.BASE_URL,
            "_sharedskill_grading_timeout_seconds": 1800,
        }
        names = {
            "_audit",
            "_is_local_model_api",
            "_timeout_evidence",
            "_audit_timeout",
            "_completion_issue",
            "_httpx_send",
            "_requests_send",
            "_urlopen",
            "_CachedResponse",
        }
        nodes = [
            node
            for node in ast.walk(ast.parse(runtime._GRADE_MONITOR))
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names
        ]
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "stub-grader-transports", "exec"), namespace)
        calls = []
        payload = {"choices": [{"finish_reason": "stop", "message": {"content": "valid judgment"}}]}

        def httpx_send(client, request, *args, **kwargs):
            calls.append(("httpx", dict(request.extensions), args, dict(kwargs)))
            return types.SimpleNamespace(status_code=200, json=lambda: payload)

        def requests_send(client, request, *args, **kwargs):
            calls.append(("requests", request, args, dict(kwargs)))
            return types.SimpleNamespace(status_code=200, text="{}", json=lambda: payload)

        def urlopen(request, *args, **kwargs):
            calls.append(("urllib", request, args, dict(kwargs)))
            response = io.BytesIO(b"{}")
            response.status = 200
            return response

        namespace.update(
            _original_httpx_send=httpx_send,
            _original_requests_send=requests_send,
            _original_urlopen=urlopen,
        )
        return namespace, calls

    def events(self, output):
        file = output / "judge-api.jsonl"
        return [json.loads(line) for line in file.read_text().splitlines()] if file.exists() else []

    def test_httpx_model_timeout_overrides_four_extensions_and_retains_other_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            monitor, calls = self.monitor(output)
            original = {"connect": 5, "read": 60, "write": 10, "pool": 5}
            request = types.SimpleNamespace(
                url=self.BASE_URL + "/chat/completions",
                content=b"request",
                extensions={"timeout": original, "trace": "unchanged"},
            )
            monitor["_httpx_send"](None, request, follow_redirects=True)
            expected = dict.fromkeys(("connect", "read", "write", "pool"), 1800)
            self.assertEqual(calls[0][1], {"timeout": expected, "trace": "unchanged"})
            self.assertEqual(calls[0][3], {"follow_redirects": True})
            override = next(e for e in self.events(output) if e["kind"] == "timeout_override")
            self.assertEqual(override["original_timeout"], original)
            self.assertEqual(override["effective_timeout"], expected)
            self.assertEqual(override["client"], "httpx")

    def test_requests_model_timeout_and_free_resource_or_mock_timeouts(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            monitor, calls = self.monitor(output)
            model = types.SimpleNamespace(url=self.BASE_URL + "/chat/completions", body="request", method="POST")
            monitor["_requests_send"](None, model, timeout=(5, 60), stream=False)
            self.assertEqual(calls[-1][3], {"timeout": 1800, "stream": False})
            for url in ("https://arxiv.org/pdf/2111.06377", "http://127.0.0.1:9000/tweets"):
                with self.subTest(url=url):
                    request = types.SimpleNamespace(url=url, body=None, method="GET")
                    monitor["_requests_send"](None, request, timeout=9)
                    self.assertEqual(calls[-1][3], {"timeout": 9})
            overrides = [e for e in self.events(output) if e["kind"] == "timeout_override"]
            self.assertEqual(len(overrides), 1)
            self.assertEqual(overrides[0]["original_timeout"], [5, 60])
            self.assertEqual(overrides[0]["effective_timeout"], 1800)

    def test_urllib_positional_keyword_and_default_timeout_preserve_other_arguments(self):
        cases = [
            ((None, 60), {}, (None, 1800), {}),
            ((b"payload", 60), {"timeout": 15, "context": "kept"}, (b"payload", 1800), {"context": "kept"}),
            ((None,), {"timeout": 60}, (None,), {"timeout": 1800}),
            ((), {}, (), {"timeout": 1800}),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            monitor, calls = self.monitor(output)
            request = types.SimpleNamespace(full_url=self.BASE_URL + "/chat/completions", get_method=lambda: "POST")
            for args, kwargs, expected_args, expected_kwargs in cases:
                with self.subTest(args=args, kwargs=kwargs):
                    monitor["_urlopen"](request, *args, **kwargs)
                    self.assertEqual(calls[-1][2:], (expected_args, expected_kwargs))
            for url in ("https://arxiv.org/pdf/2111.06377", "http://127.0.0.1:9000/tweets"):
                with self.subTest(url=url):
                    free_request = types.SimpleNamespace(full_url=url, get_method=lambda: "GET")
                    monitor["_urlopen"](free_request, None, 7)
                    self.assertEqual(calls[-1][2:], ((None, 7), {}))
            overrides = [e for e in self.events(output) if e["kind"] == "timeout_override"]
            self.assertEqual([e["original_timeout"] for e in overrides], [60, 60, 60, "<library-default>"])
            self.assertTrue(all(e["effective_timeout"] == 1800 for e in overrides))

    def test_local_endpoint_matching_excludes_sibling_runs_and_unrelated_resources_for_all_clients(self):
        outside = (
            "https://local-gateway:18182/runs/this-run/v1/chat/completions",
            "http://local-gateway:18183/runs/this-run/v1/chat/completions",
            "http://other-gateway:18182/runs/this-run/v1/chat/completions",
            "http://local-gateway:18182/runs/other-run/v1/chat/completions",
            self.BASE_URL + "-resource/chat/completions",
            "https://public.example/download?url=" + self.BASE_URL + "/chat/completions",
            "http://127.0.0.1:9000/tweets",
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            monitor, calls = self.monitor(output)
            for url in outside:
                with self.subTest(url=url):
                    self.assertFalse(monitor["_is_local_model_api"](url))
                    httpx_request = types.SimpleNamespace(url=url, content=b"", extensions={"timeout": {"read": 7}})
                    monitor["_httpx_send"](None, httpx_request)
                    self.assertEqual(calls[-1][1]["timeout"], {"read": 7})
                    requests_request = types.SimpleNamespace(url=url, body=None, method="GET")
                    monitor["_requests_send"](None, requests_request, timeout=7)
                    self.assertEqual(calls[-1][3]["timeout"], 7)
                    urllib_request = types.SimpleNamespace(full_url=url, get_method=lambda: "GET")
                    monitor["_urlopen"](urllib_request, timeout=7)
                    self.assertEqual(calls[-1][3]["timeout"], 7)
            self.assertFalse(any(e["kind"] == "timeout_override" for e in self.events(output)))
            for url in (self.BASE_URL, self.BASE_URL + "/chat/completions", self.BASE_URL + "/models?limit=1"):
                self.assertTrue(monitor["_is_local_model_api"](url))

    def test_timeout_evidence_cannot_resolve_an_existing_model_api_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            monitor, _ = self.monitor(output)
            url = self.BASE_URL + "/chat/completions"
            monitor["_audit"]("model_api", url, status=503, body=b"request")
            monitor["_audit_timeout"]("httpx", url, {"read": 60}, {"read": 1800})
            events = self.events(output)
            self.assertNotEqual(events[0]["fingerprint"], events[1]["fingerprint"])
            execution = MockExecution(output / "execution")
            execution.grade_events = events
            result = execution.execute()
            self.assertEqual(result["status"], "pending_score")
            self.assertEqual(result["scoring_api_failures"], [events[0]])

    def test_runner_injects_current_local_url_and_timeout_before_monitor(self):
        for config, expected_timeout in (
            ({}, 1800),
            ({"grading_timeout_seconds": 913, "grade_timeout_seconds": 901}, 913),
        ):
            with self.subTest(config=config), tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary)
                calls = []

                def docker(args, config, **kwargs):
                    calls.append((args, kwargs))
                    return completed('{"overall_score": 0.0}\n')

                with (
                    patch.object(runtime, "_docker", side_effect=docker),
                    patch.object(runtime, "_copy_in"),
                    patch.object(runtime, "_copy_out", return_value=False),
                    patch.object(environment, "container_api_url", return_value=self.BASE_URL),
                ):
                    runtime._grade(
                        "mock-only",
                        {"automated_checks": "def grade(**kwargs): return {'overall_score':0}"},
                        output,
                        config,
                    )
                tree = ast.parse((output / "grade-runner.py").read_text())
                injected = {
                    node.targets[0].id: ast.literal_eval(node.value)
                    for node in tree.body[:3]
                    if isinstance(node, ast.Assign)
                }
                self.assertEqual(injected["_sharedskill_model_api_base_url"], self.BASE_URL)
                self.assertEqual(injected["_sharedskill_grading_timeout_seconds"], expected_timeout)
                grade_call = next((args, kwargs) for args, kwargs in calls if "python3" in args)
                self.assertEqual(grade_call[1]["timeout"], expected_timeout)
                self.assertIn("OPENROUTER_BASE_URL=" + self.BASE_URL, grade_call[0])


class MockExecution:
    """Only simulates container effects at the module's Docker boundary."""

    def __init__(self, root: Path):
        self.root = root
        self.source = root / "upstream"
        self.workspace = root / "inputs"
        for folder in ("exec", "tmp", "gt"):
            (self.workspace / folder).mkdir(parents=True)
        (self.workspace / "exec" / "input.txt").write_text("immutable original input")
        (self.workspace / "gt" / "answer.txt").write_text("hidden answer")
        self.bank = root / "bank"
        self.bank.mkdir()
        self.output = root / "output"
        self.output.mkdir()
        self.spec = {
            "task_id": "01_mock_task_1_test",
            "prompt": "Exact original task prompt",
            "category": "01_mock",
            "timeout_seconds": 5,
            "workspace_path": str(self.workspace),
            "warmup": "echo ready",
            "automated_checks": "def grade(**kwargs): return {'overall_score': 0.0}",
        }
        self.events = []
        self.agent_calls = 0
        self.agent_returncode = 0
        self.agent_stdout = "{}"
        self.agent_stderr = ""
        self.agent_exception = None
        self.agent_elapsed = 0
        self.agent_wait_timeouts = []
        self.unix_time = 100.0
        self.actual_assistant = True
        self.chat_entries = None
        self.assistant_stop_reason = None
        self.thinking_only = False
        self.grade_scores = {"overall_score": 0.0}
        self.grade_events = []
        self.gateway_errors = []
        self.gateway_records = None
        self.warmup_returncode = 0
        self.live_container = True

    def docker(self, args, config, **kwargs):
        self.events.append(("docker", args))
        if "openclaw" in args and "agent" in args:
            self.agent_calls += 1
            self.events.append(("agent", args))
            self.agent_wait_timeouts.append(kwargs.get("timeout"))
            self.unix_time += self.agent_elapsed
            if self.agent_exception:
                raise self.agent_exception
            return completed(
                self.agent_stdout,
                self.agent_stderr or ("agent failed" if self.agent_returncode else ""),
                self.agent_returncode,
            )
        if args and args[0] == "inspect":
            run_id = "wcb-" + hashlib.sha256(str(self.output.resolve()).encode()).hexdigest()[:20]
            return completed(
                json.dumps(
                    [
                        {
                            "State": {"Running": self.live_container},
                            "Config": {"Labels": {"org.sharedskill.run_id": run_id}},
                        }
                    ]
                )
            )
        if "/tmp/sharedskill-warmup.sh" in args:
            return completed("", "warmup failed" if self.warmup_returncode else "", self.warmup_returncode)
        return completed()

    def copy_in(self, path, name, destination, config):
        self.events.append(("copy_in", str(path), destination))

    def adapters(self, name, config, api_url, manifest):
        self.events.append(("adapters", name, api_url, str(manifest)))
        return {"scope": "mock container-local adapter setup"}

    def python_bootstrap(self, name, config, manifest):
        self.events.append(("python_bootstrap", name, str(manifest)))
        return {"seed": 42, "scope": "mock container Python startup bootstrap"}

    def copy_out(self, name, source, path, config, **kwargs):
        self.events.append(("copy_out", source, str(path)))
        if source.endswith("chat.jsonl"):
            if self.chat_entries is not None:
                path.write_text("".join(json.dumps(entry) + "\n" for entry in self.chat_entries))
                return True
            if self.actual_assistant:
                content = (
                    [{"type": "thinking", "thinking": "mock reasoning only"}]
                    if self.thinking_only
                    else [{"type": "text", "text": "actual mocked activity"}]
                )
                entry = {
                    "type": "message",
                    "message": {"role": "assistant", "content": content},
                }
                if self.assistant_stop_reason:
                    entry["message"].update(
                        stopReason=self.assistant_stop_reason, errorMessage="mock model API failure"
                    )
                path.write_text(json.dumps(entry) + "\n")
                return True
            return False
        if source == "/tmp_workspace/.":
            path.mkdir(exist_ok=True)
            (path / "output.txt").write_text("actual mock execution output")
            return True
        return False

    def grade(self, name, spec, output, config, **kwargs):
        self.events.append(("grade", name, kwargs))
        return dict(self.grade_scores), list(self.grade_events)

    def context(self):
        stack = ExitStack()
        grading = types.ModuleType("src.utils.grading")
        grading.extract_usage_from_jsonl = lambda path: {"input_tokens": 13, "output_tokens": 5}
        stack.enter_context(patch.dict(sys.modules, {"src.utils.grading": grading}))
        stack.enter_context(
            patch.object(
                environment,
                "paths",
                return_value={"upstream": self.source, "runtime": self.root, "assets": self.root / "assets"},
            )
        )
        stack.enter_context(
            patch.object(environment, "container_api_url", return_value="http://mock-gateway:18182/runs/test/v1")
        )
        stack.enter_context(patch.object(environment, "openclaw_config", return_value={"mock": True}))
        stack.enter_context(patch.object(environment, "apply_local_tool_adapters", side_effect=self.adapters))
        stack.enter_context(patch.object(environment, "apply_python_bootstrap", side_effect=self.python_bootstrap))
        stack.enter_context(
            patch.object(
                environment, "_run", side_effect=AssertionError("Unmocked subprocess escaped the runtime test boundary")
            )
        )
        stack.enter_context(patch.object(runtime, "task_spec", return_value=self.spec))
        stack.enter_context(patch.object(runtime, "_variants", side_effect=lambda spec, *args: dict(spec)))
        stack.enter_context(patch.object(runtime, "_docker", side_effect=self.docker))
        stack.enter_context(patch.object(runtime, "_copy_in", side_effect=self.copy_in))
        stack.enter_context(patch.object(runtime, "_copy_out", side_effect=self.copy_out))
        stack.enter_context(patch.object(runtime, "_grade", side_effect=self.grade))
        stack.enter_context(patch.object(runtime.time, "time", side_effect=lambda: self.unix_time))
        stack.enter_context(
            patch.object(
                runtime,
                "_gateway_main_records",
                side_effect=lambda *args: (
                    self.gateway_records if self.gateway_records is not None else self.gateway_errors
                ),
            )
        )
        stack.enter_context(
            patch.object(runtime, "_gateway_main_failures", side_effect=lambda *args, **kwargs: self.gateway_errors)
        )
        return stack

    def execute(self, rescore=False):
        with self.context():
            return runtime._execute_child(self.spec["task_id"], self.bank, self.output, {}, rescore)


class TaskDeadlineTests(unittest.TestCase):
    NATIVE_MARKER = "[agent/embedded] embedded run timeout: runId=native-1 sessionId=chat timeoutMs=5000"

    def cancellation(self, **changes):
        record = {
            "request_id": "terminal-request",
            "role": "main",
            "status": "error",
            "http_status": 200,
            "started_unix": 101.0,
            "finished_unix": 105.0,
            "error": "Streaming client disconnected before completion",
        }
        return dict(record, **changes)

    def deadline_execution(self, directory, *, outer=False):
        execution = MockExecution(directory)
        execution.agent_elapsed = 35 if outer else 5
        execution.agent_stdout = json.dumps({"payloads": [], "meta": {"aborted": True, "durationMs": 5000}})
        execution.agent_stderr = self.NATIVE_MARKER
        execution.gateway_errors = [self.cancellation(finished_unix=135.0 if outer else 105.0)]
        if outer:
            execution.agent_stderr = ""
            execution.agent_exception = subprocess.TimeoutExpired(
                "mock agent", 35, output=execution.agent_stdout.encode(), stderr=b"original native stderr"
            )
        return execution

    def assert_graded_deadline(self, execution, result):
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["score"], 0.0)
        self.assertTrue(result["timed_out"])
        self.assertEqual(execution.agent_wait_timeouts, [35])
        args = next(e[1] for e in execution.events if e[0] == "agent")
        self.assertEqual(args[args.index("--timeout") + 1], "5")
        workspace_index = next(
            i for i, e in enumerate(execution.events) if e[0] == "copy_out" and e[1] == "/tmp_workspace/."
        )
        grade_index = next(i for i, e in enumerate(execution.events) if e[0] == "grade")
        self.assertLess(workspace_index, grade_index)
        self.assertEqual((execution.output / "workspace/output.txt").read_text(), "actual mock execution output")
        self.assertTrue((execution.output / "chat.jsonl").exists())
        self.assertEqual(json.loads((execution.output / "raw-score.json").read_text()), {"overall_score": 0.0})
        saved = json.loads((execution.output / "execution.json").read_text())
        self.assertTrue(saved["timed_out"])
        evidence = json.loads((execution.output / "agent-deadline.json").read_text())
        self.assertTrue(evidence["confirmed"])
        self.assertEqual(evidence, result["task_deadline_evidence"])
        self.assertEqual(evidence["cancelled_gateway_requests"], execution.gateway_errors)

    def test_native_deadline_cancellation_preserves_outputs_and_official_grading(self):
        with tempfile.TemporaryDirectory() as temporary:
            execution = self.deadline_execution(Path(temporary))
            result = execution.execute()
            self.assert_graded_deadline(execution, result)
            self.assertFalse(result["task_deadline_evidence"]["outer_timeout"])
            self.assertEqual(result["task_deadline_evidence"]["native_deadline_markers"][0]["timeout_ms"], 5000)

    def test_outer_deadline_cancellation_preserves_outputs_and_original_stderr(self):
        with tempfile.TemporaryDirectory() as temporary:
            execution = self.deadline_execution(Path(temporary), outer=True)
            result = execution.execute()
            self.assert_graded_deadline(execution, result)
            self.assertTrue(result["task_deadline_evidence"]["outer_timeout"])
            self.assertIn("original native stderr", (execution.output / "agent.stderr").read_text())

    def test_deadline_before_assistant_activity_still_preserves_trace_and_grades(self):
        with tempfile.TemporaryDirectory() as temporary:
            execution = self.deadline_execution(Path(temporary))
            execution.chat_entries = [{"type": "message", "message": {"role": "user", "content": "task"}}]
            result = execution.execute()
            self.assert_graded_deadline(execution, result)
            self.assertEqual(result["session"]["messages"], execution.chat_entries)

    def test_confirmed_deadline_does_not_mask_http500_network_or_earlier_cancellation(self):
        genuine_failures = [
            self.cancellation(http_status=500, error="server failed"),
            self.cancellation(error="ReadError: connection closed"),
            self.cancellation(request_id="earlier-request", started_unix=100.5, finished_unix=104.0),
        ]
        for failure in genuine_failures:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                execution = self.deadline_execution(Path(temporary))
                execution.gateway_errors.insert(0, failure)
                result = execution.execute()
                self.assertEqual(result["status"], "infrastructure_error")
                self.assertIsNone(result["score"])
                self.assertTrue(result["timed_out"])
                self.assertIn(failure, result["gateway_main_errors"])
                self.assertFalse(any(e[0] == "grade" for e in execution.events))

    def test_concurrent_tool_requests_do_not_prevent_deadline_stream_cancellation(self):
        with tempfile.TemporaryDirectory() as temporary:
            execution = self.deadline_execution(Path(temporary))
            execution.gateway_records = execution.gateway_errors + [
                {"role": "main", "status": "completed", "http_status": 200, "started_unix": 105.5}
            ]
            result = execution.execute()
            self.assert_graded_deadline(execution, result)

    def test_all_active_streams_cancelled_at_deadline_continue_to_grading(self):
        with tempfile.TemporaryDirectory() as temporary:
            execution = self.deadline_execution(Path(temporary))
            execution.gateway_errors.append(
                self.cancellation(request_id="second-active-stream", started_unix=103.0, finished_unix=105.4)
            )
            result = execution.execute()
            self.assert_graded_deadline(execution, result)
            self.assertEqual(len(result["task_deadline_evidence"]["cancelled_gateway_requests"]), 2)

    def test_cancellation_after_agent_exit_window_is_still_infrastructure(self):
        with tempfile.TemporaryDirectory() as temporary:
            execution = self.deadline_execution(Path(temporary))
            execution.gateway_errors.append(
                self.cancellation(request_id="outside-exit-window", started_unix=103.0, finished_unix=106.001)
            )
            result = execution.execute()
            self.assertEqual(result["status"], "infrastructure_error")
            self.assertTrue(result["timed_out"])
            self.assertEqual(result["gateway_main_errors"], [execution.gateway_errors[-1]])
            self.assertFalse(any(e[0] == "grade" for e in execution.events))

    def test_observed_ocr_deadline_stream_cancellation_is_not_infrastructure(self):
        # Exact timing from the superseded 20261012T220826Z OCR attempt. Its
        # concurrent non-streaming evaluator submitted later requests at 4148.
        evidence = {
            "confirmed": True,
            "agent_started_unix": 1791842919.2438781,
            "agent_finished_unix": 1791844149.3634424,
            "deadline_not_before_unix": 1791844119.2438781,
        }
        actual_cancel = self.cancellation(
            request_id="73ed3fa2628f438f892804c5ad7525b6",
            started_unix=1791844091.2113047,
            finished_unix=1791844121.5060694,
        )
        failures, cancelled = runtime._deadline_stream_cancellations([actual_cancel], evidence)
        self.assertEqual(failures, [])
        self.assertEqual(cancelled, [actual_cancel])

    def test_manual_abort_or_wrong_native_timer_is_not_a_task_deadline(self):
        stderr_cases = [
            "Request was aborted",
            self.NATIVE_MARKER.replace("sessionId=chat", "sessionId=other"),
            self.NATIVE_MARKER.replace("timeoutMs=5000", "timeoutMs=4000"),
        ]
        for stderr in stderr_cases:
            with self.subTest(stderr=stderr), tempfile.TemporaryDirectory() as temporary:
                execution = self.deadline_execution(Path(temporary))
                execution.agent_stderr = stderr
                execution.assistant_stop_reason = "aborted"
                result = execution.execute()
                self.assertEqual(result["status"], "infrastructure_error")
                self.assertFalse(result["timed_out"])
                self.assertTrue(result["task_deadline_evidence"]["native_meta"]["aborted"])
                self.assertFalse(any(e[0] == "grade" for e in execution.events))

    def test_confirmed_deadline_never_ignores_transcript_stop_reason_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            execution = self.deadline_execution(Path(temporary))
            execution.assistant_stop_reason = "error"
            result = execution.execute()
            self.assertEqual(result["status"], "infrastructure_error")
            self.assertTrue(result["timed_out"])
            self.assertTrue(result["transcript_model_errors"])
            self.assertFalse(any(e[0] == "grade" for e in execution.events))

    def test_aborted_metadata_after_limit_without_deadline_is_infra_even_without_gateway_errors(self):
        with tempfile.TemporaryDirectory() as temporary:
            execution = self.deadline_execution(Path(temporary))
            execution.agent_stderr = ""
            execution.gateway_errors = []
            result = execution.execute()
            self.assertEqual(result["status"], "infrastructure_error")
            self.assertTrue(result["unconfirmed_agent_abort"])
            self.assertFalse(result["timed_out"])
            self.assertFalse(any(e[0] == "grade" for e in execution.events))


class TaskExecutionTests(unittest.TestCase):
    def test_failed_start_without_assistant_is_infrastructure_not_zero(self):
        with tempfile.TemporaryDirectory() as temporary:
            execution = MockExecution(Path(temporary))
            execution.actual_assistant = False
            execution.agent_returncode = 1
            result = execution.execute()
            self.assertEqual(result["status"], "infrastructure_error")
            self.assertIsNone(result["score"])
            self.assertFalse((execution.output / "execution.json").exists())
            self.assertFalse(any(e[0] == "grade" for e in execution.events))

    def test_main_model_api_failure_is_infrastructure_even_with_partial_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            execution = MockExecution(Path(temporary))
            execution.gateway_errors = [{"role": "main", "status": "error", "http_status": 503}]
            result = execution.execute()
            self.assertEqual(result["status"], "infrastructure_error")
            self.assertIsNone(result["score"])
            self.assertFalse((execution.output / "execution.json").exists())

    def test_transcript_model_error_is_infrastructure_despite_partial_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            execution = MockExecution(Path(temporary))
            execution.assistant_stop_reason = "error"
            result = execution.execute()
            self.assertEqual(result["status"], "infrastructure_error")
            self.assertIsNone(result["score"])
            self.assertFalse((execution.output / "execution.json").exists())
            self.assertFalse(any(e[0] == "grade" for e in execution.events))

    def test_thinking_only_does_not_count_as_actual_task_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            execution = MockExecution(Path(temporary))
            execution.thinking_only = True
            result = execution.execute()
            self.assertEqual(result["status"], "infrastructure_error")
            self.assertIsNone(result["score"])
            self.assertFalse((execution.output / "execution.json").exists())

    def test_failed_fresh_attempt_cannot_reuse_stale_assistant_transcript(self):
        with tempfile.TemporaryDirectory() as temporary:
            execution = MockExecution(Path(temporary))
            stale = {"message": {"role": "assistant", "content": [{"type": "text", "text": "old execution"}]}}
            (execution.output / "chat.jsonl").write_text(json.dumps(stale) + "\n")
            execution.actual_assistant = False
            result = execution.execute()
            self.assertEqual(result["status"], "infrastructure_error")
            self.assertIsNone(result["score"])
            self.assertFalse((execution.output / "execution.json").exists())

    def test_real_zero_is_completed_and_groundtruth_is_only_added_after_agent(self):
        with tempfile.TemporaryDirectory() as temporary:
            execution = MockExecution(Path(temporary))
            result = execution.execute()
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["score"], 0.0)
            self.assertEqual(result["usage"]["input_tokens"], 13)
            agent_index = next(i for i, e in enumerate(execution.events) if e[0] == "agent")
            gt_index = next(
                i for i, e in enumerate(execution.events) if e[0] == "copy_in" and e[2] == "/tmp_workspace/gt"
            )
            self.assertGreater(gt_index, agent_index)
            agent_command = execution.events[agent_index][1]
            self.assertEqual(agent_command[agent_command.index("--message") + 1], execution.spec["prompt"])
            launch = next(e[1] for e in execution.events if e[0] == "docker" and e[1][0] == "run")
            self.assertIn(f"type=bind,src={execution.workspace / 'exec'},dst=/app,readonly", launch)
            self.assertIn("PYTHONHASHSEED=42", launch)
            self.assertIn("PYTHONPATH=" + environment.PYTHON_BOOTSTRAP_DIRECTORY, launch)
            self.assertTrue(
                any(
                    e[0] == "docker" and e[1][0] == "cp" and str(execution.workspace / "tmp") in e[1][1]
                    for e in execution.events
                )
            )
            self.assertEqual((execution.workspace / "exec" / "input.txt").read_text(), "immutable original input")
            adapter_index = next(i for i, e in enumerate(execution.events) if e[0] == "adapters")
            self.assertLess(adapter_index, agent_index, "Local tool routing must be ready before real Agent starts")
            seed_index = next(i for i, e in enumerate(execution.events) if e[0] == "python_bootstrap")
            warmup_index = next(
                i for i, e in enumerate(execution.events) if e[0] == "docker" and "/tmp/sharedskill-warmup.sh" in e[1]
            )
            self.assertLess(seed_index, warmup_index, "Mock servers must start from the seeded Python state")

    def test_swallowed_scoring_api_failure_cannot_be_reported_as_zero(self):
        with tempfile.TemporaryDirectory() as temporary:
            execution = MockExecution(Path(temporary))
            execution.grade_events = [{"fingerprint": "same-request", "kind": "model_api", "status": 503}]
            result = execution.execute()
            self.assertEqual(result["status"], "pending_score")
            self.assertIsNone(result["score"])
            self.assertTrue((execution.output / "execution.json").exists())
            self.assertEqual(result["scores"]["overall_score"], 0.0)

    def test_actual_artifact_failure_with_zero_score_is_not_infrastructure(self):
        with tempfile.TemporaryDirectory() as temporary:
            execution = MockExecution(Path(temporary))
            execution.grade_scores = {"overall_score": 0.0, "error": "Required task output artifact was not produced"}
            result = execution.execute()
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["score"], 0.0)
            self.assertEqual(result["scoring_parse_failures"], [])

    def test_model_judge_regex_fallback_keeps_a_finite_score_pending(self):
        with tempfile.TemporaryDirectory() as temporary:
            execution = MockExecution(Path(temporary))
            execution.grade_scores = {"overall_score": 0.8, "llm_quality_judge_method": "regex_fallback"}
            execution.grade_events = [{"fingerprint": "request", "kind": "model_api", "status": 200}]
            result = execution.execute()
            self.assertEqual(result["status"], "pending_score")
            self.assertIsNone(result["score"])
            self.assertTrue(result["scoring_parse_failures"])

    def test_http_200_with_empty_scoring_completion_is_pending(self):
        with tempfile.TemporaryDirectory() as temporary:
            execution = MockExecution(Path(temporary))
            execution.grade_events = [
                {
                    "fingerprint": "request",
                    "kind": "model_api",
                    "status": 200,
                    "error": "Scoring model returned empty assistant content",
                }
            ]
            result = execution.execute()
            self.assertEqual(result["status"], "pending_score")
            self.assertIsNone(result["score"])

    def test_successful_retry_resolves_matching_prior_scoring_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            execution = MockExecution(Path(temporary))
            execution.grade_scores = {"overall_score": 0.75}
            execution.grade_events = [
                {"fingerprint": "same-request", "kind": "model_api", "status": 503},
                {"fingerprint": "same-request", "kind": "model_api", "status": 200},
            ]
            result = execution.execute()
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["score"], 0.75)
            self.assertEqual(result["scoring_api_failures"], [])

    def test_pending_rescore_reuses_execution_and_never_reruns_agent(self):
        with tempfile.TemporaryDirectory() as temporary:
            execution = MockExecution(Path(temporary))
            execution.grade_scores = {"error": "grading transport failed"}
            first = execution.execute()
            self.assertEqual(first["status"], "pending_score")
            self.assertEqual(execution.agent_calls, 1)
            container = first["artifacts"]["preserved_container"]
            self.assertFalse(any(e[0] == "docker" and e[1][0] == "rm" for e in execution.events))
            execution.grade_scores = {"overall_score": 0.8}
            resumed = execution.execute(rescore=True)
            self.assertEqual(resumed["status"], "completed")
            self.assertEqual(resumed["score"], 0.8)
            self.assertEqual(execution.agent_calls, 1)
            self.assertEqual(resumed["agent_wall_seconds"], first["agent_wall_seconds"])
            self.assertGreaterEqual(resumed["scoring_wall_seconds"], 0)
            launches = [e for e in execution.events if e[0] == "docker" and e[1][0] == "run"]
            grades = [e for e in execution.events if e[0] == "grade"]
            self.assertEqual(len(launches), 1, "Rescore must reuse the still-live mock state")
            self.assertEqual([e[1] for e in grades], [container, container])
            self.assertTrue(any(e[0] == "docker" and e[1] == ["rm", "--force", container] for e in execution.events))
            self.assertEqual(len([e for e in execution.events if e[0] == "adapters"]), 1)

    def test_lost_pending_container_requires_replay_only_without_rerunning_agent(self):
        with tempfile.TemporaryDirectory() as temporary:
            execution = MockExecution(Path(temporary))
            execution.grade_scores = {"error": "transport failed"}
            self.assertEqual(execution.execute()["status"], "pending_score")
            execution.live_container = False
            execution.grade_scores = {"overall_score": 0.8}
            resumed = execution.execute(rescore=True)
            self.assertEqual(resumed["status"], "completed")
            self.assertEqual(execution.agent_calls, 1)
            grades = [e for e in execution.events if e[0] == "grade"]
            self.assertFalse(grades[0][2]["replay_only"])
            self.assertTrue(grades[1][2]["replay_only"], "Missing live mock memory must not be queried as fresh state")
            self.assertEqual(len([e for e in execution.events if e[0] == "docker" and e[1][0] == "run"]), 2)
            self.assertEqual(len([e for e in execution.events if e[0] == "adapters"]), 2)
            second_launch = [e[1] for e in execution.events if e[0] == "docker" and e[1][0] == "run"][1]
            self.assertIn(f"type=bind,src={execution.workspace / 'exec'},dst=/app,readonly", second_launch)
            self.assertIn(
                f"type=bind,src={execution.output / 'workspace'},dst=/previous_output,readonly", second_launch
            )

    def test_rescore_rejects_execution_for_another_task(self):
        with tempfile.TemporaryDirectory() as temporary:
            execution = MockExecution(Path(temporary))
            execution.grade_scores = {"error": "pending grade"}
            self.assertEqual(execution.execute()["status"], "pending_score")
            marker = execution.output / "execution.json"
            saved = json.loads(marker.read_text())
            saved["task_id"] = "different-task"
            marker.write_text(json.dumps(saved))
            result = execution.execute(rescore=True)
            self.assertEqual(result["status"], "infrastructure_error")
            self.assertIsNone(result["score"])
            self.assertIn("different task", result["error"])
            self.assertEqual(execution.agent_calls, 1)
            self.assertEqual(len([e for e in execution.events if e[0] == "grade"]), 1)

    def test_skill_membership_retains_full_reads_and_excludes_same_named_image_helper(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            (output / "shared-skill-names.json").write_text(json.dumps(["video-frames"]))
            entries = []
            for number, path in enumerate(
                (
                    "/usr/lib/node_modules/openclaw/skills/video-frames/SKILL.md",
                    "/root/skills/video-frames/SKILL.md",
                    "/root/skills/frozen-helper/SKILL.md",
                )
            ):
                entries.append(
                    {
                        "message": {
                            "role": "assistant",
                            "content": [
                                {"type": "toolCall", "id": str(number), "name": "read", "arguments": {"path": path}}
                            ],
                        }
                    }
                )
                entries.append(
                    {
                        "message": {
                            "role": "toolResult",
                            "toolCallId": str(number),
                            "content": [{"type": "text", "text": "actual read"}],
                            "isError": False,
                        }
                    }
                )
            (output / "chat.jsonl").write_text("\n".join(json.dumps(e) for e in entries) + "\n")
            session = runtime._session(output, {"task_id": "task", "prompt": "original"})
            self.assertEqual(session["actual_used_skill_refs"], ["video-frames"])
            self.assertEqual(len(session["actual_skill_reads"]), 3)
            shared = [r for r in session["actual_skill_reads"] if r["environment_or_shared"] == "shared"]
            self.assertEqual([r["path"] for r in shared], ["/root/skills/video-frames/SKILL.md"])

    def test_successful_tilde_and_parent_alias_reads_keep_original_path_and_shared_membership(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            (output / "shared-skill-names.json").write_text(json.dumps(["03_task6"]))
            paths = (
                "~/skills/03_task6/SKILL.md",
                "/root/skills/unused/../03_task6/SKILL.md",
                "~/skills/../../usr/lib/node_modules/openclaw/skills/03_task6/SKILL.md",
            )
            entries = []
            for number, path in enumerate(paths):
                entries.append(
                    {
                        "message": {
                            "role": "assistant",
                            "content": [
                                {"type": "toolCall", "id": str(number), "name": "read", "arguments": {"path": path}}
                            ],
                        }
                    }
                )
                entries.append(
                    {
                        "message": {
                            "role": "toolResult",
                            "toolCallId": str(number),
                            "content": [{"type": "text", "text": "actual tool read succeeded"}],
                            "isError": False,
                        }
                    }
                )
            (output / "chat.jsonl").write_text("\n".join(json.dumps(e) for e in entries) + "\n")
            session = runtime._session(output, {"task_id": "task", "prompt": "original"})
            self.assertEqual(session["actual_used_skill_refs"], ["03_task6"])
            self.assertEqual(session["messages"], entries)
            reads = session["actual_skill_reads"]
            shared = [r for r in reads if r["environment_or_shared"] == "shared"]
            self.assertTrue(shared)
            self.assertTrue(all(r["path"] == "/root/skills/03_task6/SKILL.md" for r in shared))
            originals = {r["original_path"] for r in shared}
            self.assertTrue(set(paths[:2]).issubset(originals))
            external = [r for r in reads if r["environment_or_shared"] == "environment"]
            self.assertTrue(any(r["original_path"] == paths[2] for r in external))

    def test_invalid_score_values_are_pending_not_completed(self):
        for score in (float("nan"), float("inf"), -0.1, 1.1, "not a score", True):
            with self.subTest(score=score), tempfile.TemporaryDirectory() as temporary:
                execution = MockExecution(Path(temporary))
                execution.grade_scores = {"overall_score": score}
                result = execution.execute()
                self.assertEqual(result["status"], "pending_score")
                self.assertIsNone(result["score"])

    def test_warmup_failure_is_infrastructure_and_never_starts_agent(self):
        with tempfile.TemporaryDirectory() as temporary:
            execution = MockExecution(Path(temporary))
            execution.warmup_returncode = 1
            result = execution.execute()
            self.assertEqual(result["status"], "infrastructure_error")
            self.assertIsNone(result["score"])
            self.assertEqual(execution.agent_calls, 0)
            self.assertFalse((execution.output / "execution.json").exists())

    def test_fresh_task_executions_use_distinct_clean_containers(self):
        with tempfile.TemporaryDirectory() as temporary:
            execution = MockExecution(Path(temporary))
            execution.execute()
            execution.output = Path(temporary) / "second-output"
            execution.output.mkdir()
            execution.execute()
            launches = [e[1] for e in execution.events if e[0] == "docker" and e[1][0] == "run"]
            self.assertEqual(len(launches), 2)
            names = [args[args.index("--name") + 1] for args in launches]
            self.assertEqual(len(set(names)), 2)


class SourceProvenanceTests(unittest.TestCase):
    def test_two_independent_python_startups_have_identical_seeded_random_sequences(self):
        # Real stdlib Python startups, no Docker, services, or inference.
        with tempfile.TemporaryDirectory() as temporary:
            config = {"experiment_root": temporary, "runtime_dir": str(Path(temporary) / "runtime"), "seed": 42}
            bootstrap = environment.python_bootstrap_source(config)
            self.assertEqual(bootstrap.name, "sitecustomize.py")
            self.assertIn("random.seed(42)", bootstrap.read_text())
            env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(bootstrap.parent), "PYTHONHASHSEED": "42"}
            probe = """import json, random, sitecustomize
draws=[random.random() for _ in range(1000)]
print(json.dumps({'draws':draws, 'injected_errors':[x<0.2 for x in draws],
                  'choices':random.choices(['slow','reject','success'], weights=[1,1,8], k=100),
                  'bootstrap':sitecustomize.__file__, 'hash':hash('sharedskill-probe')}))
"""
            results = [
                json.loads(
                    subprocess.run(
                        [sys.executable, "-c", probe],
                        env=env,
                        cwd=temporary,
                        capture_output=True,
                        text=True,
                        check=True,
                        timeout=10,
                    ).stdout
                )
                for _ in range(2)
            ]
            self.assertEqual(results[0], results[1])
            self.assertEqual(Path(results[0]["bootstrap"]).resolve(), bootstrap.resolve())
            self.assertEqual(results[0]["draws"][0], 0.6394267984578837)
            self.assertGreater(sum(results[0]["injected_errors"]), 0, "Seeding must not disable random error injection")
            self.assertLess(sum(results[0]["injected_errors"]), len(results[0]["draws"]))

    def test_dirty_tracked_files_rejected_even_when_head_is_pinned(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = {"experiment_root": temporary}
            source = environment.paths(config)["upstream"]
            (source / ".git").mkdir(parents=True)

            def git(args, **kwargs):
                if "rev-parse" in args:
                    return completed(environment.BENCHMARK_REVISION + "\n")
                if "diff" in args or "status" in args:
                    return completed("tasks/01_modified.md\n")
                self.fail(f"Unexpected subprocess: {args}")

            with patch.object(environment, "_run", side_effect=git):
                with self.assertRaisesRegex(RuntimeError, "modifi"):
                    environment.prepare_sources(config)


if __name__ == "__main__":
    unittest.main()
