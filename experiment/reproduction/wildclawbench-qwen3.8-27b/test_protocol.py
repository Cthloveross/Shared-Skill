"""Exercise phase boundaries, replay identity and publication decisions without model calls."""

from __future__ import annotations

import collections
import contextlib
import io
import json
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import protocol
import run
from protocol import accept_candidate, create_initial_bank, discover_tasks, load_config, tree_hash, write_json

sys.path.insert(0, str(protocol.REPOSITORY))
from sharedskill.skill_bundle import read_skill_bundle_with_meta


class DecisionTests(unittest.TestCase):
    def row(self, score=0.5, status="completed", errors=0, timeout=False):
        return {"task_id": "task", "score": score, "status": status, "tool_errors": errors, "timed_out": timeout}

    def test_regression_and_tie_rejected(self):
        self.assertFalse(accept_candidate([self.row()], [self.row(0.4)], "candidate")["accepted"])
        self.assertFalse(accept_candidate([self.row()], [self.row()], "candidate")["accepted"])

    def test_gain_requires_comparison_support(self):
        self.assertFalse(accept_candidate([self.row()], [self.row(0.6)], "baseline")["accepted"])
        self.assertTrue(accept_candidate([self.row()], [self.row(0.6)], "candidate")["accepted"])

    def test_missing_score_is_pending(self):
        decision = accept_candidate([self.row()], [self.row(None, "pending_score")], "candidate")
        self.assertTrue(decision["pending"])
        self.assertFalse(decision["accepted"])

    def test_stability_tie_must_reduce_failures(self):
        self.assertTrue(accept_candidate([self.row(errors=2)], [self.row(errors=1)], "candidate")["accepted"])
        self.assertFalse(
            accept_candidate([self.row(errors=2)], [self.row(errors=1, timeout=True)], "candidate")["accepted"]
        )

    def test_real_manifest_and_initial_files(self):
        config = load_config()
        tasks = discover_tasks(config)
        self.assertEqual(len(tasks), 60)
        self.assertEqual(sorted(collections.Counter(t["user_id"] for t in tasks).values()), [7, 7, 7, 7, 8, 8, 8, 8])
        self.assertEqual(sum(t["local_variant"] for t in tasks), 1)
        with tempfile.TemporaryDirectory() as temporary:
            bank = Path(temporary) / "S0"
            first = create_initial_bank(config, bank)
            self.assertEqual(first, create_initial_bank(config, bank))
            self.assertEqual(len(list(bank.iterdir())), 9)
            (bank / "03_task1" / "SKILL.md").write_text("tampered")
            with self.assertRaises(ValueError):
                create_initial_bank(config, bank)

    def test_resuming_rejects_a_changed_native_evolver_prompt(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repository"
            root = repository / "experiment/reproduction/trial"
            root.mkdir(parents=True)
            (root / "run.py").write_text("# controller")
            prompt = repository / "evolve_server/engines/EVOLVE_AGENTS.md"
            prompt.parent.mkdir(parents=True)
            prompt.write_text("Original native instructions")
            directory = Path(temporary) / "record"
            directory.mkdir()
            # This test exercises protocol identity, without hardware probes.
            write_json(directory / "startup-inventory.json", {})
            with patch.object(run, "ROOT", root):
                initial = run.freeze({"seed": 42}, [], directory)
                self.assertEqual(initial, run.freeze({"seed": 42}, [], directory))
                frozen = json.loads((directory / "frozen-protocol.json").read_text())
                self.assertIn("evolve_server/engines/EVOLVE_AGENTS.md", frozen["source_hashes"])
                prompt.write_text("Changed native instructions")
                with self.assertRaises(ValueError):
                    run.freeze({"seed": 42}, [], directory)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.bank = self.directory / "banks/S0"
        (self.bank / "skill/scripts").mkdir(parents=True)
        (self.bank / "skill/SKILL.md").write_text("0")
        (self.bank / "skill/scripts/check.py").write_text("print(0)")
        self.tasks = [
            {
                "task_id": f"test_task_{i}",
                "short_id": f"01.{i:02d}",
                "category": "test",
                "source_sha256": str(i),
                "timeout_seconds": 1,
                "user_id": f"user-{(i - 1) % 8 + 1:02d}",
                "local_variant": False,
            }
            for i in range(1, 61)
        ]
        self.config = {**load_config(), "protocol_hash": "frozen", "all_tasks": self.tasks}
        self.calls = []

    def tearDown(self):
        self.temporary.cleanup()

    def execute(self, task_id, bank, output, config):
        version = int((Path(bank) / "skill/SKILL.md").read_text())
        self.calls.append((config["phase"], config["round"], task_id, version))
        return {
            "status": "completed",
            "score": 0.2 + 0.1 * version,
            "wall_seconds": 0.1,
            "timed_out": False,
            "tool_errors": 0,
            "session": {"turns": []},
        }

    def propose(self, proposal_dir, bank, sessions, config):
        number = sessions[0]["round"]
        self.assertEqual(len(sessions), 60)
        self.assertTrue(all(s["phase"] == "day" for s in sessions))
        bundle = Path(proposal_dir) / "skill"
        shutil.copytree(Path(bank) / "skill", bundle)
        (bundle / "SKILL.md").write_text(str(number))
        (bundle / "scripts/check.py").write_text(f"print({number})")
        return [
            {
                "name": "skill",
                "bundle_dir": str(bundle),
                "bundle_sha256": tree_hash(bundle),
                "native_tree_sha256": read_skill_bundle_with_meta(bundle)[2],
                "source_task_ids": [self.tasks[0]["task_id"]],
            }
        ]

    def test_new_skill_cannot_shadow_environment_or_existing_bank(self):
        for name, config in (
            ("web-search", {**self.config, "environment_skill_names": ["web-search"]}),
            ("skill", self.config),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                candidate = {"name": name, "action": "create"}
                evolver = types.SimpleNamespace(propose_candidates=lambda *args: [candidate])
                with patch.dict(sys.modules, {"evolution": evolver}), patch.object(run, "run_stage") as execute:
                    with self.assertRaises(ValueError):
                        run.night(1, self.bank, [], self.tasks, Path(temporary), config)
                execute.assert_not_called()
                self.assertFalse((Path(temporary) / "banks/S1").exists())

    def test_six_updates_and_endpoint_use_final_bytes(self):
        write_json(self.directory / "pilot-gate.json", {"passed": True})
        adapter = types.SimpleNamespace(execute_task=self.execute, rescore_task=self.execute)
        evolver = types.SimpleNamespace(propose_candidates=self.propose)
        with (
            patch.dict(sys.modules, {"task_runtime": adapter, "evolution": evolver}),
            patch.object(run, "check_services"),
            patch.object(run, "generate"),
            patch.object(run, "outcome_preference", return_value="candidate"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            run.run_experiment(self.tasks, self.bank, self.directory, self.config)
            self.assertEqual(len(self.calls), 492)  # 480 fixed plus six related-task A/B pairs.
            days = [c for c in self.calls if c[0] == "day"]
            self.assertEqual(len(days), 360)
            self.assertTrue(all(version == number - 1 for _, number, _, version in days))
            endpoint = [c for c in self.calls if c[0] == "endpoint"]
            self.assertEqual(collections.Counter(c[3] for c in endpoint), {0: 60, 6: 60})
            final = self.directory / "banks/S6"
            self.assertEqual((final / "skill/scripts/check.py").read_text(), "print(6)")
            publication = json.loads((self.directory / "nights/06/publication.json").read_text())
            self.assertEqual(tree_hash(final), publication["bank_sha256"])
            # Entire completed rerun reuses tasks/proposals/decisions, without repeat execution.
            run.run_experiment(self.tasks, self.bank, self.directory, self.config)
            self.assertEqual(len(self.calls), 492)
        self.assertEqual(json.loads((self.directory / "state.json").read_text())["status"], "complete")

    def test_resume_rejects_bank_change(self):
        adapter = types.SimpleNamespace(execute_task=self.execute, rescore_task=self.execute)
        with patch.dict(sys.modules, {"task_runtime": adapter}), contextlib.redirect_stdout(io.StringIO()):
            run.run_one(self.tasks[0], self.bank, self.directory, self.config, "day", 1)
            run.run_one(self.tasks[0], self.bank, self.directory, self.config, "day", 1)
            self.assertEqual(len(self.calls), 1)
            (self.bank / "skill/SKILL.md").write_text("1")
            with self.assertRaises(ValueError):
                run.run_one(self.tasks[0], self.bank, self.directory, self.config, "day", 1)

    def test_pending_score_rescores_without_agent_reexecution(self):
        pending = lambda *args: {"status": "pending_score", "score": None, "session": {}}
        scored = lambda *args: {"status": "completed", "score": 0.3, "session": {}}
        adapter = types.SimpleNamespace(execute_task=pending, rescore_task=scored)
        with (
            patch.dict(sys.modules, {"task_runtime": adapter}),
            patch.object(adapter, "execute_task", wraps=pending) as execute,
            patch.object(adapter, "rescore_task", wraps=scored) as rescore,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            first = run.run_one(self.tasks[0], self.bank, self.directory, self.config, "day", 1)
            second = run.run_one(self.tasks[0], self.bank, self.directory, self.config, "day", 1)
            self.assertEqual(first["status"], "pending_score")
            self.assertEqual(second["status"], "completed")
            self.assertEqual(execute.call_count, 1)
            self.assertEqual(rescore.call_count, 1)


if __name__ == "__main__":
    unittest.main()
