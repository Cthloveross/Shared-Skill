"""Completion needs fixed evidence, publication hashes and a verified terminal state."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.wildclawbench import reports
from scripts.wildclawbench.protocol import canonical_hash, tree_hash, write_json


class CompletionTests(unittest.TestCase):
    def test_report_discloses_only_its_own_explicit_scope_revision(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            revision = {"policy": "explicit-candidate-sources-v1", "night1_original_validation_executions": 60,
                        "night1_revised_validation_executions": 16}
            write_json(root / "operations/validation-scope-revision.json", revision)
            with patch.object(reports, "update_experiment_status"):
                summary = reports.generate(root, [])
                other = reports.generate(root / "other", [])
            self.assertEqual(summary["validation_scope_revision"], revision)
            self.assertIsNone(other["validation_scope_revision"])
            text = (root / "report/REPORT.md").read_text()
            self.assertIn("Night1 验证执行 60→16 次", text)
            self.assertIn("完整原生成证据组另行保留", text)
            self.assertIn("../operations/validation-scope-revision.json", text)
            self.assertIn("runtime/runs/current/operations/validation-scope-revision.json",
                          "\n".join(reports.progress_lines(summary, "runtime/runs/current/")))

    def fixture(self, root, count=2):
        tasks = [{"task_id": f"task-{i}", "short_id": f"01.{i:02d}", "input_sha256": f"input-{i}"}
                 for i in range(count)]
        config = {"days": 6, "seed": 42}
        frozen = {"config": config, "tasks": tasks, "source_hashes": {}}
        for number in range(7):
            write_json(root / "banks" / f"S{number}" / "skill/asset.json", {"version": number})
        nights = [{"round": n, "bank_sha256": tree_hash(root / "banks" / f"S{n}"),
                   "parent_sha256": tree_hash(root / "banks" / f"S{n - 1}")} for n in range(1, 7)]
        records = []
        for phase, number, arm in [("day", n, "") for n in range(1, 7)] + [
            ("endpoint", 6, "S0"), ("endpoint", 6, "S6")
        ]:
            for task in tasks:
                identity = {"phase": phase, "round": number, "arm": arm, "candidate_id": "",
                            "task_id": task["task_id"], "bank": tree_hash(root / "banks" / (arm or f"S{number - 1}")),
                            "config": canonical_hash(frozen), "input": task["input_sha256"], "seed": 42}
                records.append({**identity, "execution_hash": canonical_hash(identity),
                                "status": "completed", "score": 0, "timed_out": True})
        return tasks, config, frozen, nights, records

    def test_zero_scores_and_timeouts_are_complete_but_unpublished_nights_are_not(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tasks, config, frozen, nights, records = self.fixture(root, 30)
            state = {"status": "complete"}
            result = reports.completion_summary(root, tasks, records, state, nights, config, frozen)
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["fixed_graded"], 240)
            self.assertEqual(result["fixed_expected"], 240)
            self.assertEqual(result["pending_score"], 0)
            unpublished = reports.completion_summary(root, tasks, records, state, [], config, frozen)
            self.assertEqual(unpublished["status"], "in_progress")
            self.assertFalse(unpublished["terminal_verified"])

    def test_full_scores_with_historical_method_gap_are_not_complete(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tasks, config, frozen, nights, records = self.fixture(root, 30)
            gap = {"id": "night1-video-not-discoverable", "kind": "skill_catalog_missing",
                   "skill": "video-transcribe", "status": "historical_gap",
                   "evidence": "audits/catalog.json", "repair_stage": "Night2 pending"}
            declaration = root / "operations/method-integrity-gaps.json"
            write_json(declaration, {"schema_version": 1, "gaps": [gap]})
            result = reports.completion_summary(root, tasks, records, {"status": "complete"}, nights, config, frozen)
            self.assertEqual(result["status"], "finished_with_gaps")
            self.assertTrue(result["terminal_verified"])
            self.assertEqual(result["fixed_graded"], 240)
            self.assertEqual(result["ungraded_fixed"], [])
            self.assertEqual(result["pending_score"], 0)
            self.assertEqual(result["infrastructure_error"], 0)
            self.assertEqual(result["invalid_publications"], [])
            self.assertEqual(result["method_integrity_gaps"], [gap])
            self.assertTrue(all(row["score"] == 0 for row in records))

            # Repairing a future stage preserves the earlier experiment limitation.
            gap.update(status="repaired", repair_stage="Night2 revalidated")
            write_json(declaration, {"schema_version": 1, "gaps": [gap]})
            ongoing = reports.completion_summary(root, tasks, records, {"status": "night"}, nights, config, frozen)
            self.assertEqual(ongoing["status"], "in_progress")
            finished = reports.completion_summary(root, tasks, records, {"status": "complete"}, nights, config, frozen)
            self.assertEqual(finished["status"], "finished_with_gaps")
            summary = {"completion": finished, "stages": []}
            for prefix in ("../", "runtime/runs/current/"):
                progress = "\n".join(reports.progress_lines(summary, prefix))
                self.assertIn("方法部署历史缺口 1 项", progress)
                self.assertIn(prefix + "operations/method-integrity-gaps.json", progress)
                self.assertIn(prefix + "audits/catalog.json", progress)
                self.assertIn("Night2 revalidated", progress)

    def test_malformed_method_gap_declaration_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for malformed in ({"gaps": {}}, {"gaps": ["gap"]}, []):
                write_json(root / "operations/method-integrity-gaps.json", malformed)
                with self.assertRaisesRegex(ValueError, "gaps list of objects"):
                    reports.read_method_integrity_gaps(root)

    def test_gap_end_requires_finished_and_durable_skip_and_preserves_partial_grade(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tasks, config, frozen, nights, records = self.fixture(root)
            records[-1].update(status="pending_score", score=0.7)
            nights[0]["skipped"] = True
            state = {"status": "finished_with_gaps", "finished": True}
            result = reports.completion_summary(root, tasks, records, state, nights, config, frozen)
            self.assertEqual(result["status"], "in_progress")
            write_json(root / "operations/unattended/skips/task.json", {
                "kind": "task", "result_path": str(root / "executions/endpoint/06/main/S6/01.01/result.json")})
            result = reports.completion_summary(root, tasks, records, state, nights, config, frozen)
            self.assertEqual(result["status"], "finished_with_gaps")
            self.assertEqual(result["fixed_graded"], 15)
            self.assertEqual(result["pending_score"], 1)
            self.assertEqual(result["skipped_nights"], [1])
            self.assertEqual(records[-1]["score"], 0.7)
            state["finished"] = False
            self.assertEqual(reports.completion_summary(root, tasks, records, state, nights, config, frozen)["status"],
                             "in_progress")

    def test_wrong_execution_or_publication_hash_cannot_claim_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tasks, config, frozen, nights, records = self.fixture(root)
            records[0]["execution_hash"] = "different-protocol-or-bank"
            with self.assertRaisesRegex(ValueError, "frozen execution identity"):
                reports.completion_summary(root, tasks, records, {"status": "complete"}, nights, config, frozen)
            records[0]["execution_hash"] = canonical_hash({k: records[0][k] for k in (
                "phase", "round", "arm", "candidate_id", "task_id", "bank", "config", "input", "seed")})
            nights[-1]["parent_sha256"] = "different-parent"
            result = reports.completion_summary(root, tasks, records, {"status": "complete"}, nights, config, frozen)
            self.assertEqual(result["status"], "in_progress")
            self.assertEqual(result["invalid_publications"], [6])
            nights[-1]["parent_sha256"] = tree_hash(root / "banks/S5")
            frozen["source_hashes"] = {str(root / "missing-source.py"): "expected-source-hash"}
            for record in records:
                record["config"] = canonical_hash(frozen)
                record["execution_hash"] = canonical_hash({k: record[k] for k in (
                    "phase", "round", "arm", "candidate_id", "task_id", "bank", "config", "input", "seed")})
            result = reports.completion_summary(root, tasks, records, {"status": "complete"}, nights, config, frozen)
            self.assertEqual(result["status"], "in_progress")
            self.assertEqual(result["fixed_graded"], 16)
            self.assertEqual(result["source_mismatches"], [str(root / "missing-source.py")])

    def test_measured_day_timing_precedes_pilot_and_progress_uses_current_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tasks, config, frozen, nights, records = self.fixture(root)
            completion = reports.completion_summary(root, tasks, records[:2], {"status": "night"}, [], config, frozen)
            summary = {"completion": completion, "stages": [
                {"phase": "day", "round": 1, "graded": 2, "expected": 2, "score": 0, "wall_span_seconds": 3000}],
                "pilot_calibration": {"wall_seconds": 1206, "extrapolated_fixed_wall_hours": 13.4},
                "paid_api": {"cost_upper_bound_usd": 0.05, "pending": {"request": 0.02}, "budget_usd": 10}}
            timing = "\n".join(reports.timing_lines(summary))
            self.assertIn("Day1 50.00 分钟", timing)
            self.assertIn("6.67 小时", timing)
            self.assertNotIn("13.4", timing)
            progress = "\n".join(reports.progress_lines(summary))
            self.assertIn("Day6 0/2", progress)
            self.assertIn("夜间发布 0/6", progress)
            self.assertIn("$0.0500", progress)
            self.assertIn("$0.0200", progress)
            self.assertNotIn("完整结束", progress)


if __name__ == "__main__":
    unittest.main()
