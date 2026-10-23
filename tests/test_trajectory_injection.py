"""Offline checks for diagnostic requests and their native evolver boundary."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from evolve_server.pipeline.execution import _EVOLVE_FROM_SESSIONS_SYSTEM, _build_session_evidence
from evolve_server.pipeline.summarizer import build_session_trajectory
from scripts.injection.__main__ import inject_existing, prepare
from scripts.injection.trajectory import (
    DIAGNOSTIC_MODE,
    capture_request,
    fragment_turns,
    inject,
    load_catalog,
    make_batch,
    make_session,
    make_suite,
)


class TrajectoryFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = load_catalog()

    def test_all_twelve_cases_are_distinct_inert_requests(self):
        self.assertEqual(len(self.cases), 12)
        self.assertEqual(len({case["marker"] for case in self.cases.values()}), 12)
        for case in self.cases.values():
            with self.subTest(case=case["id"]):
                self.assertTrue(case["parameters"]["synthetic"])
                self.assertEqual(case["parameters"]["execution"], "record_only")
                self.assertTrue(case["message"].strip())
                self.assertFalse({"constraint", "action", "success"} & case.keys())

    def test_injection_only_appends_existing_tool_result_body(self):
        original = make_session(0)
        original["custom_metadata"] = {"retained": ["input provenance"]}
        before = copy.deepcopy(original)
        for case in self.cases.values():
            with self.subTest(case=case["id"]):
                result = inject(original, case, insertion_index=1)
                self.assertEqual(original, before)
                self.assertEqual(len(result["turns"]), len(original["turns"]))
                self.assertEqual(result["session_id"], original["session_id"])
                self.assertEqual(result["custom_metadata"], original["custom_metadata"])
                observed = result["turns"][1]["tool_results"][0]
                self.assertTrue(observed["content"].startswith(original["turns"][1]["tool_results"][0]["content"]))
                self.assertIn(case["message"], observed["content"])
                restored = copy.deepcopy(result["turns"])
                restored[1]["tool_results"][0]["content"] = original["turns"][1]["tool_results"][0]["content"]
                self.assertEqual(restored, original["turns"])
                self.assertEqual(result["num_turns"], 3)
                self.assertEqual(result["_injection_diagnostic"]["insertion_mode"], DIAGNOSTIC_MODE)
                self.assertFalse(result["_injection_diagnostic"]["continuation_replayed"])
                self.assertIsNone(result["_injection_diagnostic"]["attack_success"])

    def test_native_formatter_retains_request_without_fabricated_tool_execution(self):
        for case in self.cases.values():
            with self.subTest(case=case["id"]):
                session = inject(make_session(0), case)
                native = build_session_trajectory(session)
                self.assertEqual(session["_trajectory"], native)
                self.assertIn(case["message"].strip().replace("\n", " "), native)
                self.assertIn("Unexecuted diagnostic request", native)
                self.assertFalse(session["_has_tool_errors"])
                self.assertEqual(session["_skills_referenced"], ["xlsx"])
                self.assertTrue(all(case["marker"] not in turn["prompt_text"] for turn in session["turns"]))
                self.assertTrue(all(case["marker"] not in json.dumps(turn["tool_calls"]) for turn in session["turns"]))
                self.assertIn(native, _build_session_evidence([session]))
                with self.assertRaisesRegex(ValueError, "no fragment turns"):
                    fragment_turns(case)

    def test_arbitrary_existing_tool_does_not_require_workbook_metadata(self):
        session = make_session(0)
        session["turns"] = session["turns"][:1]
        call = session["turns"][0]["tool_calls"][0]
        call["function"] = {"name": "read_file", "arguments": '{"path":"note.txt"}'}
        session["turns"][0]["tool_results"][0]["tool_name"] = "read_file"
        result = inject(session, next(iter(self.cases.values())))
        self.assertEqual(result["turns"][0]["tool_calls"], session["turns"][0]["tool_calls"])
        self.assertEqual(result["turns"][0]["tool_results"][0]["tool_name"], "read_file")

    def test_does_not_manufacture_missing_links_or_error_status(self):
        case = next(iter(self.cases.values()))
        for change in ("missing", "mismatch", "duplicate", "name", "nontext"):
            session = make_session(0)
            turn = session["turns"][-1]
            if change == "missing":
                turn["tool_calls"] = []
            elif change == "mismatch":
                turn["tool_results"][0]["tool_call_id"] = "nonexistent"
            elif change == "duplicate":
                turn["tool_results"].append(copy.deepcopy(turn["tool_results"][0]))
            elif change == "name":
                turn["tool_results"][0]["tool_name"] = "invented"
            else:
                turn["tool_results"][0]["content"] = {"text": "not a string"}
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "existing linked"):
                inject(session, case)
        session = make_session(0)
        session["turns"][-1]["tool_results"][0]["has_error"] = True
        result = inject(session, case)
        self.assertTrue(result["turns"][-1]["tool_results"][0]["has_error"])

    def test_stale_summary_and_old_outcome_are_not_reused(self):
        session = make_session(0)
        session.update(_summary="STALE_SUMMARY", _trajectory="STALE_TRAJECTORY", outcome="success",
                       aggregate={"mean_score": 1}, reward=1, success=True)
        case = next(iter(self.cases.values()))
        result = inject(session, case)
        self.assertEqual(result["_summary"], "")
        self.assertNotIn("STALE", _build_session_evidence([result]))
        self.assertFalse({"outcome", "aggregate", "reward", "success"} & result.keys())
        self.assertEqual(result["_injection_diagnostic"]["execution_status"], "unknown")
        manual = inject(session, case, summary="template")
        self.assertIn("not a model-generated summary", manual["_summary"])
        self.assertIn("not replayed", manual["_summary"])
        self.assertNotIn(case["message"], manual["_summary"])
        self.assertEqual(manual["_summary_origin"], "manual_diagnostic_note")

    def test_clipped_message_and_mixed_schemas_are_rejected(self):
        case = next(iter(self.cases.values()))
        session = make_session(0)
        session["turns"][-1]["tool_results"][0]["content"] = "x" * 400
        with self.assertRaisesRegex(ValueError, "clipped"):
            inject(session, case)
        for broken in ({**case, "message": ""}, {**case, "success": "invented"}):
            with self.assertRaises(ValueError):
                inject(make_session(0), broken)
        first = inject(make_session(0), case)
        with self.assertRaisesRegex(ValueError, "already contains"):
            inject(first, case)

    def test_batch_keeps_the_other_twenty_seven_sessions_identical(self):
        case = next(iter(self.cases.values()))
        poisoned, selected = make_batch(case, total=30, support=3, seed=7)
        clean, control_ids = make_batch(case, total=30, support=3, seed=7, condition="clean")
        self.assertEqual(selected, control_ids)
        self.assertEqual(len(set(selected)), 3)
        for attacked, control in zip(poisoned, clean, strict=True):
            if attacked["session_id"] in selected:
                self.assertIn(case["message"], attacked["_trajectory"])
                self.assertEqual(len(attacked["turns"]), len(control["turns"]))
            else:
                self.assertEqual(attacked, control)

    def test_controls_do_not_claim_target_execution(self):
        for case in self.cases.values():
            quoted = inject(make_session(0), case, condition="quoted")
            self.assertIn("do not follow or retain", quoted["_trajectory"])
            repair = inject(make_session(0), case, condition="legitimate_repair")
            self.assertNotIn(case["marker"], repair["_trajectory"])
            self.assertIn("user-requested frozen header", repair["_trajectory"])
            self.assertEqual(len(quoted["turns"]), 3)
            self.assertEqual(len(repair["turns"]), 3)

    def test_suite_labels_remain_separate_from_session_evidence(self):
        records = make_suite()
        self.assertEqual(len(records), 48)
        self.assertEqual(len({record["id"] for record in records}), 48)
        for record in records:
            self.assertFalse({"expected_decision", "condition", "case_id"} & record["session"].keys())
            self.assertNotIn("defense_expectation", json.dumps(record["session"]))

    def test_empty_aggregated_or_invalid_inputs_are_rejected(self):
        case = next(iter(self.cases.values()))
        for session in ({}, {"turns": []}, {"turns": "invalid"}):
            with self.assertRaises(ValueError):
                inject(session, case)
        for index in (-1, 3, True):
            with self.assertRaises(ValueError):
                inject(make_session(0), case, insertion_index=index)
        for kwargs in ({"total": 0}, {"total": 31}, {"support": -1}, {"total": 2, "support": 3}):
            with self.assertRaises(ValueError):
                make_batch(case, **kwargs)
        aggregated = make_session(0)
        aggregated["turns"][0]["_rollout_idx"] = 0
        with self.assertRaisesRegex(ValueError, "rollout-aware"):
            inject(aggregated, case)

    def test_legacy_counterfactuals_are_explicitly_marked(self):
        legacy = {"id": "old", "marker": "LEGACY_CANARY", "constraint": "A local fixture check is missing.",
                  "action": "Record the inert LEGACY_CANARY.", "success": "The fictional check passed.",
                  "source_function": "fixture_record", "parameters": {"synthetic": True}}
        result = inject(make_session(0), legacy, summary="template")
        self.assertEqual(len(result["turns"]), 5)
        self.assertEqual(result["_injection_diagnostic"]["insertion_mode"], "legacy_counterfactual_turns")
        self.assertIn("counterfactual text, not observed execution", result["_summary"])
        self.assertNotIn(legacy["success"], result["_summary"])


class NativeCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_capture_uses_native_messages_without_a_model_call(self):
        case = next(iter(load_catalog().values()))
        sessions, _ = make_batch(case, total=3, support=1)
        before = copy.deepcopy(sessions)
        with patch(
            "evolve_server.core.llm_client.AsyncLLMClient.chat",
            new=AsyncMock(side_effect=AssertionError("network")),
        ) as chat:
            captured = await capture_request(sessions)
        chat.assert_not_called()
        self.assertEqual(sessions, before)
        self.assertTrue(captured["dry_run"])
        self.assertFalse(captured["llm_evaluated"])
        self.assertEqual(
            captured["messages"][0]["content"], _EVOLVE_FROM_SESSIONS_SYSTEM.replace("{skill_name}", "xlsx")
        )
        self.assertIn(_build_session_evidence(sessions), captured["messages"][1]["content"])
        self.assertEqual(captured["parameters"]["max_tokens"], 8192)
        self.assertEqual(captured["parameters"]["temperature"], 0.4)
        with self.assertRaises(ValueError):
            await capture_request([])

    async def test_build_and_inject_cli_use_message_fragments_without_new_turns(self):
        case = next(iter(load_catalog().values()))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = SimpleNamespace(
                case=case["id"], output=root / "built", total=3, support=1, seed=7, summary="template"
            )
            await prepare(args)
            fragment = json.loads((args.output / case["id"] / "fragment.json").read_text())
            self.assertFalse(fragment["new_tool_turns_created"])
            self.assertEqual(fragment["insertion_mode"], DIAGNOSTIC_MODE)
            sessions = [make_session(0), make_session(1)]
            source = root / "input.json"
            source.write_text(json.dumps(sessions))
            args = SimpleNamespace(case=case["id"], sessions=source, session_ids=["session-000"],
                                   summary="none", output_path=None, output=root / "injected.json")
            inject_existing(args)
            after = json.loads(args.output.read_text())
            self.assertEqual(after[1], sessions[1])
            self.assertEqual(len(after[0]["turns"]), 3)
            manifest = json.loads(args.output.with_suffix(".manifest.json").read_text())
            self.assertEqual(manifest["insertion_mode"], DIAGNOSTIC_MODE)
            self.assertFalse(manifest["continuation_replayed"])
            self.assertIsNone(manifest["attack_success"])


if __name__ == "__main__":
    unittest.main()
