"""Exercise phase boundaries, replay identity and publication decisions without model calls."""

from __future__ import annotations

import collections
import contextlib
import hashlib
import io
import json
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.wildclawbench import protocol
from scripts.wildclawbench import run
from scripts.wildclawbench.protocol import accept_candidate, create_initial_bank, discover_tasks, fixed_execution_count, load_config, tree_hash, write_json

from sharedskill.skill_bundle import read_skill_bundle_with_meta


class DecisionTests(unittest.TestCase):
    def test_declared_task_manifest_digest_is_verified_before_use(self):
        config = load_config()
        selected = discover_tasks(config)[:30]
        with tempfile.TemporaryDirectory() as temporary:
            manifest = Path(temporary) / "tasks.json"
            write_json(manifest, {"tasks": selected})
            actual = hashlib.sha256(manifest.read_bytes()).hexdigest()

            config_path = Path(temporary) / "config.json"
            configured = {
                **json.loads(protocol.DEFAULT_CONFIG.read_text()),
                "task_manifest": "tasks.json",
            }
            write_json(config_path, configured)
            self.assertEqual(load_config(config_path)["task_manifest_sha256"], actual)
            write_json(config_path, {**configured, "task_manifest_sha256": "0" * 64})
            with self.assertRaisesRegex(ValueError, "task_manifest_sha256"):
                load_config(config_path)

            subset = {
                **config,
                "task_manifest": str(manifest),
                "expected_task_count": 30,
                "fixed_execution_count": 240,
            }

            # Older configurations without a declared digest remain valid and
            # acquire the digest of the bytes that were actually parsed.
            subset.pop("task_manifest_sha256", None)
            discover_tasks(subset)
            self.assertEqual(subset["task_manifest_sha256"], actual)

            declared = {**subset, "task_manifest_sha256": actual}
            discover_tasks(declared)
            manifest.write_text(manifest.read_text() + "\n")
            with self.assertRaisesRegex(ValueError, "task_manifest_sha256"):
                discover_tasks(declared)

    def test_thirty_task_manifest_preserves_official_inputs_and_balances_users(self):
        config = load_config()
        official = discover_tasks(config)
        selected = official[:30]
        with tempfile.TemporaryDirectory() as temporary:
            manifest = Path(temporary) / "tasks.json"
            write_json(manifest, {"selected_task_count": 30, "tasks": selected})
            subset = {**config, "task_manifest": str(manifest), "expected_task_count": 30, "fixed_execution_count": 240}
            tasks = discover_tasks(subset)
            self.assertEqual([t["task_id"] for t in tasks], [t["task_id"] for t in selected])
            self.assertEqual([t["source_sha256"] for t in tasks], [t["source_sha256"] for t in selected])
            self.assertEqual([t["timeout_seconds"] for t in tasks], [t["timeout_seconds"] for t in selected])
            self.assertEqual(sorted(collections.Counter(t["user_id"] for t in tasks).values()), [3, 3, 4, 4, 4, 4, 4, 4])
            self.assertEqual(fixed_execution_count(tasks, subset), 240)
            for change in ("duplicate", "unknown", "source", "timeout", "size", "fixed_count"):
                with self.subTest(change=change):
                    entries = [dict(task) for task in selected]
                    altered = dict(subset)
                    if change == "duplicate":
                        entries[-1] = dict(entries[0])
                    elif change == "unknown":
                        entries[-1]["short_id"] = "99.99"
                    elif change == "source":
                        entries[-1]["source_sha256"] = "tampered"
                    elif change == "timeout":
                        entries[-1]["timeout_seconds"] = 3000
                    elif change == "size":
                        entries.pop()
                    else:
                        altered["fixed_execution_count"] = 480
                    write_json(manifest, {"tasks": entries})
                    with self.assertRaises(ValueError):
                        discover_tasks(altered)

    def test_external_judge_budget_is_explicit_and_local_runtime_root_is_independent(self):
        original = json.loads((protocol.DEFAULT_CONFIG).read_text())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            new = root / "separate-experiment"
            new.mkdir()
            config_file = new / "config.json"
            write_json(config_file, {**original, "runtime_dir": "runtime", "paid_api_budget_usd": 10, "external_judge": True,
                                     "judge_base_url": "https://api.deepseek.com/v1", "judge_model_id": "deepseek-flash"})
            config = load_config(config_file)
            self.assertEqual(config["experiment_root"], str(new))
            self.assertEqual(config["runtime_dir"], str(new / "runtime"))
            self.assertEqual(config["model_id"], original["model_id"])
            config["external_judge"] = False
            write_json(config_file, config)
            with self.assertRaisesRegex(ValueError, "explicitly configured"):
                load_config(config_file)
        self.assertEqual(protocol.public_config({"judge_max_tokens": 256, "local_judge_max_tokens": 512,
                                                "api_key": "dummy"}),
                         {"judge_max_tokens": 256, "local_judge_max_tokens": 512})

    def test_external_entry_and_core_both_participate_in_resume_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "checkout/experiment/reproduction/core"
            source.mkdir(parents=True)
            (source / "run.py").write_text("# core controller")
            prompt = source.parents[2] / "evolve_server/engines/EVOLVE_AGENTS.md"
            prompt.parent.mkdir(parents=True)
            prompt.write_text("Original native instructions")
            entry = root / "external-folder"
            entry.mkdir()
            launcher = entry / "run.py"
            launcher.write_text("# independent launcher")
            directory = entry / "runtime/runs/example"
            write_json(directory / "startup-inventory.json", {})
            config = {"experiment_root": str(entry), "seed": 42}
            with patch.object(run, "ROOT", source), patch.object(run, "REPOSITORY", source.parents[2]):
                first = run.freeze(config, [], directory)
                self.assertEqual(first, run.freeze(config, [], directory))
                frozen = json.loads((directory / "frozen-protocol.json").read_text())
                self.assertIn(str(launcher), frozen["source_hashes"])
                self.assertIn("experiment/reproduction/core/run.py", frozen["source_hashes"])
                launcher.write_text("# changed launcher")
                with self.assertRaises(ValueError):
                    run.freeze(config, [], directory)

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

    def test_invalid_reward_cannot_pass_on_stability_improvement(self):
        for invalid in (float("nan"), float("inf"), -1, 2, True, "0.6"):
            with self.subTest(score=invalid):
                decision = accept_candidate([self.row(errors=1)], [self.row(invalid)], "candidate")
                self.assertFalse(decision["accepted"])
                self.assertTrue(decision["pending"])

    def test_duplicate_task_rows_cannot_multiply_quality_gain(self):
        for old, new in (([self.row()], [self.row(.6), self.row(.7)]),
                         ([self.row(.4), self.row(.5)], [self.row(.6)])):
            self.assertFalse(accept_candidate(old, new, "candidate")["accepted"])

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
            with patch.object(run, "ROOT", root), patch.object(run, "REPOSITORY", root.parents[2]):
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
        self.assertEqual(len(sessions), len(self.tasks))
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
                with patch.dict(sys.modules, {"scripts.wildclawbench.evolution": evolver}), patch.object(run, "run_stage") as execute:
                    with self.assertRaises(ValueError):
                        run.night(1, self.bank, [], self.tasks, Path(temporary), config)
                execute.assert_not_called()
                self.assertFalse((Path(temporary) / "banks/S1").exists())

    def test_pilot_requires_exact_completed_grades_and_preserves_valid_zero(self):
        selected = self.tasks[:2]
        config = {**self.config, "pilot_short_ids": [task["short_id"] for task in selected]}
        complete = [{"task_id": task["task_id"], "status": "completed", "score": 0.0,
                     "wall_seconds": 0.1} for task in selected]
        cases = [
            (complete, True),
            ([complete[0], {**complete[1], "status": "pending_score", "score": None}], False),
            (complete[:1], False),
            ([complete[0], complete[0]], False),
            ([complete[0], {**complete[1], "score": float("nan")}], False),
            ([complete[0], {**complete[1], "score": True}], False),
            ([], False),
        ]
        for rows, expected in cases:
            with (self.subTest(rows=rows), patch.object(run, "check_services"),
                  patch("scripts.wildclawbench.interface_checks.verify_interfaces"),
                  patch.object(run, "run_stage", return_value=rows),
                  patch.object(run, "event"), patch.object(run, "generate")):
                if expected:
                    run.pilot(selected, self.bank, self.directory, config)
                else:
                    with self.assertRaisesRegex(RuntimeError, "Pilot requires"):
                        run.pilot(selected, self.bank, self.directory, config)
            gate = json.loads((self.directory / "pilot-gate.json").read_text())
            self.assertEqual(gate["passed"], expected)
            self.assertEqual(gate["expected_tasks"], 2)
            if not expected:
                self.assertEqual(json.loads((self.directory / "state.json").read_text())["status"], "pilot_incomplete")

    def test_longitudinal_stage_does_not_generate_the_generic_endpoint_report(self):
        task = self.tasks[0]
        result = {"task_id": task["task_id"], "short_id": task["short_id"],
                  "status": "completed", "score": 0.5}
        with patch.object(run, "run_one", return_value=result), patch.object(run, "generate") as generate:
            run.run_stage([task], self.bank, self.directory, {
                **self.config,
                "execution_plan": "longitudinal-day-night-text-endpoint-v1",
            }, "day", 1)
            generate.assert_not_called()

            run.run_stage([task], self.bank, self.directory, self.config, "day", 2)
            generate.assert_called_once_with(self.directory, self.tasks)

    def test_six_updates_and_endpoint_use_final_bytes(self):
        write_json(self.directory / "pilot-gate.json", {"passed": True})
        adapter = types.SimpleNamespace(execute_task=self.execute, rescore_task=self.execute)
        evolver = types.SimpleNamespace(propose_candidates=self.propose)
        with (
            patch.dict(sys.modules, {"scripts.wildclawbench.task_runtime": adapter, "scripts.wildclawbench.evolution": evolver}),
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

    def test_thirty_tasks_complete_six_updates_and_both_endpoint_arms(self):
        self.tasks = self.tasks[:30]
        self.config["all_tasks"] = self.tasks
        write_json(self.directory / "pilot-gate.json", {"passed": True})
        adapter = types.SimpleNamespace(execute_task=self.execute, rescore_task=self.execute)
        evolver = types.SimpleNamespace(propose_candidates=self.propose)
        with (
            patch.dict(sys.modules, {"scripts.wildclawbench.task_runtime": adapter, "scripts.wildclawbench.evolution": evolver}),
            patch.object(run, "check_services"), patch.object(run, "generate"),
            patch.object(run, "outcome_preference", return_value="candidate"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            run.run_experiment(self.tasks, self.bank, self.directory, self.config)
        self.assertEqual(len(self.calls), 252)  # 240 fixed plus six related-task A/B pairs.
        self.assertEqual(sum(c[0] == "day" for c in self.calls), 180)
        self.assertEqual(collections.Counter(c[3] for c in self.calls if c[0] == "endpoint"), {0: 30, 6: 30})

    def test_resume_rejects_bank_change(self):
        adapter = types.SimpleNamespace(execute_task=self.execute, rescore_task=self.execute)
        with patch.dict(sys.modules, {"scripts.wildclawbench.task_runtime": adapter}), contextlib.redirect_stdout(io.StringIO()):
            run.run_one(self.tasks[0], self.bank, self.directory, self.config, "day", 1)
            run.run_one(self.tasks[0], self.bank, self.directory, self.config, "day", 1)
            self.assertEqual(len(self.calls), 1)
            (self.bank / "skill/SKILL.md").write_text("1")
            with self.assertRaises(ValueError):
                run.run_one(self.tasks[0], self.bank, self.directory, self.config, "day", 1)

    def test_pending_score_rescores_without_agent_reexecution(self):
        # Runtime certified that the sole judge request failed with HTTP 503;
        # no successful judgment would be repeated by this recovery.
        pending = lambda *args: {"status": "pending_score", "score": None, "session": {},
                                 "error": "Judge HTTP 503", "scoring_recovery_eligible": True}
        scored = lambda *args: {"status": "completed", "score": 0.3, "session": {}}
        adapter = types.SimpleNamespace(execute_task=pending, rescore_task=scored)
        with (
            patch.dict(sys.modules, {"scripts.wildclawbench.task_runtime": adapter}),
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
