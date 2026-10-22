"""Atomic publication and fail-closed preflight; every task/model call is a fixture."""

import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.wildclawbench import environment
from scripts.wildclawbench import protocol as p
from scripts.wildclawbench import run
from scripts.wildclawbench import task_runtime
from sharedskill.skill_bundle import bundle_file_records, read_skill_bundle_with_meta


class AtomicNightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run_dir = self.root / "runtime/runs/example"
        self.bank = self.run_dir / "banks/S0"
        self.bundle_root = self.root / "native-output"
        for root, names in [(self.bank, ["alpha", "beta"]), (self.bundle_root, ["alpha", "gamma"])]:
            for name in names:
                directory = root / name
                directory.mkdir(parents=True)
                (directory / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Procedure\n---\n{root.name}\n")
                if root == self.bundle_root:
                    (directory / "history").mkdir()
                    (directory / "history/v1_evidence.md").write_text("task-a session-a; Self-validation PASS")
        self.parent = p.tree_hash(self.bank)
        self.sessions = [
            {
                "task_id": "task-a",
                "session_id": "session-a",
                "phase": "day",
                "round": 1,
                "user_id": "user-01",
                "skill_bank_sha256": self.parent,
                "turns": [{"prompt_text": "Task", "response_text": "Observed", "read_skills": []}],
            }
        ]
        self.tasks = [{"task_id": "task-a", "short_id": "01.01"}, {"task_id": "unrelated", "short_id": "01.02"}]
        self.config = {
            "alignment": {"version": p.ALIGNMENT_VERSION},
            "protocol_hash": "frozen",
            "local_api_base_url": "http://127.0.0.1:18182/v1",
            "score_tolerance": 1e-6,
        }
        bundles = []
        for folder in sorted(self.bundle_root.iterdir()):
            files, _, native = read_skill_bundle_with_meta(folder)
            bundles.append(
                {
                    "name": folder.name,
                    "action": "refine" if folder.name == "alpha" else "create",
                    "bundle_dir": str(folder),
                    "bundle_sha256": p.tree_hash(folder),
                    "native_tree_sha256": native,
                    "files": bundle_file_records(files),
                }
            )
        from scripts.wildclawbench.evolution import _history_sources, _normalize_session

        from sharedskill.skill_bundle import read_skill_bundle

        bank = {x.name: read_skill_bundle(x) for x in self.bank.iterdir()}
        _, evidence = _history_sources(bundles, bank, [_normalize_session(self.sessions[0], set(bank), False)])
        self.candidate = {
            "name": "native-night-changeset",
            "action": "atomic_changeset",
            "bundles": bundles,
            "source_bank_sha256": self.parent,
            "source_task_ids": ["task-a"],
            "source_session_ids": ["session-a"],
            "source_evidence": evidence,
        }
        self.candidate["candidate_id"] = p.alignment_candidate_id(self.candidate, self.config)
        self.stage_calls = []

    def stage(self, tasks, bank, run_dir, config, phase, number, arm, candidate_id):
        self.stage_calls.append((arm, [t["task_id"] for t in tasks]))
        if arm == "old":
            self.assertFalse((bank / "gamma").exists())
        else:
            self.assertEqual((bank / "alpha/SKILL.md").read_bytes(), (self.bundle_root / "alpha/SKILL.md").read_bytes())
            self.assertTrue((bank / "gamma/SKILL.md").exists())
            self.assertTrue((bank / "beta/SKILL.md").exists())
        return [
            {
                "task_id": "task-a",
                "status": "completed",
                "score": 0.5 if arm == "old" else 0.8,
                "tool_errors": 0,
                "timed_out": False,
            }
        ]

    def night(self, passed=True, preference="candidate"):
        with (
            patch("scripts.wildclawbench.evolution.propose_candidates", return_value=[self.candidate]),
            patch.object(environment, "preflight_candidate_bank", return_value={"passed": passed}, create=True),
            patch.object(run, "run_stage", side_effect=self.stage),
            patch.object(run, "outcome_preference", return_value=preference),
        ):
            return run.night(1, self.bank, self.sessions, self.tasks, self.run_dir, self.config)

    def test_two_bundle_changeset_validated_and_published_exactly_once(self):
        published = self.night()
        self.assertEqual(self.stage_calls, [("old", ["task-a"]), ("candidate", ["task-a"])])
        self.assertTrue((published / "gamma/SKILL.md").exists())
        self.assertEqual(
            (published / "alpha/SKILL.md").read_bytes(), (self.bundle_root / "alpha/SKILL.md").read_bytes()
        )
        self.assertEqual(p.tree_hash(self.bank), self.parent)
        before = p.tree_hash(published)
        self.assertEqual(p.tree_hash(self.night()), before)
        self.assertEqual(len(self.stage_calls), 2)

    def test_rejection_publishes_whole_parent_not_partial_change(self):
        published = self.night(preference="tie")
        self.assertEqual(p.tree_hash(published), self.parent)
        self.assertFalse((published / "gamma").exists())

    def test_atomic_publication_does_not_keep_deleted_parent_support_file(self):
        resource = self.bank / "alpha/assets/obsolete.bin"
        resource.parent.mkdir()
        resource.write_bytes(b"retired resource")
        self.parent = p.tree_hash(self.bank)
        self.sessions[0]["skill_bank_sha256"] = self.parent
        self.candidate["source_bank_sha256"] = self.parent
        self.candidate["candidate_id"] = p.alignment_candidate_id(self.candidate, self.config)
        published = self.night()
        self.assertFalse((published / "alpha/assets/obsolete.bin").exists())
        self.assertTrue(resource.exists())

    def test_parser_preflight_failure_never_runs_ab_or_fills_zero_scores(self):
        published = self.night(passed=False)
        self.assertEqual(self.stage_calls, [])
        self.assertEqual(p.tree_hash(published), self.parent)
        skip = json.loads(next((self.run_dir / "operations/unattended/skips").glob("*.json")).read_text())
        self.assertEqual(skip["reason"], "preflight_rejected")
        self.assertFalse(skip["score_available"])
        self.assertNotIn("score", skip)

    def test_preflight_cannot_modify_the_candidate_bank(self):
        def mutating_preflight(bank, candidate, config):
            (bank / "alpha/SKILL.md").write_text("mutated by preflight")
            return {"passed": True}

        with (
            patch("scripts.wildclawbench.evolution.propose_candidates", return_value=[self.candidate]),
            patch.object(environment, "preflight_candidate_bank", side_effect=mutating_preflight, create=True),
            patch.object(run, "run_stage") as execute,
            self.assertRaisesRegex(ValueError, "Preflight modified"),
        ):
            run.night(1, self.bank, self.sessions, self.tasks, self.run_dir, self.config)
        execute.assert_not_called()
        self.assertFalse((self.run_dir / "banks/S1").exists())
        self.assertEqual(p.tree_hash(self.bank), self.parent)

    def test_reidentified_source_claim_still_requires_actual_day_history(self):
        self.candidate["source_task_ids"] = ["unrelated"]
        self.candidate["candidate_id"] = p.alignment_candidate_id(self.candidate, self.config)
        with (
            patch("scripts.wildclawbench.evolution.propose_candidates", return_value=[self.candidate]),
            patch.object(environment, "preflight_candidate_bank", create=True) as preflight,
            patch.object(run, "run_stage") as execute,
            self.assertRaisesRegex(ValueError, "real native history/day evidence"),
        ):
            run.night(1, self.bank, self.sessions, self.tasks, self.run_dir, self.config)
        preflight.assert_not_called()
        execute.assert_not_called()
        self.assertFalse((self.run_dir / "banks/S1").exists())

    def test_identity_changes_with_evidence_protocol_and_renderer_settings(self):
        original = self.candidate["candidate_id"]
        changed = copy.deepcopy(self.candidate)
        changed["source_evidence"][0]["sha256"] = "other"
        self.assertNotEqual(p.alignment_candidate_id(changed, self.config), original)
        self.assertNotEqual(
            p.alignment_candidate_id(self.candidate, {**self.config, "protocol_hash": "other"}), original
        )
        self.assertNotEqual(
            p.alignment_candidate_id(
                self.candidate,
                {**self.config, "alignment": {"version": p.ALIGNMENT_VERSION, "renderer_sha256": "other"}},
            ),
            original,
        )
        with self.assertRaises(ValueError):
            p.alignment_settings({"alignment": {"version": "unknown"}})


class AlignmentFreezeTests(unittest.TestCase):
    def test_effective_context_and_new_runtime_modules_are_frozen(self):
        names = ["alignment_catalog.py", "alignment_runtime.py", "agent_supervisor.py", "alignment_reads.py"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository/experiment/reproduction/core"
            root.mkdir(parents=True)
            (root / "run.py").write_text("# controller")
            for name in names:
                (root / name).write_text("# actual runtime component")
            prompt = root.parents[2] / "evolve_server/engines/EVOLVE_AGENTS.md"
            prompt.parent.mkdir(parents=True)
            prompt.write_text("Native self-check and history")
            config = {
                "alignment": {"version": p.ALIGNMENT_VERSION},
                "model_base_url": "http://127.0.0.1:18181/v1",
                "agent_tokenizer_url": "http://127.0.0.1:19999/tokenize",
                "agent_tokenizer_timeout_seconds": 25,
                "max_tokens": 16384,
                "context_length": 131072,
                "api_key": "never-persist-this",
            }
            with patch.object(run, "ROOT", root), patch.object(run, "REPOSITORY", root.parents[2]):
                for name in names:
                    with self.subTest(module=name):
                        directory = root / "runtime" / name
                        p.write_json(directory / "startup-inventory.json", {})
                        run.freeze(config, [], directory)
                        frozen = json.loads((directory / "frozen-protocol.json").read_text())
                        self.assertIn(f"experiment/reproduction/core/{name}", frozen["source_hashes"])
                        self.assertEqual(frozen["config"]["agent_tokenizer_url"], config["agent_tokenizer_url"])
                        self.assertEqual(frozen["config"]["agent_tokenizer_timeout_seconds"], 25)
                        self.assertNotIn("api_key", frozen["config"])
                        context = frozen["context_policy"]
                        self.assertEqual(context["compaction_mode"], "safeguard")
                        self.assertEqual(context["context_pruning"], "off")
                        self.assertFalse(context["uncompressed_context_claimed"])
                        module = root / name
                        original = module.read_bytes()
                        module.write_text("# changed component")
                        with self.assertRaisesRegex(ValueError, "changed"):
                            run.freeze(config, [], directory)
                        module.write_bytes(original)
                with self.assertRaisesRegex(ValueError, "compaction_mode"):
                    p.alignment_settings({"alignment": {"version": p.ALIGNMENT_VERSION, "compaction_mode": "off"}})


class CompletedBindingCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bank = self.root / "bank"
        directory = self.bank / "alpha/scripts"
        directory.mkdir(parents=True)
        self.script = directory / "worker.py"
        self.script.write_text("print('checked')\n")
        self.script.chmod(0o755)
        (directory.parent / "SKILL.md").write_text(
            "---\nname: alpha\ndescription: Use the checked worker.\n---\nRun scripts/worker.py.\n"
        )
        self.run_dir = self.root / "runtime/runs/test"
        self.run_dir.mkdir(parents=True)
        self.task = {
            "task_id": "fixture-task",
            "short_id": "01.01",
            "category": "fixture",
            "source_sha256": "source",
            "user_id": "user-01",
        }
        self.config = {
            "alignment": {"version": p.ALIGNMENT_VERSION},
            "protocol_hash": "frozen",
            "runtime_dir": str(self.root / "runtime"),
            "seed": 42,
            "infrastructure_retries": 0,
        }
        self.execute = patch.object(task_runtime, "execute_task", side_effect=self.complete)
        self.mock_execute = self.execute.start()
        self.addCleanup(self.execute.stop)

    def complete(self, task_id, bank, output, config):
        from scripts.wildclawbench.alignment_catalog import prepare_binding

        run_id = "wcb-" + hashlib.sha256(str(output.resolve()).encode()).hexdigest()[:20]
        binding = prepare_binding(config, run_id, config["execution_id"], bank)
        p.write_json(output / "execution-binding.json", binding)
        return {"status": "completed", "score": 0.5, "session": {"turns": []}}

    def execute_once(self, bank=None):
        return run.run_one(self.task, bank or self.bank, self.run_dir, self.config, "diagnostic", 0, "S0")

    def test_identical_completed_cache_has_no_second_agent_or_api_call(self):
        first = self.execute_once()
        self.assertEqual(self.execute_once(), first)
        self.mock_execute.assert_called_once()

    def test_bound_source_mode_change_rejects_completed_cache_even_with_same_legacy_hash(self):
        self.execute_once()
        before = p.tree_hash(self.bank)
        self.script.chmod(0o700)
        self.assertEqual(p.tree_hash(self.bank), before)
        with self.assertRaisesRegex(ValueError, "binding.*changed"):
            self.execute_once()
        self.mock_execute.assert_called_once()

    def test_same_byte_supplied_bank_mode_or_empty_directory_change_is_not_a_cache_hit(self):
        self.execute_once()
        other = self.root / "other"
        shutil.copytree(self.bank, other)
        self.execute_once(other)
        for mutation in ("mode", "empty-directory"):
            with self.subTest(mutation=mutation):
                if mutation == "mode":
                    (other / "alpha/scripts/worker.py").chmod(0o700)
                else:
                    (other / "alpha/scripts/worker.py").chmod(0o755)
                    (other / "alpha/empty-directory").mkdir()
                self.assertEqual(p.tree_hash(other), p.tree_hash(self.bank))
                with self.assertRaisesRegex(ValueError, "Supplied bank manifest changed"):
                    self.execute_once(other)
        self.mock_execute.assert_called_once()


if __name__ == "__main__":
    unittest.main()
