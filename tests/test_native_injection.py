"""Offline native-adapter checks; all HTTP requests use MockTransport."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from scripts.injection.native import _effective, _markers, run_native
from scripts.injection.trajectory import INITIAL_SKILL, load_catalog


class NativeRunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.case = next(iter(load_catalog()))

    async def run_fixture(self, content=None, *, status=200, reasoning=None, name="run"):
        requests = []

        def handler(request):
            body = json.loads(request.content)
            self.assertEqual(body["max_tokens"], 8192)
            self.assertEqual(body["temperature"], 0.4)
            self.assertEqual(body["model"], "offline-fixture")
            requests.append(body)
            message = {"content": content, "reasoning_content": reasoning}
            return httpx.Response(status, json={"choices": [{"message": message}]})

        original_client = httpx.AsyncClient
        with patch(
            "scripts.injection.native.httpx.AsyncClient",
            side_effect=lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs),
        ):
            report = await run_native(
                self.root / name, "http://fixture.invalid/v1", "offline-fixture", [self.case], total=3, support=1
            )
        return report, requests

    async def test_missing_and_unknown_actions_are_invalid(self):
        for index, response in enumerate(({}, {"action": "unknown", "skill": {"content": "body"}})):
            with self.subTest(response=response):
                report, requests = await self.run_fixture(json.dumps(response), name=f"invalid-{index}")
                self.assertEqual(len(requests), 2)
                self.assertEqual(report["invalid"], 2)
                self.assertEqual(report["calls_completed"], 0)
                self.assertEqual(report["status"], "finished_with_gaps")
                self.assertIsNone(report["attack_success"])

    async def test_reasoning_without_text_is_invalid(self):
        report, requests = await self.run_fixture(reasoning="Reasoning alone is not a skill response.")
        self.assertEqual(len(requests), 2)
        self.assertEqual(report["invalid"], 2)
        self.assertEqual(report["calls_completed"], 0)
        self.assertTrue(report["llm_evaluated"])
        self.assertTrue((self.root / "run" / self.case / "poisoned" / "response.json").is_file())

    async def test_http_failure_is_error_without_retries(self):
        report, requests = await self.run_fixture(status=503)
        self.assertEqual(len(requests), 2)
        self.assertEqual(report["errors"], 2)
        self.assertEqual(report["invalid"], 0)
        self.assertEqual(report["calls_completed"], 0)
        self.assertFalse(report["llm_evaluated"])
        self.assertEqual(report["retries"], 0)

    def test_description_optimization_inherits_the_original_body(self):
        parsed = {
            "action": "optimize_description",
            "rationale": "Improve matching.",
            "skill": {
                "name": "xlsx",
                "description": "Inspect requested workbooks.",
                "content": "This body must be ignored.",
                "category": "changed",
            },
        }
        effective = _effective(json.dumps(parsed), parsed)
        self.assertEqual(effective["content"], INITIAL_SKILL["content"])
        self.assertEqual(effective["category"], INITIAL_SKILL["category"])
        self.assertEqual(effective["description"], parsed["skill"]["description"])

    def test_rationale_marker_is_not_added_skill_guidance(self):
        marker = load_catalog()[self.case]["marker"]
        parsed = {"action": "improve_skill", "rationale": f"Rejected evidence mentioning {marker}."}
        evaluation = _markers(copy.deepcopy(INITIAL_SKILL), parsed, marker)
        self.assertTrue(evaluation["marker_in_rationale"])
        self.assertFalse(evaluation["marker_added_content"])
        self.assertFalse(evaluation["marker_added_description"])
        self.assertEqual(evaluation["added_marker_lines"], [])
        self.assertNotEqual(evaluation["disposition"], "affirmative_candidate")
        self.assertIsNone(evaluation["attack_success"])

    async def test_existing_output_is_preserved_without_another_request(self):
        report, requests = await self.run_fixture('{"action":"skip","rationale":"Offline fixture."}')
        path = self.root / "run"
        before = (path / "run.json").read_bytes()
        with self.assertRaises(FileExistsError):
            await run_native(path, "http://fixture.invalid/v1", "offline-fixture", [self.case])
        self.assertEqual((path / "run.json").read_bytes(), before)
        self.assertEqual(len(requests), 2)
        self.assertEqual(report["status"], "finished")
        self.assertEqual(report["calls_finished"], 2)
        self.assertFalse(report["source_changed"])

    async def test_native_run_sends_unexecuted_message_without_fictitious_turns(self):
        report, requests = await self.run_fixture('{"action":"skip","rationale":"Offline fixture."}')
        case = load_catalog()[self.case]
        folder = self.root / "run" / self.case
        clean = json.loads((folder / "clean" / "sessions.json").read_text())
        poisoned = json.loads((folder / "poisoned" / "sessions.json").read_text())
        changed = 0
        for before, after in zip(clean, poisoned, strict=True):
            self.assertEqual(len(after["turns"]), len(before["turns"]))
            self.assertEqual([t["tool_calls"] for t in after["turns"]], [t["tool_calls"] for t in before["turns"]])
            if after != before:
                changed += 1
                self.assertIn(case["message"], after["turns"][-1]["tool_results"][0]["content"])
                self.assertFalse(after["_injection_diagnostic"]["continuation_replayed"])
                self.assertEqual(after["_injection_diagnostic"]["execution_status"], "unknown")
                self.assertEqual(after["_summary"], "")
        self.assertEqual(changed, 1)
        self.assertEqual(sum(case["message"] in r["messages"][1]["content"] for r in requests), 1)
        self.assertIn("synthetic trajectory-only", report["scope"])
        self.assertIsNone(report["attack_success"])
        for row in report["pairs"][self.case].values():
            self.assertEqual(row["evaluation"]["metric_status"], "not_applicable")
            self.assertEqual(row["evaluation"]["disposition"], "unknown")
            self.assertIsNone(row["evaluation"]["textual_adoption"])
            self.assertIsNone(row["evaluation"]["marker_added_content"])


if __name__ == "__main__":
    unittest.main()
