"""Reporting must never overwrite a different experiment or its current run."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.wildclawbench import reports
from scripts.wildclawbench.protocol import write_json


class DocumentOwnershipTests(unittest.TestCase):
    def tasks(self):
        return [{"task_id": f"task-{i}", "short_id": f"01.{i:02d}",
                 "category": "test", "local_variant": False} for i in range(60)]

    def test_missing_frozen_config_uses_data_folder_and_keeps_engine_document(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            owner, engine = base / "experiment", base / "engine"
            owner.mkdir(); engine.mkdir()
            (owner / "README.md").write_text("# Owner\n")
            (engine / "README.md").write_text("# Engine\n")
            run_dir = owner / "runtime/runs/test"
            run_dir.parent.mkdir(parents=True)
            (owner / "runtime/latest-run.txt").write_text(str(run_dir))
            with patch.object(reports, "ROOT", engine):
                summary = reports.generate(run_dir, self.tasks())
            self.assertEqual(summary["experiment_root"], str(owner))
            self.assertIn("runtime/runs/test", (owner / "README.md").read_text())
            self.assertEqual((engine / "README.md").read_text(), "# Engine\n")

    def test_mismatched_experiment_root_rejected_before_any_export(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            owner, other = base / "owner", base / "other"
            other.mkdir(); (other / "README.md").write_text("# Other\n")
            run_dir = owner / "runtime/runs/test"
            write_json(run_dir / "frozen-protocol.json", {"config": {"experiment_root": str(other)}})
            with self.assertRaisesRegex(ValueError, "different experiment_root"):
                reports.generate(run_dir, self.tasks())
            self.assertFalse((run_dir / "report").exists())
            self.assertEqual((other / "README.md").read_text(), "# Other\n")

    def test_legacy_config_derives_owner_from_run_and_checks_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary) / "owner"
            run_dir = owner / "runtime/runs/test"
            self.assertEqual(reports.report_owner(run_dir, {"runtime_dir": str(owner / "runtime")}), owner)
            with self.assertRaisesRegex(ValueError, "different experiment folder"):
                reports.report_owner(run_dir, {"runtime_dir": str(owner / "different")})

    def test_historical_report_cannot_replace_latest_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary) / "experiment"
            run_dir = owner / "runtime/runs/historical"
            run_dir.mkdir(parents=True)
            document = owner / "README.md"
            document.write_text("# Current run stays here\n")
            (owner / "runtime/latest-run.txt").write_text(str(owner / "runtime/runs/current"))
            reports.generate(run_dir, self.tasks())
            self.assertTrue((run_dir / "report/REPORT.md").is_file())
            self.assertEqual(document.read_text(), "# Current run stays here\n")

    def test_missing_current_run_pointer_leaves_document_untouched(self):
        with tempfile.TemporaryDirectory() as temporary:
            owner = Path(temporary) / "owner"
            owner.mkdir()
            document = owner / "README.md"
            document.write_text("# Owner\n")
            reports.generate(owner / "runtime/runs/historical", self.tasks())
            self.assertEqual(document.read_text(), "# Owner\n")

    def test_document_symlink_cannot_write_another_folder(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            owner, other = base / "owner", base / "other"
            owner.mkdir(); other.mkdir()
            target = other / "README.md"
            target.write_text("# Other\n")
            (owner / "README.md").symlink_to(target)
            with self.assertRaisesRegex(ValueError, "owning folder"):
                reports.update_experiment_status(owner / "runtime/runs/test", {"experiment_root": str(owner)})
            self.assertEqual(target.read_text(), "# Other\n")

    def test_unowned_diagnostic_report_leaves_engine_document_untouched(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            engine = base / "engine"; engine.mkdir()
            (engine / "README.md").write_text("# Engine\n")
            with patch.object(reports, "ROOT", engine):
                summary = reports.generate(base / "diagnostic", self.tasks())
            self.assertIsNone(summary["experiment_root"])
            self.assertEqual((engine / "README.md").read_text(), "# Engine\n")


if __name__ == "__main__":
    unittest.main()
