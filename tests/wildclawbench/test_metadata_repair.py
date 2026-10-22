"""Operational quoting preserves original bytes and uses new candidates under native validation."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import yaml
from scripts.wildclawbench.operations import metadata_repair
from scripts.wildclawbench.protocol import canonical_hash, tree_hash, write_json

from sharedskill.skill_bundle import read_skill_bundle_with_meta


class MetadataRepairTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.bank = self.root / "banks/S1"
        self.bank.mkdir(parents=True)
        self.directory = self.root / "nights/02"
        self.raw = b'---\nname: video\ndescription: Use when: watching "video".\n---\n\n# Body\nDo this: exactly.\n'
        self.bundle = self.root / "original/video"
        self.bundle.mkdir(parents=True)
        (self.bundle / "SKILL.md").write_bytes(self.raw)
        (self.bundle / "asset.txt").write_bytes(b"unchanged asset\x00")
        self.tasks = [{"task_id": f"task-{i}", "short_id": f"05.{i:02d}", "input_sha256": f"input-{i}"}
                      for i in (1, 2)]
        self.frozen = {"tasks": self.tasks, "config": {"seed": 42}}
        write_json(self.root / "frozen-protocol.json", self.frozen)
        self.revision = {"policy": metadata_repair.POLICY, "applies_from_round": 2,
                         "frozen_protocol_sha256": canonical_hash(self.frozen), "repair_published_skills": True,
                         "published_skill_source_task_ids": {"video": [t["task_id"] for t in self.tasks]}}
        write_json(self.root / "operations/skill-metadata-revision.json", self.revision)
        self.repair = metadata_repair.MetadataRepair(self.root)
        self.repair.validate = Mock(return_value={"results": [{"valid": False}, {"valid": True}]})
        _, files, native_hash = read_skill_bundle_with_meta(self.bundle)
        self.candidate = {"name": "video", "candidate_id": "original-candidate", "bundle_dir": str(self.bundle),
                          "bundle_sha256": tree_hash(self.bundle), "native_tree_sha256": native_hash,
                          "files": files, "changed_files": [], "source_bank_sha256": "source-bank",
                          "evidence_group": "group", "action": "create", "source_task_ids": ["task-1"]}

    def tearDown(self):
        self.temporary.cleanup()

    def test_quote_only_preserves_description_body_assets_and_original_bundle(self):
        result = self.repair.apply([self.candidate], self.directory, self.bank)[0]
        fixed = (Path(result["bundle_dir"]) / "SKILL.md").read_bytes()
        self.assertEqual((self.bundle / "SKILL.md").read_bytes(), self.raw)
        self.assertEqual(fixed.split(b"\n---", 1)[1], self.raw.split(b"\n---", 1)[1])
        self.assertEqual(yaml.safe_load(fixed.split(b"---")[1])["description"], 'Use when: watching "video".')
        self.assertEqual((Path(result["bundle_dir"]) / "asset.txt").read_bytes(), b"unchanged asset\x00")
        self.assertNotEqual(result["bundle_sha256"], self.candidate["bundle_sha256"])
        self.assertNotEqual(result["candidate_id"], self.candidate["candidate_id"])
        self.assertEqual(result["bundle_sha256"], tree_hash(Path(result["bundle_dir"])))
        self.assertEqual(result["native_tree_sha256"], read_skill_bundle_with_meta(result["bundle_dir"])[2])
        self.assertEqual(result["source_bank_sha256"], "source-bank")
        self.repair.validate.assert_called_once_with(
            [self.bundle / "SKILL.md", Path(result["bundle_dir"]) / "SKILL.md"], "video")

    def test_decided_or_candidate_arm_evidence_refuses_repair_but_old_arm_is_reusable(self):
        decision = self.directory / "validation/001-video/decision.json"
        write_json(decision, {"accepted": True})
        with self.assertRaisesRegex(ValueError, "decision or candidate-arm artifact"):
            self.repair.apply([self.candidate], self.directory, self.bank)
        decision.unlink()
        candidate_result = self.root / "executions/validation/02/001-video/candidate/05.01/result.json"
        write_json(candidate_result, {"status": "completed", "score": 1})
        with self.assertRaisesRegex(ValueError, "decision or candidate-arm artifact"):
            self.repair.apply([self.candidate], self.directory, self.bank)
        candidate_result.unlink()
        candidate_config = candidate_result.with_name("openclaw-config.json")
        write_json(candidate_config, {"started": True})
        with self.assertRaisesRegex(ValueError, "candidate-arm artifact"):
            self.repair.apply([self.candidate], self.directory, self.bank)
        import shutil
        shutil.rmtree(candidate_result.parents[1])
        old_result = self.root / "executions/validation/02/001-video/old/05.01/result.json"
        write_json(old_result, {"status": "completed", "execution_hash": "old-bank-identity", "score": 0.5})
        before = old_result.read_bytes()
        self.repair.apply([self.candidate], self.directory, self.bank)
        self.assertEqual(old_result.read_bytes(), before)

    def test_published_bad_skill_appends_refine_from_actual_same_bank_daytime_sources(self):
        import shutil
        shutil.copytree(self.bundle, self.bank / "video")
        bank_hash = tree_hash(self.bank)
        for task in self.tasks:
            identity = {"phase": "day", "round": 2, "arm": "", "candidate_id": "", "task_id": task["task_id"],
                        "bank": bank_hash, "config": canonical_hash(self.frozen),
                        "input": task["input_sha256"], "seed": 42}
            write_json(self.root / "executions/day/02/main/shared" / task["short_id"] / "result.json", {
                "execution_hash": canonical_hash(identity), "status": "completed", "score": 0,
                "session": {"session_id": task["task_id"] + "-session"}, "timed_out": True, "tool_errors": 0})
        before = tree_hash(self.bank)
        result = self.repair.apply([], self.directory, self.bank)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["action"], "refine")
        self.assertEqual(result[0]["source_task_ids"], ["task-1", "task-2"])
        self.assertEqual(result[0]["source_session_ids"], ["task-1-session", "task-2-session"])
        self.assertEqual(result[0]["source_outcomes"][0]["outcome"]["score"], 0)
        self.assertEqual(tree_hash(self.bank), before)
        self.assertIn("Operational YAML description quoting only", result[0]["rationale"]["reason"])
        self.assertEqual(self.repair.apply(result, self.directory, self.bank), result)
        path = self.root / "executions/day/02/main/shared/05.01/result.json"
        data = json.loads(path.read_text())
        data["execution_hash"] = "different-day-input-or-bank"
        write_json(path, data)
        with self.assertRaisesRegex(ValueError, "matching daytime evidence"):
            self.repair.apply([], self.directory, self.bank)

    def test_absent_revision_keeps_behavior_and_unparseable_other_metadata_is_refused(self):
        (self.root / "operations/skill-metadata-revision.json").unlink()
        disabled = metadata_repair.MetadataRepair(self.root)
        self.assertEqual(disabled.apply([self.candidate], self.directory, self.bank), [self.candidate])
        with self.assertRaises(yaml.YAMLError):
            metadata_repair.description_repair(self.raw.replace(b"name: video", b"name: invalid: name"), "video")

    def test_valid_published_folder_alias_keeps_header_name_and_bytes(self):
        bundle = self.bank / "self-improving-agent-3.0.5"
        bundle.mkdir()
        raw = b'---\nname: self-improvement\ndescription: "Valid official skill"\n---\nOfficial body\n'
        (bundle / "SKILL.md").write_bytes(raw)
        before = tree_hash(self.bank)
        self.assertEqual(metadata_repair.description_repair(raw, None), raw)
        self.assertEqual(self.repair.apply([], self.directory, self.bank), [])
        self.assertEqual(tree_hash(self.bank), before)
        with self.assertRaisesRegex(ValueError, "matching name"):
            metadata_repair.description_repair(raw, bundle.name)
        s0 = self.root / "banks/S0" / bundle.name
        s0.mkdir(parents=True)
        (s0 / "SKILL.md").write_bytes(raw)
        alias = {**self.candidate, "name": bundle.name, "action": "refine", "bundle_dir": str(bundle),
                 "bundle_sha256": tree_hash(bundle)}
        result = self.repair.apply([alias], self.directory, self.bank)
        self.assertEqual(result, [alias])
        self.repair.validate.assert_called_once_with([bundle / "SKILL.md"], "self-improvement")
        with self.assertRaisesRegex(ValueError, "matching name"):
            self.repair.apply([{**alias, "action": "create"}], self.directory, self.bank)

    def test_actual_parser_gate_requires_matching_name_length_and_validator_hash(self):
        validator = self.root / "validator/validate.mjs"
        validator.parent.mkdir()
        validator.write_text("// pinned actual parser helper fixture")
        self.repair.revision["actual_frontmatter_validator"] = {
            "path": str(validator), "runtime": "node", "tree_sha256": tree_hash(validator.parent),
            "timeout_seconds": 20}
        del self.repair.validate
        row = {"path": str((self.bundle / "SKILL.md").resolve()), "valid": True, "name": "video",
               "skill_sha256": metadata_repair.hashlib.sha256(self.raw).hexdigest(),
               "description_type": "string", "description_chars": 40}
        with patch.object(metadata_repair.subprocess, "run", return_value=SimpleNamespace(
                stdout=json.dumps(row) + "\n", stderr="", returncode=0)) as command:
            self.repair.validate([self.bundle / "SKILL.md"], "video")
            self.assertNotIn("env", command.call_args.kwargs)
            row["description_chars"] = 1025
            command.return_value.stdout = json.dumps(row) + "\n"
            with self.assertRaisesRegex(ValueError, "actual OpenClaw"):
                self.repair.validate([self.bundle / "SKILL.md"], "video")
        validator.write_text("modified validator")
        with self.assertRaisesRegex(ValueError, "provenance differs"):
            self.repair.validate([self.bundle / "SKILL.md"], "video")


if __name__ == "__main__":
    unittest.main()
