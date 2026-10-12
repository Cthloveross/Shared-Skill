"""Ensure pending grades and the modified OCR target are reported separately."""

import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import reports
from protocol import write_json


class ReportTests(unittest.TestCase):
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
            for arm in ("S0", "S6"):
                for i, task in enumerate(tasks):
                    score = 0.4 if arm == "S0" else (0.2 if i == 0 else 0.5)
                    record = {
                        **task,
                        "phase": "endpoint",
                        "round": 6,
                        "arm": arm,
                        "experiment_record": True,
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
                    write_json(root / "executions" / arm / str(i) / "result.json", record)
            with patch.object(reports, "update_experiment_status"):
                summary = reports.generate(root, tasks)
            self.assertEqual(summary["paired_graded"], 59)
            self.assertEqual(summary["unchanged_prompt_pairs"], 58)
            self.assertAlmostEqual(summary["unchanged_prompt_delta"], 0.1)
            self.assertAlmostEqual(summary["ocr_variant"]["delta"], -0.2)
            self.assertNotIn("终点 S6−S0：", (root / "report/REPORT.md").read_text())
            self.assertEqual(summary["phase_totals"]["endpoint"]["usage"]["total_tokens"], 1800)
            with (root / "report/tasks.csv").open() as stream:
                self.assertEqual(len(list(csv.DictReader(stream))), 120)


if __name__ == "__main__":
    unittest.main()
