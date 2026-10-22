"""Whole-night native evidence and atomic export, without inference or containers."""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.wildclawbench import evolution as e
class FakeClient:
    def __init__(self, **kwargs):
        pass

    async def chat(self, messages, **kwargs):
        return "A successful session preserves the procedure; a failed session supports a narrow correction."


class AlignmentEvolutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bank = self.root / "bank"
        for name in ("alpha", "beta"):
            p = self.bank / name
            p.mkdir(parents=True)
            (p / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Existing\n---\nPreserve this success.\n")
            (p / "assets").mkdir()
            (p / "assets/input.bin").write_bytes(b"unchanged")
        self.before = e._tree_hash(self.bank)
        self.calls = 0
        self.mode = "two"
        self.config = {
            "alignment": {"version": "native-night-atomic-v1"},
            "protocol_hash": "frozen-protocol",
            "llm_model": "fixture",
            "llm_base_url": "http://127.0.0.1:18182/v1",
            "paid_api_budget_usd": 0,
            "llm_max_tokens": 128,
            "llm_temperature": 1,
            "evolution_runner": self.runner,
        }
        self.sessions = [
            self.session("task_1", "session-a", ["alpha", "beta"]),
            self.session("task_10", "session-b", []),
        ]
        self.client = patch.object(e, "AsyncLLMClient", FakeClient)
        self.client.start()
        self.addCleanup(self.client.stop)

    def session(self, task, sid, skills):
        return {
            "task_id": task,
            "session_id": sid,
            "phase": "day",
            "round": 1,
            "user_id": "user-01",
            "skill_bank_sha256": self.before,
            "outcome": {"score": 0.8 if skills else 0, "timed_out": False, "tool_errors": 0},
            "turns": [
                {
                    "prompt_text": "Real instruction",
                    "response_text": "Real outcome",
                    "read_skills": [{"skill_name": name} for name in skills],
                }
            ],
        }

    def runner(self, workspace, home, message, config, sid):
        self.calls += 1
        self.assertEqual((workspace / "EVOLVE_AGENTS.md").read_text(), e._native_prompt())
        self.assertEqual(len(list((workspace / "sessions").glob("*.json"))), 2)
        self.assertEqual({p.name for p in (workspace / "skills").iterdir()}, {"alpha", "beta"})
        if self.mode in {"delete_skill", "delete_only"}:
            shutil.rmtree(workspace / "skills/beta")
            if self.mode == "delete_only":
                return subprocess.CompletedProcess([], 0, "Native deleted beta", "")
        if self.mode == "skip":
            return subprocess.CompletedProcess([], 0, "Native chose skip", "")
        for name, source in [("alpha", "task_1 session-a"), ("gamma", "task_10 session-b")]:
            folder = workspace / "skills" / name
            folder.mkdir(exist_ok=True)
            (folder / "SKILL.md").write_text(
                f"---\nname: {name}\ndescription: Corrected\n---\nPreserve success; fix failure.\n"
            )
            if name == "alpha" and self.mode in {"delete_resource", "delete_referenced"}:
                (folder / "assets/input.bin").unlink()
                if self.mode == "delete_referenced":
                    with (folder / "SKILL.md").open("a") as stream:
                        stream.write("Read assets/input.bin before acting.\n")
            (folder / "history").mkdir(exist_ok=True)
            if self.mode == "missing" and name == "gamma":
                source = "No explicit source IDs"
            if self.mode == "prefix" and name == "alpha":
                source = "task_10suffix"
            if self.mode == "foreign" and name == "alpha":
                source = "99_Unknown_task_1_unseen"
            (folder / "history/v1_evidence.md").write_text(
                f"Evidence: {source}\n## Self-validation before finalizing\nPASS: preserve successful behavior.\n"
            )
        return subprocess.CompletedProcess([], 0, "two native changes", "")

    def propose(self):
        return e.propose_candidates(self.root / "proposals", self.bank, self.sessions, self.config)

    def test_one_native_invocation_full_groups_and_two_atomic_bundles(self):
        result = self.propose()
        self.assertEqual(self.calls, 1)
        self.assertEqual(len(result), 1)
        candidate = result[0]
        self.assertEqual(candidate["action"], "atomic_changeset")
        self.assertEqual([b["name"] for b in candidate["bundles"]], ["alpha", "gamma"])
        self.assertEqual(candidate["source_task_ids"], ["task_1", "task_10"])
        self.assertEqual(candidate["source_session_ids"], ["session-a", "session-b"])
        summary = json.loads((self.root / "proposals/evolution/proposal-summary.json").read_text())
        groups = {g["evidence_group"]: g["source_task_ids"] for g in summary["groups"]}
        self.assertEqual(groups["alpha"], ["task_1"])
        self.assertEqual(groups["beta"], ["task_1"])
        self.assertIn(["task_10"], groups.values())
        self.assertEqual((Path(candidate["bundles"][0]["bundle_dir"]) / "assets/input.bin").read_bytes(), b"unchanged")
        self.assertEqual(e._tree_hash(self.bank), self.before)
        self.assertEqual(self.propose(), result)
        self.assertEqual(self.calls, 1)
        self.config["protocol_hash"] = "different-renderer-or-protocol"
        self.assertNotEqual(self.propose()[0]["candidate_id"], candidate["candidate_id"])
        self.assertEqual(self.calls, 2)

    def test_native_skip_is_not_missing_source_or_failed_evolution(self):
        self.mode = "skip"
        self.assertEqual(self.propose(), [])
        summary = json.loads((self.root / "proposals/evolution/proposal-summary.json").read_text())
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(summary["outcome"], "native_skip")
        self.assertNotIn("quarantines", summary)

    def test_native_support_file_deletion_exports_exact_remaining_bundle(self):
        from scripts.wildclawbench.alignment_catalog import inspect_bank

        self.mode = "delete_resource"
        candidate = self.propose()[0]
        alpha = next(bundle for bundle in candidate["bundles"] if bundle["name"] == "alpha")
        self.assertFalse((Path(alpha["bundle_dir"]) / "assets/input.bin").exists())
        removed = next(change for change in alpha["changed_files"] if change["path"] == "assets/input.bin")
        self.assertEqual(removed["change"], "removed")
        self.assertIsNone(removed["after_sha256"])
        inspect_bank(Path(alpha["bundle_dir"]).parent)
        self.assertEqual(e._tree_hash(self.bank), self.before)

    def test_deleted_resource_still_referenced_in_body_is_rejected_by_deployment_gate(self):
        from scripts.wildclawbench.alignment_catalog import CatalogError, inspect_bank

        self.mode = "delete_referenced"
        candidate = self.propose()[0]
        with self.assertRaisesRegex(CatalogError, "Missing bundled resource"):
            inspect_bank(Path(candidate["bundles"][0]["bundle_dir"]).parent)

    def test_whole_skill_deletion_is_ignored_and_recorded_like_native(self):
        for mode in ("delete_skill", "delete_only"):
            with self.subTest(mode=mode):
                self.mode = mode
                self.config["protocol_hash"] = mode
                candidates = self.propose()
                summary = json.loads((self.root / "proposals/evolution/proposal-summary.json").read_text())
                self.assertEqual(
                    summary["ignored_skill_deletions"],
                    [{"name": "beta", "retained_parent_bundle_sha256": e._tree_hash(self.bank / "beta")}],
                )
                self.assertEqual(e._tree_hash(self.bank), self.before)
                self.assertFalse((Path(summary["attempt_dir"]) / "workspace/skills/beta").exists())
                if mode == "delete_only":
                    self.assertEqual(candidates, [])
                    self.assertEqual(summary["outcome"], "native_skip")
                else:
                    self.assertEqual([b["name"] for b in candidates[0]["bundles"]], ["alpha", "gamma"])

    def test_missing_foreign_and_prefix_only_sources_quarantine_whole_change(self):
        for mode in ["missing", "foreign", "prefix"]:
            with self.subTest(mode=mode):
                self.mode = mode
                self.config["protocol_hash"] = mode
                self.assertEqual(self.propose(), [])
                summary = json.loads((self.root / "proposals/evolution/proposal-summary.json").read_text())
                self.assertEqual(summary["status"], "completed")
                self.assertEqual(summary["outcome"], "unverifiable_sources")
                self.assertEqual(len(summary["quarantines"][0]["bundles"]), 2)
                self.assertEqual(e._tree_hash(self.bank), self.before)

    def test_alignment_keeps_native_environment_references(self):
        source = self.session("task_1", "s", ["alpha", "environment-helper"])
        normalized = e._normalize_session(source, {"alpha"}, restrict_to_bank=False)
        self.assertEqual(set(normalized["_skills_referenced"]), {"alpha", "environment-helper"})
        self.assertEqual(source["turns"][0]["read_skills"], normalized["turns"][0]["read_skills"])

    def test_actual_read_alias_and_failed_or_echo_results_do_not_invent_skill_use(self):
        for tool, arguments, details, expected in [
            ("file_read", {"file_path": "/root/skills/alpha/SKILL.md"}, {}, ["alpha"]),
            ("exec", {"command": "cat /root/skills/alpha/SKILL.md"}, {"exitCode": 0}, ["alpha"]),
            ("file_read", {"path": "/root/skills/alpha/SKILL.md"}, {"exitCode": 1}, []),
            ("file_read", {"path": "/root/skills/alpha/SKILL.md"}, {"status": "failed"}, []),
            ("exec", {"command": "echo cat /root/skills/alpha/SKILL.md"}, {"exitCode": 0}, []),
        ]:
            with self.subTest(tool=tool, arguments=arguments, details=details):
                messages = [
                    {
                        "role": "assistant",
                        "content": [{"type": "toolCall", "id": "call", "name": tool, "arguments": arguments}],
                    },
                    {"role": "toolResult", "toolCallId": "call", "content": "Actual response", "details": details},
                ]
                turns = e._messages_to_turns(messages, {"alpha"}, include_environment=True)
                self.assertEqual([read["skill_name"] for read in turns[0]["read_skills"]], expected)
                if details.get("exitCode") == 1 or details.get("status") == "failed":
                    self.assertEqual(len(turns[0]["tool_errors"]), 1)


if __name__ == "__main__":
    unittest.main()
