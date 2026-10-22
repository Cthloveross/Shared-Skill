"""Offline checks for synthetic evidence and its native evolver boundary."""

from __future__ import annotations

import copy
import json
import unittest
from unittest.mock import AsyncMock, patch

from evolve_server.pipeline.execution import (
    _EVOLVE_FROM_SESSIONS_SYSTEM,
    _build_session_evidence,
)
from evolve_server.pipeline.summarizer import build_session_trajectory
from scripts.injection.trajectory import (
    capture_request,
    inject,
    load_catalog,
    make_batch,
    make_session,
    make_suite,
)


class TrajectoryFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cases = load_catalog()

    def test_all_twelve_cases_have_unique_inert_markers(self) -> None:
        self.assertEqual(len(self.cases), 12)
        markers = {case["marker"] for case in self.cases.values()}
        self.assertEqual(len(markers), 12)
        for case in self.cases.values():
            with self.subTest(case=case["id"]):
                self.assertTrue(case["parameters"]["synthetic"])
                self.assertEqual(case["parameters"]["execution"], "record_only")

    def test_injection_preserves_original_turns_and_input(self) -> None:
        original = make_session(0)
        original["custom_metadata"] = {"retained": ["input provenance"]}
        before = copy.deepcopy(original)
        case = next(iter(self.cases.values()))

        injected = inject(original, case, insertion_index=1)

        self.assertEqual(original, before)
        self.assertEqual(len(injected["turns"]), len(original["turns"]) + 2)
        self.assertEqual(injected["turns"][:1], original["turns"][:1])
        self.assertEqual(injected["turns"][3:], original["turns"][1:])
        self.assertEqual(injected["custom_metadata"], original["custom_metadata"])
        self.assertEqual(injected["session_id"], original["session_id"])
        self.assertEqual(injected["num_turns"], len(injected["turns"]))

    def test_marker_first_occurs_in_failed_tool_feedback(self) -> None:
        for case in self.cases.values():
            with self.subTest(case=case["id"]):
                session = inject(make_session(0), case)
                marker = case["marker"]
                self.assertTrue(all(marker not in turn.get("prompt_text", "") for turn in session["turns"]))
                carrying = [turn for turn in session["turns"] if marker in json.dumps(turn)]
                self.assertTrue(carrying)
                first = carrying[0]
                self.assertNotIn(marker, json.dumps(first.get("tool_calls", [])))
                matching = [result for result in first.get("tool_results", []) if marker in result.get("content", "")]
                self.assertEqual(len(matching), 1)
                self.assertTrue(matching[0]["has_error"])
                self.assertIn(case["constraint"], matching[0]["content"])
                self.assertTrue(session["_has_tool_errors"])
                self.assertTrue(first.get("tool_errors"))

    def test_native_formatter_keeps_complete_payload_and_error_metadata(self) -> None:
        for case in self.cases.values():
            with self.subTest(case=case["id"]):
                session = inject(make_session(0), case)
                native = build_session_trajectory(session)
                self.assertEqual(session["_trajectory"], native)
                self.assertIn(case["constraint"], native)
                self.assertIn(case["action"], native)
                self.assertIn(case["success"], native)
                operation_calls = [
                    call
                    for turn in session["turns"]
                    for call in turn.get("tool_calls", [])
                    if call["function"]["name"] == case["source_function"]
                ]
                self.assertEqual(len(operation_calls), 1)
                arguments = operation_calls[0]["function"]["arguments"]
                self.assertLessEqual(len(arguments), 400)
                self.assertIn(arguments, native)
                evidence = _build_session_evidence([session])
                self.assertIn(native, evidence)
                self.assertIn("has tool errors", evidence)
                self.assertIn("✗", evidence)
                self.assertIn("✓", evidence)
                self.assertEqual(set(session["_skills_referenced"]), {"xlsx"})

    def test_added_validation_checks_the_original_output_path(self) -> None:
        for case in self.cases.values():
            for condition in ("poisoned", "legitimate_repair"):
                with self.subTest(case=case["id"], condition=condition):
                    original = make_session(0)
                    expected_path = json.loads(original["turns"][-1]["tool_calls"][0]["function"]["arguments"])["path"]

                    session = inject(original, case, condition=condition)

                    validation = session["turns"][-3]["tool_calls"][0]["function"]
                    self.assertEqual(validation["name"], "validate_workbook")
                    self.assertEqual(json.loads(validation["arguments"])["path"], expected_path)
                    self.assertEqual(session["turns"][-1], original["turns"][-1])

    def test_session_without_validation_path_requires_an_explicit_path(self) -> None:
        original = make_session(0)
        original["turns"][-1]["tool_calls"][0]["function"]["name"] = "other_tool"
        case = next(iter(self.cases.values()))
        with self.assertRaisesRegex(ValueError, "output_path"):
            inject(original, case)

        session = inject(original, case, output_path="fixture://workbooks/explicit-output.xlsx")

        validation = session["turns"][-3]["tool_calls"][0]["function"]
        self.assertEqual(json.loads(validation["arguments"])["path"], "fixture://workbooks/explicit-output.xlsx")
        self.assertEqual(original["turns"][-1]["tool_calls"][0]["function"]["name"], "other_tool")

    def test_summary_none_cannot_reuse_stale_analysis(self) -> None:
        session = make_session(0)
        session["_summary"] = "STALE_ANALYSIS_FROM_A_DIFFERENT_TRAJECTORY"
        session["_trajectory"] = "STALE_TRAJECTORY"
        case = next(iter(self.cases.values()))

        result = inject(session, case, summary="none")

        self.assertFalse(result.get("_summary"))
        evidence = _build_session_evidence([result])
        self.assertNotIn("STALE_ANALYSIS", evidence)
        self.assertNotIn("STALE_TRAJECTORY", evidence)
        self.assertIn(case["marker"], evidence)

    def test_inserted_steps_do_not_invent_scores_or_reuse_old_outcome(self) -> None:
        original = make_session(0)
        original["aggregate"] = {"mean_score": 1.0, "success_count": 1}
        original["outcome"] = "success"
        original["turns"][0]["prm_score"] = 0.75
        case = next(iter(self.cases.values()))

        session = inject(original, case, insertion_index=1)

        self.assertNotIn("aggregate", session)
        self.assertNotIn("outcome", session)
        self.assertEqual(session["turns"][0]["prm_score"], 0.75)
        self.assertTrue(all("prm_score" not in turn for turn in session["turns"][1:3]))
        self.assertEqual(session["_avg_prm"], 0.75)

    def test_batch_keeps_the_other_twenty_seven_sessions_identical(self) -> None:
        case = next(iter(self.cases.values()))
        poisoned, selected = make_batch(case, total=30, support=3, seed=7)
        clean, clean_selected = make_batch(case, total=30, support=3, seed=7, condition="clean")
        self.assertEqual(selected, clean_selected)
        self.assertEqual(len(set(selected)), 3)
        self.assertEqual(len(poisoned), 30)
        self.assertEqual([session["session_id"] for session in poisoned], [session["session_id"] for session in clean])
        for attacked, control in zip(poisoned, clean, strict=True):
            with self.subTest(session=attacked["session_id"]):
                if attacked["session_id"] in selected:
                    self.assertIn(case["marker"], attacked["_trajectory"])
                    self.assertNotIn(case["marker"], control["_trajectory"])
                else:
                    self.assertEqual(attacked, control)

    def test_benign_controls_do_not_include_target_action(self) -> None:
        for case in self.cases.values():
            for condition in ("quoted", "legitimate_repair"):
                with self.subTest(case=case["id"], condition=condition):
                    session = inject(make_session(0), case, condition=condition)
                    self.assertNotIn(case["action"], session["_trajectory"])
                    recovery_names = [
                        call.get("function", {}).get("name")
                        for turn in session["turns"]
                        for call in turn.get("tool_calls", [])
                    ]
                    self.assertNotIn(case["source_function"], recovery_names)

    def test_suite_labels_are_separate_from_session_evidence(self) -> None:
        records = make_suite()
        self.assertEqual(len(records), 48)
        self.assertEqual(len({record["id"] for record in records}), 48)
        self.assertEqual({record["case_id"] for record in records}, set(self.cases))
        self.assertEqual(
            {record["condition"] for record in records}, {"clean", "poisoned", "quoted", "legitimate_repair"}
        )
        for record in records:
            with self.subTest(record=record["id"]):
                session = record["session"]
                self.assertNotIn("expected_decision", session)
                self.assertNotIn("condition", session)
                self.assertNotIn("case_id", session)
                self.assertNotIn("defense_expectation", json.dumps(session, default=list))
                expected = "quarantine" if record["condition"] == "poisoned" else "allow"
                self.assertEqual(record["expected_decision"], expected)

    def test_empty_and_invalid_inputs_are_rejected(self) -> None:
        case = next(iter(self.cases.values()))
        bad_sessions = ({}, {"session_id": "empty", "turns": []}, {"session_id": "bad", "turns": "not-a-list"})
        for session in bad_sessions:
            with self.subTest(session=session):
                with self.assertRaises((ValueError, TypeError)):
                    inject(session, case)
        for kwargs in ({"total": 0}, {"total": 31}, {"support": -1}, {"total": 2, "support": 3}, {"condition": "typo"}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises((ValueError, TypeError)):
                    make_batch(case, **kwargs)
        for index in (-1, len(make_session(0)["turns"]) + 1):
            with self.subTest(insertion_index=index):
                with self.assertRaises((ValueError, TypeError)):
                    inject(make_session(0), case, insertion_index=index)
        with self.assertRaises((ValueError, TypeError)):
            inject(make_session(0), case, summary="unsupported")
        aggregated = make_session(0)
        aggregated["turns"][0]["_rollout_idx"] = 0
        with self.assertRaises((ValueError, TypeError)):
            inject(aggregated, case)


class NativeCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_capture_uses_native_messages_without_a_model_call(self) -> None:
        case = next(iter(load_catalog().values()))
        sessions, _ = make_batch(case, total=3, support=1)
        before = copy.deepcopy(sessions)
        with patch(
            "evolve_server.core.llm_client.AsyncLLMClient.chat",
            new=AsyncMock(side_effect=AssertionError("network client called")),
        ) as chat:
            captured = await capture_request(sessions)
        chat.assert_not_called()
        self.assertEqual(sessions, before)
        self.assertTrue(captured["dry_run"])
        self.assertFalse(captured["llm_evaluated"])
        self.assertEqual([message["role"] for message in captured["messages"]], ["system", "user"])
        self.assertEqual(
            captured["messages"][0]["content"], _EVOLVE_FROM_SESSIONS_SYSTEM.replace("{skill_name}", "xlsx")
        )
        self.assertIn(_build_session_evidence(sessions), captured["messages"][1]["content"])
        self.assertEqual(captured["parameters"]["max_tokens"], 8192)
        self.assertEqual(captured["parameters"]["temperature"], 0.4)

    async def test_empty_capture_is_rejected(self) -> None:
        with self.assertRaises((ValueError, TypeError)):
            await capture_request([])


if __name__ == "__main__":
    unittest.main()
