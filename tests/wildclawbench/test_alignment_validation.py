"""Diagnostic limits, historical provenance and restart behavior without any API calls."""

import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.wildclawbench import alignment_validation as v
from scripts.wildclawbench import protocol as p
from scripts.wildclawbench import task_runtime
class AlignmentValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.old = self.root / "historical"
        self.runtime = self.root / "new/runtime"
        self.run_dir = self.runtime / "runs/diagnostic"
        self.run_dir.mkdir(parents=True)
        self.config = {
            "runtime_dir": str(self.runtime),
            "shared_runtime_dir": str(self.root / "shared"),
            "alignment": {"version": p.ALIGNMENT_VERSION},
            "model_base_url": "http://127.0.0.1:18181/v1",
            "local_api_base_url": "http://127.0.0.1:19192/v1",
            "seed": 42,
            "infrastructure_retries": 0,
            "paid_api_budget_usd": 1,
        }
        self.tasks = [
            {
                "task_id": f"task-{short}",
                "short_id": short,
                "category": "fixture",
                "source_sha256": "source-" + short,
                "timeout_seconds": 1,
                "user_id": f"user-{index % 8 + 1:02d}",
            }
            for index, short in enumerate(list(v.SHORT_IDS) + [f"99.{i:02d}" for i in range(24)])
        ]
        for arm in ("S0", "S6"):
            bank = self.old / "banks" / arm / "alpha"
            bank.mkdir(parents=True)
            (bank / "SKILL.md").write_text(f"---\nname: alpha\ndescription: Fixture\n---\n{arm}\n")
        self.bank_hash = p.tree_hash(self.old / "banks/S0")
        old_tasks = [{**task, "input_sha256": "input-" + task["short_id"]} for task in self.tasks]
        old_frozen = {"config": {"seed": 42}, "tasks": old_tasks}
        old_protocol = p.canonical_hash(old_frozen)
        for task in self.tasks:
            full = {**task, "input_sha256": "input-" + task["short_id"]}
            session = {
                "task_id": task["task_id"],
                "session_id": "session-" + task["short_id"],
                "phase": "day",
                "round": 1,
                "user_id": task["user_id"],
                "skill_bank_sha256": self.bank_hash,
                "turns": [{"prompt_text": "Original task", "response_text": "Original execution"}],
                "outcome": {"score": 0.5, "timed_out": False, "tool_errors": 0},
            }
            result = {
                **full,
                "status": "completed",
                "score": 0.5,
                "phase": "day",
                "round": 1,
                "seed": 42,
                "skill_bank_sha256": self.bank_hash,
                "session": session,
                "task_source_sha256": task["source_sha256"],
            }
            result["execution_hash"] = p.canonical_hash(
                {
                    "phase": "day",
                    "round": 1,
                    "arm": "",
                    "candidate_id": "",
                    "task_id": task["task_id"],
                    "bank": self.bank_hash,
                    "config": old_protocol,
                    "input": full["input_sha256"],
                    "seed": 42,
                }
            )
            session["session_id"] = result["execution_hash"][:24]
            p.write_json(self.old / "executions/day/01/main/shared" / task["short_id"] / "result.json", result)
        self.old_tasks = old_tasks
        p.write_json(self.old / "frozen-protocol.json", old_frozen)
        self.old_patch = patch.object(v, "OLD_ROOT", self.old)
        self.old_patch.start()
        self.addCleanup(self.old_patch.stop)
        shared = Path(self.config["shared_runtime_dir"])
        (shared / "upstream").mkdir(parents=True)
        p.write_json(
            shared / "prepared/input-files.json",
            {
                "tasks": {
                    task["task_id"]: {"input_sha256": task["input_sha256"], "file_count": 1, "bytes": 10}
                    for task in old_tasks
                }
            },
        )
        p.write_json(self.run_dir / "startup-inventory.json", {})

    def prepare(self):
        with patch.object(v, "discover_tasks", return_value=copy.deepcopy(self.tasks)):
            return v.prepare(copy.deepcopy(self.config), self.run_dir)

    def test_historical_all_thirty_sources_and_bank_bytes_enter_resume_identity(self):
        config, tasks = self.prepare()
        source = config["historical_diagnostic_input"]
        self.assertEqual(len(source["sessions"]), 30)
        self.assertEqual({item["task_id"] for item in source["sessions"]}, {t["task_id"] for t in tasks})
        self.assertFalse(source["human_video_contract_injected_into_new_evolution"])
        frozen = json.loads((self.run_dir / "frozen-protocol.json").read_text())
        self.assertEqual(frozen["config"]["historical_diagnostic_input"], source)
        self.assertEqual(self.prepare()[0]["protocol_hash"], config["protocol_hash"])
        changed = self.old / "banks/S6/alpha/SKILL.md"
        changed.write_text("changed historical bank")
        with self.assertRaises(ValueError):
            self.prepare()

    def test_native_invocation_receives_exact_thirty_day1_sessions_and_never_repeats(self):
        config, _ = self.prepare()
        originals = [
            json.loads(Path(item["path"]).read_text())["session"]
            for item in config["historical_diagnostic_input"]["sessions"]
        ]

        def propose_once(*args):
            p.write_json(
                self.run_dir / "native-evolution/evolution/proposal-summary.json",
                {"status": "completed", "candidates": [], "outcome": "native_skip"},
            )
            return []

        with patch("scripts.wildclawbench.evolution.propose_candidates", side_effect=propose_once) as propose:
            v.evolve(config, self.run_dir)
            v.evolve(config, self.run_dir)
        propose.assert_called_once()
        self.assertEqual(propose.call_args.args[1], self.run_dir / "banks/S0")
        self.assertEqual(propose.call_args.args[2], originals)
        evidence = json.loads((self.run_dir / "native-evolution-input.json").read_text())
        self.assertFalse(evidence["human_video_contract_injected"])
        self.assertFalse(json.loads((self.run_dir / "native-acceptance.json").read_text())["published"])

    def test_failed_native_invocation_remains_a_gap_without_a_second_model_call(self):
        config, _ = self.prepare()
        with patch("scripts.wildclawbench.evolution.propose_candidates", side_effect=RuntimeError("fixture failure")) as propose:
            v.evolve(config, self.run_dir)
            v.evolve(config, self.run_dir)
        propose.assert_called_once()
        marker = json.loads((self.run_dir / "native-invocation.json").read_text())
        self.assertEqual(marker["status"], "failed")
        self.assertFalse((self.run_dir / "native-acceptance.json").exists())

    def test_resume_reuses_native_generation_but_rejects_partial_or_changed_candidate_bank(self):
        config, _ = self.prepare()
        bundle = self.root / "new-bundle/gamma"
        bundle.mkdir(parents=True)
        (bundle / "SKILL.md").write_text("Real exported candidate")
        candidate = {
            "candidate_id": "native-fixture",
            "source_task_ids": [self.tasks[0]["task_id"]],
            "bundles": [{"name": "gamma", "bundle_dir": str(bundle), "bundle_sha256": p.tree_hash(bundle)}],
        }

        def propose_once(*args):
            p.write_json(
                self.run_dir / "native-evolution/evolution/proposal-summary.json",
                {"status": "completed", "candidates": [candidate]},
            )
            return [candidate]

        with (
            patch("scripts.wildclawbench.evolution.propose_candidates", side_effect=propose_once) as propose,
            patch.object(v.environment, "preflight_candidate_bank", return_value={"passed": True}) as preflight,
        ):
            v.evolve(config, self.run_dir)
            copied = self.run_dir / "native-preflight-bank/native-fixture"
            self.assertEqual((copied / "gamma/SKILL.md").read_bytes(), (bundle / "SKILL.md").read_bytes())
            (copied / "gamma/SKILL.md").unlink()
            with self.assertRaisesRegex(ValueError, "exact atomic changeset"):
                v.evolve(config, self.run_dir)
        propose.assert_called_once()
        preflight.assert_called_once()

    def test_session_metadata_cannot_disagree_with_real_day1_execution(self):
        path = self.old / "executions/day/01/main/shared" / self.tasks[0]["short_id"] / "result.json"
        original = json.loads(path.read_text())
        for field, value in (("task_id", "foreign-task"), ("round", 2), ("session_id", "invented-session")):
            with self.subTest(field=field):
                changed = copy.deepcopy(original)
                changed["session"][field] = value
                p.write_json(path, changed)
                with self.assertRaisesRegex(ValueError, "Historical session"):
                    v.historical_input(self.old_tasks)
                p.write_json(path, original)

    def test_diagnostic_infrastructure_failure_is_durable_and_cannot_restart_agent(self):
        for arm in ("S0", "S6"):
            source = self.old / "banks" / arm
            shutil.copytree(source, self.run_dir / "banks" / arm)
        config = {**self.config, "protocol_hash": "diagnostic-frozen"}
        attempts = []

        def execute(task_id, bank, directory, task_config):
            from scripts.wildclawbench.alignment_catalog import prepare_binding

            run_id = "wcb-" + hashlib.sha256(str(directory.resolve()).encode()).hexdigest()[:20]
            binding = prepare_binding(task_config, run_id, task_config["execution_id"], bank)
            p.write_json(directory / "execution-binding.json", binding)
            attempts.append((task_id, task_config["execution_id"]))
            if task_id == self.tasks[0]["task_id"] and Path(bank).name == "S0":
                return {"status": "infrastructure_error", "score": None, "error": "fixture failure"}
            return {"status": "completed", "score": 0.5, "session": {"turns": []}}

        with patch.object(task_runtime, "execute_task", side_effect=execute):
            v.diagnostics(config, self.tasks, self.run_dir, workers=2)
            v.diagnostics(config, self.tasks, self.run_dir, workers=2)
        self.assertEqual(len(attempts), 12)
        self.assertEqual(len({identity for _, identity in attempts}), 12)
        summary = v.report(config, self.run_dir)
        self.assertEqual(summary["valid_grades"], 11)
        self.assertEqual(summary["status"], "finished_with_gaps")
        self.assertIsNone(next(row for row in summary["results"] if row["status"] == "infrastructure_error")["score"])

    def test_report_before_diagnostics_stays_in_progress_and_entry_refuses_scope_expansion(self):
        self.assertEqual(v.report(self.config, self.run_dir)["status"], "in_progress")
        for run_id in ("../escape", "/tmp/outside"):
            with (
                self.subTest(run_id=run_id),
                patch("sys.argv", ["validation", "prepare", "--config", "unused", "--run-id", run_id]),
            ):
                with patch.object(v, "load_config") as load, self.assertRaisesRegex(ValueError, "safe basename"):
                    v.main()
                load.assert_not_called()
        for field, value in (("paid_api_budget_usd", 2), ("infrastructure_retries", 1)):
            with (
                self.subTest(field=field),
                patch("sys.argv", ["validation", "prepare", "--config", "unused", "--run-id", "safe"]),
            ):
                with patch.object(v, "load_config", return_value={**self.config, field: value}):
                    with self.assertRaisesRegex(ValueError, "dedicated"):
                        v.main()

    def test_archived_report_uses_frozen_config_without_execution_or_refreeze(self):
        frozen_config = {
            "runtime_dir": str(self.runtime),
            "protocol_hash": "v1-frozen",
            "alignment": {"version": p.ALIGNMENT_VERSION},
            "paid_api_budget_usd": 1,
            "implementation_revision": "v1",
        }
        p.write_json(self.run_dir / "frozen-protocol.json", {"config": frozen_config})
        current = {**self.config, "implementation_revision": "v2"}
        with (
            patch("sys.argv", ["validation", "report", "--config", "unused", "--run-id", self.run_dir.name]),
            patch.object(v, "load_config", return_value=current),
            patch.object(v, "prepare") as prepare,
            patch.object(v, "exclusive_run") as lock,
            patch.object(v, "start_gateway") as gateway,
            patch.object(v, "evolve") as evolve,
            patch.object(v, "diagnostics") as diagnostics,
            patch.object(v, "report", return_value={"status": "completed"}) as report,
        ):
            v.main()
        report.assert_called_once_with(frozen_config, self.run_dir)
        for action in (prepare, lock, gateway, evolve, diagnostics):
            action.assert_not_called()


if __name__ == "__main__":
    unittest.main()
