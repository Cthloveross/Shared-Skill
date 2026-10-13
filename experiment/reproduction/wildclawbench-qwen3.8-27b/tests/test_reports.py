"""Ensure pending grades and the modified OCR target are reported separately."""

import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import reports
from protocol import write_json


class ReportTests(unittest.TestCase):
    def test_thirty_task_report_uses_complete_subset_denominators_and_own_document(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            experiment = root / "new-experiment"
            unrelated = root / "code-root"
            experiment.mkdir()
            unrelated.mkdir()
            (experiment / "EXPERIMENT.md").write_text("# New experiment\n")
            (unrelated / "EXPERIMENT.md").write_text("# Untouched code documentation\n")
            run_dir = experiment / "runtime/runs/test"
            run_dir.parent.mkdir(parents=True)
            (experiment / "runtime/latest-run.txt").write_text(str(run_dir))
            tasks = [
                {"task_id": f"task-{i}", "short_id": f"01.{i:02d}",
                 "category": f"category-{i // 5}", "local_variant": False}
                for i in range(30)
            ]
            write_json(run_dir / "frozen-protocol.json", {"config": {"experiment_root": str(experiment), "days": 6}})
            for phase, arm, score in (("endpoint", "S0", 0.4), ("endpoint", "S6", 0.5), ("day", "", 0.4)):
                for task in tasks:
                    record = {
                        **task, "phase": phase, "round": 6 if phase == "endpoint" else 1, "arm": arm,
                        "experiment_record": True, "score": score, "status": "completed", "wall_seconds": 2,
                        "timed_out": False, "tool_errors": 0,
                    }
                    number = "06" if phase == "endpoint" else "01"
                    write_json(run_dir / "executions" / phase / number / "main" / (arm or "shared") /
                               task["short_id"] / "result.json", record)
            with patch.object(reports, "ROOT", unrelated):
                summary = reports.generate(run_dir, tasks)
            self.assertEqual(summary["expected_tasks"], 30)
            self.assertEqual(summary["fixed_execution_count"], 240)
            self.assertEqual(summary["paired_graded"], 30)
            self.assertAlmostEqual(summary["endpoint_delta"], 0.1)
            text = (run_dir / "report/REPORT.md").read_text()
            self.assertIn("有效评分 30/30", text)
            self.assertIn("保留原题提示 30/30", text)
            self.assertIn("终点完成质量均分 S6−S0：+10.00", text)
            self.assertTrue((run_dir / "report/six-category-curves.png").is_file())
            self.assertIn("终点有效配对：30/30", (experiment / "EXPERIMENT.md").read_text())
            self.assertEqual((unrelated / "EXPERIMENT.md").read_text(), "# Untouched code documentation\n")

    def test_endpoint_pairs_ignore_pending_partial_grades(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tasks = [
                {
                    "task_id": f"task-{i}",
                    "short_id": "02.06" if i == 0 else f"01.{i:02d}",
                    "category": "category",
                    "local_variant": i == 0,
                }
                for i in range(60)
            ]
            evidence = root / "api-evidence"
            write_json(root / "frozen-protocol.json", {"config": {"gateway_log_dir": str(evidence)}})
            for arm in ("S0", "S6"):
                for i, task in enumerate(tasks):
                    score = 0.4 if arm == "S0" else (0.2 if i == 0 else 0.5)
                    record = {
                        **task,
                        "phase": "endpoint",
                        "round": 6,
                        "arm": arm,
                        "experiment_record": True,
                        "run_id": f"{arm}-{i}",
                        "score": score,
                        "status": "completed",
                        "wall_seconds": 2,
                        "timed_out": False,
                        "tool_errors": 0,
                        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15, "request_count": 1},
                        "started_at": "2026-10-12T20:00:00+00:00",
                        "finished_at": "2026-10-12T20:00:02+00:00",
                    }
                    if i == 59 and arm == "S6":
                        record["status"] = "pending_score"  # Even a partial numeric grade must be excluded.
                    write_json(root / "executions/endpoint/06/main" / arm / task["short_id"] / "result.json", record)
                    request = evidence / record["run_id"] / "request-1"
                    write_json(request / "record.json", {"role": "main", "status": "completed"})
                    write_json(
                        request / "response.bin",
                        {
                            "usage": {
                                "prompt_tokens": 10,
                                "completion_tokens": 4,
                                "total_tokens": 14,
                                "completion_tokens_details": {"reasoning_tokens": 1},
                            }
                        },
                    )
            # Agent-created workspace files must not be interpreted as controller records.
            write_json(root / "executions/endpoint/06/main/S0/02.06/workspace/result.json", record)
            with patch.object(reports, "update_experiment_status"):
                summary = reports.generate(root, tasks)
            self.assertEqual(summary["paired_graded"], 59)
            self.assertEqual(summary["unchanged_prompt_pairs"], 58)
            self.assertAlmostEqual(summary["unchanged_prompt_delta"], 0.1)
            self.assertAlmostEqual(summary["ocr_variant"]["delta"], -0.2)
            self.assertNotIn("终点完成质量均分 S6−S0：", (root / "report/REPORT.md").read_text())
            final_stage = next(s for s in summary["stages"] if s["arm"] == "S6")
            self.assertAlmostEqual(final_stage["grading_coverage"], 59 / 60)
            self.assertIsNone(final_stage["full_credit_rate"])
            self.assertIn("（部分）", (root / "report/REPORT.md").read_text())
            self.assertEqual(summary["phase_totals"]["endpoint"]["usage"]["total_tokens"], 1680)
            with (root / "report/tasks.csv").open() as stream:
                self.assertEqual(len(list(csv.DictReader(stream))), 120)

    def test_completed_and_full_credit_are_different_from_task_success(self):
        rows = [
            {"status": "completed", "score": 0, "timed_out": True, "session": {"read_skills": []}},
            {"status": "completed", "score": 1, "session": {"read_skills": ["alpha", "beta"]}},
            {"status": "pending_score", "score": 1},
        ]
        result = reports.summarize(rows)
        self.assertEqual(result["graded"], 2)
        self.assertEqual(result["score"], 0.5)
        self.assertEqual(result["mean_task_score_0_to_100"], 50)
        self.assertEqual(result["zero_score_tasks"], 1)
        self.assertEqual(result["full_credit_tasks"], 1)
        self.assertEqual(result["timeouts"], 1)
        self.assertEqual(result["shared_skill_tasks"], 1)
        self.assertEqual(result["shared_skill_names"], ["alpha", "beta"])

    def test_backend_usage_handles_streams_subcalls_and_cancelled_requests(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            usage = {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
                "completion_tokens_details": {"reasoning_tokens": 3},
            }
            for name, role in (("agent", "main"), ("tool", "main"), ("cancelled", "main"), ("audio", "audio")):
                write_json(root / name / "record.json", {"role": role})
            (root / "agent/response.sse").write_text(
                'data: {"choices":[{"delta":{"content":"answer"}}]}\n\n'
                + "data: "
                + json.dumps({"usage": usage})
                + "\n\ndata: [DONE]\n\n"
            )
            write_json(root / "tool/response.bin", {"usage": usage})
            write_json(root / "audio/response.bin", {"usage": usage})
            (root / "cancelled/response.sse").write_text('data: {"choices":')
            actual = reports.backend_usage(root)
            self.assertEqual(
                actual,
                {
                    "input_tokens": 20,
                    "output_tokens": 10,
                    "total_tokens": 30,
                    "request_count": 3,
                    "measured_request_count": 2,
                    "missing_usage_requests": 1,
                },
            )
            self.assertIsNone(reports.backend_usage(root / "absent"))

    def test_night_timings_use_native_evolution_output_location(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for bank in ("S0", "S1"):
                write_json(root / "banks" / bank / "skill" / "asset.json", {"content": bank})
            write_json(root / "nights/01/publication.json", {"round": 1})
            write_json(root / "nights/01/validation/001-skill/decision.json", {"name": "skill", "accepted": True})
            write_json(root / "nights/01/proposals/workspace/decision.json", {"accepted": False})
            write_json(
                root / "nights/01/proposals/evolution/proposal-summary.json",
                {"elapsed_seconds": 90, "summarization_elapsed_seconds": 30},
            )
            with patch.object(reports, "update_experiment_status"):
                summary = reports.generate(root, [])
            self.assertEqual(summary["nights"][0]["proposal_seconds"], 90)
            self.assertEqual(summary["nights"][0]["summary_seconds"], 30)
            self.assertEqual(summary["nights"][0]["changed_skills"], ["skill"])
            self.assertEqual(summary["candidate_accepts"], 1)
            self.assertEqual(summary["candidate_rejects"], 0)


if __name__ == "__main__":
    unittest.main()
