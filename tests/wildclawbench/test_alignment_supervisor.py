"""Real Linux subprocess tests; no Docker, model or paid API."""

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from scripts.wildclawbench import agent_supervisor

SCRIPT = Path(agent_supervisor.__file__)


class AgentSupervisorTests(unittest.TestCase):
    def run_supervisor(self, source, directory, timeout=0.3):
        evidence = directory / "evidence.json"
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--timeout",
                str(timeout),
                "--evidence",
                str(evidence),
                "--",
                sys.executable,
                "-c",
                source,
            ],
            capture_output=True,
            text=True,
            timeout=8,
        )
        return result, json.loads(evidence.read_text())

    def test_deadline_stops_detached_writer(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            target = directory / "late.txt"
            writer = f'import time;time.sleep(.8);open({str(target)!r},"w").write("late")'
            source = (
                "import subprocess,sys,time;"
                f'subprocess.Popen([sys.executable,"-c",{writer!r}],start_new_session=True);time.sleep(5)'
            )
            result, evidence = self.run_supervisor(source, directory)
            time.sleep(0.8)
            self.assertEqual(result.returncode, 124, result.stderr)
            self.assertTrue(evidence["quiescent"])
            self.assertFalse(target.exists())

    def test_normal_exit_stops_background_children_but_not_unrelated_service(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            target = directory / "late.txt"
            service = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(10)"])
            try:
                writer = f'import time;time.sleep(.8);open({str(target)!r},"w").write("late")'
                source = (
                    "import subprocess,sys;"
                    f'subprocess.Popen([sys.executable,"-c",{writer!r}],start_new_session=True);print("agent done")'
                )
                result, evidence = self.run_supervisor(source, directory, timeout=3)
                time.sleep(0.8)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("agent done", result.stdout)
                self.assertTrue(evidence["quiescent"])
                self.assertFalse(target.exists())
                self.assertIsNone(service.poll())
            finally:
                service.terminate()
                service.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
