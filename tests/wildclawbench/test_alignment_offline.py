"""Failed verification must replace old success evidence and exit unsuccessfully."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.wildclawbench import alignment_offline as offline
class OfflineEvidenceTests(unittest.TestCase):
    def test_failed_recheck_replaces_old_passed_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.json"
            config.write_text("{}")
            output = root / "validation"
            output.mkdir()
            report = output / "offline30.json"
            report.write_text('{"passed":true,"stale":true}')
            with patch("sys.argv", ["offline", "--config", str(config)]), \
                 patch.object(offline, "load_config", return_value={"runtime_dir": str(root)}), \
                 patch.object(offline, "offline_tasks", side_effect=ValueError("Changed official task")), \
                 patch("builtins.print"):
                self.assertEqual(offline.main(), 1)
            result = json.loads(report.read_text())
            self.assertFalse(result["passed"])
            self.assertNotIn("stale", result)
            self.assertIn("Changed official task", result["error"])

    def test_supervisor_failure_replaces_old_proof_without_claiming_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.json"
            config.write_text("{}")
            output = root / "validation"
            output.mkdir()
            proof = output / "supervisor-task-image.json"
            proof.write_text('{"passed":true,"stale":true}')
            with patch("sys.argv", ["offline", "--config", str(config), "--supervisor"]), \
                 patch.object(offline, "load_config", return_value={"runtime_dir": str(root)}), \
                 patch.object(offline, "offline_tasks", return_value={"passed": True}), \
                 patch.object(offline, "supervisor_in_image", side_effect=RuntimeError("Docker unavailable")), \
                 patch("builtins.print"):
                self.assertEqual(offline.main(), 1)
            result = json.loads(proof.read_text())
            self.assertFalse(result["passed"])
            self.assertNotIn("stale", result)


if __name__ == "__main__":
    unittest.main()
